#!/usr/bin/env python3
"""Start the shared local portal stack from the Organization directory."""
import argparse
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import signal
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent


def discover_test_sources():
    """Include the working repository and sibling checkouts in a workspace."""
    cwd = Path.cwd().resolve()
    result = subprocess.run(['git', '-C', str(cwd), 'rev-parse', '--show-toplevel'],
                            capture_output=True, text=True)
    root = Path(result.stdout.strip()) if result.returncode == 0 else cwd
    roots = {root}
    for child in root.iterdir():
        if child.is_dir() and not child.is_symlink() and (child / '.git').exists():
            roots.add(child)
    return sorted(roots)


def run_logged_test(command, sources=()):
    """Run a host test once per input fingerprint, keeping evidence by its source."""
    import datetime
    import fcntl
    import hashlib
    import platform
    import time
    import uuid

    automatic = not sources
    paths = sorted({Path(source).resolve(strict=True) for source in
                    (sources or discover_test_sources())})
    if not paths or not command:
        raise ValueError('A test command and its source inputs are required')
    # A directory owns its log; a file keeps its log immediately beside it.
    logs = [path / '.test-log.jsonl' if path.is_dir()
            else path.with_name(path.name + '.test-log.jsonl') for path in paths]
    excluded = {'.git', '__pycache__', '.pytest_cache', 'node_modules', '.venv', '.test-output'}

    def fingerprint():
        digest = hashlib.sha256()
        def add(value):
            data = value if isinstance(value, bytes) else str(value).encode()
            digest.update(len(data).to_bytes(8, 'big'))
            digest.update(data)
        add(Path.cwd().resolve())
        add(json.dumps(command))
        add(sys.version)
        add(sys.executable)
        add(platform.platform())
        add(json.dumps(dict(os.environ), sort_keys=True))
        add(Path(__file__).read_bytes())
        for path in paths:
            add(path)
            if automatic and (path / '.git').exists():
                listing = subprocess.run(
                    ['git', '-C', str(path), 'ls-files', '--cached', '--others',
                     '--exclude-standard', '-z'], capture_output=True, check=True)
                files = sorted({path / os.fsdecode(name) for name in listing.stdout.split(b'\0')
                                if name and not excluded.intersection(Path(os.fsdecode(name)).parts)
                                and not os.fsdecode(name).endswith(('.test-log.jsonl', '.test-log.jsonl.lock'))})
            else:
                files = [path] if path.is_file() else sorted(
                    item for item in path.rglob('*')
                    if item.is_file() and not excluded.intersection(item.relative_to(path).parts)
                    and not item.name.endswith(('.test-log.jsonl', '.test-log.jsonl.lock')))
            for item in files:
                add(item)
                if item.is_symlink():
                    add(os.readlink(item))
                if not item.is_file():
                    add('missing-or-directory')
                    continue
                add(item.stat().st_mode)
                add(item.read_bytes())
        return digest.hexdigest()

    # Serialize matching source sets, including concurrent launcher invocations.
    with logs[0].with_name(logs[0].name + '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = fingerprint()
        if logs[0].exists():
            for line in reversed(logs[0].read_text().splitlines()):
                try:
                    previous = json.loads(line)
                except ValueError:
                    continue
                if previous.get('fingerprint') == before and previous.get('inputs_stable'):
                    print(f'Reusing test result ({previous["returncode"]}): {logs[0]}', flush=True)
                    return previous['returncode']
        output_dir = logs[0].parent / '.test-output'
        output_dir.mkdir(exist_ok=True)
        output = output_dir / (uuid.uuid4().hex + '.log')
        started = time.monotonic()
        record = {'date': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  'command': command, 'cwd': str(Path.cwd()), 'sources': list(map(str, paths)),
                  'fingerprint': before, 'output': str(output), 'automatic_sources': automatic}
        code = 1
        completed = False
        try:
            with output.open('w') as stream:
                try:
                    code = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT).returncode
                except OSError as error:
                    stream.write(str(error) + '\n')
                    code = 127
                completed = True
        finally:
            record.update(returncode=code, duration_seconds=time.monotonic() - started,
                          inputs_stable=completed and fingerprint() == before)
            for log in logs:
                with log.open('a') as stream:
                    stream.write(json.dumps(record, sort_keys=True) + '\n')
            print(f'Test result ({code}): {output}', flush=True)
        return code


def portal_ready(port):
    """Inspect the actual local portal, rather than the Docker process alone."""
    import ssl
    from urllib.request import urlopen
    from urllib.error import URLError
    try:
        with urlopen(f'https://localhost:{port}/availability',context=ssl._create_unverified_context(),timeout=5) as response:
            return response.status==200
    except (OSError,URLError):return False


def launch_container(args):
    """Keep the local launcher visible alongside the services it starts."""
    from tempfile import TemporaryDirectory
    import shutil
    name = 'swarm-node'
    for legacy in ('lifetech-launcher','lifetech-launcher-session'):
        if subprocess.run(['docker','inspect',name],capture_output=True).returncode==0:break
        if subprocess.run(['docker','inspect',legacy],capture_output=True).returncode==0:
            subprocess.run(['docker','rename',legacy,name],check=True);break
    image = 'organization-swarm-node:1'
    existing = subprocess.run(['docker', 'inspect', '--format', '{{.State.Running}}', name], capture_output=True, text=True)
    if existing.returncode == 0 and existing.stdout.strip() == 'true' and not args.recreate_launcher:
        if portal_ready(args.port):
            print(f'Local portal is ready: https://localhost:{args.port}/')
        else:
            print(f'{name} is running, but the portal at https://localhost:{args.port}/ is not ready.\n'
                  f'Check startup progress: docker logs --tail 40 {name}\n'
                  'Check frontend startup: docker logs --tail 40 lifetech-community-web')
            return 1
        if args.login:
            container_login(args, name)
        return 0
    from scripts.swarm_node import build_node_image
    build_node_image(image)
    if existing.returncode == 0:
        subprocess.run(['docker', 'rm', '--force', name], check=True)
    # The full launcher takes over the peer port from a test-initialized session.
    subprocess.run(['docker','rm','--force','lifetech-launcher-session'],capture_output=True)
    socket = Path('/var/run/docker.sock')
    docker_cli = Path(shutil.which('docker')).resolve()
    command = ['docker', 'run', '--pull=never', '--detach', '--init', '--name', name, '--restart', 'unless-stopped',
               '--log-opt', 'max-size=5m', '--log-opt', 'max-file=3',
               '--network', 'host', '--user', f'{os.getuid()}:{os.getgid()}', '--group-add', str(socket.stat().st_gid),
               '--label', 'organization.container-class=swarm-node', '--label', 'organization.color=biotech-gold',
               '--mount', f'type=bind,src={ROOT},dst={ROOT}',
               '--mount', f'type=bind,src={socket},dst=/var/run/docker.sock', '--workdir', str(ROOT),
               '--mount', f'type=bind,src={docker_cli},dst=/usr/local/bin/docker,readonly',
               '--env', 'HOME=/tmp', '--env', 'PYTHONUNBUFFERED=1', '--env', 'ORGANIZATION_LAUNCHER_CONTAINER=1',
               '--env', 'ORGPORTAL_JOURNAL_AUTO_SYNC=false']
    if 'JSWARM_WORKER_STORAGE' in os.environ:
        if os.environ['JSWARM_WORKER_STORAGE'] not in ('durable','ram-only'):raise ValueError('Invalid JSwarm worker storage mode')
        command+=['--env','JSWARM_WORKER_STORAGE']
    lan_inventory=Path.home()/'.local/state/orgportal/lan-ssh-devices.json'
    ssh_folder=Path.home()/'.ssh'
    if lan_inventory.exists():
        command += ['--mount',f'type=bind,src={lan_inventory},dst=/run/docker-utils-inventory.json,readonly',
                    '--env','DOCKER_UTILS_INVENTORY=/run/docker-utils-inventory.json']
    if ssh_folder.exists():
        command += ['--mount',f'type=bind,src={ssh_folder},dst=/run/lan-ssh,readonly',
                    '--env','DOCKER_UTILS_SSH_KEY=/run/lan-ssh/orgportal_lan_ed25519',
                    '--env','DOCKER_UTILS_KNOWN_HOSTS=/run/lan-ssh/known_hosts']
    peer_folder = Path.home() / '.config/lifetech-launcher'
    peer_folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    command += ['--mount', f'type=bind,src={peer_folder},dst=/run/launcher-config',
                '--env', 'LIFETECH_PEER_CONFIG=/run/launcher-config/peers.json']
    job_root=ROOT/'.runtime/launcher-jobs'
    job_root.mkdir(parents=True,exist_ok=True,mode=0o700)
    command += ['--mount','type=bind,src=/etc/machine-id,dst=/etc/machine-id,readonly',
                '--env','LIFETECH_JOB_ROOT='+str(job_root)]
    for key in os.environ:
        if key.startswith(('ORGPORTAL_', 'PIDP_', 'CHAT_')) and key != 'ORGPORTAL_JOURNAL_AUTO_SYNC':
            command += ['--env', key]
    command += [image, 'python', str(ROOT / 'run.py'), '--prefix', args.prefix, '--network', args.network, '--port', str(args.port)]
    subprocess.run(command, check=True)
    print(f'Swarm node: {name}\nConsole: docker logs -f {name}')
    if args.login:
        container_login(args, name)
    return 0


def container_login(args, name):
    import time
    print('Waiting for launcher startup before opening local sign-in…', flush=True)
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        state = json.loads(subprocess.check_output(['docker', 'inspect', '--format', '{{json .State}}', name], text=True))
        if state.get('Health', {}).get('Status') == 'healthy':
            base = os.getenv('ORGPORTAL_LOCAL_PUBLIC_BASE_URL') or f'https://localhost:{args.port}'
            sys.path.insert(0, str(ROOT / 'OrgPortal'))
            from service_names import service_name
            command = [str(ROOT / 'local-login'), '--base', base, '--container', service_name(args.prefix, 'pidp-dev')]
            if args.login == 'admin': command.append('--admin')
            if args.no_open: command.append('--no-open')
            subprocess.run(command, check=True)
            return
        if not state.get('Running') or state.get('Restarting'):
            raise RuntimeError(f'Launcher startup failed. Inspect: docker logs {name}')
        time.sleep(2)
    raise RuntimeError(f'Launcher startup timed out. Inspect: docker logs {name}')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--test-source', action='append', type=Path, default=[],
                        help='Optional explicit input scope; defaults to automatic repository/workspace discovery.')
    parser.add_argument('--test', nargs=argparse.REMAINDER,
                        help='Run a host test command with automatic change detection and adjacent logs. Must be last.')
    parser.add_argument('--stop', action='store_true', help='Stop Organization services and containers, preserving data.')
    parser.add_argument('--all', action='store_true', help='With --stop, stop all enrolled LAN nodes too.')
    parser.add_argument('--memory-pressure-benchmark',choices=['run','status'],help='Benchmark a complete alignment shard under FS-Cache RAM pressure.')
    parser.add_argument('--monitor-view',choices=['deploy','status','device-controls','authenticated-nodes'],help='Publish monitor assets without restarting nodes or workers.')
    parser.add_argument('--pipeline-view',choices=['deploy','status','device-controls','authenticated-nodes'],help='Publish the read-only shard pipeline without restarting compute workers.')
    parser.add_argument('--network-reference-cache',choices=['build','test','qualify','status','stop'],help='Test and inspect launcher-managed FS-Cache over LAN; does not change production placement.')
    parser.add_argument('--shared-reference',choices=['prepare','enable','status'],help='Manage the validated stock-BWA shared index for memory-constrained workers.')
    parser.add_argument('--node-alignment',choices=['build','qualify','deploy','status'],help='Manage qualified node-owned BWA CPU/GPU execution and shared reference mounts.')
    parser.add_argument('--vulkan-alignment',choices=['build','deploy','deploy-controller','prepare-nodes','reconcile','status'],help='Manage the independently admitted Vulkan alignment lane.')
    parser.add_argument('--alignment-build',type=Path,default=Path.home()/'.cache/lifetech/bwa-vulkan-exact')
    parser.add_argument('--stream-work',choices=['deploy','status','open'],help='Deploy launcher streaming support, inspect it, or open enrolled nodes for work.')
    parser.add_argument('--genomics-followup',choices=['build','deploy','deploy-orchestration','stop','status'],help='Manage receipt-gated bounded follow-up genomics jobs.')
    parser.add_argument('--shard-qc',choices=['build','deploy','start','stop','status'],help='Manage the streaming CPU/GPU shard feeder through this runner.')
    parser.add_argument('--wgs-root',type=Path,default=Path('/mnt/PILEDRIVE/genomics-workbench/wgs'),help='Durable genome run-manager state.')
    parser.add_argument('--recreate-node','--recreate-launcher',dest='recreate_launcher', action='store_true', help='Recreate the swarm-node container to load updated code/configuration; keep service containers and data.')
    parser.add_argument('--worker-mode',choices=['auto','always','paused'],help='Accept jobs on AC power, always, or submit only. Existing jobs finish.')
    parser.add_argument('--worker-storage',choices=['durable','ram-only'],help='Persist this node container’s payload storage policy; running workers finish.')
    parser.add_argument('--swarm',action='store_true',help='Open this launcher’s local swarm controls.')
    parser.add_argument('--peer-register', type=Path, help='Register this machine using a private internet launcher invitation file.')
    parser.add_argument('--peer-enroll', action='store_true', help='Enroll saved LAN machines and this coordinator for launcher WebRTC; does not restart services.')
    parser.add_argument('--release', choices=['runtime', 'check', 'build', 'deploy', 'verify'], help='Run a release action in the pinned Bookworm container.')
    parser.add_argument('--target', choices=['all', 'web', 'api'], default='all')
    parser.add_argument('--release-checkout', type=Path, help='Clean OrgPortal checkout to release.')
    parser.add_argument('--prefix', default='bmoremedtech-', help='Existing local volume namespace (default: bmoremedtech-).')
    parser.add_argument('--network', default='bmoremedtech', help='Docker network (default: bmoremedtech).')
    parser.add_argument('--port', type=int, default=os.getenv('ORGPORTAL_LOCAL_GATEWAY_PORT', '8443'), help='Local HTTPS port (default: 8443).')
    parser.add_argument('--login', choices=['member', 'admin'], help='Sign your browser in after startup.')
    parser.add_argument('--no-open', action='store_true', help='Print the one-use login link instead of opening a browser.')
    parser.add_argument('--vm', action='store_true', help='Run the stack and test suite in a disposable Multipass VM.')
    parser.add_argument('--vm-cpus', type=int, default=4, help='Multipass virtual CPUs (default: 4).')
    parser.add_argument('--vm-memory', default='8G', help='Multipass RAM (default: 8G).')
    parser.add_argument('--vm-disk', default='40G', help='Multipass disk (default: 40G).')
    parser.add_argument('--vm-timeout', type=int, default=5400, help='Total VM run timeout in seconds (default: 5400).')
    parser.add_argument('--vm-image-archive', type=Path, help='Optional docker-save archive to preload inside the VM.')
    args = parser.parse_args(argv)
    if args.test is not None and not args.test:
        parser.error('--test requires a command')
    if args.test_source and args.test is None:
        parser.error('--test-source requires --test')
    if args.all and not args.stop:
        parser.error('--all requires --stop')
    if args.recreate_launcher and (args.release or args.vm or args.peer_enroll or args.peer_register):
        parser.error('--recreate-launcher cannot be combined with --release, --vm or --peer-enroll')
    if args.peer_enroll and args.peer_register:
        parser.error('Choose either LAN enrollment or internet registration')
    if (args.peer_enroll or args.peer_register) and (args.release or args.vm or args.login):
        parser.error('--peer-enroll cannot be combined with --release, --vm or --login')
    if args.release_checkout and not args.release:
        parser.error('--release-checkout requires --release')
    if args.release and (args.vm or args.login):
        parser.error('--release cannot be combined with --vm or --login')
    if args.port in (8769, 8770):
        parser.error('--port conflicts with the MedTech (8769) or LifeTech (8770) preview')
    if not 1024 <= args.port <= 65535:
        parser.error('--port must be between 1024 and 65535')
    if args.no_open and not args.login:
        parser.error('--no-open requires --login')
    if args.vm_cpus < 1 or args.vm_timeout < 1:
        parser.error('--vm-cpus and --vm-timeout must be positive')
    if args.vm and (args.login or args.port != 8443 or args.prefix != 'bmoremedtech-' or args.network != 'bmoremedtech'):
        parser.error('--vm uses its own fixed test namespace and port; omit --login, --port, --prefix and --network overrides')
    if args.vm_image_archive and (not args.vm or not args.vm_image_archive.is_file()):
        parser.error('--vm-image-archive requires --vm and an existing docker-save archive')
    return args


def start_site_previews(args):
    """Build and serve both public sites using the existing local services."""
    site = ROOT / 'BmoreMedTech'
    if not (site / 'run.py').is_file():
        raise RuntimeError('Missing BmoreMedTech checkout. Run ./pull-all first.')
    from service_names import service_name
    spec = importlib.util.spec_from_file_location('local_site_run', site / 'run.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for brand, port, hostname in [('medtech', 8769, 'medtech.social'), ('lifetech', 8770, 'lifetech.fyi')]:
        options = argparse.Namespace(
            site_brand=brand, site_port=port, site_container_name=f'{brand}-website',
            node_image=module.DEFAULT_NODE_IMAGE, orgportal_dir=str(ROOT / 'OrgPortal'),
            tenant_host=hostname, medtech_only=False, system_network=args.network,
            org_api_origin=f'http://{service_name(args.prefix, "org")}:{os.getenv("ORGPORTAL_WORKER_PORT") or "8001"}',
            pidp_origin=f'http://{service_name(args.prefix, "pidp-dev")}:8000',
            portal_origin=f'http://{service_name(args.prefix, "portal-dev")}:5173',
            chat_origin=f'http://{service_name(args.prefix, "chat")}:8003',
        )
        module.build_site(options, validate=False)
        module.start_site(options)
        print(f'Local {brand.title()}: https://localhost:{port}/')


def start_codecollective_preview():
    site = ROOT / 'CodeCollective'
    spec = importlib.util.spec_from_file_location('codecollective_local_run', site / 'run.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ensure_datacenters_image()
    module.start_static_site(8877)
    print('Local USAJOBS visualizer: http://localhost:8877/usa/')


def main(argv=None):
    args = parse_args(argv)
    if args.test is not None:
        return run_logged_test(args.test, args.test_source)
    if args.stop:
        from scripts.stop_organization import stop
        return stop(ROOT, fleet=args.all)
    if args.stream_work:
        from scripts.deploy_stream_work import manage
        return manage(args.stream_work, args.wgs_root/'recall-hosts.json')
    if args.memory_pressure_benchmark:
        from scripts.deploy_memory_benchmark import manage
        return manage(args.memory_pressure_benchmark,args.wgs_root)
    if args.monitor_view:
        from scripts.deploy_monitor_view import manage
        return manage(args.monitor_view)
    if args.pipeline_view:
        from scripts.deploy_pipeline_view import manage
        return manage(args.pipeline_view)
    if args.network_reference_cache:
        from scripts.network_reference_cache import manage
        return manage(args.network_reference_cache,args.wgs_root)
    if args.shared_reference:
        from scripts.shared_bwa_reference import manage
        return manage(args.shared_reference,args.wgs_root)
    if args.node_alignment:
        from scripts.node_alignment import manage
        return manage(args.node_alignment,args.wgs_root)
    if args.vulkan_alignment:
        from scripts.vulkan_alignment_deployment import manage
        return manage(args.vulkan_alignment,args.wgs_root,args.alignment_build)
    if args.genomics_followup:
        from scripts.genomics_followup_controller import manage
        return manage(args.genomics_followup,args.wgs_root)
    if args.shard_qc:
        from scripts.shard_qc_deployment import manage
        return manage(args.shard_qc,args.wgs_root)
    if args.worker_storage:
        from scripts.launcher_streams import set_worker_storage_policy
        config=Path(os.getenv('LIFETECH_PEER_CONFIG',str(Path.home()/'.config/lifetech-launcher/peers.json')))
        set_worker_storage_policy(config,args.worker_storage)
        print('Worker storage: '+args.worker_storage+'; existing workers finish')
        return 0
    if args.worker_mode:
        folder=Path.home()/'.config/lifetech-launcher';folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        path=folder/'worker-policy.json';temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'mode':args.worker_mode}));temporary.chmod(0o600);temporary.replace(path)
        print('Worker mode: '+args.worker_mode+'; existing jobs finish, submission remains available')
        return 0
    if args.swarm:
        import webbrowser
        sys.path.insert(0,str(ROOT/'scripts'))
        from launcher_peers import owner_launcher_url
        webbrowser.open(owner_launcher_url())
        print('http://127.0.0.1:8791/launcher')
        return 0
    if args.peer_register:
        sys.path.insert(0, str(ROOT / 'scripts'))
        from launcher_registration import register
        print(json.dumps(register(args.peer_register)))
        return 0
    if args.peer_enroll:
        sys.path.insert(0, str(ROOT / 'scripts'))
        from enroll_launcher_peers import enroll
        enroll(ROOT)
        return 0
    if args.release:
        portal = ROOT / 'OrgPortal'
        sys.path.insert(0, str(portal))
        spec = importlib.util.spec_from_file_location('orgportal_release_run', portal / 'run.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.run_release(args.release, args.target, args.release_checkout)
    if args.vm:
        from vm_runner import run_vm
        return run_vm(ROOT, args)
    if not os.getenv('ORGANIZATION_LAUNCHER_CONTAINER'):
        return launch_container(args)
    Path('/tmp/launcher-ready').unlink(missing_ok=True)
    if not (ROOT / 'OrgPortal' / 'run.py').is_file() or not (ROOT / 'PIdP' / 'run.py').is_file():
        raise RuntimeError('Missing application repositories. Run ./pull-all first.')
    sys.stdout.reconfigure(line_buffering=True)
    context = subprocess.run(['docker','context','inspect','--format','{{json .Endpoints.docker.Host}}'], capture_output=True, text=True, check=True)
    if not (os.getenv('DOCKER_HOST') or json.loads(context.stdout)).startswith('unix://'):
        raise RuntimeError('Use a local Docker context for this local deployment.')
    base = (os.getenv('ORGPORTAL_LOCAL_PUBLIC_BASE_URL') or f'https://localhost:{args.port}').rstrip('/')
    host = urlsplit(base).hostname
    if host != 'localhost' and not ipaddress.ip_address(host).is_private:
        raise RuntimeError('Use a private network address for this deployment.')
    os.environ.setdefault('ORGPORTAL_LOCAL_BIND_ADDRESS', '127.0.0.1')
    os.environ['ORGPORTAL_LOCAL_GATEWAY_PORT'] = str(args.port)
    os.environ.setdefault('ORGPORTAL_LOCAL_PUBLIC_BASE_URL', base)
    portal = ROOT / 'OrgPortal'
    sys.path.insert(0, str(portal))
    spec = importlib.util.spec_from_file_location('orgportal_local_run', portal / 'run.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Keep lifecycle/configuration logic in its owning repository. The historical
    # volume namespace preserves existing accounts, local newsletters and D1 data.
    sys.path.insert(0, str(ROOT / 'scripts'))
    from launcher_peers import start
    stop_peers = start(args.prefix)
    os.environ.setdefault('ORGPORTAL_LOCAL_PREVIEW_ORIGINS', ','.join(
        f'https://{host}:{port}' for port in (8769, 8770) for host in ('localhost', '127.0.0.1')))
    module.run(args.prefix, args.network)
    start_site_previews(args)
    start_codecollective_preview()
    print(f'\nLocal portal: {base}/')
    print(f'Local administrator: {ROOT / "local-login"} --admin')
    if os.getenv('ORGANIZATION_LAUNCHER_CONTAINER'):
        Path('/tmp/launcher-ready').touch()
        print('Startup complete. Swarm node is available in the machine container view.')
        def shutdown(*_):
            stop_peers()
            sys.exit(0)
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        while True:
            signal.pause()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'Local startup failed: {error}', file=sys.stderr)
        raise SystemExit(1)
