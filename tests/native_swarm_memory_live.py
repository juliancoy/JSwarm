"""Host-service checks of real native binary peers and discovered scoped IPv6."""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deployment',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--node',default='compai.local')
    parser.add_argument('--peer',action='append',help='Limit the proof to these deployed peers; defaults to ready participants')
    args=parser.parse_args();plan=json.loads(args.deployment.read_text())
    record=plan['nodes'][args.node];release=Path(record['release'])
    config=json.loads((release/'config.json').read_text())
    name=json.loads((Path(config['tls_root'])/'config.json').read_text())['name']
    if name!=args.node:raise ValueError('Run in the enrolled mount-service host environment')
    obj=config['objects'][0]['manifest'];results={}
    with tempfile.TemporaryDirectory(prefix='native-memory-proof-') as folder:
        path=Path(folder)/'config.json'
        def read(candidate):
            path.write_text(json.dumps(candidate))
            p=subprocess.run([str(release/'bin/swarm-memory'),'--config',str(path),'--read',obj['id'],'0',str(obj['page_bytes'])],capture_output=True,timeout=45)
            if p.returncode:raise RuntimeError(p.stderr.decode())
            if hashlib.sha256(p.stdout).hexdigest()!=obj['pages'][0]:raise AssertionError('Page hash differs')
            return json.loads(p.stderr)
        peers=args.peer or [n for n in config['objects'][0]['peers'] if plan['nodes'].get(n,{}).get('ready')]
        if not peers:raise ValueError('No ready peers to qualify')
        if any(n not in config['objects'][0]['peers'] for n in peers):raise ValueError('Unconfigured peer')
        for peer in peers:
            candidate=json.loads(json.dumps(config));candidate['objects'][0].update(sources=[],peers=[peer]);candidate['replicas']=0
            try:
                stats=read(candidate)
                assert stats['counters']['peer_hits']==1 and stats['counters'].get('source_requests',0)==0
                results[peer]={'passed':True,'source_disabled':True,'metrics':stats}
            except Exception as error:results[peer]={'passed':False,'error':str(error)}
        snapshot=json.loads(Path(config['discovery']).read_text())
        snapshot['hosts']={n:[r for r in rows if r['family']==6] for n,rows in snapshot['hosts'].items()}
        discovery=Path(folder)/'ipv6.json';discovery.write_text(json.dumps(snapshot))
        candidate=json.loads(json.dumps(config));candidate['discovery']=str(discovery);candidate['objects'][0]['peers']=[];candidate['replicas']=0
        try:
            stats=read(candidate);assert stats['transport']['ipv6_connections']>0
            results['ipv6-source']={'passed':True,'ipv4_excluded_for_test':True,'metrics':stats}
        except Exception as error:results['ipv6-source']={'passed':False,'error':str(error)}
    report={'date':datetime.datetime.now(datetime.timezone.utc).isoformat(),'node':name,'revision':record['revision'],
            'binary_sha256':record['binary_sha256'],'environment':'native mount-service host','excluded_peers':[n for n in config['objects'][0]['peers'] if n not in peers],'results':results}
    args.report.write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
    return int(any(not r['passed'] for r in results.values()))

if __name__=='__main__':raise SystemExit(main())
