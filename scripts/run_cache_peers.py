"""Select signed cache peers, then fall back to the authorized input source."""
import time
import copy
import urllib.error
import http.client

class PeerRanges:
    def __init__(self,peer,manifest,identity,total,clock=time.monotonic,persistent=False):
        self.peer=copy.copy(peer);self.peer.manifest={**peer.manifest,'nodes':dict(peer.manifest['nodes'])}
        for name,endpoint in manifest.get('cache_peers',{}).items():
            if name not in peer.manifest['nodes']:raise ValueError('Unenrolled cache peer')
            self.peer.manifest['nodes'][name+':cache']={**peer.manifest['nodes'][name],**endpoint}
        self.manifest=manifest;self.identity=identity;self.total=total
        self.clock=clock;self.scores={};self.cooldown={};self.hits=0;self.source_fetches=0
        self.persistent=persistent;self.connections={}

    def fetch_from(self,name,offset,length,cache=False):
        path=('/cache/' if cache else '/objects/')+self.identity
        if self.persistent:
            key=name+':cache' if cache else name
            try:
                connection=self.connections.get(key)
                if connection is None:
                    node=self.peer.manifest['nodes'][key]
                    connection=http.client.HTTPSConnection(node['address'],node['port'],timeout=3 if cache else 60,context=self.peer.context(remote=key))
                    self.connections[key]=connection
                connection.request('GET',path,headers={'Range':f'bytes={offset}-{offset+length-1}'})
                response=connection.getresponse()
                expected=f'bytes {offset}-{offset+length-1}/{self.total}'
                if response.status!=206 or response.getheader('Content-Range')!=expected or int(response.getheader('Content-Length','-1'))!=length:raise ValueError('Peer range differs from signed input')
                data=response.read(length+1)
                if len(data)!=length:raise ValueError('Incomplete peer range')
                return data
            except Exception:
                connection=self.connections.pop(key,None)
                if connection:connection.close()
                raise
        with self.peer.open_peer(name+':cache' if cache else name,path,timeout=3 if cache else 60,
                headers={'Range':f'bytes={offset}-{offset+length-1}'}) as response:
            expected=f'bytes {offset}-{offset+length-1}/{self.total}'
            if (response.status!=206 or response.headers.get('Content-Range')!=expected
                or int(response.headers['Content-Length'])!=length):
                raise ValueError('Peer range differs from signed input')
            data=response.read(length+1)
            if len(data)!=length:raise ValueError('Incomplete peer range')
            return data

    def close(self):
        for connection in self.connections.values():connection.close()
        self.connections.clear()

    def __call__(self,offset,length):
        now=self.clock()
        candidates=[name for name in self.manifest.get('cache_peers',{})
                    if name!=self.peer.name and self.cooldown.get(name,0)<=now]
        candidates.sort(key=lambda name:self.scores.get(name,float('inf')))
        for name in candidates[:3]:
            started=self.clock()
            try:
                data=self.fetch_from(name,offset,length,cache=True)
                elapsed=max(self.clock()-started,0.000001)/length
                self.scores[name]=0.7*self.scores.get(name,elapsed)+0.3*elapsed
                self.hits+=1;return data
            except (OSError,ValueError,TimeoutError,http.client.HTTPException):
                # No recursive peer fetch: a miss/failure always reaches the origin.
                self.cooldown[name]=self.clock()+60
        self.source_fetches+=1
        return self.fetch_from(self.manifest['source'],offset,length)
