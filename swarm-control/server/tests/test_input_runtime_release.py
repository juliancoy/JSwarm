import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from input_runtime_release import InputRuntimeRelease,INSTALL

class InputReleaseTests(unittest.TestCase):
    def test_atomic_install_verifies_bytes_and_preserves_previous(self):
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)/'peers.json';config.write_text(json.dumps({'name':'fixture'}))
            env={**os.environ,'LIFETECH_PEER_CONFIG':str(config)}
            release=InputRuntimeRelease(Path(__file__).resolve().parents[3])
            fixture_machine=Path(directory)/'machine-id';fixture_machine.write_text('isolated-input-release-machine')
            install=INSTALL.replace("pathlib.Path('/etc/machine-id')","pathlib.Path(sys.argv[1])")
            self.assertNotEqual(install,INSTALL)
            def deploy(files,revision=None,machine=None):
                manifest={name:hashlib.sha256(text.encode()).hexdigest() for name,text in files.items()}
                payload={'files':files,'revision':revision or hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest(),
                         'node':'fixture','machine':machine or hashlib.sha256(fixture_machine.read_bytes()).hexdigest()}
                return subprocess.run([sys.executable,'-c',install,str(fixture_machine)],input=json.dumps(payload),capture_output=True,text=True,env=env)
            result=deploy(release.files);self.assertEqual(result.returncode,0,result.stderr)
            current=config.parent/'input-releases/current';old=current.resolve()
            self.assertEqual(json.loads(result.stdout)['revision'],release.revision)
            # A regular third-party scripts package must not shadow this release.
            other=Path(directory)/'other/scripts';other.mkdir(parents=True)
            (other/'__init__.py').write_text('')
            probe=subprocess.run([sys.executable,'-c','import scripts.run_input_cache as m; print(m.__file__)'],
                                 cwd=directory,env={**env,'PYTHONPATH':os.pathsep.join([str(current),str(other.parent)])},
                                 capture_output=True,text=True)
            self.assertEqual(probe.returncode,0,probe.stderr)
            self.assertEqual(Path(probe.stdout.strip()).resolve(),(current/'scripts/run_input_cache.py').resolve())
            changed={**release.files,'run_inputs.py':release.files['run_inputs.py']+'\n# release fixture\n'}
            self.assertEqual(deploy(changed).returncode,0)
            self.assertEqual((current.parent/'previous').resolve(),old)
            active=current.resolve()
            for result in [deploy(changed,revision='wrong'),deploy(changed,machine='wrong'),deploy({'run_inputs.py':'pass'})]:
                self.assertNotEqual(result.returncode,0)
                self.assertEqual(current.resolve(),active)
            (active/'scripts/run_inputs.py').write_text('tampered')
            self.assertNotEqual(deploy(changed).returncode,0)
