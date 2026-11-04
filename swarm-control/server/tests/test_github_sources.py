import subprocess
import tempfile
import unittest
from pathlib import Path
from github_sources import check_index, local_credentials, publish
from unittest.mock import patch


class PublicationSecretsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.repo = Path(self.folder.name)
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.org')
        self.git('config', 'user.name', 'Test')
        (self.repo / 'safe.txt').write_text('initial\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'initial')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], check=True,
                              capture_output=True).stdout

    def test_candidate_rejects_key_without_changing_index(self):
        (self.repo / 'notes.txt').write_text('-----BEGIN ' + 'PRIVATE KEY-----\n')
        before = self.git('write-tree')
        with self.assertRaisesRegex(RuntimeError, 'credential content in notes.txt'):
            check_index(self.repo, candidate=True)
        self.assertEqual(before, self.git('write-tree'))

    def test_staged_key_cannot_be_hidden_by_worktree_edit(self):
        target = self.repo / 'safe.txt'
        target.write_text('gh' + 'p_' + 'a' * 40)
        self.git('add', '.')
        target.write_text('safe now\n')
        check_index(self.repo, candidate=True)
        with self.assertRaisesRegex(RuntimeError, 'credential content'):
            check_index(self.repo)

    def test_sensitive_files_and_safe_deletion(self):
        target = self.repo / '.dev.vars'
        target.write_text('anything')
        with self.assertRaisesRegex(RuntimeError, 'sensitive file'):
            check_index(self.repo, candidate=True)
        target.unlink()
        (self.repo / 'safe.txt').unlink()
        check_index(self.repo, candidate=True)

    def test_literal_credentials_redacted_and_examples_allowed(self):
        secret = 'z' * 48
        target = self.repo / 'config.py'
        target.write_text('api_token = "' + secret + '"')
        with self.assertRaises(RuntimeError) as error:
            check_index(self.repo, candidate=True)
        self.assertNotIn(secret, str(error.exception))
        target.unlink()
        (self.repo / '.env.example').write_text('API_TOKEN=replace-me\n')
        check_index(self.repo, candidate=True)

    def test_known_local_credential_in_plain_notes_is_blocked(self):
        value = 'opaque-' + 'q' * 33
        (self.repo / '.env.cloudflare').write_text('CLOUDFLARE_API_TOKEN=' + value)
        (self.repo / '.gitignore').write_text('.env.cloudflare\n')
        (self.repo / 'notes.txt').write_text(value)
        credentials = local_credentials([self.repo])
        with self.assertRaisesRegex(RuntimeError, 'credential content'):
            check_index(self.repo, candidate=True, credentials=credentials)

    def test_publish_preflight_blocks_before_commit_or_index_changes(self):
        self.git('remote', 'add', 'origin', 'https://github.com/example/test.git')
        (self.repo / 'notes.txt').write_text('-----BEGIN ' + 'OPENSSH PRIVATE KEY-----')
        before_head = self.git('rev-parse', 'HEAD')
        before_index = self.git('write-tree')
        with patch('github_sources.REPOSITORIES', ('.',)):
            with self.assertRaisesRegex(RuntimeError, 'credential content'):
                publish(self.repo, 'test', lambda: False, lambda *args: None)
        self.assertEqual(before_head, self.git('rev-parse', 'HEAD'))
        self.assertEqual(before_index, self.git('write-tree'))
