"""Independent, existing-identity source catalogue and transfer-capability service."""
import argparse
import hashlib
import importlib.util
import json
import logging
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import os
from pathlib import Path
import re
import ssl
import threading
import time

if __package__:
    from .swarm_data_authorization import AccessDenied, DataAuthorization
else:
    try:
        from swarm_data_authorization import AccessDenied, DataAuthorization
    except ModuleNotFoundError:
        # Installed source releases use a fixed, self-contained filename.
        from authorization import AccessDenied, DataAuthorization


class SourceService:
    def __init__(self,base,policy):
        self.base=Path(base);self.config=json.loads((self.base/'config.json').read_text());self.policy=policy
        self.lock=threading.Lock()
        self.authorization = (DataAuthorization(policy['authorization'], self.config['peers'])
                              if 'authorization' in policy else None)
        if self.authorization:
            # Refuse activation while old capabilities can bypass the new gate.
            for folder,field,roots in (('objects','path',policy['inputs']),('inboxes','destination',policy['outputs'])):
                for manifest in (self.base/folder).glob('*.json'):
                    row=json.loads(manifest.read_text())
                    target=Path(row[field]).resolve()
                    if not any(target==Path(root).resolve() or Path(root).resolve() in target.parents for root in roots):continue
                    if row.get('expires',float('inf'))<=time.time():continue
                    if (row.get('expires',float('inf'))>time.time()+60
                            or (folder=='objects' and not row.get('job_key'))
                            or (folder=='inboxes' and row.get('protected') is not True)):
                        raise ValueError('Retire legacy transfer capabilities before enabling protected source')
        spec=importlib.util.spec_from_file_location('source_bulk',Path(__file__).with_name('bulk.py'))
        self.bulk=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.bulk);self.bulk.BASE=self.base
        self.protected=None
        if 'encrypted_catalogue' in policy:
            if self.authorization is None:raise ValueError('Encrypted sources require authorization')
            if __package__:from .swarm_protected_sessions import ProtectedSessions
            else:from protected import ProtectedSessions
            self.protected=ProtectedSessions(self)

    def allowed(self,value,kind):
        path=Path(value).resolve()
        if not any(path==Path(root).resolve() or Path(root).resolve() in path.parents for root in self.policy[kind]):
            raise ValueError('Path outside configured source-service scope')
        return path

    def receipts(self,args):
        root=self.allowed(args['work'],'inputs')/'shards'
        start=args.get('start',0);limit=args.get('limit',128)
        if type(start) is not int or start<0 or type(limit) is not int or not 1<=limit<=128:raise ValueError('Invalid catalogue page')
        paths=sorted(root.glob('[0-9]*.json'));rows=[]
        for path in paths[start:start+limit]:
            self.allowed(str(path),'inputs')
            if path.stat().st_size>48*1024:raise ValueError('Receipt exceeds limit')
            rows.append(json.loads(path.read_text()))
        proof=self.allowed(str(root/'stream-verified.json'),'inputs')
        verified=json.loads(proof.read_text()) if proof.exists() and proof.stat().st_size<=1024**2 else None
        return {'shards':rows,'verified':verified,'next':start+limit if start+limit<len(paths) else None}

    def offers(self,peer,inputs,access=None):
        if peer not in self.config['peers'] or not isinstance(inputs,dict) or not 1<=len(inputs)<=16:raise ValueError('Invalid destination or inputs')
        result={}
        for name,row in inputs.items():
            if not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}',name):raise ValueError('Invalid input name')
            path=self.allowed(row['path'],'inputs')
            if type(row['bytes']) is not int or row['bytes']<1:raise ValueError('Invalid input size')
            obj=self.bulk.offer(self.config,str(path),row['sha256'],row['bytes'],peer,self.config['machine'],
                                **({'access':access} if access else {}))
            result[name]={**obj,'peer':self.config['name'],'machine':self.config['machine'],'sha256':row['sha256'],'bytes':row['bytes']}
        return result

    def dispatch(self,path,args,token=None,job_key=None):
        if self.protected:
            if path!='/v1/session':raise AccessDenied('Encrypted source requires a protected job session')
            return self.protected.create(args,token,job_key)
        access = self.authorization.authorize(path,args,token,job_key) if self.authorization else None
        if path=='/v1/shards':return self.receipts(args)
        if path=='/v1/offers':
            with self.lock:return self.offers(args['peer'],args['inputs'],access)
        if path=='/v1/plan':
            page=self.receipts(args);rows=[];folder=self.allowed(args['work'],'inputs')/'shards'
            with self.lock:
                for receipt in page['shards']:
                    for mate,source in enumerate(receipt['files']):
                        offered=self.offers(args['peer'],{'data':{**source,'path':str(folder/Path(source['path']).name)}},access)['data']
                        rows.append({'id':f"{receipt['shard']:06d}-{mate}",'shard':receipt['shard'],'mate':mate,'reads':receipt['pairs'],'input':offered})
            return {'items':rows,'next':page['next']}
        if path=='/v1/grants':
            destination=self.allowed(args['destination'],'outputs')
            if args['max_bytes']>2*1024**3:raise ValueError('Output grant exceeds quota')
            with self.lock:return self.bulk.inbox(self.config,str(destination),args['peer'],self.config['machine'],args['job_key'],args['files'],args['max_bytes'],access['expires'] if access else time.time()+7*86400,**({'access':access} if access else {}))
        raise ValueError('Unknown source operation')


def make_server(base,policy,bind,port):
    service=SourceService(base,policy);cfg=service.config
    allowed={peer['certificate_sha256'] for peer in cfg['peers'].values()}
    controller=cfg['peers'][policy['controller']]['certificate_sha256']
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass
        def reply(self,code,value):
            data=json.dumps(value).encode();self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        def do_GET(self):
            fingerprint=hashlib.sha256(self.connection.getpeercert(binary_form=True)).hexdigest()
            if self.path!='/health' or fingerprint not in allowed:self.reply(403,{'error':'Not authorized'});return
            self.reply(200,{'status':'ok','node':cfg['name'],'machine':cfg['machine']})
        def do_POST(self):
            self.close_connection=True;self.connection.settimeout(30)
            fingerprint=hashlib.sha256(self.connection.getpeercert(binary_form=True)).hexdigest()
            release=self.path=='/v1/release' and service.protected is not None
            if (release and fingerprint not in allowed) or (not release and fingerprint!=controller):self.reply(403,{'error':'Approved identity required'});return
            try:
                length=self.headers.get('Content-Length','')
                if self.headers.get('Transfer-Encoding') or not length.isdecimal() or not 0<int(length)<=1024**2:raise ValueError('Invalid request length')
                args=json.loads(self.rfile.read(int(length)))
                if release:
                    self.reply(200,service.protected.release(args,fingerprint));return
                auth=self.headers.get('Authorization','')
                token=auth[7:] if auth.startswith('Bearer ') else None
                self.reply(200,service.dispatch(self.path,args,token,self.headers.get('X-Job-Key')))
            except AccessDenied:self.reply(403,{'error':'Data access denied'})
            except (OSError,ValueError,KeyError,TypeError):self.reply(409,{'error':'Source request unavailable or outside configured scope'})
    server=ThreadingHTTPServer((bind,port),Handler)
    if service.protected is not None:server.service_actions=service.protected.prune
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.minimum_version=ssl.TLSVersion.TLSv1_2
    context.verify_mode=ssl.CERT_REQUIRED;context.load_verify_locations(str(Path(base)/'ca.crt'))
    context.load_cert_chain(str(Path(base)/'identity.crt'),str(Path(base)/'identity.key'))
    server.socket=context.wrap_socket(server.socket,server_side=True)
    return server


def main():
    logging.basicConfig(level=logging.INFO,format='%(message)s')
    parser=argparse.ArgumentParser();parser.add_argument('--base',type=Path,required=True);parser.add_argument('--policy',type=Path,required=True)
    args=parser.parse_args();cfg=json.loads((args.base/'config.json').read_text());policy=json.loads(args.policy.read_text())
    if hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest()!=cfg['machine']:raise ValueError('Source owner machine changed')
    make_server(args.base,policy,cfg['address'],policy['port']).serve_forever()


if __name__=='__main__':main()
