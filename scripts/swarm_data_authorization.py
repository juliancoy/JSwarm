"""Live PIdP identity checks plus operator-owned exact job approvals.

No tokens are written to disk. This is a data-release gate, not a key service.
"""
import hashlib
import http.client
import json
import logging
import math
import os
from pathlib import Path
import re
import stat
import time
from urllib.parse import urlencode, urlsplit


class AccessDenied(Exception):
    pass


def read_private_file(path,limit=1024**2):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError('Authorization files must be owner-only regular files')
        data=stream.read(limit+1)
    if len(data)>limit:raise ValueError('Authorization file exceeds limit')
    return data


def audit(operation, job_key, outcome):
    # Hash caller-controlled identifiers; never include tokens, paths or profiles.
    logging.getLogger('swarm.data_access').info(json.dumps({
        'event': 'data_access', 'outcome': outcome,
        'operation': operation if operation in ('/v1/shards','/v1/offers','/v1/grants','/v1/session','release_inputs','release_outputs') else 'unknown',
        'job_digest': hashlib.sha256(str(job_key).encode()).hexdigest(),
    }))


def request_digest(operation, arguments):
    """Hash the exact source operation, including paths, digests and quotas."""
    encoded = json.dumps([operation, arguments], sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def introspect(config, token):
    """Use a pinned HTTPS endpoint; never follow redirects or log credentials."""
    try:
        endpoint = urlsplit(config['introspection_endpoint'])
        if (endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username
                or endpoint.password or endpoint.query or endpoint.fragment):
            raise ValueError('Invalid introspection endpoint')
        secret = read_private_file(config['secret_file'],4096).decode().strip()
        if len(secret) < 32 or '\n' in secret or '\r' in secret:
            raise ValueError('Invalid resource secret')
        connection = http.client.HTTPSConnection(endpoint.hostname, endpoint.port, timeout=5)
        try:
            body = urlencode({'token': token, 'resource': config['resource']})
            connection.request('POST', endpoint.path or '/', body=body,
                               headers={'Authorization': 'Bearer ' + secret,
                                        'Content-Type': 'application/x-www-form-urlencoded',
                                        'User-Agent':'JSwarm/1.0 (+https://github.com/juliancoy/JSwarm)',
                                        'Accept':'application/json'})
            response = connection.getresponse()
            raw = response.read(16385)
            if response.status != 200 or len(raw) > 16384:
                raise ValueError('Introspection unavailable')
            return json.loads(raw)
        finally:
            connection.close()
    except Exception:
        # Do not propagate remote bodies, tokens, secrets or network diagnostics.
        raise AccessDenied('Identity verification unavailable') from None


class DataAuthorization:
    def __init__(self, config, peers):
        self.config = config
        self.peers = peers
        # Missing/invalid protected-mode configuration must never disable checks.
        if not isinstance(config, dict):
            raise ValueError('Invalid data authorization configuration')
        for name in ('issuer', 'resource', 'introspection_endpoint', 'secret_file', 'approvals_file'):
            if not isinstance(config.get(name), str) or not config[name]:
                raise ValueError('Missing data authorization setting: ' + name)

    def authorize(self, operation, arguments, token, job_key):
        try:
            if operation not in ('/v1/shards', '/v1/offers', '/v1/grants', '/v1/session'):
                raise ValueError('Unsupported protected operation')
            if (not isinstance(token, str) or not 1 <= len(token) <= 16384
                    or any(c.isspace() for c in token)
                    or not isinstance(job_key, str)
                    or not re.fullmatch('[a-f0-9]{64}', job_key)):
                raise ValueError('Missing identity or job')
            # Reload approvals for every request: removal takes effect without restart.
            approvals = json.loads(read_private_file(self.config['approvals_file']))
            approval = approvals[job_key]
            if (not isinstance(approval['requests'], list) or not approval['requests']
                    or any(not isinstance(v,str) or not re.fullmatch('[a-f0-9]{64}',v)
                           for v in approval['requests'])):
                raise ValueError('Invalid approval contract')
            now = time.time()
            expiry = approval['expires']
            if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= now:
                raise ValueError('Expired approval')
            if request_digest(operation, arguments) not in approval['requests']:
                raise ValueError('Request outside approved contract')
            peer = approval['peer']
            if self.peers[peer]['machine'] != approval['machine']:
                raise ValueError('Worker identity changed')
            if operation != '/v1/shards' and arguments.get('peer') != peer:
                raise ValueError('Different destination')
            if operation == '/v1/grants' and arguments.get('job_key') != job_key:
                raise ValueError('Different output job')
            claims = introspect(self.config, token)
            if (claims.get('active') is not True
                    or claims.get('iss') != self.config['issuer']
                    or claims.get('aud') != self.config['resource']
                    or claims.get('sub') != approval['subject']
                    or 'genomics:data' not in claims.get('scope', '').split()):
                raise ValueError('Identity not entitled')
            token_expiry = claims['exp']
            if type(token_expiry) not in (int, float) or not math.isfinite(token_expiry):
                raise ValueError('Invalid token expiry')
            expiry = min(expiry, token_expiry, now + 60)
            if expiry <= time.time():
                raise ValueError('Expired token')
            audit(operation, job_key, 'allow')
            return {'job_key': job_key, 'expires': expiry}
        except AccessDenied:
            audit(operation, job_key, 'deny')
            raise
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            audit(operation, job_key, 'deny')
            raise AccessDenied('Data access denied') from None
