"""Reuse successful release checks only for byte-identical payloads and inputs."""
import hashlib
import importlib.metadata
import json
import os
import shutil
import datetime
from pathlib import Path
import subprocess
import sys

PRIVATE = ['tests/test_filesystem_deployment.py','tests/test_swarm_filesystem.py',
           'tests/test_swarm_host_service.py','tests/test_node_releases.py']
PUBLIC = ['tests/test_swarm_paged_cache.py','tests/test_swarm_shared_cache.py',
          'tests/test_swarm_mdns_discovery.py','tests/test_node_releases.py']
WORKFLOW = ['tests/test_deploy_publish.py','tests/test_release_secrets.py',
            'tests/test_release_validation.py','tests/test_deployment_lock.py']
WORKFLOW_SOURCE = {'scripts/deploy_swarm_inputs.py','scripts/release_validation.py',
                   'scripts/release_secrets.py','scripts/release-secrets-allowlist.json'}


def environment():
    value = {'python':sys.version,'packages':sorted((d.metadata['Name'],d.version)
             for d in importlib.metadata.distributions() if d.metadata['Name']),
             'docker_host':os.getenv('DOCKER_HOST','unix:///var/run/docker.sock'),
             'docker_context':os.getenv('DOCKER_CONTEXT')}
    for label,path in [('docker_binary',shutil.which('docker')),
                       ('docker_socket','/var/run/docker.sock'),
                       ('docker_config',str(Path.home()/'.docker/config.json'))]:
        if path and Path(path).exists():
            st=Path(path).stat();value[label]=[str(Path(path).resolve()),st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns]
    config=Path.home()/'.docker/config.json'
    if config.is_file():value['docker_config_sha256']=hashlib.sha256(config.read_bytes()).hexdigest()
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()


def evidence_path(artifact):
    artifact=Path(artifact)
    return artifact.parent/'validation'/(artifact.name+'.json')


def record(artifact, suites, *, reused=None, command=None, evidence=None):
    artifact=Path(artifact).resolve()
    value={'recorded_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'artifact_sha256':hashlib.sha256(artifact.read_bytes()).hexdigest(),
           'environment':environment(),'passed_suites':suites,'reused_from':reused,
           'command':command,'evidence':evidence}
    path=evidence_path(artifact);path.parent.mkdir(exist_ok=True,mode=0o700)
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(value,sort_keys=True)+'\n')
    temporary.chmod(0o600);temporary.replace(path)


def changed_paths(root, before, after):
    return subprocess.check_output(['git','-C',str(root),'diff','--name-only',before,after],
                                   text=True,timeout=30).splitlines()


def select(current, previous, full, changed):
    """Pure selection; callers first verify immutable successful evidence."""
    full=list(dict.fromkeys([*full,'tests/test_release_validation.py']))
    if previous is None:return full
    if (current.files!=previous.files or current.supervisor_source!=previous.supervisor_source
            or current.git_source['installer']!=previous.git_source['installer']):return full
    if any(current.git_source['repositories'][name]!=previous.git_source['repositories'][name]
           for name in ('Organization','GenomeHarbor')):return full
    from deployment_components import FILES, FILESYSTEM_FILES
    known=set(FILES)|set(FILESYSTEM_FILES)|WORKFLOW_SOURCE
    selected=[]
    for path in changed:
        if path.endswith('.md') and Path(path).name!='AGENTS.md':continue
        if path.startswith('tests/') and path.endswith('-test-runs.jsonl'):continue
        if Path(path).name.startswith('test_') and path.endswith('.py'):
            selected.append(path);continue
        if path in WORKFLOW_SOURCE:selected.extend(WORKFLOW);continue
        if path not in known:return full
    if set(current.components)!=set(previous.components):return full
    for name,component in current.components.items():
        if component['sha256']==previous.components[name]['sha256']:continue
        if name=='private-filesystem':selected.extend(PRIVATE)
        elif name=='public-pager':selected.extend(PUBLIC)
        else:return full
    # Packaging/provenance remains checked for every new publication.
    selected.append('tests/test_node_releases.py')
    return list(dict.fromkeys(selected))


def choose(current, root, releases, full):
    from agent_updates import LauncherUpdates
    pointer=Path(releases)/'desired'
    if not pointer.exists():return select(current,None,full,[]),None
    artifact=pointer.resolve()
    try:
        proof=json.loads(evidence_path(artifact).read_text())
        if (proof['environment']!=environment() or proof['artifact_sha256']!=hashlib.sha256(artifact.read_bytes()).hexdigest()
                or not set(full)<=set(proof['passed_suites'])):raise ValueError('Validation inputs differ')
        previous=LauncherUpdates.published(artifact,lambda:False,lambda *args:None)
        paths=changed_paths(root,previous.git_source['repositories']['JSwarm']['commit'],
                            current.git_source['repositories']['JSwarm']['commit'])
        return select(current,previous,full,paths),str(artifact)
    except (OSError,ValueError,KeyError,subprocess.SubprocessError):
        return select(current,None,full,[]),None
