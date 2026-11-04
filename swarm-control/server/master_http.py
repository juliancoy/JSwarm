"""HTTP boundary shared by the authenticated REST and MCP routes."""
import json
import ipaddress
import os
import socket
from urllib.parse import urlsplit
from master_api import API,APIError,authorized
from master_mcp import VERSIONS,dispatch,error

MAX_BODY=65536


def send(handler,status,value=None,headers=None):
    raw=b'' if value is None else json.dumps(value,separators=(',',':')).encode()
    handler.send_response(status)
    handler.send_header('Content-Type','application/json')
    handler.send_header('Cache-Control','no-store')
    handler.send_header('X-Content-Type-Options','nosniff')
    handler.send_header('Content-Length',str(len(raw)))
    for key,value in (headers or {}).items():handler.send_header(key,value)
    handler.end_headers()
    try:handler.wfile.write(raw)
    except (BrokenPipeError,ConnectionResetError):pass


def local_dashboard_settings(handler):
    """Trust the LAN socket, or its loopback proxy's overwritten client IP."""
    try:
        peer=ipaddress.ip_address(handler.client_address[0].split('%')[0])
        real=handler.headers.get_all('X-Real-IP',[])
        forwarded=handler.headers.get_all('X-Forwarded-For',[])
        if real or forwarded:
            if not peer.is_loopback or len(real)!=1 or len(forwarded)>1:return False
            peer=ipaddress.ip_address(real[0].split('%')[0])
            if forwarded and forwarded[0]!=real[0]:return False
        networks=[ipaddress.ip_network(n.strip()) for n in os.getenv('MAP_LAN_NETWORKS','10.42.0.0/24').split(',') if n.strip()]
        return peer.is_loopback or (peer.version==6 and peer.is_link_local) or any(peer in n for n in networks)
    except (ValueError,TypeError,AttributeError,IndexError):return False


def authorize_dashboard(handler, local_settings=False):
    """LAN settings use network authority; execution writes still require a token."""
    host=handler.headers.get('Host','')
    hosts={v.strip() for v in os.getenv('MAP_MASTER_ALLOWED_HOSTS','map.local,localhost,127.0.0.1,::1,10.42.0.1').split(',')}
    try:
        valid=len(handler.headers.get_all('Host',[]))==1 and urlsplit('http://'+host).hostname in hosts
    except ValueError:valid=False
    origin=handler.headers.get('Origin')
    if not valid or (origin and origin not in ('http://'+host,'https://'+host)):
        send(handler,403,{'error':'Host or Origin denied'});return False
    if local_settings and origin in ('http://'+host,'https://'+host) and handler.headers.get('Sec-Fetch-Site') not in ('cross-site',) and local_dashboard_settings(handler):return True
    if not authorized(handler.headers):
        send(handler,401,{'error':'Use an authenticated launcher, REST/MCP client, or the localhost dashboard with a master token'});return False
    return True


def handle(handler,api):
    path=urlsplit(handler.path).path.rstrip('/')
    mcp=path=='/mcp'
    if not mcp and path!=API and not path.startswith(API+'/'):return False
    try:
        hosts={value.strip().lower() for value in os.getenv('MAP_MASTER_ALLOWED_HOSTS','map.local,localhost,127.0.0.1,::1,10.42.0.1').split(',') if value.strip()}
        host=handler.headers.get('Host','')
        if len(handler.headers.get_all('Host',[]))!=1 or urlsplit('http://'+host).hostname not in hosts:
            raise APIError(403,'host_denied','Host denied')
        origin=handler.headers.get('Origin')
        if origin and origin not in ('http://'+host,'https://'+host):raise APIError(403,'origin_denied','Origin denied')
        codex=path.startswith(API+'/codex/')
        if codex:
            # The LAN reverse proxy sets these headers. Codex control stays on
            # the direct loopback endpoint, including authenticated SSH tunnels.
            if handler.headers.get('X-Real-IP') or handler.headers.get('X-Forwarded-For'):
                raise APIError(403,'codex_local_only','Codex control requires a direct localhost connection or SSH tunnel')
            if not authorized(handler.headers,'codex'):raise APIError(401,'unauthorized','A separate Codex owner token is required')
        elif not authorized(handler.headers):
            launcher_route=(handler.command=='GET' and path==API+'/status') or (handler.command=='POST' and path==API+'/test-requests')
            if not launcher_route or not authorized(handler.headers,'launcher'):
                raise APIError(401,'unauthorized','A configured master bearer token is required')
        if codex and path==API+'/codex/transcribe':
            from voice import transcribe,VoiceError,MAX_AUDIO,TYPES
            try:
                if handler.command!='POST':raise VoiceError('POST required',405)
                if origin not in ('http://'+host,'https://'+host):raise VoiceError('Same-origin browser required',403)
                if handler.headers.get('Transfer-Encoding') or len(handler.headers.get_all('Content-Length',[]))!=1:raise ValueError()
                kind=handler.headers.get('Content-Type','');size=int(handler.headers['Content-Length'])
                if kind.split(';')[0] not in TYPES or not 32<=size<=MAX_AUDIO:raise ValueError()
                handler.connection.settimeout(20);audio=handler.rfile.read(size)
                if len(audio)!=size:raise ValueError()
                send(handler,200,transcribe(audio,kind))
            except VoiceError as exc:send(handler,exc.status,{'error':{'message':str(exc)}})
            except (ValueError,OSError):send(handler,400,{'error':{'message':'Invalid audio upload'}})
            return True
        if mcp and handler.command!='POST':
            send(handler,405,{'error':{'code':'method_not_allowed','message':'This stateless MCP endpoint accepts POST; no SSE session is opened'}},{'Allow':'POST'});return True
        if handler.command not in ('GET','POST'):
            send(handler,405,{'error':{'code':'method_not_allowed','message':'Method not allowed'}},{'Allow':'GET, POST'});return True
        body=None
        if handler.command=='POST':
            if handler.headers.get('Transfer-Encoding'):raise APIError(400,'invalid_body','Chunked request bodies are not supported')
            media=handler.headers.get('Content-Type','').split(';')[0].strip()
            if media!='application/json':raise APIError(415,'unsupported_media_type','Use application/json')
            try:size=int(handler.headers.get('Content-Length','-1'))
            except ValueError:raise APIError(400,'invalid_body','Invalid Content-Length') from None
            limit=6*1024*1024 if path in (API+'/codex/commands',API+'/codex/queue') else MAX_BODY
            if not 0<=size<=limit:raise APIError(413,'body_too_large','Request exceeds the size limit')
            if mcp and not {'application/json','text/event-stream'}.issubset(set(x.split(';')[0].strip() for x in handler.headers.get('Accept','').split(','))):
                raise APIError(406,'not_acceptable','MCP clients must accept application/json and text/event-stream')
            if mcp and handler.headers.get('MCP-Protocol-Version') and handler.headers['MCP-Protocol-Version'] not in VERSIONS:
                raise APIError(400,'unsupported_protocol','Unsupported MCP protocol version')
            handler.connection.settimeout(10)
            try:
                raw=handler.rfile.read(size)
                if len(raw)!=size:raise ValueError()
                body=json.loads(raw) if raw else {}
            except (ValueError,UnicodeDecodeError):
                send(handler,400,error(None,-32700,'JSON parse error') if mcp else {'error':{'code':'invalid_json','message':'Invalid JSON body'}});return True
        if mcp:
            status,value=dispatch(api,body)
        else:status,value=api.rest(handler.command,handler.path,body)
        send(handler,status,value)
    except APIError as exc:
        headers={'WWW-Authenticate':'Bearer realm="map-master"'} if exc.status==401 else None
        send(handler,exc.status,{'error':{'code':exc.code,'message':exc.message}},headers)
    except (TimeoutError,socket.timeout):
        handler.close_connection=True
        send(handler,408,{'error':{'code':'request_timeout','message':'Request body timed out'}})
    return True
