"""Native PKCE authorization with in-memory, serialized rotating refresh tokens."""
import base64
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import secrets
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import webbrowser


class PIdPClient:
    def __init__(self,config):
        self.config=config
        issuer=urlsplit(config['issuer'])
        if issuer.scheme!='https' or not issuer.hostname or issuer.username or issuer.password or issuer.query or issuer.fragment:
            raise ValueError('PIdP issuer must be an exact HTTPS URL')
        redirect=urlsplit(config['redirect_uri'])
        if redirect.scheme!='http' or redirect.hostname!='127.0.0.1' or not redirect.port or redirect.query or redirect.fragment:
            raise ValueError('Register an exact loopback redirect URI with a fixed port')
        self.issuer=issuer;self.redirect=redirect;self.tokens=None;self.lock=threading.Lock()

    def exchange(self,fields):
        connection=http.client.HTTPSConnection(self.issuer.hostname,self.issuer.port,timeout=15)
        try:
            connection.request('POST',self.issuer.path.rstrip('/')+'/oauth/mcp/token',
                body=urlencode({'client_id':self.config['client_id'],**fields}),
                headers={'Content-Type':'application/x-www-form-urlencoded',
                         'User-Agent':'JSwarm/1.0 (+https://github.com/juliancoy/JSwarm)',
                         'Accept':'application/json'})
            response=connection.getresponse();raw=response.read(16385)
            if response.status!=200 or len(raw)>16384:raise ValueError('PIdP token exchange rejected; reconnect')
            tokens=json.loads(raw)
            if not isinstance(tokens.get('access_token'),str) or not isinstance(tokens.get('refresh_token'),str):
                raise ValueError('Invalid PIdP token response')
            lifetime=tokens.get('expires_in',300)
            if type(lifetime) not in (int,float) or not 0<lifetime<=300:raise ValueError('Invalid access token lifetime')
            self.tokens={**tokens,'deadline':time.monotonic()+lifetime}
        finally:connection.close()

    def login(self,timeout=300):
        state=secrets.token_urlsafe(32);verifier=secrets.token_urlsafe(48)
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        result={};redirect=self.redirect;issuer=self.config['issuer']
        class LoopbackServer(HTTPServer):
            def get_request(self):
                connection,address=super().get_request();connection.settimeout(3)
                return connection,address
        class Callback(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                parsed=urlsplit(self.path);query=parse_qs(parsed.query)
                valid=(parsed.path==redirect.path and query.get('state')==[state]
                       and query.get('iss')==[issuer] and len(query.get('code',[]))==1)
                if valid:result['code']=query['code'][0]
                data=b'Authorization received. You can close this window.' if valid else b'Authorization rejected.'
                self.send_response(200 if valid else 400)
                self.send_header('Content-Length',str(len(data)));self.send_header('Content-Type','text/plain')
                self.send_header('Cache-Control','no-store');self.send_header('Referrer-Policy','no-referrer')
                self.end_headers();self.wfile.write(data)
        with LoopbackServer(('127.0.0.1',redirect.port),Callback) as server:
            server.timeout=1
            url=issuer.rstrip('/')+'/oauth/mcp/authorize?'+urlencode({
                'response_type':'code','client_id':self.config['client_id'],
                'redirect_uri':self.config['redirect_uri'],'resource':self.config['resource'],
                'scope':'genomics:data','state':state,'code_challenge':challenge,'code_challenge_method':'S256'})
            print('Open this PIdP authorization URL in your browser:\n'+url,flush=True)
            webbrowser.open(url)
            deadline=time.monotonic()+timeout
            while not result and time.monotonic()<deadline:server.handle_request()
        if not result:raise TimeoutError('PIdP login timed out')
        self.exchange({'grant_type':'authorization_code','code':result['code'],'code_verifier':verifier,
                       'redirect_uri':self.config['redirect_uri'],'resource':self.config['resource']})
        return self

    def __call__(self):
        with self.lock:
            if self.tokens is None:raise ValueError('PIdP login required')
            if self.tokens['deadline']-time.monotonic()<30:
                refresh=self.tokens['refresh_token']
                # Refresh is deliberately never retried after an ambiguous failure.
                self.tokens=None
                self.exchange({'grant_type':'refresh_token','refresh_token':refresh,'resource':self.config['resource']})
            return self.tokens['access_token']
