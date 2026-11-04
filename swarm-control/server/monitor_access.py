"""Saved SSH access for Docker observations, independent of agent test execution."""
import json
from pathlib import Path
import re


def targets():
    path=Path.home()/'.local/state/orgportal/lan-ssh-devices.json'
    accounts=json.loads(path.read_text()).get('accounts',{})
    result=[]
    for host,user in sorted(accounts.items()):
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]*\.local',host) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]*',user):raise ValueError('Invalid saved monitor account')
        result.append({'host':host,'user':user})
    return result


def ssh(target):
    return ['ssh','-i',str(Path.home()/'.ssh/orgportal_lan_ed25519'),'-o','BatchMode=yes','-o','IdentitiesOnly=yes',
            '-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=6','-o','ForwardAgent=no',target['user']+'@'+target['host']]
