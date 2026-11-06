"""Read-only runtime observations for ongoing genome work; never reads genomic content.

Independent sidecars keep sampler observations separate from executor-owned task files.
Rates use counter deltas from the same phase/attempt, never file size alone.
"""
import argparse
import datetime
import json
import math
import os
from pathlib import Path
import subprocess
import time


def load(path):
    try:return json.loads(Path(path).read_text())
    except (OSError,ValueError):return {}


def measured(current, previous, now):
    """Add rates only for fresh, monotonic counters from the same attempt and phase."""
    result={**current,'sampled_at':now}
    start=current.get('attempt_started')
    if isinstance(start,(int,float)) and start<=now:result['elapsed_seconds']=now-start
    same=all(current.get(k)==previous.get(k) for k in ('phase_key','attempt_started','unit','total_units'))
    dt=now-previous.get('sampled_at',now)
    count=current.get('completed_units');prior=previous.get('completed_units')
    if same and 1<=dt<=90 and all(type(v) in (int,float) and math.isfinite(v) for v in (count,prior)) and count>=prior:
        rate=(count-prior)/dt
        result['units_per_second']=rate
        if type(current.get('cpu_seconds')) in (int,float) and type(previous.get('cpu_seconds')) in (int,float) and current['cpu_seconds']>=previous['cpu_seconds']:
            result['cpu_percent']=100*(current['cpu_seconds']-previous['cpu_seconds'])/dt
        total=current.get('total_units')
        if rate>0 and type(total) in (int,float) and total>count and not current.get('eta_unavailable_reason'):
            result.update(execution_eta_seconds=(total-count)/rate,eta_scope=current.get('current_phase','Current phase'),eta_basis='Observed counter delta; current phase only, excludes subsequent verification and dependent work')
    return result


REMOTE=r'''
import os,pathlib,json,sys,time,subprocess
request=json.loads(sys.argv[1]);out={'sampled_at':time.time()}
if request.get('destination'):
 p=pathlib.Path(request['destination']);candidates=[p,p.parent/'.rsync-partial'/p.name]
 out['copy_bytes']=max([x.stat().st_size for x in candidates if x.exists()]+[0])
 matches=[]
 for proc in pathlib.Path('/proc').glob('[0-9]*'):
  try:
   args=(proc/'cmdline').read_bytes().split(b'\0')
   if b'fetch' not in args or str(p).encode() not in args:continue
   for fd in (proc/'fd').iterdir():
    try:
     if pathlib.Path(os.readlink(fd)) not in candidates:continue
     info=dict(line.split(':',1) for line in (proc/'fdinfo'/fd.name).read_text().splitlines())
     # The Python fetch process opens the archive read-only for SHA256 validation.
     if int(info['flags'].strip(),8)&3:continue
     matches.append(int(info['pos'].strip()))
    except (OSError,ValueError,KeyError):pass
   stat=(proc/'stat').read_text().rsplit(')',1)[1].split();io=dict(line.split(':',1) for line in (proc/'io').read_text().splitlines())
   out.update(cpu_seconds=(int(stat[11])+int(stat[12]))/os.sysconf('SC_CLK_TCK'),rss_bytes=int(stat[21])*os.sysconf('SC_PAGE_SIZE'),process_read_bytes=int(io['read_bytes']),process_sampled_at=time.time())
  except (OSError,ValueError,KeyError):pass
 if matches:out['checksum_bytes']=max(matches)
 if p.exists():
  proof=p.with_name(p.name+'.verified.json')
  if proof.exists():
   try:
    v=json.loads(proof.read_text());s=p.stat()
    out['archive_verified']=v.get('identity')==[s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns] and v.get('sha256')==request.get('sha256') and v.get('bytes')==request.get('total_bytes')
   except (OSError,ValueError):pass
 progress=p.with_name(p.name+'.verification-progress.json')
 if progress.exists():out['decompression']=json.loads(progress.read_text())
if request.get('shards'):
 p=pathlib.Path(request['shards']);receipts=list(p.glob('[0-9]*.json'));out['available_shards']=len(receipts)
 out['latest_shard_at']=max((f.stat().st_mtime for f in receipts),default=0)
 job=p.parent/'job.json'
 if job.exists():out['source_archive_bytes']=sum(i['bytes'] for i in json.loads(job.read_text()).get('items',[]))
 proof=p/'stream-verified.json'
 if proof.exists():out['stream_verified']=json.loads(proof.read_text())
 replay=p/'replay-progress.json'
 if replay.exists():out['source_replay']=json.loads(replay.read_text())
 out['source_md5_progress']=[json.loads(f.read_text()) for f in sorted(p.glob('source-*.md5-progress.json'))]
if request.get('shards'):
 run=subprocess.run(['docker','ps','--format','{{json .}}'],capture_output=True,text=True,timeout=12,check=True)
 out['running_containers']=[json.loads(line)['Names'] for line in run.stdout.splitlines() if line.strip()]
if request.get('container'):
 run=subprocess.run(['docker','stats','--no-stream','--format','{{json .}}',request['container']],capture_output=True,text=True,timeout=12)
 if run.returncode==0 and run.stdout.strip():
  v=json.loads(run.stdout.splitlines()[-1]);out['producer_cpu_percent']=float(v['CPUPerc'].rstrip('%'));out['producer_memory']=v['MemUsage']
print(json.dumps(out))
'''


class Sampler:
    def __init__(self,state,wgs):
        self.state=Path(state);self.wgs=Path(wgs);self.previous={}
        import sys
        sys.path.insert(0,str((Path(__file__).resolve().parents[2] if Path(__file__).resolve().parents[1].name in ('GenomeHarbor', 'JSwarm') else Path(__file__).resolve().parents[1])/'swarm-control/server'))
        from docker_utils.policy import inventory,ssh
        from agent_client import CONFIG
        config=load(CONFIG);name='julian-latitude.local';account=next(h for h in inventory() if h['host']==name)
        base=ssh(account)
        self.ssh=base[:-1]+['-4','-o','HostName='+config['peers'][name],'-o','HostKeyAlias='+name]+base[-1:]

    def remote(self,request):
        import shlex
        p=subprocess.run(self.ssh+[shlex.join(['python3','-c',REMOTE,json.dumps(request)])],capture_output=True,text=True,timeout=25,check=True)
        return json.loads(p.stdout)

    def publish(self,task,child,current):
        key=task+'/'+child;now=time.time();value=measured(current,self.previous.get(key,{}),now);self.previous[key]=value
        folder=self.state/'task-runtime'/task;folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        path=folder/(child+'.json');tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(value));tmp.chmod(0o600);tmp.replace(path)

    def replica(self):
        state=load(self.state/'julian-original-copy/state.json')
        for mate,row in state.get('files',{}).items():
            if row.get('verified'):continue
            name='mate-'+mate;total=row.get('total_bytes',0)
            # Destination names come from the already-authorized copy manifest, not process arguments.
            manifest=load('/mnt/Cancer/genomics-workbench/julian-huref2-20261101/manifest.json')
            source=manifest['reads'][int(mate)-1]['path'];dest=state['destination_directory']+'/'+Path(source).name+'.gz'
            remote=self.remote({'destination':dest,'sha256':row.get('compressed_sha256'),'total_bytes':total})
            phase=row.get('phase','Working');count=row.get('completed_bytes',0)
            if 'Copying original' in phase or 'HTTPS unavailable' in phase:
                if 'checksum_bytes' in remote:phase='Verifying transferred archive SHA256';count=remote['checksum_bytes']
                elif remote.get('archive_verified'):phase='Archive verified; preparing independent content verification';count=total
                else:phase='Transferring compressed archive';count=remote['copy_bytes']
            elif remote.get('decompression',{}).get('updated',0)>time.time()-45:
                phase='Verifying decompressed content';count=remote['decompression']['bytes'];total=manifest['reads'][int(mate)-1]['bytes']
            current={'current_phase':phase,'phase_key':phase,'completed_units':count,'total_units':total,'unit':'input bytes','basis':'Observed phase bytes; input completion is not workflow completion'}
            for k in ('cpu_seconds','rss_bytes','process_read_bytes','producer_cpu_percent'): 
                if k in remote:current[k]=remote[k]
            prior=self.previous.get('julian-original-read-replica/'+name,{})
            current['attempt_started']=prior.get('attempt_started',time.time()) if prior.get('phase_key')==phase else time.time()
            current['timing_basis']='Phase elapsed since monitoring began; original start time unavailable'
            self.publish('julian-original-read-replica',name,current)

    def venter(self):
        root=self.wgs/'recall/venter';generation=load(root/'generation.json')
        from scripts.venter_task_progress import current_state
        _,_,_,state=current_state(root)
        if not generation.get('work'):return
        stream=load(root/'stream.json')
        source_request={'shards':generation['work']+'/shards','container':stream.get('container') if stream.get('phase')=='running' else None}
        cache=self.state/'task-runtime/venter-source.json'
        try:
            remote=self.remote(source_request)
            from scripts.launcher_streams import atomic_json
            atomic_json(cache,{'generation':generation['generation'],'observation':remote})
        except (OSError,RuntimeError,subprocess.SubprocessError):
            saved=load(cache);remote=saved.get('observation',{}) if saved.get('generation')==generation['generation'] else {}
        done=sum(j.get('phase')=='complete' for j in state.get('jobs',{}).values())
        verified=remote.get('stream_verified',{});total=verified.get('shards')
        phase='Mapping verified source shards' if total else 'Preparing and mapping streamed source shards'
        current={'phase_key':generation['generation'],'current_phase':phase,'completed_units':done,'unit':'shards','available_shards':remote.get('available_shards'),'basis':'Committed mapping receipts; total remains unknown until complete paired-source verification','timing_basis':'Current source generation start'}
        if total:current['total_units']=total
        try:current['attempt_started']=datetime.datetime.fromisoformat(generation['started']).timestamp()
        except (KeyError,ValueError):pass
        for k in ('producer_cpu_percent','producer_memory'):
            if k in remote:current[k]=remote[k]
        current['runtime_detail']=f"{done:,} mapping receipts; {remote.get('available_shards',0):,} source shards available. "+('Complete source denominator verified.' if total else 'Source preparation still running; full denominator not yet verified.')
        replay=remote.get('source_replay',{})
        if not total and replay.get('phase')=='indexed_content_replay':
            current.update(phase_key=generation['generation']+':indexed-source-replay',current_phase='Verifying original source against completed alignment inputs',
                completed_units=replay['verified_shards'],unit='source shards',decoded_bytes=replay.get('decoded_bytes'),
                decoded_bytes_per_second=replay.get('decoded_bytes_per_second'),checkpoint_shards=replay.get('checkpoint_shards'),mapping_shards_completed=done,
                basis='Byte-exact source comparisons; indexed restart checkpoints; publisher MD5 verification remains mandatory')
            current['runtime_detail']=f"{replay['verified_shards']:,} source shards revalidated; {done:,} mapping receipts already complete. Checkpoint retains {replay.get('checkpoint_shards',0):,} verified shards across restarts."
            current['source_checksum_progress']=remote.get('source_md5_progress',[])
            available=remote.get('available_shards',0)
            # Existing shard count is a real bounded replay denominator. New
            # receipts after the last replay sample prove the parser moved on.
            if available>=replay['verified_shards'] and available>0:current['total_units']=available
            if replay.get('updated') and remote.get('latest_shard_at',0)>replay['updated']:
                current.update(phase_key=generation['generation']+':unseen-source-tail',
                    current_phase='Parsing remaining source reads and mapping available shards',
                    completed_units=done,total_units=available,unit='available shards',
                    eta_unavailable_reason='Source scan still discovering reads; final paired-read count requires end-of-file',
                    basis='Mapped receipts / currently available paired shards; growing backlog, not a final whole-source denominator')
                current['runtime_detail']=f"{replay['verified_shards']:,} original alignment-input shards revalidated. {done:,}/{available:,} currently available shards mapped; the source parser is producing additional shards."
                archive_bytes=remote.get('source_archive_bytes')
                if archive_bytes:current['runtime_detail']+=f" Original compressed input size is known: {archive_bytes/1e9:.2f} GB; compressed size does not specify the number of read pairs."
        distributed=load(root/'distributed/progress.json')
        if distributed:
            current['distributed_verification']=distributed
            current['runtime_detail']+=f" Remote shard verification: {distributed.get('completed_mate_tasks',0):,}/{distributed.get('total_mate_tasks',0):,} mate tasks; original-byte confirmation remains mandatory."
        self.publish('alignment-reliability-recovery-20261102','venter-resume',current)
        # Publish live execution assignments separately from historical workflow
        # summaries, which can belong to an earlier generation.
        running=set(remote.get('running_containers',[]));children=[]
        now=time.time()
        if stream.get('container') in running:
            children.append({'id':'source-parser','name':'Parse Venter source reads into paired shards',
                'worker':stream['host'],'status':'running','sampled_at':now,
                'current_phase':'Source parsing and shard production',
                'detail':str(remote.get('available_shards',0))+' paired shards available; source scan is still open. '+
                    str(remote.get('producer_cpu_percent','unmeasured'))+'% CPU; '+str(remote.get('producer_memory','memory unmeasured')),
                'basis':'Live container observation; whole-source percentage unavailable'})
        folder=root/'generations'/generation['generation']/'full'
        ram_waits={}
        for path in folder.glob('SRR*.json'):
            report=load(path)
            if report.get('phase')=='waiting_capacity' and report.get('storage_policy')=='ram-only' and not path.with_name(path.stem+'.receipt.json').exists():
                ram_waits[report['host']]=report
            if report.get('host')!=stream.get('host') or report.get('container') not in running:continue
            progress=report.get('node_progress') or {}
            fresh=0<=now-progress.get('updated',0)<60
            observed={key:progress[key] for key in ('completed_units','total_units','unit','units_per_second','execution_eta_seconds','eta_scope') if key in progress} if fresh else {}
            children.append({'id':path.stem,'name':'Align Venter shard '+path.stem.rsplit('-',1)[-1],
                'worker':report['host'],'status':'running','sampled_at':now,
                'current_phase':progress.get('phase') if fresh else 'Alignment pipeline: mapping, sorting and verification',
                'detail':'Node-owned execution; progress reported by the worker.' if fresh else 'Live container verified. Reserved '+str(report.get('cpus'))+' CPUs and '+str(report.get('memory_mb'))+' MiB RAM. Awaiting a fresh worker progress sample.',**observed})
        for worker,report in ram_waits.items():
            children.append({'id':'ram-only-'+worker.replace('.','-'),'name':'RAM-only alignment admission','worker':worker,
                'status':'waiting_capacity','sampled_at':now,'current_phase':'Waiting for '+str(report['memory_mb'])+' MiB available RAM',
                'detail':report['detail']})
        task={'id':'venter-live-source-worker','name':'Venter · live execution assignments',
              'status':'running' if children else 'waiting','updated':now,'children':children,
              'detail':'Fresh read-only Docker observations; executor ownership is unchanged.'}
        from scripts.launcher_streams import atomic_json
        atomic_json(self.state/'tasks'/(task['id']+'.json'),task)
        self.project_current_venter(root,remote)



    def project_current_venter(self,root,remote):
        from concurrent.futures import ThreadPoolExecutor
        from scripts.swarm_runtime import AgentClient,CONFIG
        from scripts.venter_task_progress import project
        from scripts.launcher_streams import atomic_json
        config=load(CONFIG)
        hosts=[h for h in load(self.wgs/'recall-hosts.json') if h['host'] in config.get('peers',{})] if (self.wgs/'recall-hosts.json').exists() else []
        def observe(host):
            client=AgentClient({**host,'agent_endpoint':'http://'+config['peers'][host['host']]+':8790'})
            try:
                row=client.call('status',timeout=2)
                if row.get('machine')!=host['machine']:raise ValueError('Worker identity changed')
                return host['host'],{'sampled_at':time.time(),'workers':row.get('workers',[])}
            except Exception as error:return host['host'],{'error':type(error).__name__}
            finally:client.close()
        with ThreadPoolExecutor(max_workers=max(1,len(hosts))) as pool:fleet=dict(pool.map(observe,hosts))
        try:
            probe=subprocess.run(['docker','inspect','swarm-control-alignment','--format','{{json .State}}'],capture_output=True,text=True,timeout=5)
            controller=json.loads(probe.stdout) if probe.returncode==0 else {}
        except (OSError,ValueError,subprocess.SubprocessError):controller={}
        alignment,assignments=project(root,remote,fleet,controller=controller)
        folder=self.state/'task-projections'
        for task in (alignment,assignments):atomic_json(folder/(task['id']+'.json'),task)
        recovery=load(self.state/'tasks/alignment-reliability-recovery-20261102.json')
        if recovery:
            recovery.update(updated=time.time(),detail=alignment['detail'],stale=alignment['stale'])
            for child in recovery.get('children',[]):
                if child['id']=='venter-resume':child.update(status=alignment['status'],detail=alignment['detail'],completed_units=alignment['primary_progress']['completed'],total_units=alignment['primary_progress']['total'],unit='batches')
            atomic_json(folder/(recovery['id']+'.json'),recovery)
        qc=load(self.wgs/'qc/shard-audit/progress.json')
        proof=remote.get('stream_verified',{});total=proof.get('shards',0)*2
        if qc.get('generation')==alignment['generation'] and proof.get('phase')=='paired_stream_md5_verified' and total>0:
            counts=qc.get('jobs',{});done=counts.get('completed',0);failed=counts.get('failed',0)+counts.get('failed_integrity',0)
            status='completed' if done==total and not failed else 'failed' if failed else 'running'
            task=load(self.state/'tasks/minipc-venter-shard-qc.json')
            if task:
                task.update(status=status,updated=time.time(),stale=False)
                for child in task.get('children',[]):child.update(status=status,completed_units=done,total_units=total,unit='mate shards')
                atomic_json(folder/(task['id']+'.json'),task)


    def once(self):
        errors=[]
        for name in ('replica','venter'):
            try:getattr(self,name)()
            except Exception as error:errors.append(name+': '+type(error).__name__)
        return errors


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--state',type=Path,default=Path.home()/'.local/state/swarm-control');p.add_argument('--wgs',type=Path,default=Path('/mnt/PILEDRIVE/genomics-workbench/wgs'));p.add_argument('--once',action='store_true');args=p.parse_args()
    os.umask(0o077);sampler=Sampler(args.state,args.wgs)
    while True:
        errors=sampler.once()
        if errors:print('; '.join(errors),flush=True)
        if args.once:break
        time.sleep(15)

if __name__=='__main__':main()
