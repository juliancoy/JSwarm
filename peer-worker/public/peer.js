// Browser/WASM adapter. Archive bytes travel on RTCDataChannel, never the broker.
const encoder=new TextEncoder();
const decoder=new TextDecoder();
const HASH=/^[a-f0-9]{64}$/;
const MAX_MANIFEST=8*1024*1024;
const CHUNK_SIZE=256*1024;
export async function sha256(bytes) {
  return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),v=>v.toString(16).padStart(2,'0')).join('');
}
function claims(token) {
  const value=token.split('.')[1];
  return JSON.parse(atob(value.replace(/-/g,'+').replace(/_/g,'/')));
}
function safePath(path) {
  return typeof path==='string' && path.length<=1024 && !path.includes('\\') && path.split('/').every(p=>p && p!=='.' && p!=='..') && !path.includes('\0');
}
export function validateManifest(m) {
  if (m.version!==1 || !['testing','encrypted-dr','public'].includes(m.kind) || !Array.isArray(m.files) || m.files.length>10000) throw Error('Invalid dataset manifest');
  if (['testing','public'].includes(m.kind) && (!Number.isSafeInteger(m.expires) || m.expires*1000<=Date.now())) throw Error('Dataset expired');
  if (m.kind==='public' && (m.files.length!==1 || m.files[0].path!=='public-snapshot.json' || m.files[0].size>32*1024*1024)) throw Error('Invalid public snapshot package');
  if (m.kind==='public' && Object.keys(m).some(key=>!['version','kind','files','expires'].includes(key))) throw Error('Unexpected public manifest metadata');
  const paths=new Set();let total=0;
  for (const file of m.files) {
    if (m.kind==='public' && Object.keys(file).some(key=>!['path','size','chunks'].includes(key))) throw Error('Unexpected public file metadata');
    if (!safePath(file.path) || paths.has(file.path) || !Number.isSafeInteger(file.size) || file.size<0 || !Array.isArray(file.chunks)) throw Error('Invalid file');
    paths.add(file.path);let size=0;
    for (const part of file.chunks) {
      if (m.kind==='public' && Object.keys(part).some(key=>!['hash','size'].includes(key))) throw Error('Unexpected public chunk metadata');
      if (!HASH.test(part.hash) || !Number.isSafeInteger(part.size) || part.size<1 || part.size>CHUNK_SIZE) throw Error('Invalid chunk');
      size+=part.size;
    }
    if (size!==file.size) throw Error('File size differs from manifest');
    total+=size;if (!Number.isSafeInteger(total)) throw Error('Dataset too large');
  }
  return total;
}

class ChunkStore {
  constructor(root) {this.root=root;}
  static async open(room) {
    const root=await navigator.storage.getDirectory();
    const datasets=await root.getDirectoryHandle('organization-peers',{create:true});
    // Testing data has an actual local expiry cleanup, including previous sessions.
    for await (const [name,entry] of datasets.entries()) if (entry.kind==='directory') {
      try {
        const file=await (await entry.getFileHandle('manifest.json')).getFile();
        const m=JSON.parse(await file.text());
        if (['testing','public'].includes(m.kind) && m.expires*1000<=Date.now()) await datasets.removeEntry(name,{recursive:true});
      } catch { /* Incomplete transfers can be resumed. */ }
    }
    return new ChunkStore(await datasets.getDirectoryHandle(room,{create:true}));
  }
  async put(name,bytes) {
    const handle=await this.root.getFileHandle(name,{create:true});const writer=await handle.createWritable();
    try {await writer.write(bytes);await writer.close();} catch(e) {await writer.abort();throw e;}
  }
  async get(name) {
    try {return new Uint8Array(await (await (await this.root.getFileHandle(name)).getFile()).arrayBuffer());}
    catch(e) {if (e.name==='NotFoundError') return null;throw e;}
  }
  async clear() {
    for await (const [name] of this.root.entries()) await this.root.removeEntry(name,{recursive:true});
  }
}

export class ArchivePeer extends EventTarget {
  constructor({url=location.origin,token,relayOnly=false}) {
    super();this.url=url;this.token=token;this.ticket=claims(token);this.relayOnly=relayOnly;
    if (!HASH.test(this.ticket.room)) throw Error('Invalid dataset invitation');
    this.links=new Map();this.requests=new Map();this.manifest=null;this.store=null;this.sequence=0;
  }
  emit(type,detail) {this.dispatchEvent(new CustomEvent(type,{detail}));}
  active() {
    if (Date.now()>=this.ticket.exp*1000) throw Error('Device invitation expired');
    if (['testing','public'].includes(this.manifest?.kind) && Date.now()>=this.manifest.expires*1000) {
      void this.forget().catch(()=>{});throw Error('Dataset expired');
    }
  }
  async connect() {
    this.active();this.store=await ChunkStore.open(this.ticket.room);
    const saved=await this.store.get('manifest.json');
    if (saved) await this.acceptManifest(saved);
    const response=await fetch(new URL('/ice',this.url),{headers:{Authorization:'Bearer '+this.token},cache:'no-store'});
    if (!response.ok) throw Error('Could not obtain peer connection configuration');
    const ice=await response.json();this.relayAvailable=ice.relayAvailable;
    if (this.relayOnly && !ice.relayAvailable) throw Error('TURN is required for relay-only mode');
    this.rtcConfig={iceServers:ice.iceServers,iceTransportPolicy:this.relayOnly?'relay':'all'};
    const socketUrl=new URL('/signal',this.url);socketUrl.protocol=socketUrl.protocol==='https:'?'wss:':'ws:';
    this.socket=new WebSocket(socketUrl,['archive.v1','auth.'+this.token]);
    this.socket.onmessage=event=>{void this.signal(JSON.parse(event.data)).catch(e=>this.emit('error',e.message));};
    this.socket.onclose=()=>this.disconnect('Signaling disconnected; reconnect with a valid invitation');
    await new Promise((resolve,reject)=>{
      const timeout=setTimeout(()=>reject(Error('Signaling connection timed out')),15000);
      this.socket.addEventListener('open',()=>{clearTimeout(timeout);resolve();},{once:true});
      this.socket.addEventListener('error',()=>{clearTimeout(timeout);reject(Error('Signaling connection failed'));},{once:true});
    });
    this.expiryTimer=setTimeout(()=>this.disconnect('Device invitation expired'),Math.max(0,this.ticket.exp*1000-Date.now()));
    this.emit('status','Connected to signaling; waiting for dataset peers');return this;
  }
  sendSignal(data) {this.active();if (this.socket.readyState!==WebSocket.OPEN) throw Error('Signaling unavailable');this.socket.send(JSON.stringify(data));}
  async signal(data) {
    this.active();
    if (data.type==='welcome') {
      if (data.room!==this.ticket.room || data.peer!==this.ticket.peer) throw Error('Signaling identity mismatch');
      for (const id of data.peers) await this.addPeer(id);
    } else if (data.type==='joined') await this.addPeer(data.peer);
    else if (data.type==='left') this.removePeer(data.peer);
    else {
      const link=await this.addPeer(data.from,false);if (!link) return;
      if (data.type==='candidate') {
        if (link.pc.remoteDescription) await link.pc.addIceCandidate(data.candidate);else link.candidates.push(data.candidate);
      } else if (['offer','answer'].includes(data.type)) {
        await link.pc.setRemoteDescription(data.description);
        for (const candidate of link.candidates.splice(0)) await link.pc.addIceCandidate(candidate);
        if (data.type==='offer') {
          await link.pc.setLocalDescription(await link.pc.createAnswer());
          this.sendSignal({type:'answer',to:data.from,description:link.pc.localDescription.toJSON()});
        }
      }
    }
  }
  async addPeer(id,initiate=true) {
    if (id===this.ticket.peer) return null;
    if (this.links.has(id)) return this.links.get(id);
    const pc=new RTCPeerConnection(this.rtcConfig);const link={pc,id,dc:null,candidates:[],incoming:null,chain:Promise.resolve()};
    this.links.set(id,link);
    pc.onicecandidate=event=>{if (event.candidate) {try {this.sendSignal({type:'candidate',to:id,candidate:event.candidate.toJSON()});} catch { /* Closing. */ }}};
    pc.ondatachannel=event=>this.channel(link,event.channel);
    pc.onconnectionstatechange=()=>{
      this.emit('peer',{id,state:pc.connectionState});
      if (['failed','closed'].includes(pc.connectionState)) this.removePeer(id);
    };
    if (initiate && this.ticket.peer<id) {
      this.channel(link,pc.createDataChannel('archive.v1',{ordered:true}));
      await pc.setLocalDescription(await pc.createOffer());
      this.sendSignal({type:'offer',to:id,description:pc.localDescription.toJSON()});
    }
    return link;
  }
  channel(link,dc) {
    if (dc.label!=='archive.v1' || link.dc) {dc.close();return;}
    link.dc=dc;dc.binaryType='arraybuffer';dc.bufferedAmountLowThreshold=128*1024;
    dc.onopen=()=>{dc.send(JSON.stringify({type:'hello',manifest:!!this.manifest}));this.emit('peer',{id:link.id,state:'ready'});};
    dc.onmessage=event=>{
      link.chain=link.chain.then(()=>this.message(link,event.data)).catch(e=>{
        this.emit('error',e.message);this.removePeer(link.id);
      });
    };
    dc.onclose=()=>this.removePeer(link.id);
  }
  removePeer(id) {
    const link=this.links.get(id);if (!link) return;
    this.links.delete(id);link.pc.close();
    for (const [key,request] of this.requests) if (request.peer===id) {clearTimeout(request.timer);request.reject(Error('Peer disconnected; retry to resume'));this.requests.delete(key);}
    this.emit('peer',{id,state:'closed'});
  }
  async send(link,data) {
    this.active();const dc=link.dc;if (!dc || dc.readyState!=='open') throw Error('Data channel closed');
    if (dc.bufferedAmount>256*1024) await new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>{cleanup();reject(Error('Peer transfer timed out'));},15000);
      const ready=()=>{cleanup();resolve();};const closed=()=>{cleanup();reject(Error('Data channel closed'));};
      const cleanup=()=>{clearTimeout(timer);dc.removeEventListener('bufferedamountlow',ready);dc.removeEventListener('close',closed);};
      dc.addEventListener('bufferedamountlow',ready,{once:true});dc.addEventListener('close',closed,{once:true});
    });
    this.active();dc.send(data);
  }
  async message(link,data) {
    this.active();
    if (typeof data!=='string') {
      const incoming=link.incoming;
      if (!incoming || data.byteLength>16384 || incoming.received+data.byteLength>incoming.size) throw Error('Unexpected data frame');
      incoming.parts.push(new Uint8Array(data));incoming.received+=data.byteLength;return;
    }
    if (data.length>32768) throw Error('Peer control message too large');
    const m=JSON.parse(data);
    if (m.type==='hello') {
      if (m.manifest && !this.manifest) await this.send(link,JSON.stringify({type:'manifest-request'}));
    } else if (m.type==='manifest-request') {
      if (this.manifestBytes) await this.sendBytes(link,'manifest',this.manifestBytes);
    } else if (m.type==='request') {
      if (!this.manifest || !this.parts.has(m.hash)) throw Error('Chunk outside dataset');
      const bytes=await this.store.get(m.hash);
      if (!bytes) await this.send(link,JSON.stringify({type:'missing',hash:m.hash}));
      else if (await sha256(bytes)!==m.hash) throw Error('Local chunk is corrupt');
      else await this.sendBytes(link,m.hash,bytes);
    } else if (m.type==='start') {
      const expected=m.hash==='manifest'?MAX_MANIFEST:this.parts?.get(m.hash);
      if (link.incoming || !Number.isSafeInteger(m.size) || m.size<1 || !expected || (m.hash!=='manifest' && m.size!==expected) || m.size>expected) throw Error('Invalid transfer size');
      if (m.hash!=='manifest' && !this.requests.has(m.hash)) throw Error('Unrequested chunk');
      link.incoming={hash:m.hash,size:m.size,received:0,parts:[]};
    } else if (m.type==='end') {
      const incoming=link.incoming;link.incoming=null;
      if (!incoming || incoming.hash!==m.hash || incoming.received!==incoming.size) throw Error('Incomplete transfer');
      const bytes=new Uint8Array(incoming.size);let offset=0;
      for (const part of incoming.parts) {bytes.set(part,offset);offset+=part.byteLength;}
      if (m.hash==='manifest') await this.acceptManifest(bytes);
      else {
        if (await sha256(bytes)!==m.hash) throw Error('Received chunk failed SHA-256 verification');
        await this.store.put(m.hash,bytes);const request=this.requests.get(m.hash);
        if (request) {clearTimeout(request.timer);this.requests.delete(m.hash);request.resolve();}
      }
    } else if (m.type==='missing') {
      const request=this.requests.get(m.hash);
      if (request) {clearTimeout(request.timer);this.requests.delete(m.hash);request.reject(Error('Peer does not have this chunk'));}
    } else throw Error('Unknown peer message');
  }
  async sendBytes(link,hash,bytes) {
    await this.send(link,JSON.stringify({type:'start',hash,size:bytes.byteLength}));
    for (let offset=0;offset<bytes.byteLength;offset+=16384) await this.send(link,bytes.slice(offset,offset+16384));
    await this.send(link,JSON.stringify({type:'end',hash}));
  }
  async acceptManifest(bytes) {
    if (bytes.byteLength>MAX_MANIFEST || await sha256(bytes)!==this.ticket.room) throw Error('Manifest does not match device invitation');
    const manifest=JSON.parse(decoder.decode(bytes));validateManifest(manifest);
    this.manifest=manifest;this.manifestBytes=bytes;this.parts=new Map();
    for (const file of manifest.files) for (const part of file.chunks) {
      if (this.parts.has(part.hash) && this.parts.get(part.hash)!==part.size) throw Error('Conflicting chunk sizes');
      this.parts.set(part.hash,part.size);
    }
    await this.store.put('manifest.json',bytes);
    clearTimeout(this.datasetExpiryTimer);
    if (['testing','public'].includes(manifest.kind)) this.datasetExpiryTimer=setTimeout(()=>{void this.forget().then(()=>this.disconnect('Dataset expired')).catch(e=>this.emit('error',e.message));},Math.min(2147483647,Math.max(0,manifest.expires*1000-Date.now())));
    this.emit('manifest',manifest);
  }
  async seed(files) {
    this.active();const selected=new Map(Array.from(files,file=>[(file.webkitRelativePath||file.name).split('/').slice(file.webkitRelativePath?1:0).join('/'),file]));
    const manifest=selected.get('manifest.json');if (!manifest) throw Error('Choose a prepared peer-share package directory');
    await this.acceptManifest(new Uint8Array(await manifest.arrayBuffer()));
    for (const [hash,size] of this.parts) {
      const file=selected.get('chunks/'+hash);if (!file || file.size!==size) throw Error('Package chunk missing');
      const bytes=new Uint8Array(await file.arrayBuffer());if (await sha256(bytes)!==hash) throw Error('Package chunk is corrupt');
      await this.store.put(hash,bytes);
    }
    for (const link of this.links.values()) if (link.dc?.readyState==='open') await this.send(link,JSON.stringify({type:'hello',manifest:true}));
    this.emit('status','Dataset verified and available to peers');
  }
  async sync() {
    this.active();if (!this.manifest) throw Error('Waiting for a peer with this dataset');
    const estimate=await navigator.storage.estimate();let required=0;const missing=[];
    for (const [hash,size] of this.parts) {
      const saved=await this.store.get(hash);
      if (!saved || saved.byteLength!==size || await sha256(saved)!==hash) {missing.push([hash,size]);required+=size;}
    }
    if (estimate.quota && required>estimate.quota-estimate.usage) throw Error('Insufficient browser storage for dataset');
    let done=this.parts.size-missing.length;this.emit('progress',{done,total:this.parts.size});
    for (const [hash] of missing) {
      this.active();let received=false;
      const peers=Array.from(this.links.values()).filter(link=>link.dc?.readyState==='open');
      for (const link of peers) {
        try {
          await new Promise((resolve,reject)=>{
            const timer=setTimeout(()=>{this.requests.delete(hash);reject(Error('Chunk transfer timed out'));},30000);
            this.requests.set(hash,{peer:link.id,resolve,reject,timer});
            this.send(link,JSON.stringify({type:'request',hash})).catch(error=>{clearTimeout(timer);this.requests.delete(hash);reject(error);});
          });received=true;break;
        } catch { /* Try another connected peer; verified chunks survive retries. */ }
      }
      if (!received) throw Error('No peer supplied this chunk; reconnect and resume');
      this.emit('progress',{done:++done,total:this.parts.size});
    }
    await navigator.storage.persist();this.emit('status','Complete dataset verified; this device can seed it');return this.manifest;
  }
  async file(path) {
    this.active();const entry=this.manifest?.files.find(f=>f.path===path);if (!entry) throw Error('File is outside dataset');
    const name='file-'+await sha256(encoder.encode(path));const handle=await this.store.root.getFileHandle(name,{create:true});const writer=await handle.createWritable();
    try {
      for (const part of entry.chunks) {
        this.active();const bytes=await this.store.get(part.hash);
        if (!bytes || bytes.byteLength!==part.size || await sha256(bytes)!==part.hash) throw Error('Dataset is incomplete or corrupt');
        await writer.write(bytes);
      }
      await writer.close();return handle.getFile();
    } catch(e) {await writer.abort();throw e;}
  }
  async mountWasm(FS,root='/fixtures') {
    if (this.manifest?.kind!=='testing') throw Error('Only testing datasets may be mounted');
    for (const entry of this.manifest.files) {
      const path=root+'/'+entry.path;FS.mkdirTree(path.slice(0,path.lastIndexOf('/')));
      FS.writeFile(path,new Uint8Array(await (await this.file(entry.path)).arrayBuffer()));
    }
  }
  async forget() {await this.store?.clear();this.manifest=null;this.manifestBytes=null;this.parts=new Map();clearTimeout(this.datasetExpiryTimer);}
  disconnect(reason='Disconnected') {
    clearTimeout(this.expiryTimer);clearTimeout(this.datasetExpiryTimer);
    for (const id of Array.from(this.links.keys())) this.removePeer(id);
    if (this.socket && this.socket.readyState<2) this.socket.close();this.emit('status',reason);
  }
}
