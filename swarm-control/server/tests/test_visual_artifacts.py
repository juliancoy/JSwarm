import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from visual_artifacts import MAX_FILES, VisualArtifacts, publish


class VisualArtifactTests(unittest.TestCase):
    def test_expanded_layout_matrix_transfers_all_files(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as output:
            for index in range(75):
                (Path(source) / f'capture-{index}.json').write_text('{}')
            receiver = VisualArtifacts(output)
            received = []
            def emit(kind, **event):
                result = receiver.receive('fixture-run', 'fixture.local', 'visual', {'kind': kind, **event})
                if result:
                    received.append(result)
            publish(source, 'visual', emit)
            self.assertEqual(len(received), 75)
            self.assertEqual(receiver.read('fixture-run', 'fixture.local', 'visual', 'capture-74.json')[0], b'{}')

    def test_publisher_rejects_over_budget_before_sending(self):
        with tempfile.TemporaryDirectory() as source:
            for index in range(MAX_FILES + 1):
                (Path(source) / f'capture-{index}.json').write_text('{}')
            events = []
            with self.assertRaisesRegex(ValueError, 'Too many'):
                publish(source, 'visual', lambda *args, **kwargs: events.append(args))
            self.assertEqual(events, [])

    def test_receiver_enforces_file_limit(self):
        with tempfile.TemporaryDirectory() as output:
            receiver = VisualArtifacts(output)
            for index in range(MAX_FILES):
                receiver.receive('fixture-run', 'fixture.local', 'visual', {
                    'kind': 'artifact_begin', 'name': f'capture-{index}.json',
                    'size': 2, 'sha256': hashlib.sha256(b'{}').hexdigest()})
            with self.assertRaisesRegex(ValueError, 'limit'):
                receiver.receive('fixture-run', 'fixture.local', 'visual', {
                    'kind': 'artifact_begin', 'name': 'extra.json',
                    'size': 2, 'sha256': hashlib.sha256(b'{}').hexdigest()})


if __name__ == '__main__':
    unittest.main()
