#!/usr/bin/env python3
"""Register Swarm Control with PIdP using an explicitly configured owner."""
import argparse
import json
import os
import identity


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owner-subject',required=True)
    parser.add_argument('--owner-canonical-id',required=True)
    parser.add_argument('--resource-secret-file',required=True)
    parser.add_argument('--origin',default='https://compai.lifetech.fyi')
    parser.add_argument('--issuer',default='https://orgportal.cc')
    args=parser.parse_args()
    if identity.CONFIG.exists():
        existing=identity.config(args.origin)
        if existing['owner_subject']!=args.owner_subject:raise RuntimeError('Configured owner differs; configuration preserved')
        print('Swarm Control identity is already configured.');return
    metadata=identity.fetch(args.issuer+'/.well-known/oauth-authorization-server')
    if metadata['issuer']!=args.issuer or metadata['registration_endpoint']!=args.issuer+'/oauth/mcp/register' or 'swarm:control' not in metadata['scopes_supported']:
        raise RuntimeError('PIdP does not advertise Swarm Control authorization')
    redirect=args.origin+'/auth/callback'
    registration=identity.fetch(metadata['registration_endpoint'],json.dumps({
        'client_name':'Swarm Control','redirect_uris':[redirect],
        'token_endpoint_auth_method':'client_secret_basic','grant_types':['authorization_code'],
        'response_types':['code'],'scope':'swarm:control'}).encode(),{'Content-Type':'application/json'})
    from pathlib import Path
    secret=Path(args.resource_secret_file)
    if secret.stat().st_mode&0o077:raise RuntimeError('Resource credential must be owner-private')
    cfg={key:metadata[key] for key in ('issuer','authorization_endpoint','token_endpoint','jwks_uri','introspection_endpoint')}
    cfg.update({key:registration[key] for key in ('client_id','client_secret')})
    cfg['public_profile_endpoint']=args.issuer+('/pidp' if args.issuer=='https://orgportal.cc' else '')+'/auth/public/users'
    cfg.update(redirect_uri=redirect,resource=args.origin+'/api/swarm',scope='swarm:control',owner_subject=args.owner_subject,
               owner_canonical_user_id=args.owner_canonical_id,resource_secret=secret.read_text().strip())
    identity.CONFIG.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(identity.CONFIG,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream:json.dump(cfg,stream)
    identity.config(args.origin)
    print('Registered Swarm Control sign-in: '+args.origin+'/auth/login')


if __name__=='__main__':main()
