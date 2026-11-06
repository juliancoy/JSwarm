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
    for name in ('Organization', 'JSwarm', 'GenomeHarbor'):
        folder=workspace if name=='Organization' else workspace/name
        result[name]={'origin':git(folder,'remote','get-url','origin'),
                      'commit':git(folder,'rev-parse','HEAD')}
    validate(result)
    return result


def validate(repositories):
    if set(repositories)!={'Organization','JSwarm','GenomeHarbor'}:
        raise ValueError('Incomplete Git source manifest')
    for spec in repositories.values():
        if set(spec)!={'origin','commit'} or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}',spec['commit']):
            raise ValueError('Invalid pinned Git commit')
        origin=spec['origin']
        if not isinstance(origin,str) or not origin or origin.startswith('-') or any(c in origin for c in '\r\n\0'):
            raise ValueError('Invalid Git origin')


def sync(repositories, base):
    """Fetch exact commits into dedicated detached worktrees, preserving old ones.

    The configuration directory is already mounted in node containers. Neither
    a branch nor an operator's working tree is reset, stashed or cleaned.
    """
    validate(repositories)
    base=Path(base);base.mkdir(parents=True,exist_ok=True,mode=0o700)
    commits={}
    for name,spec in repositories.items():
        cache=base/(name+'.git')
        if not cache.exists():
            subprocess.run(['git','init','--bare',str(cache)],check=True,capture_output=True,timeout=30)
        # No submodules, hooks, or arbitrary branch names from the manifest.
        git(cache,'-c','core.hooksPath=/dev/null','fetch','--no-tags','--',spec['origin'],spec['commit'])
        commit=git(cache,'rev-parse',spec['commit']+'^{commit}')
        if commit!=spec['commit']:raise ValueError('Fetched Git commit differs from publication')
        checkout=base/name/commit
        checkout.parent.mkdir(exist_ok=True)
        if not checkout.exists():
            git(cache,'-c','core.hooksPath=/dev/null','worktree','add','--detach',str(checkout),commit)
        if git(checkout,'rev-parse','HEAD')!=commit or git(checkout,'status','--porcelain','--untracked-files=all'):
            raise RuntimeError('Managed Git source changed; refusing deployment')
        commits[name]=str(checkout)
    return commits


def workspace(repositories, base):
    """Assemble the sibling layout used by the existing runtime release builder."""
    import hashlib
    paths=sync(repositories,base)
    identity=hashlib.sha256(json.dumps(repositories,sort_keys=True).encode()).hexdigest()
    folder=Path(base)/'workspaces'/identity
    folder.mkdir(parents=True,exist_ok=True)
    for name in ('JSwarm','GenomeHarbor'):
        target=folder/name
        if not target.exists():target.symlink_to(paths[name],target_is_directory=True)
        if target.resolve()!=Path(paths[name]).resolve():raise ValueError('Git workspace link changed')
    return folder
