import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from launcher_agent import worker_environment


class WorkerEnvironmentTests(unittest.TestCase):
    def test_pinned_worker_can_import_sibling_script_modules(self):
        env=worker_environment(Path('/isolated'),Path('/checkout'))
        self.assertEqual(env['PYTHONPATH'].split(os.pathsep),['/checkout','/checkout/scripts','/checkout/swarm-control/server'])

    def test_rootless_engine_is_preserved_without_deployment_credentials(self):
        with patch.dict(os.environ, {'DOCKER_HOST': 'unix:///run/user/1000/docker.sock',
                                    'CLOUDFLARE_API_TOKEN': 'private', 'HOME': '/owner'}, clear=True):
            env = worker_environment(Path('/isolated'))
        self.assertEqual(env['DOCKER_HOST'], 'unix:///run/user/1000/docker.sock')
        self.assertEqual(env['HOME'], '/isolated')
        self.assertNotIn('CLOUDFLARE_API_TOKEN', env)


if __name__ == '__main__':
    unittest.main()
