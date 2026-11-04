"""Stage an explicit atomic peer-agent source update on this enrolled machine."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

FILES=('launcher_peers.py','launcher_discovery.py','launcher_registration.py','launcher_agent.py','launcher_registry.py')


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rollback',action='store_true')
    args=parser.parse_args()
    config=Path(os.environ.get('LIFETECH_PEER_CONFIG',str(Path.home()/'.config/lifetech-launcher/peers.json')))
    if not config.is_file():raise SystemExit('Enroll this machine before updating its agent')
    releases=config.parent/'agent-releases';releases.mkdir(exist_ok=True,mode=0o700)
    current=releases/'current';previous=releases/'previous'
    if args.rollback:
        if not previous.is_symlink():raise SystemExit('No previous installed agent release')
        target=os.readlink(previous)
    else:
        folder=Path(__file__).resolve().parent
        sources={name:(folder/name).read_bytes() for name in FILES}
        target=hashlib.sha256(b''.join(sources[name] for name in FILES)).hexdigest()
        if not (releases/target).exists():
            with tempfile.TemporaryDirectory(dir=releases,prefix='.staging-') as staging:
                staging=Path(staging)
                for name,data in sources.items():(staging/name).write_bytes(data)
                (staging/'manifest.json').write_text(json.dumps({name:hashlib.sha256(data).hexdigest() for name,data in sources.items()}))
                staging.rename(releases/target)
    # Existing releases and rollback targets are verified too, before touching
    # either pointer. A corrupt immutable directory must never become desired.
    destination=(releases/target).resolve()
    if destination.parent!=releases.resolve():raise ValueError('Agent release escapes release directory')
    manifest=json.loads((destination/'manifest.json').read_text())
    if set(manifest)!=set(FILES):raise ValueError('Incomplete installed agent release')
    for name,digest in manifest.items():
        data=(destination/name).read_bytes()
        if hashlib.sha256(data).hexdigest()!=digest:raise ValueError('Installed agent release checksum mismatch')
        compile(data,name,'exec')
    if hashlib.sha256(b''.join((destination/name).read_bytes() for name in FILES)).hexdigest()!=destination.name:
        raise ValueError('Installed agent release identity mismatch')
    if current.is_symlink() and os.readlink(current)==target:
        print('Agent release is already current; no reload requested');return
    if current.is_symlink():
        temporary=releases/'previous.tmp';temporary.unlink(missing_ok=True)
        temporary.symlink_to(os.readlink(current));temporary.replace(previous)
    temporary=releases/'current.tmp';temporary.unlink(missing_ok=True)
    temporary.symlink_to(target);temporary.replace(current)
    print('Agent release staged: '+target+'; supervisor replaces the agent, preserves Docker workers and rolls back failed startup')


if __name__=='__main__':main()
