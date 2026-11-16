"""Pinned Git sources for node deployments; no developer checkout is modified."""
import json
import os
from pathlib import Path
import re
import subprocess


def git(folder, *args):
    return subprocess.check_output(['git', '-C', str(folder), *args], text=True,
                                   env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
                                   stderr=subprocess.PIPE, timeout=90).strip()


def snapshot(workspace):
    """Record the exact committed repositories exported by the release gate."""
    workspace=Path(workspace)
    result={}
    for name in ('Organization', 'JSwarm', 'HarborGenetics'):
        folder=workspace if name=='Organization' else workspace/name
        result[name]={'origin':git(folder,'remote','get-url','origin'),
                      'commit':git(folder,'rev-parse','HEAD')}
    validate(result)
    return result


def validate(repositories):
    if set(repositories) not in ({'Organization','JSwarm','HarborGenetics'}, {'Organization','JSwarm','GenomeHarbor'}):
        raise ValueError('Incomplete Git source manifest')
    for spec in repositories.values():
        if set(spec)!={'origin','commit'} or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}',spec['commit']):
            raise ValueError('Invalid pinned Git commit')
        origin=spec['origin']
        if not isinstance(origin,str) or not origin or origin.startswith('-') or any(c in origin for c in '\r\n\0'):
            raise ValueError('Invalid Git origin')


def sync(repositories, base, *, checkout_root=None, offline=False, portable=False):
    """Fetch exact commits into dedicated detached worktrees, preserving old ones.

    The configuration directory is already mounted in node containers. Neither
    a branch nor an operator's working tree is reset, stashed or cleaned.
    """
    validate(repositories)
    base=Path(base);base.mkdir(parents=True,exist_ok=True,mode=0o700)
    commits={}
    # Historical publications keep their original key and directory for rollback.
    genome_name = 'HarborGenetics' if 'HarborGenetics' in repositories else 'GenomeHarbor'
    for name in ('Organization', 'JSwarm', genome_name):
        spec=repositories[name]
        cache=base/(name+'.git')
        if not cache.exists():
            subprocess.run(['git','init','--bare',str(cache)],check=True,capture_output=True,timeout=30)
        git(cache,'config','remote.origin.url',spec['origin'])
        # No submodules, hooks, or arbitrary branch names from the manifest.
        try:cached=git(cache,'rev-parse','--verify',spec['commit']+'^{commit}')
        except subprocess.CalledProcessError:cached=None
        if cached!=spec['commit']:
            if offline:
                raise RuntimeError('Pinned source has not been delivered by the controller: '+name)
            git(cache,'-c','core.hooksPath=/dev/null','fetch','--no-tags','--',spec['origin'],spec['commit'])
        commit=git(cache,'rev-parse',spec['commit']+'^{commit}')
        if commit!=spec['commit']:raise ValueError('Fetched Git commit differs from publication')
        checkout=(Path(checkout_root) if name=='Organization' else Path(checkout_root)/name) if checkout_root is not None else base/name/commit
        checkout.parent.mkdir(parents=True,exist_ok=True)
        if not checkout.exists():
            if portable:
                # Linked worktrees record host-absolute gitdir paths. A shared
                # clone with a relative alternate works at either bind path.
                # Pin borrowed objects so Git GC cannot break live checkouts.
                git(cache,'update-ref','refs/jswarm-source/'+commit,commit)
                import tempfile
                with tempfile.TemporaryDirectory(prefix='.checkout-',dir=base) as temporary:
                    staging=Path(temporary)/'tree'
                    git(base,'-c','core.hooksPath=/dev/null','clone','--shared','--no-checkout','--',str(cache),str(staging))
                    git(staging,'config','remote.origin.url',spec['origin'])
                    git(staging,'-c','core.hooksPath=/dev/null','checkout','--detach',commit)
                    (staging/'.git/objects/info/alternates').write_text(
                        os.path.relpath(cache/'objects',checkout/'.git/objects')+'\n')
                    staging.rename(checkout)
            else:
                git(cache,'-c','core.hooksPath=/dev/null','worktree','add','--detach',str(checkout),commit)
        if git(checkout,'rev-parse','HEAD')!=commit or git(checkout,'status','--porcelain','--untracked-files=all'):
            raise RuntimeError('Managed Git source changed; refusing deployment')
        commits[name]=str(checkout)
    return commits


def workspace(repositories, base, *, offline=False):
    """Assemble the sibling layout used by the existing runtime release builder."""
    import hashlib
    identity=hashlib.sha256(json.dumps(repositories,sort_keys=True).encode()).hexdigest()
    folder=Path(base)/('mounted-workspaces' if offline else 'workspaces')/identity
    # Real adjacent worktrees preserve cross-repository relative symlinks.
    # Directory aliases resolve through the physical per-repository checkout
    # and silently leave those links pointing outside the assembled workspace.
    sync(repositories,base,checkout_root=folder,offline=offline,portable=offline)
    return folder


def import_bundle(base, name, commit, path, digest):
    """Import controller-delivered Git objects without contacting an origin."""
    import hashlib
    if name not in ('Organization','JSwarm','HarborGenetics','GenomeHarbor'):
        raise ValueError('Unknown source repository')
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}',commit):
        raise ValueError('Invalid source commit')
    path=Path(path)
    checksum=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):checksum.update(block)
    if checksum.hexdigest()!=digest:
        raise ValueError('Source bundle checksum mismatch')
    cache=Path(base)/(name+'.git')
    if not cache.exists():
        subprocess.run(['git','init','--bare',str(cache)],check=True,capture_output=True,timeout=30)
    git(cache,'bundle','verify',str(path))
    git(cache,'-c','core.hooksPath=/dev/null','fetch','--no-tags','--',str(path),commit+':refs/jswarm-source/'+commit)
    if git(cache,'rev-parse',commit+'^{commit}')!=commit:
        raise ValueError('Source bundle commit mismatch')


def stage_source(repositories, config, revision):
    """Select a mounted checkout; no application code is copied or generated."""
    import hashlib
    config=Path(config)
    root=workspace(repositories,config.parent/'node-source',offline=True)
    names=('launcher_peers.py','launcher_discovery.py','launcher_registration.py','launcher_agent.py','launcher_registry.py')
    data=b''.join((root/'JSwarm/scripts'/name).read_bytes() for name in names)
    identity=hashlib.sha256(data+json.dumps(repositories,sort_keys=True).encode()).hexdigest()
    if identity!=revision:
        raise ValueError('Mounted source differs from publication')
    releases=config.parent/'agent-releases';releases.mkdir(exist_ok=True,mode=0o700)
    record=releases/revision;record.mkdir(exist_ok=True,mode=0o700)
    descriptor=json.dumps({'mode':'mounted-v1','repositories':repositories,'revision':revision},sort_keys=True)
    source=record/'source.json'
    if source.exists() and source.read_text()!=descriptor:
        raise ValueError('Immutable source descriptor changed')
    pending=record/'source.tmp';pending.write_text(descriptor);pending.chmod(0o600);pending.replace(source)
    current=releases/'current'
    if current.is_symlink() and current.resolve()==record.resolve():return root
    if current.is_symlink():
        pending=releases/'previous.tmp';pending.unlink(missing_ok=True)
        pending.symlink_to(os.readlink(current));pending.replace(releases/'previous')
    pending=releases/'current.tmp';pending.unlink(missing_ok=True)
    pending.symlink_to(revision);pending.replace(current)
    return root
