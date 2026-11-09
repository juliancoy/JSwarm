import importlib.util
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('logged_launcher', Path(__file__).resolve().parents[1] / 'run.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class LoggedRunnerTests(unittest.TestCase):
    def test_automatic_discovery_detects_edits_additions_deletions_and_nested_repos(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / '.gitignore').write_text('ignored.txt\n')
            source = root / 'source.py'
            source.write_text('one')
            subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
            nested = root / 'child'
            nested.mkdir()
            subprocess.run(['git', 'init', '-q', str(nested)], check=True)
            dependency = nested / 'dependency.py'
            dependency.write_text('one')
            command = [sys.executable, '-c', 'pass']
            with patch.object(launcher.Path, 'cwd', return_value=root):
                def run():
                    self.assertEqual(launcher.run_logged_test(command), 0)
                def count():
                    return len((root / '.test-log.jsonl').read_text().splitlines())
                run()
                run()
                self.assertEqual(count(), 1)
                (root / 'ignored.txt').write_text('ignored output')
                run()
                self.assertEqual(count(), 1)
                source.write_text('two')
                run()
                self.assertEqual(count(), 2)
                added = root / 'new.py'
                added.write_text('new source')
                run()
                self.assertEqual(count(), 3)
                source.unlink()
                run()
                self.assertEqual(count(), 4)
                dependency.write_text('two')
                run()
                self.assertEqual(count(), 5)
                self.assertTrue((nested / '.test-log.jsonl').exists())

    def test_reuses_results_and_invalidates_changed_inputs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source.py'
            source.write_text('version one')
            count = root / 'count'
            command = [sys.executable, '-c',
                       f'from pathlib import Path; p=Path({str(count)!r}); '
                       'p.write_text(p.read_text()+"x" if p.exists() else "x"); print("evidence")']
            self.assertEqual(launcher.run_logged_test(command, [source]), 0)
            self.assertEqual(launcher.run_logged_test(command, [source]), 0)
            self.assertEqual(count.read_text(), 'x')
            source.write_text('version two')
            self.assertEqual(launcher.run_logged_test(command, [source]), 0)
            self.assertEqual(count.read_text(), 'xx')
            records = [json.loads(line) for line in (root / 'source.py.test-log.jsonl').read_text().splitlines()]
            self.assertEqual(len(records), 2)
            self.assertIn('evidence', Path(records[0]['output']).read_text())
            self.assertNotEqual(records[0]['fingerprint'], records[1]['fingerprint'])

    def test_failure_is_reused_and_command_change_invalidates(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'source.py'
            source.write_text('source')
            fail = [sys.executable, '-c', 'raise SystemExit(3)']
            self.assertEqual(launcher.run_logged_test(fail, [source]), 3)
            self.assertEqual(launcher.run_logged_test(fail, [source]), 3)
            self.assertEqual(launcher.run_logged_test([sys.executable, '-c', 'pass'], [source]), 0)
            self.assertEqual(len(source.with_name('source.py.test-log.jsonl').read_text().splitlines()), 2)

    def test_modified_during_test_is_not_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'source.py'
            source.write_text('source')
            command = [sys.executable, '-c', f'from pathlib import Path; p=Path({str(source)!r}); p.write_text(p.read_text()+"x")']
            launcher.run_logged_test(command, [source])
            record = json.loads(source.with_name('source.py.test-log.jsonl').read_text())
            self.assertFalse(record['inputs_stable'])


if __name__ == '__main__':
    unittest.main()
