"""Read-only route qualification from the actual launcher container."""
import json
import socket
import sys
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parents[1]/'swarm-control/server')]
from scripts.launcher_discovery import resolve_endpoint,network_signature
from agent_client import AgentClient,CONFIG,multipath_connection
config=json.loads(CONFIG.read_text());rows=[]
for node in config['peers']:
    row={'node':node,'paths':[]}
    try:
        endpoint,addresses=resolve_endpoint(node)
        client=AgentClient({'host':node,'agent_endpoint':endpoint,'resolved_addresses':addresses})
        try:
            status=client._request('status',timeout=5)
            assert status['node']==node
            row['authenticated_hostname_access']=True
        finally:client.close()
        for address in addresses:
            url='http://'+('['+address+']' if ':' in address else address)+':8790'
            probe=AgentClient({'host':node,'agent_endpoint':url})
            probe.connection._create_connection=multipath_connection
            try:
                value=probe._request('status',timeout=2)
                assert value['node']==node
                row['paths'].append({'family':6 if ':' in address else 4,'scoped':'%' in address,'available':True})
            except Exception as error:row['paths'].append({'family':6 if ':' in address else 4,'scoped':'%' in address,'available':False,'error':type(error).__name__})
            finally:probe.close()
    except Exception as error:row['route_error']=type(error).__name__+': '+str(error)[:160]
    rows.append(row)
print(json.dumps({'environment':'actual launcher container','interfaces':sorted(set(name for name,_ in network_signature())),'nodes':rows,'multipath_subflows_verified':False}),flush=True)
if not any(r.get('authenticated_hostname_access') for r in rows):raise SystemExit(1)
