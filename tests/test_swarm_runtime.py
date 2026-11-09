import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts.swarm_runtime import copy_verified_file as copy_receipt

class TransferTests(unittest.TestCase):
    def test_modified_source_never_becomes_verified_final(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'public.fastq.gz';source.write_bytes(b'corrupted')
            first={'host':'source','local':True,'machine':'id'}
            second={'host':'target','local':True,'cache':str(Path(folder)/'cache')}
            receipt={'host':'source','path':str(source),'sha256':hashlib.sha256(b'expected!').hexdigest(),'bytes':9}
            with patch('scripts.swarm_runtime.AgentClient') as client:
                client.return_value.call.return_value={'machine':'id'}
                with self.assertRaisesRegex(RuntimeError,'SHA256 mismatch'):copy_receipt(first,second,receipt)
            self.assertFalse((Path(second['cache'])/'incoming'/receipt['sha256']/source.name).exists())
    def test_verified_copy_matches_source(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'public.fastq.gz';source.write_bytes(b'expected!')
            first={'host':'source','local':True,'machine':'id'}
            second={'host':'target','local':True,'cache':str(Path(folder)/'cache')}
            receipt={'host':'source','path':str(source),'sha256':hashlib.sha256(b'expected!').hexdigest(),'bytes':9}
            with patch('scripts.swarm_runtime.AgentClient') as client:
                client.return_value.call.return_value={'machine':'id'}
                result=copy_receipt(first,second,receipt)
            self.assertEqual(Path(result['path']).read_bytes(),b'expected!')
            self.assertEqual(result['original_host'],'source')

    def test_cached_copy_never_opens_source_stream_and_corruption_is_repaired(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'synthetic.bin';source.write_bytes(b'expected!')
            first={'host':'source','local':True,'machine':'id'}
            second={'host':'target','local':True,'cache':str(Path(folder)/'cache')}
            receipt={'host':'source','path':str(source),'sha256':hashlib.sha256(b'expected!').hexdigest(),'bytes':9}
            with patch('scripts.swarm_runtime.AgentClient') as client:
                client.return_value.call.return_value={'machine':'id'}
                result=copy_receipt(first,second,receipt)
                import subprocess
                real_popen=subprocess.Popen
                def probe_only(args, **kwargs):
                    if args[-1]!='probe':raise AssertionError('Cache hit opened source stream')
                    return real_popen(args, **kwargs)
                with patch('scripts.swarm_runtime.subprocess.Popen',side_effect=probe_only):
                    self.assertEqual(copy_receipt(first,second,receipt),result)
                Path(result['path']).write_bytes(b'corrupted')
                copy_receipt(first,second,receipt)
                self.assertEqual(Path(result['path']).read_bytes(),b'expected!')

class CleanupTests(unittest.TestCase):
    def test_lost_removal_reply_is_confirmed_without_repeating_force_remove(self):
        import subprocess
        from scripts.swarm_runtime import cleanup_container
        missing=subprocess.CompletedProcess([],1,'','Error: No such container: swarm-job-test')
        with patch('scripts.swarm_runtime.subprocess.run',side_effect=[subprocess.TimeoutExpired('docker rm',30),missing]) as run:
            self.assertTrue(cleanup_container({'local':True},'swarm-job-test'))
        self.assertEqual(len(run.call_args_list),2)
        self.assertEqual(run.call_args_list[1].args[0][:3],['docker','container','inspect'])

    def test_cleanup_retires_only_freshly_observed_stopped_instance(self):
        import json,subprocess
        from scripts.swarm_runtime import cleanup_container
        ident='a'*64;name='swarm-job-test'
        denied=subprocess.CompletedProcess([],1,'','removal in progress')
        observed=subprocess.CompletedProcess([],0,json.dumps([{'Id':ident,'Name':'/'+name,'State':{'Running':False,'Restarting':False}}]),'')
        removed=subprocess.CompletedProcess([],0,ident,'')
        with patch('scripts.swarm_runtime.subprocess.run',side_effect=[denied,observed,removed]) as run:
            self.assertTrue(cleanup_container({'local':True},name))
        self.assertEqual(run.call_args_list[-1].args[0],['docker','rm',ident])

    def test_dead_ssh_still_removes_container_before_releasing_lease(self):
        from functools import partial
        from scripts.swarm_runtime import run_job
        leased_run=partial(run_job,group='test')
        from unittest.mock import MagicMock
        events=[]
        with tempfile.TemporaryDirectory() as folder:
            host={'host':'local','local':True,'machine':'id'}
            def call(operation,*args,**kwargs):
                events.append(operation)
                if operation=='status':return {'machine':'id','capabilities':['renewable-resources']}
                if operation=='resource.capacity':return {'available_mb':4096,'available_cpus':4}
                if operation=='resource.acquire':return {'acquired':True}
                return {}
            transport=MagicMock();transport.wait.return_value=1;transport.poll.return_value=1
            with patch('scripts.swarm_runtime.AgentClient') as client,patch('scripts.swarm_runtime.eligible',return_value=True),patch('scripts.swarm_runtime.subprocess.Popen',return_value=transport),patch('scripts.swarm_runtime.cleanup_container',side_effect=lambda *a:events.append('container_removed') or True):
                client.return_value.call.side_effect=call
                with self.assertRaisesRegex(RuntimeError,'Worker stage failed'):leased_run(host,'sha256:pinned','false',[],folder,1,1024,Path(folder)/'status.json')
            report=json.loads((Path(folder)/'status.json').read_text())
            self.assertEqual(report['exit_code'],1)
            attempt=Path(report['attempt_report'])
            self.assertTrue(attempt.exists())
            self.assertTrue(attempt.with_suffix('.stderr').exists())
            self.assertEqual(json.loads(attempt.read_text())['container'],report['container'])
            self.assertLess(events.index('container_removed'),events.index('resource.release'))
    def test_failed_cleanup_keeps_lease_reserved(self):
        from functools import partial
        from scripts.swarm_runtime import run_job
        leased_run=partial(run_job,group='test')
        from unittest.mock import MagicMock
        operations=[]
        with tempfile.TemporaryDirectory() as folder:
            def call(operation,*args,**kwargs):
                operations.append(operation)
                return {'machine':'id','capabilities':['renewable-resources']} if operation=='status' else {'available_mb':4096,'available_cpus':4} if operation=='resource.capacity' else {'acquired':True}
            transport=MagicMock();transport.wait.return_value=1;transport.poll.return_value=1
            with patch('scripts.swarm_runtime.AgentClient') as client,patch('scripts.swarm_runtime.eligible',return_value=True),patch('scripts.swarm_runtime.subprocess.Popen',return_value=transport),patch('scripts.swarm_runtime.cleanup_container',return_value=False):
                client.return_value.call.side_effect=call
                with self.assertRaisesRegex(RuntimeError,'lease retained'):leased_run({'host':'local','local':True,'machine':'id'},'sha256:pinned','false',[],folder,1,1024,Path(folder)/'status.json')
            self.assertNotIn('resource.release',operations)

class AdmissionTests(unittest.TestCase):
    def test_merge_can_queue_for_capacity_without_starting_unleased_work(self):
        from functools import partial
        from scripts.swarm_runtime import run_job
        leased_run=partial(run_job,group='test')
        from unittest.mock import MagicMock
        operations=[];admitted=[];requests=[]
        def call(op,args=None,**kwargs):
            operations.append(op)
            if op=='status':return {'machine':'id','capabilities':['renewable-resources']}
            if op=='resource.capacity':return {'available_mb':0,'available_cpus':0}
            if op=='resource.acquire':requests.append(args);return {'acquired':len(requests)>1}
            return {}
        transport=MagicMock();transport.wait.return_value=0;transport.poll.return_value=0
        with tempfile.TemporaryDirectory() as folder,patch('scripts.swarm_runtime.AgentClient') as client,patch('scripts.swarm_runtime.eligible',return_value=True),patch('scripts.swarm_runtime.time.sleep'),patch('scripts.swarm_runtime.subprocess.Popen',return_value=transport),patch('scripts.swarm_runtime.cleanup_container',return_value=True):
            client.return_value.call.side_effect=call
            leased_run({'host':'peer','machine':'id','local':True},'sha256:pinned','true',[],folder,2,1024,Path(folder)/'status.json',wait_for_capacity=True,on_admitted=lambda:admitted.append(True))
        self.assertEqual(len(requests),3);self.assertTrue(all(r==requests[0] for r in requests));self.assertEqual(admitted,[True]);self.assertIn('resource.release',operations)


def test_failed_attempt_keeps_oom_evidence_across_retry(tmp_path):
    from unittest.mock import MagicMock
    import subprocess
    import pytest
    from functools import partial
    from scripts.swarm_runtime import run_job
    leased_run=partial(run_job,group='test')
    def call(op,args=None,**kwargs):
        if op=='status':return {'machine':'id','capabilities':['renewable-resources']}
        if op=='resource.capacity':return {'available_mb':4096,'available_cpus':4}
        return {'acquired':True}
    transport=MagicMock();transport.wait.return_value=137;transport.poll.return_value=137
    inspected=subprocess.CompletedProcess([],0,json.dumps({'Status':'exited','ExitCode':137,'OOMKilled':True,'Error':''}),'')
    with patch('scripts.swarm_runtime.AgentClient') as client, patch('scripts.swarm_runtime.eligible',return_value=True), patch('scripts.swarm_runtime.subprocess.Popen',return_value=transport) as launch, patch('scripts.swarm_runtime.subprocess.run',return_value=inspected), patch('scripts.swarm_runtime.cleanup_container',return_value=True):
        client.return_value.call.side_effect=call
        for _ in range(2):
            with pytest.raises(RuntimeError,match='OOM=True'):
                leased_run({'host':'local','local':True,'machine':'id'},'sha256:pinned','false',[],str(tmp_path),1,1024,tmp_path/'status.json')
        assert '--rm' not in launch.call_args.args[0]
    attempts=list((tmp_path/'attempts').glob('*.json'))
    assert len(attempts)==2
    for p in attempts:
        assert json.loads(p.read_text())['container_state']['OOMKilled'] is True
        assert p.with_suffix('.stderr').exists()


def test_owner_stop_requeues_stage_without_alignment_failure(tmp_path):
    from unittest.mock import MagicMock
    import pytest
    from functools import partial
    from scripts.swarm_runtime import run_job
    leased_run=partial(run_job,group='test')
    from scripts.genomics_distributed import CapacityWait
    def call(op,*args,**kwargs):
        if op=='status':return {'machine':'id','capabilities':['renewable-resources']}
        if op=='resource.capacity':return {'available_mb':4096,'available_cpus':4}
        if op=='worker.policy':return {'accepting_jobs':False,'mode':'paused'}
        return {'acquired':True}
    transport=MagicMock();transport.wait.return_value=143;transport.poll.return_value=143
    with patch('scripts.swarm_runtime.AgentClient') as client,patch('scripts.swarm_runtime.eligible',return_value=True),patch('scripts.swarm_runtime.subprocess.Popen',return_value=transport),patch('scripts.swarm_runtime.subprocess.run',return_value=__import__('subprocess').CompletedProcess([],0,'{}','')),patch('scripts.swarm_runtime.cleanup_container',return_value=True):
        client.return_value.call.side_effect=call
        with pytest.raises(CapacityWait,match='Owner paused'):
            leased_run({'host':'worker.local','local':True,'machine':'id'},'sha256:pinned','align',[],str(tmp_path),1,1024,tmp_path/'status.json')
        assert any(c.args[0]=='resource.release' for c in client.return_value.call.call_args_list)
        assert json.loads((tmp_path/'status.json').read_text())['phase']=='interrupted'


def test_ram_scratch_is_bounded_inside_the_reserved_non_swapping_container(tmp_path):
    from unittest.mock import MagicMock,patch
    from functools import partial
    from scripts.swarm_runtime import run_job
    leased_run=partial(run_job,group='test')
    def call(op,*args,**kwargs):
        if op=='status':return {'machine':'id','capabilities':['renewable-resources','docker-workers']}
        if op=='resource.capacity':return {'available_mb':16384,'available_cpus':8}
        return {'acquired':True}
    transport=MagicMock();transport.wait.return_value=0;transport.poll.return_value=0
    with patch('scripts.swarm_runtime.AgentClient') as client,patch('scripts.swarm_runtime.eligible',return_value=True),patch('scripts.swarm_runtime.subprocess.Popen',return_value=transport) as launched,patch('scripts.swarm_runtime.cleanup_container',return_value=True):
        client.return_value.call.side_effect=call
        leased_run({'host':'local','local':True,'machine':'id'},'sha256:pinned','true',[],str(tmp_path),2,8192,tmp_path/'status.json',tmpfs={'/ram':2048})
        argv=launched.call_args.args[0]
        assert argv[argv.index('--memory')+1]=='8192m'
        assert argv[argv.index('--memory-swap')+1]=='8192m'
        assert argv[argv.index('--tmpfs')+1]=='/ram:rw,nosuid,nodev,noexec,size=2048m,mode=1777'


def test_runtime_import_has_no_genome_application_dependencies():
    import subprocess,sys
    code="import sys;from scripts import swarm_runtime;assert not any(n.startswith(('scripts.genomics','scripts.wgs','scripts.julian','scripts.huref','scripts.genome_')) for n in sys.modules)"
    subprocess.run([sys.executable,'-c',code],check=True,capture_output=True)


def test_generic_jobs_use_their_own_group_and_no_genome_labels(tmp_path):
    from unittest.mock import MagicMock
    from scripts import swarm_runtime as runtime
    def call(op,args=None,**kwargs):
        if op=='status':return {'machine':'id','ready':True,'protocol':'lifetech-agent.v1','worker_policy':{'accepting_jobs':True},'capabilities':['renewable-resources','docker-workers','generic-workers','independent-job-sinks-v1']}
        if op=='resource.capacity':return {'available_mb':4096,'available_cpus':4}
        return {'acquired':True}
    process=MagicMock();process.wait.return_value=0
    with patch.object(runtime,'AgentClient') as client,patch.object(runtime.subprocess,'Popen',return_value=process) as launch,patch.object(runtime,'cleanup_container',return_value=True):
        client.return_value.call.side_effect=call
        runtime.run_job({'host':'local','local':True,'machine':'id'},'sha256:pinned','true',[],str(tmp_path),1,1024,tmp_path/'status.json',group='image-processing',labels={'org.example.pipeline':'thumbnail'})
    argv=launch.call_args.args[0]
    assert argv[argv.index('--name')+1].startswith('swarm-job-')
    assert 'org.lifetech.group=image-processing' in argv
    assert 'org.example.pipeline=thumbnail' in argv
    assert 'org.lifetech.genomics=true' not in argv
    acquisition=next(c for c in client.return_value.call.call_args_list if c.args[0]=='resource.acquire')
    assert acquisition.args[1]['group']=='image-processing'
    assert acquisition.args[1]['container'].startswith('swarm-job-')

    assert acquisition.args[1]['sink']=='cpu'
    assert 'org.lifetech.sink=cpu' in argv
    assert argv[argv.index('--pids-limit')+1]=='32'
    with patch.object(runtime,'AgentClient') as client,patch.object(runtime.subprocess,'Popen',return_value=process) as launch,patch.object(runtime,'cleanup_container',return_value=True):
        client.return_value.call.side_effect=call
        runtime.run_job({'host':'local','local':True,'machine':'id'},'sha256:pinned','true',[],str(tmp_path),1,1024,tmp_path/'bounded-threads.json',group='image-processing',pids_limit=128)
    argv=launch.call_args.args[0]
    assert argv[argv.index('--pids-limit')+1]=='128'


def test_worker_pid_limits_reject_unbounded_values_before_admission(tmp_path):
    import pytest
    from scripts import swarm_runtime as runtime
    for limit in (0,-1,True,31,4097,128.0):
        with patch.object(runtime,'AgentClient') as client,pytest.raises(ValueError,match='PID limit'):
            runtime.run_job({},'image','true',[],str(tmp_path),1,1024,tmp_path/'unused.json',group='test',pids_limit=limit)
        client.assert_not_called()

def test_combined_ram_scratch_is_checked_before_contacting_a_worker(tmp_path):
    import pytest
    from scripts import swarm_runtime as runtime
    with patch.object(runtime,'AgentClient') as client:
        with pytest.raises(ValueError,match='Combined RAM scratch'):
            runtime.run_job({},'image','command',[],str(tmp_path),1,1024,tmp_path/'status.json',group='test',tmpfs={'/first':600,'/second':600})
        client.assert_not_called()


def test_application_cannot_change_runtime_identity_labels(tmp_path):
    import pytest
    from scripts import swarm_runtime as runtime
    with patch.object(runtime,'AgentClient') as client:
        with pytest.raises(ValueError,match='identity labels'):
            runtime.run_job({},'image','command',[],str(tmp_path),1,1024,tmp_path/'status.json',group='test',labels={'org.lifetech.group':'someone-else'})
        client.assert_not_called()


def test_shared_cache_mount_and_private_memory_are_preserved_in_receipt(tmp_path):
    from unittest.mock import MagicMock
    from scripts import swarm_runtime as runtime,swarm_shared_cache
    cache={'key':'key','container':'owner','volume':'ram-volume','memory_mb':5318}
    def call(op,*args,**kwargs):
        if op=='status':return {'machine':'id','capabilities':['renewable-resources','docker-workers']}
        if op=='resource.capacity':return {'available_mb':4096,'available_cpus':4}
        return {'acquired':True}
    process=MagicMock();process.wait.return_value=0;process.poll.return_value=0
    mount=['--mount','type=volume,src=ram-volume,dst=/dev/shm,readonly,volume-nocopy']
    with patch.object(runtime,'AgentClient') as client,patch.object(runtime,'eligible',return_value=True),patch.object(runtime.subprocess,'Popen',return_value=process) as launched,patch.object(runtime,'cleanup_container',return_value=True),patch.object(swarm_shared_cache,'mount_args',return_value=mount):
        client.return_value.call.side_effect=call
        result=runtime.run_job({'host':'local','local':True,'machine':'id'},'image','true',[],str(tmp_path),2,1408,tmp_path/'status.json',group='test',tmpfs={'/work':512},shared_cache=cache)
    argv=launched.call_args.args[0]
    assert mount[1] in argv
    assert argv[argv.index('--memory')+1]=='1408m'
    assert json.loads((tmp_path/'status.json').read_text())['shared_cache']==cache


def test_docker_worker_observation_reconnects_without_relaunching():
    import json
    from types import SimpleNamespace
    from scripts import swarm_runtime as runtime
    def worker(running):
        return SimpleNamespace(returncode=0,stdout=json.dumps({'Name':'/swarm-job-lease','Config':{'Labels':{'org.lifetech.worker':'true','org.lifetech.job':'lease'}},'State':{'Running':running,'Status':'running' if running else 'exited','ExitCode':0}}),stderr='')
    with patch.object(runtime,'shell_command',side_effect=lambda host,argv:argv),patch.object(runtime.subprocess,'run',side_effect=[worker(True),ConnectionError('node restarting'),worker(True),worker(False)]) as calls,patch.object(runtime.time,'sleep'):
        assert runtime.observe_worker({},'swarm-job-lease','lease')==0
    assert len(calls.call_args_list)==4
    assert all(call.args[0][:2]==['docker','inspect'] for call in calls.call_args_list)


def test_docker_worker_observation_rejects_reused_name():
    import json,pytest
    from types import SimpleNamespace
    from scripts import swarm_runtime as runtime
    row=SimpleNamespace(returncode=0,stdout=json.dumps({'Name':'/swarm-job-lease','Config':{'Labels':{'org.lifetech.worker':'true','org.lifetech.job':'another'}}}),stderr='')
    with patch.object(runtime,'shell_command',side_effect=lambda host,argv:argv),patch.object(runtime.subprocess,'run',return_value=row):
        with pytest.raises(ValueError,match='ownership'):runtime.observe_worker({},'swarm-job-lease','lease')


def test_remote_job_detaches_and_observes_existing_container(tmp_path):
    from types import SimpleNamespace
    from scripts import swarm_runtime as runtime
    launched=[]
    def call(op,args=None,**kwargs):
        if op=='status':return {'machine':'id','capabilities':['renewable-resources','docker-workers']}
        if op=='resource.capacity':return {'available_mb':4096,'available_cpus':4}
        return {'acquired':True}
    def launch(argv,**kwargs):
        launched.append(argv)
        return SimpleNamespace(wait=lambda:0)
    with patch.object(runtime,'AgentClient') as client,patch.object(runtime,'eligible',return_value=True),patch.object(runtime,'shell_command',side_effect=lambda host,argv:argv),patch.object(runtime.subprocess,'Popen',side_effect=launch),patch.object(runtime,'observe_worker',return_value=0) as observe,patch.object(runtime.subprocess,'run',return_value=SimpleNamespace(returncode=0)),patch.object(runtime,'cleanup_container',return_value=True):
        client.return_value.call.side_effect=call
        runtime.run_job({'host':'peer','local':False,'machine':'id'},'image','true',[],str(tmp_path),1,1024,tmp_path/'report.json',group='test')
    assert '--detach' in launched[0]
    assert launched[0][launched[0].index('--log-driver')+1]=='local'
    observe.assert_called_once()
    host,name,lease=observe.call_args.args
    assert name=='genomics-'+lease
    assert 'org.lifetech.job='+lease in launched[0]


def test_recorded_worker_reconnects_and_recovers_same_completed_attempt():
    from scripts import swarm_runtime as runtime
    from unittest.mock import Mock
    lease='a'*32;name='genomics-'+lease;client=Mock()
    observation={'found':True,'job':lease,'container':name,'container_id':'b'*64,'exit_code':0,'running':False}
    client.call.side_effect=[ConnectionError('node replaced'),{'machine':'machine'},observation]
    with patch.object(runtime.time,'sleep'):
        assert runtime.observe_recorded_worker(client,{'machine':'machine'},name,lease)==observation
    client.close.assert_called_once()
    assert [c.args[0] for c in client.call.call_args_list]==['status','status','worker.status']


def test_recorded_worker_does_not_complete_missing_or_replaced_attempt():
    import pytest
    from scripts import swarm_runtime as runtime
    from unittest.mock import Mock
    client=Mock();client.call.side_effect=[{'machine':'other'}]
    with pytest.raises(ValueError,match='identity changed'):runtime.observe_recorded_worker(client,{'machine':'machine'},'genomics-'+'a'*32,'a'*32)
    client.call.side_effect=[{'machine':'machine'},{'found':False,'last_observation':None}]
    with pytest.raises(ValueError,match='missing'):runtime.observe_recorded_worker(client,{'machine':'machine'},'genomics-'+'a'*32,'a'*32)


def test_expired_start_permit_is_reacquired_before_docker_launch(tmp_path):
    import pytest
    from scripts.swarm_runtime import run_job,CapacityWait
    acquired=[]
    def call(op,args=None,**kwargs):
        if op=='status':return {'machine':'id','capabilities':['renewable-resources','docker-workers']}
        if op=='resource.capacity':return {'available_mb':4096,'available_cpus':4}
        if op=='resource.acquire':
            acquired.append(args)
            return {'acquired':len(acquired)==1}
        return {}
    with patch('scripts.swarm_runtime.AgentClient') as client,patch('scripts.swarm_runtime.eligible',return_value=True),patch('scripts.swarm_runtime.subprocess.Popen') as launch:
        client.return_value.call.side_effect=call
        with pytest.raises(CapacityWait,match='reservation expired'):
            run_job({'host':'local','local':True,'machine':'id'},'sha256:pinned','true',[],str(tmp_path),1,1024,tmp_path/'status.json',group='test')
        launch.assert_not_called()
    assert len(acquired)==2


def test_reclaimable_mapping_has_private_reservation_and_larger_no_swap_limit(tmp_path):
    from unittest.mock import MagicMock
    from scripts import swarm_runtime as runtime
    def call(op,args=None,**kw):
        if op=='status':return {'machine':'id','ready':True,'protocol':'lifetech-agent.v1','worker_policy':{'accepting_jobs':True},'capabilities':['renewable-resources','docker-workers','generic-workers']}
        if op=='resource.capacity':return {'available_mb':2048,'available_cpus':4}
        return {'acquired':True}
    process=MagicMock();process.wait.return_value=0
    with patch.object(runtime,'AgentClient') as client,patch.object(runtime.subprocess,'Popen',return_value=process) as launch,patch.object(runtime,'cleanup_container',return_value=True):
        client.return_value.call.side_effect=call
        runtime.run_job({'host':'local','local':True,'machine':'id'},'sha256:pinned','true',[],str(tmp_path),2,6656,tmp_path/'status.json',group='mapping',memory_reservation_mb=1408,tmpfs={'/work':512})
    argv=launch.call_args.args[0]
    for flag,value in [('--memory','6656m'),('--memory-swap','6656m'),('--memory-reservation','1408m')]:
        assert argv[argv.index(flag)+1]==value
    requests=[c.args[1] for c in client.return_value.call.call_args_list if c.args[0]=='resource.acquire']
    assert requests and all(r['memory_mb']==1408 for r in requests)


def test_private_reservation_must_cover_scratch_and_fit_hard_limit(tmp_path):
    import pytest
    from scripts import swarm_runtime as runtime
    for reservation,scratch in [(2048,512),(128,512),(True,0)]:
        with patch.object(runtime,'AgentClient') as client,pytest.raises(ValueError):
            runtime.run_job({},'image','true',[],str(tmp_path),1,1024,tmp_path/'status.json',group='test',memory_reservation_mb=reservation,tmpfs={'/work':scratch} if scratch else None)
        client.assert_not_called()


def test_gpu_completion_marker_rejects_paths_outside_private_work():
    import pytest
    from scripts.swarm_runtime import run_job
    for marker in ('relative', '/work', '/work/../gpu-locks/release', '/gpu-locks/release'):
        with pytest.raises(ValueError,match='completion marker'):
            run_job({},'image','true',[], '/work',1,128,'report',group='test',
                    gpu={},gpu_compute_complete_file=marker)
    with pytest.raises(ValueError,match='completion marker'):
        run_job({},'image','true',[], '/work',1,128,'report',group='test',
                gpu_compute_complete_file='/work/complete')


def test_integrated_gpu_workspace_is_included_before_host_admission(tmp_path):
    import pytest
    from scripts import swarm_runtime as runtime
    from test_node_execution import amd_binding
    gpu={**amd_binding(tmp_path/'runtime'),'memory_domain':'shared-system-memory','vram_mb':256}
    def call(op,args=None,**kw):
        if op=='status':return {'machine':'id','capabilities':['renewable-resources']}
        if op=='resource.capacity':return {'available_mb':500,'available_cpus':4}
        return {}
    with patch.object(runtime,'AgentClient') as client,patch.object(runtime,'eligible',return_value=True),patch.object(runtime.subprocess,'Popen') as launch:
        client.return_value.call.side_effect=call
        with pytest.raises(runtime.CapacityWait):
            runtime.run_job({'host':'local','local':True,'machine':'id'},'sha256:pinned','true',[],str(tmp_path),1,2048,tmp_path/'status.json',group='test',memory_reservation_mb=300,gpu=gpu)
        launch.assert_not_called()


def test_gpu_setup_uses_daemon_ram_mount_and_embeds_admission_code(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from scripts import swarm_runtime as runtime
    from scripts.launcher_streams import GPU_ADMISSION_SOURCE
    import time
    gpu={'uuid':'GPU-11111111-1111-1111-1111-111111111111','minimum_free_mib':512,'vram_mb':64}
    def call(op,args=None,**kw):
        if op=='status':return {'machine':'id','storage_policy':{'mode':'ram-only'},
            'capabilities':['renewable-resources','gpu-resource-inventory-v1','independent-job-sinks-v1']}
        if op=='resource.capacity':return {'available_mb':4096,'available_cpus':4}
        if op=='resource.gpus':return {'machine':'id','sampled_at':time.time(),'gpus':[
            {'uuid':gpu['uuid'],'allocation_known':True,'memory_free_mib':8192,'workers_known':True}]}
        return {'acquired':True}
    process=MagicMock();process.wait.return_value=0
    with patch.object(runtime,'AgentClient') as client,patch.object(runtime,'eligible',return_value=True),\
            patch.object(runtime,'shell_command',side_effect=lambda h,c:c),\
            patch.object(runtime.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as prepare,\
            patch.object(runtime.subprocess,'Popen',return_value=process) as launch,\
            patch.object(runtime,'observe_worker',return_value=0),\
            patch.object(runtime,'cleanup_container',return_value=True):
        client.return_value.call.side_effect=call
        runtime.run_job({'host':'peer','local':False,'machine':'id'},'sha256:pinned','true',[],
            '/work',2,1280,tmp_path/'report.json',group='test',gpu=gpu)
    helper=prepare.call_args_list[0].args[0]
    assert helper[:2]==['docker','run']
    assert 'type=bind,src=/dev/shm,dst=/gpu-parent' in helper
    assert helper[-1]=='jswarm-gpu-locks/id' and '--read-only' in helper
    assert helper[helper.index('--memory')+1]==helper[helper.index('--memory-swap')+1]=='64m'
    # Existing public ancestors are valid; the final accounting directory is
    # private. Never traverse an attacker-controlled symlink during setup.
    daemon=tmp_path/'daemon';daemon.mkdir();(daemon/'shared').mkdir(mode=0o755)
    setup_code=helper[-2].replace("'/gpu-parent'",repr(str(daemon)))
    with patch.object(runtime.sys,'argv',['setup','shared/private']):exec(setup_code,{})
    assert (daemon/'shared/private').stat().st_mode & 0o777==0o700
    outside=tmp_path/'outside';outside.mkdir();(daemon/'link').symlink_to(outside,target_is_directory=True)
    import pytest
    with patch.object(runtime.sys,'argv',['setup','link/private']),pytest.raises(AssertionError):exec(setup_code,{})
    assert not (outside/'private').exists()
    worker=launch.call_args.args[0]
    import shlex
    command=json.loads((tmp_path/'report.json').read_text())['command']
    assert shlex.split(command)[:3]==['python3','-c',GPU_ADMISSION_SOURCE]
    assert '/gpu-locks/gpu-admission-' not in worker[-1]

class ActiveTransferTests(unittest.TestCase):
    def test_live_destination_lock_waits_without_touching_partial(self):
        import fcntl
        from scripts.swarm_runtime import CapacityWait
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'public.bam';source.write_bytes(b'expected!')
            digest=hashlib.sha256(b'expected!').hexdigest()
            target=Path(folder)/'cache/incoming'/digest;target.mkdir(parents=True)
            partial=target/'public.bam.partial';partial.write_bytes(b'expect')
            first={'host':'source','local':True,'machine':'id'}
            second={'host':'target','local':True,'cache':str(Path(folder)/'cache')}
            receipt={'host':'source','path':str(source),'sha256':digest,'bytes':9}
            with (target/'.transfer.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                with patch('scripts.swarm_runtime.AgentClient') as client:
                    client.return_value.call.return_value={'machine':'id'}
                    with self.assertRaises(CapacityWait):copy_receipt(first,second,receipt)
                self.assertEqual(partial.read_bytes(),b'expect')


def test_workers_mount_every_shared_object_set_readonly(tmp_path):
    from unittest.mock import MagicMock,patch,call
    from scripts.swarm_runtime import run_job
    def respond(op,args=None,**kwargs):
        if op=='status':return {'machine':'id','capabilities':['renewable-resources']}
        if op=='resource.capacity':return {'available_mb':4096,'available_cpus':4}
        return {'acquired':True}
    process=MagicMock();process.wait.return_value=0;process.poll.return_value=0
    host={'host':'owner','local':True,'machine':'id'};private={'key':'private'};reference={'key':'reference'}
    with patch('scripts.swarm_runtime.AgentClient') as client,patch('scripts.swarm_runtime.eligible',return_value=True),patch('scripts.swarm_runtime.subprocess.Popen',return_value=process) as launch,patch('scripts.swarm_runtime.cleanup_container',return_value=True),patch('scripts.swarm_shared_cache.mount_args',side_effect=lambda h,c,**kw:['--mount','immutable-'+c['key']]) as mounts:
        client.return_value.call.side_effect=respond
        run_job(host,'pinned','true',[],str(tmp_path),1,1024,tmp_path/'run.json',group='test',shared_cache=private,shared_caches={'/references/one':reference})
    mounts.assert_has_calls([call(host,private),call(host,reference,target='/references/one',primary=False)])
    assert 'immutable-reference' in launch.call_args.args[0]


def test_receipt_persistence_failure_preserves_container_and_lease(tmp_path):
    from unittest.mock import MagicMock,patch
    from scripts import swarm_runtime as runtime
    def call(op,args=None,**kw):
        if op=='status':return {'machine':'id','ready':True,'protocol':'lifetech-agent.v1','worker_policy':{'accepting_jobs':True},'capabilities':['renewable-resources','docker-workers','generic-workers']}
        if op=='resource.capacity':return {'available_mb':2048,'available_cpus':4}
        return {'acquired':True}
    process=MagicMock();process.wait.return_value=0
    original=runtime.atomic_json;report=tmp_path/'status.json'
    def persist(path,value):
        if path==report and value.get('phase')=='complete':raise OSError('receipt storage unavailable')
        original(path,value)
    with patch.object(runtime,'AgentClient') as client,patch.object(runtime.subprocess,'Popen',return_value=process),patch.object(runtime,'cleanup_container') as cleanup,patch.object(runtime,'atomic_json',side_effect=persist):
        client.return_value.call.side_effect=call
        import pytest
        with pytest.raises(RuntimeError,match='lease retained'):
            runtime.run_job({'host':'local','local':True,'machine':'id'},'sha256:pinned','true',[],str(tmp_path),1,1024,report,group='test')
        cleanup.assert_not_called()
        assert not any(c.args[0]=='resource.release' for c in client.return_value.call.call_args_list)
