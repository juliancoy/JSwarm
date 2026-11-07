"""In-memory delegation broker; custody keys and user tokens never reach workers."""
import base64
import json
import hashlib
import http.client
from pathlib import Path
import secrets
import threading
import time
import ssl

if __package__:
    from .swarm_data_authorization import AccessDenied,read_private_file,audit
    from .swarm_protected_contract import job_key, cipher_limit
    from . import swarm_crypt4gh as c4gh
else:
    from authorization import AccessDenied,read_private_file,audit
    from contracts import job_key, cipher_limit
    import crypto as c4gh


class ProtectedSessions:
    def __init__(self, source):
        self.source=source;self.sessions={};self.lock=threading.Lock()

    def prune(self):
        with self.lock:
            self.sessions={k:v for k,v in self.sessions.items() if v['expires']>time.time()}

    def create(self,args,token,key):
        contract=args['contract']
        if job_key(contract)!=key or args['peer']!=contract['peer']:
            raise AccessDenied('Job contract differs')
        access=self.source.authorization.authorize('/v1/session',args,token,key)
        self.verify_transport()
        selected=self.catalogue(contract)  # validate before delegating
        with self.source.lock:
            offers=self.source.offers(contract['peer'],selected,access)
        with self.lock:
            self.sessions={k:v for k,v in self.sessions.items() if v['expires']>time.time()}
            old=next((k for k,v in self.sessions.items() if v['key']==key),None)
            if not old and len(self.sessions)>=1024:raise AccessDenied('Delegation capacity reached')
            lease=old or secrets.token_hex(32)
            # Controller renews this in RAM with its current PIdP access token.
            self.sessions[lease]={'key':key,'args':args,'token':token,'expires':access['expires']}
            return {'lease':lease,'job_key':key,'expires':access['expires'],'inputs':offers}

    def verify_transport(self):
        """Do not activate against an older bulk service ignoring expiration."""
        context=ssl.create_default_context(cafile=str(self.source.base/'ca.crt'))
        # Legacy enrollment PKI remains CA-verified and exactly pinned below.
        context.verify_flags &= ~getattr(ssl,'VERIFY_X509_STRICT',0)
        context.load_cert_chain(str(self.source.base/'identity.crt'),str(self.source.base/'identity.key'))
        cfg=self.source.config
        connection=http.client.HTTPSConnection(cfg['address'],cfg['port'],context=context,timeout=5)
        try:
            connection.connect()
            expected=hashlib.sha256(ssl.PEM_cert_to_DER_cert((self.source.base/'identity.crt').read_text())).hexdigest()
            if hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()!=expected:
                raise AccessDenied('Bulk service certificate differs')
            connection.request('GET','/health');response=connection.getresponse();raw=response.read(4097)
            if response.status!=200 or len(raw)>4096 or 'expiring-capabilities-v1' not in json.loads(raw).get('capabilities',[]):
                raise AccessDenied('Updated bulk service required')
        except (OSError,ValueError,http.client.HTTPException):
            raise AccessDenied('Updated bulk service unavailable') from None
        finally:connection.close()

    def catalogue(self,contract):
        catalogue=json.loads(read_private_file(self.source.policy['encrypted_catalogue']))
        selected={}
        for alias,expected in contract['inputs'].items():
            row=catalogue[expected['dataset']]
            if any(row.get(k)!=expected[k] for k in ('sha256','bytes','plaintext_sha256','plaintext_bytes')):
                raise AccessDenied('Dataset differs from approved contract')
            path=self.source.allowed(row['path'],'inputs')
            key=Path(row['key_file']).resolve(strict=True)
            if any(key==Path(root).resolve() or Path(root).resolve() in key.parents for root in self.source.policy['inputs']+self.source.policy['outputs']):
                raise AccessDenied('Custody keys must be outside payload roots')
            if self.source.base/'source-authorization' not in key.parents:
                raise AccessDenied('Custody key outside protected configuration')
            if path.stat().st_size!=row['bytes']:raise AccessDenied('Ciphertext changed')
            selected[alias]={**row,'path':str(path),'key_file':str(key)}
        return selected

    def release(self,args,fingerprint):
        key=None;operation='unknown'
        try:
            if set(args)-{'lease','phase','recipient','input'} or args['phase'] not in ('inputs','outputs'):
                raise AccessDenied('Invalid release request')
            with self.lock: session=dict(self.sessions[args['lease']])
            key=session['key'];operation='release_'+args['phase']
            if session['expires']<=time.time():raise AccessDenied('Delegation expired')
            contract=session['args']['contract'];peer=contract['peer']
            enrolled=self.source.config['peers'][peer]
            if enrolled['certificate_sha256']!=fingerprint or enrolled['machine']!=contract['machine']:
                raise AccessDenied('Approved worker certificate required')
            self.verify_transport()
            access=self.source.authorization.authorize('/v1/session',session['args'],session['token'],session['key'])
            if args['phase']=='outputs':
                files={name+'.c4gh':cipher_limit(limit) for name,limit in contract['outputs'].items()}
                destination=self.source.allowed(str(Path(self.source.policy['outputs'][0])/session['key']),'outputs')
                with self.source.lock:
                    grant=self.source.bulk.inbox(self.source.config,str(destination),peer,self.source.config['machine'],
                        session['key'],files,sum(files.values()),access['expires'],access=access)
                audit(operation,key,'allow');return {'output':grant}
            recipient=bytes.fromhex(args['recipient'])
            if len(recipient)!=32:raise AccessDenied('Invalid recipient')
            result={}
            for alias,row in self.catalogue(contract).items():
                if alias!=args.get('input'):continue
                with Path(row['path']).open('rb') as encrypted:
                    replacement,_=c4gh.rewrap(encrypted,c4gh.private_key(row['key_file']),recipient)
                with self.source.lock:
                    offer=self.source.offers(peer,{alias:row},access)[alias]
                result[alias]={'offer':offer,'header':base64.b64encode(replacement).decode('ascii')}
            if not result:raise AccessDenied('Input outside approved contract')
            audit(operation,key,'allow')
            return {'job_key':session['key'],'inputs':result}
        except AccessDenied:
            audit(operation,key,'deny');raise
        except (OSError,ValueError,KeyError,TypeError):
            audit(operation,key,'deny')
            raise AccessDenied('Protected release unavailable') from None
