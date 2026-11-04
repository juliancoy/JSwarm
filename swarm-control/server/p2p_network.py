"""Real cross-machine WebRTC checks. Authenticated launcher agents carry control and the initial seed."""
from paths import WORKSPACE
import argparse
import base64
import concurrent.futures
import hashlib
import importlib.util
import io
import ipaddress
import json
from pathlib import Path
import re
import shlex
import socket
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid

ROOT=WORKSPACE
IMAGE='selenium/standalone-chrome:latest'
URL='https://organization-archive-peers.jcloiacon.workers.dev'
KEY=Path.home()/'.config/organization-peers/signing.key'


def execute(args,**kwargs):
    result=subprocess.run(args,capture_output=True,timeout=kwargs.pop('timeout',60),**kwargs)
    if result.returncode:raise RuntimeError(Path(args[0]).name+' failed; private subprocess output withheld')
    return result.stdout


def direct_evidence(links,participants,local_host):
    """A socket opening or a same-machine browser pair is insufficient evidence."""
    if len({p['machine'] for p in participants})!=len(participants) or len(participants)<2:
        raise RuntimeError('At least two distinct agent machine identities are required')
    peers={p['peer']:p for p in participants if p.get('peer')};found=[];reasons=[]
    for link in links:
        remote=peers.get(link['peer'])
        if not remote or remote['host']==local_host or link.get('state')!='connected':continue
        try:
            pair=link.get('pair')
            if not pair or pair.get('state')!='succeeded':raise RuntimeError('Selected ICE candidate pair is unavailable')
            if pair['local'].get('candidateType')=='relay' or pair['remote'].get('candidateType')=='relay':
                raise RuntimeError('TURN relay cannot satisfy the direct P2P test')
            for candidate,host in [(pair['local'],next(p for p in participants if p['host']==local_host)),(pair['remote'],remote)]:
                if candidate.get('candidateType') not in {'host','srflx','prflx'} or candidate.get('protocol') not in {'udp','tcp'}:
                    raise RuntimeError('Direct ICE candidate type or protocol is unavailable')
                try:address=ipaddress.ip_address(candidate.get('address','').split('%')[0])
                except ValueError:raise RuntimeError('ICE address is hidden; direct network path cannot be verified') from None
                if address.is_loopback or address.is_unspecified:raise RuntimeError('Loopback ICE candidate cannot satisfy a network test')
                if candidate.get('candidateType')=='host' and str(address) not in host['addresses']:
                    raise RuntimeError('Selected host candidate is not on the attested machine physical interfaces')
            if pair.get('bytesReceived',0)<=0 and pair.get('bytesSent',0)<=0:raise RuntimeError('No bytes observed on selected ICE path')
        except RuntimeError as error:
            reasons.append(str(error))
            continue  # Only attested routes contribute to transfer evidence.
        found.append(link)
    if not found:raise RuntimeError('No verified cross-machine data channel: '+('; '.join(reasons) or json.dumps(links)[:800]))
    return found


STATS='''const result=[];for(const [id,link] of peer.links){
 const stats=await link.pc.getStats();let pair;
 for(const row of stats.values())if(row.type==='transport'&&row.selectedCandidatePairId)pair=stats.get(row.selectedCandidatePairId);
 const candidate=c=>c?({address:c.address,port:c.port,protocol:c.protocol,candidateType:c.candidateType}):null;
 const channels=Array.from(stats.values()).filter(r=>r.type==='data-channel').map(r=>({state:r.state,label:r.label,bytesSent:r.bytesSent,bytesReceived:r.bytesReceived}));
 result.push({peer:id,state:link.pc.connectionState,channels,pair:pair?{state:pair.state,bytesSent:pair.bytesSent,bytesReceived:pair.bytesReceived,
 local:candidate(stats.get(pair.localCandidateId)),remote:candidate(stats.get(pair.remoteCandidateId))}:null});}return result;'''


class Browser:
    """Minimal W3C WebDriver client; no host Python/Node dependency installation."""
    def __init__(self,base,transport=None):
        self.base=base;self.transport=transport;self.session=None
        value=self.call('/session',{'capabilities':{'alwaysMatch':{'browserName':'chrome','goog:chromeOptions':{'args':[
            '--headless=new','--disable-gpu','--host-resolver-rules=MAP localhost 127.0.0.2','--no-sandbox','--disable-dev-shm-usage','--disable-features=WebRtcHideLocalIpsWithMdns',
            '--force-webrtc-ip-handling-policy=default_public_and_private_interfaces']}}}})
        self.session=value['sessionId'];self.version=value['capabilities']['browserVersion']
        self.command('/timeouts',{'script':120000,'pageLoad':120000})
        self.command('/url',{'url':'http://localhost:8789/'})
    def call(self,path,data=None):
        if self.transport:
            result=self.transport(path,data,'POST' if data is not None else 'GET')
        else:
            request=urllib.request.Request(self.base+path,data=json.dumps(data).encode() if data is not None else None,
                headers={'Content-Type':'application/json'})
            try:
                with urllib.request.urlopen(request,timeout=150) as response:result=json.load(response)
            except urllib.error.HTTPError as error:result=json.load(error)
        value=result.get('value')
        if isinstance(value,dict) and value.get('error'):
            raise RuntimeError('WebDriver '+str(value['error'])+' during '+path+': '+str(value.get('message',''))[:400])
        return value
    def command(self,path,data):return self.call('/session/'+self.session+path,data)
    def js(self,body,*args):
        value=self.command('/execute/async',{'script':'''const done=arguments[arguments.length-1];
          (async()=>{'''+body+'''})().then(value=>done({ok:true,value:value??null})).catch(e=>done({ok:false,error:e.message}));''','args':list(args)})
        if not value or not value.get('ok'):raise RuntimeError((value or {}).get('error','Browser script failed'))
        return value['value']
    def wait(self,expression,timeout=45):
        until=time.monotonic()+timeout
        while time.monotonic()<until:
            if self.js('return !!('+expression+');'):return
            time.sleep(.25)
        raise RuntimeError('Timed out waiting for browser peer state')
    def close(self):
        if self.session:
            try:
                if self.transport:self.transport('/session/'+self.session,None,'DELETE')
                else:
                    request=urllib.request.Request(self.base+'/session/'+self.session,method='DELETE')
                    urllib.request.urlopen(request,timeout=10).close()
            except Exception:pass


class NetworkP2P:
    def __init__(self,hosts,ident,emit=lambda *args:None,stopped=lambda:False,source_commit=None,images=None):
        self.hosts=hosts;self.ident=ident;self.emit=emit;self.stopped=stopped
        self.images=images
        self.source_commit=source_commit;self.resources=[];self.report={'status':'running','coverage':{'direct':'pending','cross_nat':'not_measured','turn':'unavailable'},'checks':[],'participants':[]}
    def check_stop(self):
        if self.stopped():raise RuntimeError('P2P run stopped')
    def log(self,host,line):self.emit(host,{'kind':'log','suite':'p2p','phase':'network','line':line})
    def source(self,name):
        commit=self.source_commit
        if not commit or not re.fullmatch('[a-f0-9]{40}',commit):raise RuntimeError('P2P source requires a published Organization commit')
        return subprocess.check_output(['git','-C',str(ROOT),'show',commit+':'+name],timeout=30)
    def attest(self,host):
        from agent_client import AgentClient
        for attempt in range(3):
            self.check_stop();client=AgentClient(host)
            try:
                value=client.call('status',timeout=150);break
            except (OSError,RuntimeError) as error:
                transient=isinstance(error,(TimeoutError,ConnectionError)) or str(error).startswith('TimeoutExpired:')
                if not transient or attempt==2:raise
                self.log(host,'Waiting for authenticated agent status; retrying')
                time.sleep(1+attempt)
            finally:client.close()
        if not value['addresses']:raise RuntimeError('No physical network addresses on '+host['host'])
        value['clock_skew_seconds']=round(value['time']-time.time(),3)
        if abs(value['clock_skew_seconds'])>60:raise RuntimeError('Agent clock differs by more than 60 seconds')
        return {**value,'host':host['host']}

    def prepare(self,host,source):
        from agent_client import AgentClient
        self.check_stop();self.log(host,'Preparing agent-owned isolated browser on '+host['host'])
        client=AgentClient(host)
        resource={'host':host,'client':client,'browser':None};self.resources.append(resource)
        upload=client.upload(source,self.stopped)
        deadline=time.monotonic()+1800
        while True:
            self.check_stop()
            try:
                result=client.call('p2p.start',{'id':self.ident,'upload':upload,'image':self.report['browser_images'][host['host']][IMAGE]},timeout=120)
            except RuntimeError as error:
                # A signed agent may still run an older lease sampler. Its
                # read-only Docker inspection timeout means admission is unknown.
                # Retrying this same browser ID cannot admit another reservation.
                if not (str(error).startswith('TimeoutExpired:') and 'inspect' in str(error) and time.monotonic()<deadline):raise
                self.log(host,'Waiting for Docker capacity inspection; browser start will retry')
                time.sleep(1);continue
            if result.get('started'):break
            if not result.get('waiting_capacity') or time.monotonic()>deadline:raise RuntimeError('P2P browser resource admission failed')
            time.sleep(.5)
        def transport(path,data=None,method='POST'):
            return client.call('p2p.webdriver',{'id':self.ident,'path':path,'data':data,'method':method},timeout=180)
        until=time.monotonic()+180
        while time.monotonic()<until:
            self.check_stop()
            try:
                if transport('/status',None,'GET')['value']['ready']:break
            except (OSError,ValueError,RuntimeError):pass
            time.sleep(.5)
        else:raise RuntimeError('Isolated browser did not become ready on '+host['host'])
        browser=Browser('',transport);resource['browser']=browser
        self.log(host,'Agent browser ready: Chrome '+browser.version)
        return resource

    def connect(self,resource,secret,room):
        # Load the bounded enrollment helper without placing its key in a worker snapshot.
        import sys
        sys.path.insert(0,str(ROOT/'scripts'))
        from peer_archive import ticket
        claims,token=ticket(secret,room,1800)
        value=resource['browser'].js('''const {ArchivePeer,sha256}=await import('/peer.js');
          window.socketEvents=[];const NativeSocket=window.WebSocket;
          window.WebSocket=class extends NativeSocket {constructor(...args){super(...args);
            for(const name of ['open','error','close'])this.addEventListener(name,e=>socketEvents.push({event:name,code:e.code}));}};
          window.peer=new ArchivePeer({url:arguments[0],token:arguments[1]});window.peerErrors=[];
          peer.addEventListener('error',e=>peerErrors.push(e.detail));
          try{await peer.connect();}catch(e){throw Error(e.message+'; socket events '+JSON.stringify(socketEvents));}
          return {sdk:await sha256(await (await fetch('/peer.js')).arrayBuffer()),relayAvailable:peer.relayAvailable};''',URL,token)
        if value['sdk']!=self.sdk_hash:raise RuntimeError('Remote browser loaded a different SDK revision')
        participant=next(p for p in self.report['participants'] if p['host']==resource['host']['host'])
        participant.update(peer=claims['peer'],browser=resource['browser'].version,client_sha256=value['sdk'])
    def verify(self,resource,expected,full_transfer=True):
        value=resource['browser'].js('''const {sha256}=await import('/peer.js');
         const file=await peer.file('testing-copy.json');return {size:file.size,sha256:await sha256(await file.arrayBuffer())};''')
        if value!=expected:raise RuntimeError('Received fixture bytes differ from the source')
        for attempt in range(10):
            observed=resource['browser'].js(STATS)
            local=next(p for p in self.report['participants'] if p['host']==resource['host']['host'])
            # Chrome can hide a peer-reflexive remote address. Corroborate it
            # against the selected local candidate reported by that peer.
            for link in observed:
                pair=link.get('pair') or {}
                remote_candidate=pair.get('remote') or {}
                if remote_candidate.get('address') or remote_candidate.get('candidateType')!='prflx':continue
                participant=next((p for p in self.report['participants'] if p.get('peer')==link['peer']),None)
                other=next((r for r in self.resources if participant and r['host']['host']==participant['host']),None)
                if not other:continue
                for reverse in other['browser'].js(STATS):
                    candidate=(reverse.get('pair') or {}).get('local') or {}
                    if (reverse['peer']==local['peer'] and reverse.get('state')=='connected'
                        and (reverse.get('pair') or {}).get('state')=='succeeded'
                        and candidate.get('port')==remote_candidate.get('port')
                        and candidate.get('protocol')==remote_candidate.get('protocol')
                        and candidate.get('address') in participant['addresses']):
                        remote_candidate['address']=candidate['address']
                        link['remote_address_evidence']='Opposite peer selected local ICE candidate'
            try:
                links=direct_evidence(observed,self.report['participants'],resource['host']['host'])
                break
            except RuntimeError:
                if attempt==9:
                    self.report['unverified_routes']=observed
                    raise
                time.sleep(1)
        if sum(c.get('bytesReceived',0) for link in links for c in link['channels'])<(expected['size'] if full_transfer else 1):
            raise RuntimeError('Data channel counters do not account for received fixture bytes')
        return links
    def cleanup(self):
        def stop(resource):
            # Removing the owned container also closes its browser session.
            # A dead WebDriver must not add a session timeout before removal.
            # Removal is idempotent. A timed-out acknowledgement must be
            # retried on a fresh connection before claiming cleanup failed.
            for attempt in range(2):
                try:
                    result=resource['client'].call('p2p.stop',{'id':self.ident},timeout=40)
                    if result.get('acknowledged') is not True:raise RuntimeError('Cleanup not acknowledged')
                    return
                except Exception:
                    resource['client'].close()
            return resource['host']['host']
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1,len(self.resources))) as pool:
            errors=[host for host in pool.map(stop,self.resources) if host]
        if errors:self.report.setdefault('cleanup_errors',[]).extend(errors)
    def run(self):
        started=time.monotonic()
        try:
            if len(self.hosts)<2:raise RuntimeError('Cross-machine P2P requires at least two verified agent devices')
            secret=KEY.read_text().strip()
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                self.report['participants']=list(pool.map(self.attest,self.hosts))
            if len({p['machine'] for p in self.report['participants']})!=len(self.hosts):raise RuntimeError('Enrolled agents resolve to the same machine')
            def image_progress(image,phase,source,destination):
                for host in self.hosts:
                    if not destination or host['host']==destination:self.log(host,'Browser image '+phase+((' from '+source) if source else ''))
            # Reuse the run resolver and its refreshed references; never create
            # a second downloader for the P2P phase.
            if self.images:
                self.images.ensure(IMAGE,self.hosts)
                self.report['browser_images']=self.images.host_images
            else:
                # A run already in flight has prepared and recorded these IDs.
                # Verify them without refreshing tags or making another resolver.
                from agent_client import AgentClient
                self.report['browser_images']={}
                for host in self.hosts:
                    expected=host.get('images',{}).get(IMAGE)
                    actual=AgentClient(host).call('image.inspect',{'image':IMAGE})
                    if not expected or not actual or actual['id']!=expected:raise RuntimeError('Prepared P2P image is unavailable on '+host['host'])
                    self.report['browser_images'][host['host']]={IMAGE:expected}
            self.sdk_hash=hashlib.sha256(self.source('peer-worker/public/peer.js')).hexdigest()
            with tempfile.TemporaryDirectory(prefix='distributed-p2p-') as folder:
                folder=Path(folder);source=folder/'browser.tgz'
                with tarfile.open(source,'w:gz') as archive:
                    for path,name in [('peer-worker/public/peer.js','peer.js'),('swarm-control/server/p2p_remote.py','remote.py')]:
                        data=self.source(path);info=tarfile.TarInfo(name);info.size=len(data);info.mode=0o644;archive.addfile(info,io.BytesIO(data))
                with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:resources=list(pool.map(lambda host:self.prepare(host,source),self.hosts))
                import sys
                sys.path.insert(0,str(ROOT/'scripts'))
                from peer_archive import package_file
                body=json.dumps({'fixture':'Synthetic distributed P2P test','nonce':uuid.uuid4().hex,'records':__import__('os').urandom(600000).hex()}).encode()
                fixture=folder/'fixture';fixture.write_bytes(body);package=folder/'package';room=package_file(fixture,package,'testing')
                self.report.update(dataset=room,fixture_sha256=hashlib.sha256(body).hexdigest(),fixture_bytes=len(body),client_sha256=self.sdk_hash)
                for resource in resources[:2]:self.connect(resource,secret,room)
                a,b=resources[:2];a['browser'].wait("Array.from(peer.links.values()).some(l=>l.dc?.readyState==='open')")
                files=[{'path':str(file.relative_to(package)),'base64':base64.b64encode(file.read_bytes()).decode()} for file in package.rglob('*') if file.is_file()]
                a['browser'].js('''const files=arguments[0].map(item=>{const file=new File([Uint8Array.from(atob(item.base64),c=>c.charCodeAt(0))],item.path.split('/').pop());
                 Object.defineProperty(file,'webkitRelativePath',{value:'package/'+item.path});return file;});await peer.seed(files);''',files)
                b['browser'].wait('!!peer.manifest');b['browser'].js('await peer.sync();')
                expected={'size':len(body),'sha256':hashlib.sha256(body).hexdigest()}
                links=self.verify(b,expected);self.report['checks'].append({'name':'cross_machine_transfer','status':'passed','host':b['host']['host'],'routes':links})
                self.log(b['host'],'Verified '+str(len(body))+' bytes over direct cross-machine WebRTC; SHA-256 '+expected['sha256'])
                a['browser'].js('await peer.forget();peer.disconnect();');a['browser'].wait('peer.socket.readyState===3')
                receivers=resources[2:] or [a]
                reseed=[]
                for receiver in receivers:
                    self.connect(receiver,secret,room);receiver['browser'].wait('!!peer.manifest');receiver['browser'].js('await peer.sync();')
                    routes=self.verify(receiver,expected)
                    reseed.append({'host':receiver['host']['host'],'routes':routes})
                    self.log(receiver['host'],'Verified reseeding with the original seed disconnected')
                self.report['checks'].append({'name':'reseed_original_offline','status':'passed','receivers':reseed})
                # One missing chunk is repaired over the network while all verified chunks remain cached.
                repair=b['browser'].js('''const [hash,size]=peer.parts.entries().next().value;await peer.store.root.removeEntry(hash);
                  let count=0;peer.addEventListener('progress',e=>{count=e.detail.done;});await peer.sync();return {repairedBytes:size,chunks:count};''')
                self.verify(b,expected,full_transfer=False);self.report['checks'].append({'name':'resume_cached_chunks','status':'passed','host':b['host']['host'],**repair})
                mounted=receiver['browser'].js('''const files={};await peer.mountWasm({mkdirTree(){},writeFile(path,bytes){files[path]=bytes}});
                  const name=Array.from(new TextEncoder().encode('fixtureSize'));
                  const binary=new Uint8Array([0,97,115,109,1,0,0,0,1,5,1,96,0,1,127,2,19,1,3,101,110,118,11,...name,0,0,7,7,1,3,114,117,110,0,0]);
                  const {instance}=await WebAssembly.instantiate(binary,{env:{fixtureSize:()=>files['/fixtures/testing-copy.json'].length}});return instance.exports.run();''')
                if mounted!=len(body):raise RuntimeError('WASM did not consume the received fixture')
                self.report['checks'].append({'name':'wasm_fixture_consumption','status':'passed','host':receiver['host']['host']})
                self.report['coverage']['direct']='passed';self.report['status']='passed'
        except Exception as error:
            self.report.update(status='stopped' if self.stopped() else 'failed',error=type(error).__name__+': '+str(error)[:400])
        finally:
            self.cleanup();self.report['duration_seconds']=round(time.monotonic()-started,2)
            if self.report.get('cleanup_errors'):self.report['status']='failed'
        return self.report


if __name__=='__main__':
    raise SystemExit('Run P2P through Start to publish and pin the GitHub source commits')
