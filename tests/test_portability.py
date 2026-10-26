"""Failure evidence must survive missing Docker or cleanup interruption; no Docker is run."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('portability_checker', Path(__file__).resolve().parents[1] / 'scripts/check-portability.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class PortabilityFailureEvidence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.archive = root / 'synthetic.zip'
        self.archive.write_bytes(b'SYNTHETIC archive bytes; no container reads these')
        self.output = root / 'failure.json'
        self.args = ['check-portability', '--archive', str(self.archive), '--image', 'sha256:' + '1' * 64,
                     '--output', str(self.output), '--execute']

    def test_missing_docker_preserves_preflight_failure_without_claiming_a_run(self):
        with patch.object(sys, 'argv', self.args), redirect_stdout(io.StringIO()), \
                patch.object(checker.subprocess, 'check_output', side_effect=FileNotFoundError('SYNTHETIC missing Docker')), \
                patch.object(checker.subprocess, 'run') as run:
            with self.assertRaises(SystemExit):
                checker.main()
        body = json.loads(self.output.read_text())
        self.assertFalse(body['executed'])
        self.assertFalse(body['container_created'])
        self.assertIsNone(body['container_removed'])
        self.assertEqual(body['phase'], 'preflight')
        self.assertEqual(body['new_provider_calls'], 0)
        run.assert_not_called()

    def test_cleanup_timeout_does_not_erase_failed_execution_evidence(self):
        image = json.dumps([{'Id': 'sha256:' + '1' * 64, 'Os': 'linux', 'Architecture': 'arm64'}])
        stopped = subprocess.CompletedProcess(['SYNTHETIC docker'], 1, stdout=b'{"passed":false}', stderr=b'')
        with patch.object(sys, 'argv', self.args), redirect_stdout(io.StringIO()), \
                patch.object(checker.subprocess, 'check_output', return_value=image), \
                patch.object(checker.subprocess, 'run', side_effect=[stopped, subprocess.TimeoutExpired('SYNTHETIC cleanup', 20)]):
            with self.assertRaises(SystemExit):
                checker.main()
        body = json.loads(self.output.read_text())
        self.assertTrue(body['executed'])
        self.assertFalse(body['result']['passed'])
        self.assertFalse(body['container_removed'])
        self.assertIn('cleanup_error', body)
