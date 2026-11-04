import unittest
from unittest.mock import patch

from docker_utils.policy import prepare_build


class BuildImagePolicyTests(unittest.TestCase):
    def test_external_bases_and_copy_sources_are_resolved_but_stages_are_not(self):
        source='ARG BASE=python:3.12-bookworm\nFROM ${BASE} AS build\nFROM build AS final\nCOPY --from=node:22-bookworm /usr/local/bin/node /usr/local/bin/node\n'
        def image(reference):return 'sha256:'+('a' if reference.startswith('python') else 'b')*64
        with patch('docker_utils.policy.ensure_image',side_effect=image) as ensure:
            prepared=prepare_build(source)
        self.assertEqual([call.args[0] for call in ensure.call_args_list],['python:3.12-bookworm','node:22-bookworm'])
        self.assertIn('FROM build AS final',prepared)
        self.assertNotIn('FROM ${BASE}',prepared)
        self.assertIn('COPY --from=sha256:',prepared)

    def test_unresolved_base_cannot_fall_through_to_a_registry_pull(self):
        with patch('docker_utils.policy.ensure_image') as ensure:
            with self.assertRaises(ValueError):prepare_build('FROM ${MISSING}\n')
        ensure.assert_not_called()

    def test_builder_features_that_bypass_engine_cache_are_rejected(self):
        for dockerfile in ('FROM --platform=linux/arm64 python:3.12\n','RUN --mount=type=cache,target=/cache echo build\n'):
            with patch('docker_utils.policy.ensure_image') as ensure:
                with self.assertRaises(ValueError):prepare_build(dockerfile)
            ensure.assert_not_called()
