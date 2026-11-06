"""Source metadata via the existing node's independent mTLS data service."""
import hashlib
import http.client
import json
from pathlib import Path
import ssl
import time

BASE=Path.home()/'.local/state/lifetech-launcher/bulk-transport'


class SourceClient:
    def __init__(self,owner,base=BASE):
        self.base=Path(base);cfg=json.loads((self.base/'config.json').read_text())
        self.name=owner['host'] if isinstance(owner,dict) else owner
        self.peer=cfg['peers'][self.name]
        if isinstance(owner,dict) and owner.get('machine') and self.peer['machine']!=owner['machine']:raise ValueError('Source machine identity differs')
        self.port=self.peer.get('source_port',cfg['port']+2)
        self.context=ssl.create_default_context(cafile=str(self.base/'ca.crt'));self.context.minimum_version=ssl.TLSVersion.TLSv1_2
        self.context.load_cert_chain(str(self.base/'identity.crt'),str(self.base/'identity.key'))

    def call(self,path,args=None):
        for attempt in range(3):
            connection=http.client.HTTPSConnection(self.peer['address'],self.port,context=self.context,timeout=30)
            try:
                connection.connect()
                actual=hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
                if actual!=self.peer['certificate_sha256']:raise ValueError('Source certificate identity differs')
                data=json.dumps(args).encode() if args is not None else None
                connection.request('POST' if data is not None else 'GET',path,body=data,headers={'Content-Type':'application/json'})
                response=connection.getresponse();raw=response.read(8*1024**2+1)
                if len(raw)>8*1024**2:raise ValueError('Source response exceeds limit')
                if response.status!=200:raise RuntimeError('Source service '+self.name+' returned HTTP '+str(response.status))
                return json.loads(raw)
            except (OSError,http.client.HTTPException):
                if attempt==2:raise
                time.sleep(.2*(attempt+1))
            finally:connection.close()

    def shards(self,work):
        rows=[];start=0
        while True:
            page=self.call('/v1/shards',{'work':work,'start':start});rows.extend(page['shards'])
            if page['next'] is None:return {'shards':rows,'verified':page['verified']}
            if page['next']<=start:raise ValueError('Source catalogue cursor did not advance')
            start=page['next']

    def plan(self,work,peer,start=0):return self.call('/v1/plan',{'work':work,'peer':peer,'start':start})
    def offers(self,peer,inputs):return self.call('/v1/offers',{'peer':peer,'inputs':inputs})
    def grant(self,args):return self.call('/v1/grants',args)
