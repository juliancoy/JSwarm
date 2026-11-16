from pathlib import Path
import unittest
from unittest.mock import Mock,patch

from agent_updates import LauncherUpdates


class LauncherUpdateTests(unittest.TestCase):
    def setUp(self):
        self.progress=Mock()
        self.updates=LauncherUpdates(Path(__file__).resolve().parents[3],lambda:False,self.progress)
        self.host={'host':'worker','lan_address':'10.42.0.2','agent_endpoint':'http://worker-swarm.local:8790'}
        self.status={'node':'worker','machine':'a'*64,'ready':True,'source_revision':'old','active_jobs':[],'capabilities':['node-command-v1']}

    def test_node_release_contains_only_pinned_unmodified_source(self):
        from agent_updates import source_revision
        root=Path(__file__).resolve().parents[3]
        self.assertEqual(self.updates.files['launcher_agent.py'],(root/'scripts/launcher_agent.py').read_text())
        self.assertEqual(self.updates.git_source['mode'],'mounted-v1')
        self.assertEqual(self.updates.revision,source_revision(self.updates.files,self.updates.git_source))

    def test_current_launcher_does_not_reload(self):
        status={**self.status,'source_revision':self.updates.revision}
        with patch.object(self.updates,'stage') as stage:
            self.assertIs(self.updates.ensure(self.host,Mock(),status),status)
        stage.assert_not_called()

    def test_staging_uses_signed_api_and_ignores_ambient_ssh_settings(self):
        with patch('agent_updates.subprocess.run',return_value=Mock(returncode=0)) as run, patch('agent_client.CONFIG') as config, patch('scripts.node_source_delivery.deliver') as delivery:
            config.read_text.return_value='{"controller_public_key":"'+'ab'*32+'","peers":{"worker":"10.42.0.2"}}'
            self.updates.stage(self.host,self.status)
        command=run.call_args.args[0]
        self.assertTrue(command[1].endswith('swarm_command.py'))
        self.assertIn('http://worker-swarm.local:8790',command)
        self.assertIn('a'*64,command)
        self.assertNotIn('ssh',command)
        import json
        self.assertEqual(json.loads(run.call_args.kwargs['input'])['peers'],{'worker':'10.42.0.2'})

    def test_outdated_launcher_waits_through_restart_and_checks_revision(self):
        ready={**self.status,'source_revision':self.updates.revision}
        client=Mock();client.call.side_effect=[ConnectionError('restarting'),ready]
        with patch.object(self.updates,'stage') as stage,patch('agent_updates.time.sleep'):
            self.assertEqual(self.updates.ensure(self.host,client,self.status),ready)
        stage.assert_called_once_with(self.host,self.status)
        self.assertEqual(self.host['launcher_update']['status'],'updated')
        self.assertEqual([call.args[1] for call in self.progress.call_args_list],['updating','waiting_for_update'])

    def test_busy_launcher_updates_without_waiting_for_job_completion(self):
        status={**self.status,'active_jobs':['existing-run'],'active_commands':25}
        client=Mock();client.call.return_value={**status,'source_revision':self.updates.revision}
        with patch.object(self.updates,'stage') as stage:
            self.updates.ensure(self.host,client,status)
        stage.assert_called_once_with(self.host,status)

    def test_explicit_rolling_update_drains_without_stopping_preparation(self):
        status={**self.status,'active_jobs':['existing-run'],'preparations':['accepted-preparation']}
        client=Mock();client.call.side_effect=[
            {**status,'ready':False,'worker_policy':{'mode':'draining'}},
            {**self.status,'source_revision':self.updates.revision}]
        with patch.object(self.updates,'stage') as stage,patch('agent_updates.time.sleep'):
            result=self.updates.ensure(self.host,client,status,drain=True,wait_timeout=900)
        stage.assert_called_once_with(self.host,status)
        self.assertEqual(result['source_revision'],self.updates.revision)
        self.assertTrue(all(call.args==('status',) for call in client.call.call_args_list))

    def test_busy_ready_worker_can_be_verified_without_interrupting_work(self):
        status={**self.status,'active_jobs':['existing-run']}
        with patch.object(self.updates,'stage') as stage:
            self.assertIs(self.updates.ensure(self.host,Mock(),status,allow_compatible=True),status)
        stage.assert_not_called()
        self.assertEqual(self.host['launcher_update']['status'],'deferred')

    def test_idle_compatible_worker_does_not_wait_for_an_update(self):
        with patch.object(self.updates,'stage') as stage:
            self.assertIs(self.updates.ensure(self.host,Mock(),self.status,allow_compatible=True),self.status)
        stage.assert_not_called()
        self.assertEqual(self.host['launcher_update']['status'],'deferred')

    def test_unready_worker_is_replaced_and_verified_before_reuse(self):
        status={**self.status,'ready':False,'active_jobs':['run']}
        client=Mock();client.call.return_value={**self.status,'source_revision':self.updates.revision}
        with patch.object(self.updates,'stage') as stage:
            result=self.updates.ensure(self.host,client,status,allow_compatible=True)
        self.assertTrue(result['ready']);stage.assert_called_once()

    def test_replacement_machine_cannot_pass_update_readiness(self):
        client=Mock();client.call.return_value={**self.status,'machine':'another','source_revision':self.updates.revision}
        with patch.object(self.updates,'stage'):
            with self.assertRaisesRegex(RuntimeError,'identity changed'):
                self.updates.ensure(self.host,client,self.status)

    def test_cancellation_during_reload_stops_polling(self):
        self.updates.stopped=lambda:True
        client=Mock()
        with patch.object(self.updates,'stage'):
            with self.assertRaises(InterruptedError):self.updates.ensure(self.host,client,self.status)
        client.call.assert_not_called()

    def test_unready_replacement_times_out_without_admitting_worker(self):
        with patch.object(self.updates,'stage'),patch('agent_updates.time.monotonic',side_effect=[0,241]):
            with self.assertRaisesRegex(RuntimeError,'did not become ready'):
                self.updates.ensure(self.host,Mock(),self.status)

    def test_launch_reservations_do_not_defer_node_replacement(self):
        status={**self.status,'active_resource_leases':1}
        client=Mock();client.call.return_value={**self.status,'source_revision':self.updates.revision}
        with patch.object(self.updates,'stage') as stage:
            self.updates.ensure(self.host,client,status)
        stage.assert_called_once_with(self.host,status)

    def test_supervisor_fix_is_part_of_standard_api_deployment(self):
        import json,hashlib
        with patch('agent_updates.subprocess.run',return_value=Mock(returncode=0)) as run,patch('agent_client.CONFIG') as config, patch('scripts.node_source_delivery.deliver') as delivery:
            config.read_text.return_value=json.dumps({'controller_public_key':'ab'*32,'peers':{'worker':'10.42.0.2'}})
            self.updates.stage(self.host,self.status)
        payload=json.loads(run.call_args.kwargs['input'])
        self.assertIn('prepare_update',payload['supervisor_source'])
        self.assertEqual(payload['supervisor_sha256'],hashlib.sha256(payload['supervisor_source'].encode()).hexdigest())

    def test_automatic_update_cannot_remove_newer_runtime_capabilities(self):
        status={**self.status,'capabilities':['stream-work-v1','encrypted-backup']}
        self.updates.capabilities={'encrypted-backup'}
        with patch.object(self.updates,'stage') as stage:
            self.assertIs(self.updates.ensure(self.host,Mock(),status),status)
        stage.assert_not_called()
        self.assertIn('stream-work-v1',self.host['launcher_update']['reason'])




def test_update_command_rejects_changed_identity_and_missing_api_capability(monkeypatch):
    import json,pytest
    updates=LauncherUpdates.__new__(LauncherUpdates)
    monkeypatch.setattr('agent_client.CONFIG',Mock(read_text=lambda:json.dumps({'peers':{'worker':'10.42.0.2'}})))
    status={'node':'worker','machine':'a'*64,'capabilities':['node-command-v1']}
    with pytest.raises(ValueError,match='identity'):
        updates.command({'host':'worker'},'pass',status={**status,'node':'other'})
    with pytest.raises(ValueError,match='identity'):
        updates.command({'host':'worker','machine':'b'*64},'pass',status=status)
    with pytest.raises(RuntimeError,match='command API unavailable'):
        updates.command({'host':'worker'},'pass',status={**status,'capabilities':[]})


def test_offline_update_never_falls_back_to_local_process_or_ssh(monkeypatch):
    import json,pytest
    updates=LauncherUpdates.__new__(LauncherUpdates);client=Mock();client.call.side_effect=OSError('Offline')
    monkeypatch.setattr('agent_client.CONFIG',Mock(read_text=lambda:json.dumps({'peers':{'worker':'10.42.0.2'}})))
    monkeypatch.setattr('agent_client.AgentClient',Mock(return_value=client))
    execute=Mock(side_effect=AssertionError('Offline update attempted a subprocess'))
    monkeypatch.setattr('agent_updates.subprocess.run',execute)
    with pytest.raises(OSError,match='Offline'):updates.command({'host':'worker','local':True,'agent_endpoint':'http://worker-swarm.local:8790'},'pass')
    execute.assert_not_called();client.close.assert_called_once()


