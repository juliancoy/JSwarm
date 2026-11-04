#!/usr/bin/env python3
"""Manage Swarm Control containers using Docker directly."""
import argparse
import json
import os
from pathlib import Path
import subprocess

SERVER = Path(__file__).resolve().parent
WHISPER_IMAGE = 'ghcr.io/ggml-org/whisper.cpp@sha256:3f86f6bcaa1d83f5c73a578ff263888570eb82688474bf587dcc51e6f3672a1c'
NAMES = {name: 'swarm-control-' + name for name in ('traffic', 'view', 'whisper')}


def run_command(service):
    command = ['docker', 'run', '--detach', '--pull=never', '--name', NAMES[service],
               '--restart', 'unless-stopped', '--read-only', '--cap-drop', 'ALL']
    def mount(source, target):
        command.extend(['--mount', f'type=bind,src={source},dst={target},readonly'])
    if service == 'whisper':
        command += ['--user', '65534:65534', '--workdir', '/tmp',
                    '--security-opt', 'no-new-privileges:true', '--cpus', '4',
                    '--memory', '2g', '--memory-swap', '2g', '--pids-limit', '128',
                    '--publish', '127.0.0.1:8094:8080', '--tmpfs', '/tmp:size=128m,mode=1777',
                    '--log-driver', 'none', '--health-cmd', 'curl --fail --silent http://127.0.0.1:8080/',
                    '--health-interval', '30s', '--health-timeout', '5s', '--health-retries', '3',
                    '--health-start-period', '30s', '--entrypoint', '/app/build/bin/whisper-server',
                    WHISPER_IMAGE, '--host', '0.0.0.0', '--port', '8080', '--model',
                    '/app/models/ggml-base.en.bin', '--threads', '4', '--no-gpu', '--convert',
                    '--suppress-nst', '--public', '/app/examples/server/public']
        return command
    command += ['--network', 'host', '--log-driver', 'json-file',
                '--log-opt', 'max-size=5m', '--log-opt', 'max-file=3']
    if service == 'traffic':
        state = os.environ.get('SWARM_STATE_DIR')
        if not state:
            raise ValueError('Set SWARM_STATE_DIR to the existing collector state directory')
        command += ['--user', '0:1000', '--cap-add', 'NET_RAW']
        mount(SERVER / 'packet_monitor.py', '/app/packet_monitor.py')
        command += ['--mount', f'type=bind,src={Path(state).resolve()},dst=/state',
                    'python:3.12-slim', 'python', '-u', '/app/packet_monitor.py']
    else:
        workspace = Path(os.environ.get('SWARM_WORKSPACE', str(SERVER.parents[2])))
        genome = workspace / 'GenomeHarbor/web'
        if not genome.is_dir():
            raise ValueError('Set SWARM_WORKSPACE to the workspace containing GenomeHarbor/web')
        command += ['--user', '0:0', '--cap-add', 'NET_BIND_SERVICE', '--tmpfs', '/tmp',
                    '--health-cmd', 'wget -q -O /dev/null http://10.42.0.1/api/health',
                    '--health-interval', '15s', '--health-timeout', '3s', '--health-retries', '3']
        mount(SERVER / 'view', '/usr/share/nginx/html')
        mount(genome.resolve(), '/usr/share/nginx/html/genome')
        mount(SERVER / 'nginx.conf', '/etc/nginx/nginx.conf')
        command += ['--entrypoint', 'nginx', 'nginx:alpine', '-g', 'daemon off;']
    return command


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('up', 'down', 'status'))
    parser.add_argument('services', nargs='*', choices=tuple(NAMES))
    parser.add_argument('--recreate', action='store_true')
    parser.add_argument('--dry-run', action='store_true', help='Print Docker argument arrays without executing them')
    args = parser.parse_args(argv)
    services = args.services or ['traffic', 'view']
    # Validate all configurations before changing any containers.
    commands = {service: run_command(service) for service in services} if args.action == 'up' else {}
    for service in services:
        name = NAMES[service]
        if args.dry_run:
            print(json.dumps(commands[service] if args.action == 'up' else (['docker', 'rm', '--force', name] if args.action == 'down' else ['docker', 'ps', '--all', '--filter', 'name=^/' + name + '$'])))
            continue
        if args.action == 'status':
            subprocess.run(['docker', 'ps', '--all', '--filter', 'name=^/' + name + '$'], check=True)
        elif args.action == 'down':
            existing = subprocess.run(['docker', 'container', 'inspect', name], capture_output=True)
            if existing.returncode == 0:
                subprocess.run(['docker', 'rm', '--force', name], check=True)
        else:
            existing = subprocess.run(['docker', 'inspect', '--format', '{{.State.Running}}', name], capture_output=True, text=True)
            if existing.returncode == 0:
                if existing.stdout.strip() == 'true' and not args.recreate:
                    continue
                subprocess.run(['docker', 'rm', '--force', name], check=True)
            subprocess.run(commands[service], check=True)


if __name__ == '__main__':
    main()
