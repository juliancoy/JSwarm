import json
import unittest
from unittest.mock import patch

from distributed_worker import container_user


class ContainerUserTests(unittest.TestCase):
    def test_rootless_engine_uses_its_host_owner_mapping(self):
        with patch('subprocess.check_output', return_value=json.dumps(['name=seccomp', 'name=rootless'])):
            self.assertEqual(container_user(), '0:0')

    def test_rootful_engine_uses_the_worker_account(self):
        with patch('subprocess.check_output', return_value='[]'), \
                patch('os.getuid', return_value=1000), patch('os.getgid', return_value=1001):
            self.assertEqual(container_user(), '1000:1001')


if __name__ == '__main__':
    unittest.main()
