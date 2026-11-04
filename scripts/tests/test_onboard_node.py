import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import onboard_node


class OnboardNodeTests(unittest.TestCase):
    def rejected_probe(self, probe, message):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            config = home / '.config/lifetech-launcher/peers.json'
            original = {'name': 'master.local', 'peers': {'master.local': '10.42.0.1'}, 'key': '00' * 32}
            onboard_node.save(config, original)
            result = subprocess.CompletedProcess([], 0, json.dumps(probe), '')
            interfaces = [{'addr_info': [{'local': '10.42.0.1', 'prefixlen': 24, 'scope': 'global'}]}]
            with patch.object(Path, 'home', return_value=home), \
                    patch.object(sys, 'argv', ['onboard_node.py', 'julian@inspiron.local']), \
                    patch.object(subprocess, 'run', return_value=result) as remote, \
                    patch.object(subprocess, 'check_output', return_value=json.dumps(interfaces)), \
                    self.assertRaisesRegex(RuntimeError, message):
                onboard_node.main()
            self.assertEqual(json.loads(config.read_text()), original)
            self.assertEqual(remote.call_count, 1, 'Rejected discovery must never provision the remote host')

    def test_wrong_hostname_cannot_receive_fleet_credentials(self):
        self.rejected_probe({'host': 'other.local', 'docker': 'linux'}, 'hostname')

    def test_non_linux_docker_is_rejected_before_enrollment(self):
        self.rejected_probe({'host': 'inspiron.local', 'docker': 'windows'}, 'platform')

    def test_address_outside_lan_cannot_receive_fleet_credentials(self):
        self.rejected_probe({'host': 'inspiron.local', 'docker': 'linux', 'interfaces': [
            {'ifname': 'wlan0', 'addr_info': [{'local': '192.168.1.2', 'scope': 'global'}]}]}, 'physical address')

    def test_requires_an_mdns_identity(self):
        with patch.object(sys, 'argv', ['onboard_node.py', 'julian@10.42.0.110']), \
                contextlib.redirect_stderr(io.StringIO()), \
                patch.object(subprocess, 'run') as remote, self.assertRaises(SystemExit):
            onboard_node.main()
        remote.assert_not_called()


if __name__ == '__main__':
    unittest.main()
