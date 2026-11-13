import base64
import json
import subprocess
import sys
import time
from pathlib import Path
import pytest
from scripts.launcher_commands import CommandService,WINDOW


def spawn(argv):return subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)


def test_update_waits_for_exited_command_output_to_be_collected():
    service=CommandService(spawn=spawn);ident='d'*32
    try:
        service.dispatch('command.start',{'id':ident,'argv':[sys.executable,'-c','print("result")']})
        service.rows[ident].process.wait(timeout=5)
        assert service.active()==1
        deadline=time.monotonic()+5
        while True:
            row=service.dispatch('command.poll',{'id':ident,'stdout':0,'stderr':0})
            if row['stdout_eof']:break
            assert time.monotonic()<deadline
            time.sleep(.001)
        assert base64.b64decode(row['stdout'])==b'result\n'
        service.dispatch('command.close',{'id':ident})
        assert service.active()==0
    finally:service.close()


def test_drain_preserves_existing_command_identity_and_output():
    from unittest.mock import patch
    service=CommandService(spawn=spawn);ident='e'*32
    args={'id':ident,'argv':[sys.executable,'-c','print("kept")']}
    try:
        service.dispatch('command.start',args)
        with patch('scripts.launcher_commands.Path.exists',return_value=True):
            assert service.dispatch('command.start',args)['started']
            with pytest.raises(RuntimeError,match='draining'):
                service.dispatch('command.start',{**args,'id':'f'*32})
            service.rows[ident].process.wait(timeout=5)
            assert service.dispatch('command.poll',{'id':ident,'stdout':0,'stderr':0})['exit_code']==0
            assert service.dispatch('command.close',{'id':ident})['closed']
    finally:service.close()


def test_acknowledged_binary_windows_replay_without_duplicate_stdin_or_output():
    service=CommandService(spawn=spawn);ident='a'*32
    argv=[sys.executable,'-c','import sys;d=sys.stdin.buffer.read();sys.stdout.buffer.write(d);sys.stderr.buffer.write(b"done")']
    try:
        service.dispatch('command.start',{'id':ident,'argv':argv})
        service.dispatch('command.start',{'id':ident,'argv':argv})
        with pytest.raises(ValueError,match='already assigned'):service.dispatch('command.start',{'id':ident,'argv':['other']})
        data=b'\x00\xffACGT'*50000
        first={'id':ident,'stdout':0,'stderr':0,'offset':0,'data':base64.b64encode(data).decode(),'eof':True}
        # Simulate losing an input acknowledgement; replay the exact input window.
        service.dispatch('command.poll',first)
        service.dispatch('command.poll',first)
        stdout=stderr=b'';deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            args={'id':ident,'stdout':len(stdout),'stderr':len(stderr),'offset':len(data),'eof':True}
            r=service.dispatch('command.poll',args)
            assert len(base64.b64decode(r['stdout']))<=WINDOW
            # Losing output must not advance the server's acknowledgement cursor.
            replay=service.dispatch('command.poll',args)
            assert base64.b64decode(replay['stdout']).startswith(base64.b64decode(r['stdout']))
            stdout+=base64.b64decode(replay['stdout']);stderr+=base64.b64decode(replay['stderr'])
            if replay['exit_code'] is not None and replay['stdout_eof'] and replay['stderr_eof']:break
            time.sleep(.001)
        assert stdout==data and stderr==b'done' and replay['exit_code']==0
        with pytest.raises(ValueError,match='acknowledgement'):
            service.dispatch('command.poll',{'id':ident,'stdout':len(stdout)+1,'stderr':len(stderr)})
        service.dispatch('command.close',{'id':ident})
        assert service.dispatch('command.close',{'id':ident})['closed']
        assert service.dispatch('command.start',{'id':ident,'argv':argv})['closed']
        assert ident not in service.rows
    finally:service.close()


def test_normal_remote_commands_and_image_checks_do_not_require_ssh():
    from scripts.swarm_runtime import shell_command
    from docker_utils.images import ImageResolver
    host={'host':'worker.local','local':False,'machine':'a'*64,'agent_endpoint':'http://10.0.0.2:8790','ssh':['/missing/ssh']}
    argv=shell_command(host,['docker','ps'])
    assert argv[0]==sys.executable and 'swarm_command.py' in argv[1]
    assert '/missing/ssh' not in argv
    resolver=ImageResolver(ssh=lambda _:pytest.fail('Normal operation attempted SSH'))
    assert resolver.command(host,['docker','ps'])==argv
    with pytest.raises(ValueError,match='no SSH fallback'):shell_command({'local':False},['true'])


def test_command_operations_require_the_controller_signature():
    from scripts.launcher_agent import verify_controller
    with pytest.raises(ValueError,match='signature rejected'):
        verify_controller({'from':'master','op':'command.start'},{'master':'master','controller_public_key':'00'*32})


def test_real_encrypted_command_transport_with_no_ssh(tmp_path):
    import asyncio,os,threading
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    from scripts.launcher_agent import Agent
    from scripts.swarm_runtime import shell_command
    key=Ed25519PrivateKey.generate();private=key.private_bytes(serialization.Encoding.Raw,serialization.PrivateFormat.Raw,serialization.NoEncryption())
    public=key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
    signing=tmp_path/'.config/map-master/controller-signing.key';signing.parent.mkdir(parents=True);signing.write_bytes(private)
    common={'key':'ab'*32,'master':'master','peers':{'master':'10.0.0.1','worker':'10.0.0.2'},'controller_public_key':public.hex()}
    node=tmp_path/'worker.json';node.write_text(json.dumps({**common,'name':'worker'}))
    controller=tmp_path/'controller.json';controller.write_text(json.dumps({**common,'name':'master'}))
    agent=Agent.__new__(Agent);agent.config=node;agent.nonces={};agent.nonce_lock=threading.Lock()
    service=CommandService(spawn=spawn)
    agent.dispatch=lambda op,args:({'machine':'c'*64,'capabilities':['node-command-v1']} if op=='status' else service.dispatch(op,args))
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def do_POST(self):
            result=asyncio.run(agent.process(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200);self.send_header('Content-Length',str(len(result)));self.end_headers();self.wfile.write(result)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
    host={'host':'worker','local':False,'machine':'c'*64,'agent_endpoint':f'http://127.0.0.1:{server.server_port}','ssh':['/missing/ssh']}
    argv=shell_command(host,[sys.executable,'-c','import sys;sys.stdout.buffer.write(sys.stdin.buffer.read());sys.stderr.write("ok")'])
    try:
        payload=os.urandom(2*1024*1024)
        result=subprocess.run(argv,input=payload,capture_output=True,timeout=30,env={**os.environ,'HOME':str(tmp_path),'LIFETECH_PEER_CONFIG':str(controller)})
        assert result.returncode==0,result.stderr
        assert result.stdout==payload and result.stderr==b'ok'
        # High-volume pipe polling cannot consume the general control replay cache.
        assert len(agent.nonces)==1
    finally:server.shutdown();server.server_close();service.close()


def test_stream_observation_survives_full_mutation_replay_cache_but_requires_authentication(tmp_path):
    import asyncio,base64,secrets,threading,time
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from scripts.launcher_agent import Agent,VERSION
    key=Ed25519PrivateKey.generate();public=key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
    config=tmp_path/'node.json';config.write_text(json.dumps({'name':'worker','master':'master','key':'ab'*32,'peers':{'master':'address'},'controller_public_key':public.hex()}))
    agent=Agent.__new__(Agent);agent.config=config;agent.nonces={str(n):time.time()+120 for n in range(4097)};agent.nonce_lock=threading.Lock()
    agent.dispatch=lambda op,args:{'state':'running'}
    cipher=AESGCM(bytes.fromhex('ab'*32))
    def request(op,authenticated=True):
        value={'from':'master','to':'worker','time':time.time(),'nonce':secrets.token_hex(16),'op':op,'args':{}}
        signature=key.sign(json.dumps(value,sort_keys=True,separators=(',',':')).encode())
        value['controller_signature']=base64.b64encode(signature if authenticated else b'wrong').decode()
        nonce=secrets.token_bytes(12);return nonce+cipher.encrypt(nonce,json.dumps(value).encode(),VERSION.encode())
    reply=asyncio.run(agent.process(request('stream.status')))
    assert json.loads(cipher.decrypt(reply[:12],reply[12:],VERSION.encode()))['result']['state']=='running'
    assert len(agent.nonces)==4097
    with pytest.raises(ValueError,match='authentication rejected'):asyncio.run(agent.process(request('stream.status',False)))
    mutation=request('stream.submit')
    asyncio.run(agent.process(mutation))
    with pytest.raises(ValueError,match='authentication rejected'):asyncio.run(agent.process(mutation))


@pytest.mark.parametrize('persistent',[False,True])
def test_rootless_owner_runtime_does_not_request_unmapped_supplementary_groups(tmp_path,monkeypatch,persistent):
    from unittest.mock import MagicMock,patch
    import docker
    from scripts import launcher_commands as module
    home=tmp_path/'owner';config=home/'.config/lifetech-launcher/peers.json';config.parent.mkdir(parents=True);config.write_text('{}')
    (config.parent/'bulk-streams.json').write_text(json.dumps({'config_dir':str(config.parent)}))
    client=MagicMock();client.info.return_value={'SecurityOptions':['name=rootless']}
    client.images.get.return_value.id='sha256:'+'f'*64
    client.containers.get.side_effect=docker.errors.NotFound('missing')
    helper=client.containers.create.return_value;helper.labels={'org.lifetech.owner-home':str(home)};helper.attrs={'State':{'Running':False}}
    agent=MagicMock();agent.config=config;agent.client.return_value.__enter__.return_value=client
    service=CommandService(agent=agent)
    exists=Path.exists
    monkeypatch.setattr(Path,'exists',lambda p:True if str(p)=='/.dockerenv' else exists(p))
    try:
        with patch.object(module.subprocess,'Popen'):
            if persistent:service.ensure_owner_helper(serve_script=tmp_path/'immutable.py')
            else:service.owner_spawn(['python3','-c','print(1)'])
        options=client.containers.create.call_args.kwargs
        assert options['user']=='0:0' and options['group_add']==[]
        assert str(home) in options['volumes'] and options['cap_drop']==['ALL']
        assert options['volumes'][str(home)]['mode']=='rw,rslave'
        assert options['volumes']['/etc/machine-id']=={'bind':'/etc/machine-id','mode':'ro'}
        assert options['volumes']['/dev']=={'bind':'/dev','mode':'ro'}
        if persistent:
            assert options['environment']['JSWARM_OWNER_RUNTIME']=='1'
            assert client.containers.create.call_args.args[1]==['python3','-B',str(config.parent/'command-runtimes/immutable.py'),'--serve',str(config)]
        assert not options.get('privileged') and not options.get('devices')
    finally:service.close()


def test_slow_command_cleanup_does_not_block_other_command_sessions():
    import threading
    service=CommandService(spawn=spawn);first='a'*32;second='b'*32;entered=threading.Event();release=threading.Event()
    service.dispatch('command.start',{'id':first,'argv':[sys.executable,'-c','import time;time.sleep(30)']})
    row=service.rows[first];close=row.close
    def slow_close():entered.set();release.wait(5);close()
    row.close=slow_close
    thread=threading.Thread(target=lambda:service.dispatch('command.close',{'id':first}));thread.start()
    try:
        assert entered.wait(1)
        service.dispatch('command.start',{'id':second,'argv':[sys.executable,'-c','print("independent")']})
        assert not release.is_set() and second in service.rows
    finally:release.set();thread.join();service.close()


def test_command_pipe_survives_agent_proxy_replacement():
    import os,signal,tempfile
    from types import SimpleNamespace
    from scripts import launcher_commands
    folder=Path(tempfile.mkdtemp(prefix='swcmd-',dir='/tmp'));config=folder/'peers.json';config.write_text('{}')
    proxy=None;pid=None;ident='a'*32
    try:
        source=Path(launcher_commands.__file__).read_text()
        proxy=launcher_commands.CommandProxy(SimpleNamespace(config=config),source)
        pid=proxy._call('runtime.status',{})['pid']
        argv=[sys.executable,'-u','-c','import sys,time; print("before",flush=True); time.sleep(.3); print("after",flush=True)']
        proxy.dispatch('command.start',{'id':ident,'argv':argv})
        proxy.close()
        replacement=launcher_commands.CommandProxy(SimpleNamespace(config=config),source)
        assert replacement._call('runtime.status',{})['pid']==pid
        assert replacement.active()==1
        deadline=time.monotonic()+10;offsets={'stdout':0,'stderr':0};out=b''
        while time.monotonic()<deadline:
            row=replacement.dispatch('command.poll',{'id':ident,**offsets,'offset':0,'eof':True})
            for stream in offsets:
                chunk=base64.b64decode(row[stream]);offsets[stream]+=len(chunk)
                if stream=='stdout':out+=chunk
            if row['exit_code'] is not None and row['stdout_eof'] and row['stderr_eof']:break
            time.sleep(.01)
        assert out==b'before\nafter\n'
        assert row['exit_code']==0
        replacement.dispatch('command.close',{'id':ident})
        assert replacement.active()==0
    finally:
        if pid:os.kill(pid,signal.SIGTERM)
        import shutil
        shutil.rmtree(folder)


def test_new_command_release_routes_retained_commands_to_their_original_owner(tmp_path):
    import os,signal,tempfile,shutil
    from types import SimpleNamespace
    from scripts import launcher_commands
    folder=Path(tempfile.mkdtemp(prefix='swcmd-',dir='/tmp'));config=folder/'peers.json';config.write_text('{}')
    pids=set();ident='b'*32
    try:
        source=Path(launcher_commands.__file__).read_text()
        original=launcher_commands.CommandProxy(SimpleNamespace(config=config),source)
        first=original._call('runtime.status',{})['pid'];pids.add(first)
        original.dispatch('command.start',{'id':ident,'argv':[sys.executable,'-c','print("retained")']})
        original._call('runtime.status',{})
        revised=source+'\n# next verified release\n'
        replacement=launcher_commands.CommandProxy(SimpleNamespace(config=config),revised)
        second=replacement._call('runtime.status',{})['pid'];pids.add(second)
        assert second!=first
        assert replacement.active()==1
        assert replacement.dispatch('command.start',{'id':ident,'argv':[sys.executable,'-c','print("retained")']})['started']
        offsets={'stdout':0,'stderr':0};output=b'';deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            row=replacement.dispatch('command.poll',{'id':ident,**offsets})
            for name in offsets:
                data=base64.b64decode(row[name]);offsets[name]+=len(data)
                if name=='stdout':output+=data
            if row['exit_code'] is not None and row['stdout_eof'] and row['stderr_eof']:break
            time.sleep(.01)
        assert output==b'retained\n'
        replacement.dispatch('command.close',{'id':ident})
        status=replacement._call('runtime.status',{});pids.add(status['pid'])
        assert status['pid']!=first
        assert status['source_sha256']==__import__('hashlib').sha256(revised.encode()).hexdigest()
    finally:
        for pid in pids:
            try:os.kill(pid,signal.SIGTERM)
            except ProcessLookupError:pass
        shutil.rmtree(folder)


def test_proxy_recovers_legacy_session_without_restarting_or_replaying_input(tmp_path):
    from types import SimpleNamespace
    from scripts.launcher_commands import CommandProxy
    from unittest.mock import Mock
    proxy=CommandProxy.__new__(CommandProxy)
    proxy.config=tmp_path/'peers.json';proxy.socket=tmp_path/'command-runtime-new.sock'
    legacy=tmp_path/'command-runtime.sock';legacy.touch()
    ident='c'*32;calls=[]
    def call(op,args,path=None):
        calls.append((op,args,path))
        if path==proxy.socket:
            if op=='runtime.lookup':return {'known':False}
            raise FileNotFoundError('not here')
        if op=='runtime.lookup':raise ValueError('Unknown command runtime operation')
        if op=='command.poll':raise ValueError('Invalid output acknowledgement')
        if op=='command.start':return {'id':ident,'started':True}
        raise AssertionError(op)
    proxy._call=call
    assert proxy.dispatch('command.start',{'id':ident,'argv':['python3','retained.py']})['started']
    assert calls[-1][2]==legacy
    poll=next(args for op,args,path in calls if op=='command.poll')
    assert 'data' not in poll and 'eof' not in poll


def test_proxy_remembers_owner_and_recovers_when_owner_disappears(tmp_path):
    from scripts.launcher_commands import CommandProxy
    proxy=CommandProxy.__new__(CommandProxy);proxy.socket=tmp_path/'current.sock'
    old=tmp_path/'legacy.sock';ident='f'*32;calls=[];owner=old
    proxy.sockets=lambda:[proxy.socket,old]
    def call(op,args,path=None):
        calls.append(path)
        if path!=owner:raise FileNotFoundError('unknown command')
        return {'stdout':'','stderr':'','exit_code':None}
    proxy._call=call
    args={'id':ident,'stdout':0,'stderr':0}
    proxy.dispatch('command.poll',args)
    assert calls==[proxy.socket,old]
    calls.clear();proxy.dispatch('command.poll',args)
    assert calls==[old]
    owner=proxy.socket;calls.clear();proxy.dispatch('command.poll',args)
    assert calls==[old,proxy.socket]


def test_owner_exec_cancellation_kills_remote_process_group(tmp_path):
    import os
    import signal
    from scripts.launcher_commands import OWNER_EXEC,OWNER_CANCEL
    token=os.urandom(16).hex();started=tmp_path/'started'
    command=[sys.executable,'-c','import pathlib,time,sys;pathlib.Path(sys.argv[1]).write_text(str(__import__("os").getpid()));time.sleep(300)',str(started)]
    wrapper=subprocess.Popen([sys.executable,'-c',OWNER_EXEC,token,json.dumps(command)])
    try:
        deadline=time.monotonic()+10
        while not started.exists():
            assert time.monotonic()<deadline
            time.sleep(.01)
        child=int(started.read_text())
        subprocess.run([sys.executable,'-c',OWNER_CANCEL,token],check=True)
        assert wrapper.wait(timeout=5)!=0
        with pytest.raises(ProcessLookupError):os.kill(child,0)
    finally:
        if wrapper.poll() is None:wrapper.kill();wrapper.wait()


def test_command_close_invokes_remote_cleanup_before_local_cli_exit():
    from scripts.launcher_commands import Command
    events=[]
    def remote_cleanup():events.append('remote')
    command=Command([sys.executable,'-c','import time;time.sleep(300)'],spawn)
    command.process.remote_close=remote_cleanup
    command.close()
    assert events==['remote']
    assert command.process.poll() is not None


def test_persistent_owner_service_runs_native_process_with_real_exit_and_binary_output(monkeypatch):
    from scripts.launcher_commands import CommandService
    monkeypatch.setenv('JSWARM_OWNER_RUNTIME','1')
    service=CommandService();ident='9'*32
    try:
        service.dispatch('command.start',{'id':ident,'argv':[sys.executable,'-c','import sys;sys.stdout.buffer.write(b"abc\\x00xyz");sys.exit(7)']})
        deadline=time.monotonic()+5;offsets={'stdout':0,'stderr':0};output=b''
        while time.monotonic()<deadline:
            value=service.dispatch('command.poll',{'id':ident,**offsets,'eof':True})
            for name in offsets:
                data=base64.b64decode(value[name]);offsets[name]+=len(data)
                if name=='stdout':output+=data
            if value['exit_code'] is not None and value['stdout_eof'] and value['stderr_eof']:break
            time.sleep(.01)
        assert output==b'abc\x00xyz' and value['exit_code']==7
    finally:service.close()


def test_command_stdin_reader_cancels_with_open_input_and_full_queue():
    import os,queue,threading
    from scripts.swarm_command import input_reader
    read,write=os.pipe();stopped=threading.Event();inputs=queue.Queue(maxsize=1)
    thread=threading.Thread(target=input_reader,args=(read,inputs,stopped))
    try:
        thread.start();os.write(write,b'first')
        assert inputs.get(timeout=2)==b'first'
        inputs.put(b'occupied');os.write(write,b'next')
        stopped.set();thread.join(timeout=2)
        assert not thread.is_alive()
    finally:
        stopped.set();os.close(write);thread.join(timeout=2);os.close(read)
