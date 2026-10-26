"""Exercise runtime selection without starting a VM or bypassing the sandbox."""

import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

script = Path(__file__).resolve().parents[1] / "scripts/start-local-runtime.py"
spec = importlib.util.spec_from_file_location("runtime_bootstrap", script)
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class RuntimeBootstrap(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "instances"
        self.home.mkdir()
        self.runtime = self.root / "runtime"
        for name in ["lima/bin/limactl", "docker/docker"]:
            path = self.runtime / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        (self.runtime / "lima-home.txt").write_text(str(self.home))
        self.lima = self.runtime / "lima/bin/limactl"

    def tearDown(self):
        self.temp.cleanup()

    def existing_instance(self):
        instance = self.home / "fresh-eyes"
        instance.mkdir()
        (instance / "lima.yaml").write_text("mounts: []\ncpus: 2\n")
        return instance

    def test_new_instance_uses_template_without_host_mounts(self):
        cmd = bootstrap.start_command(self.lima, self.home)
        self.assertEqual(cmd[-1], "template:docker")
        self.assertIn("--mount-none", cmd)
        self.assertIn("--name=fresh-eyes", cmd)

    def test_existing_instance_starts_by_name(self):
        self.existing_instance()
        cmd = bootstrap.start_command(self.lima, self.home)
        self.assertEqual(cmd, [str(self.lima), "start", "--tty=false", "--timeout=5m", "fresh-eyes"])

    def test_incomplete_directory_is_preserved(self):
        instance = self.home / "fresh-eyes"
        instance.mkdir()
        marker = instance / "keep-me"
        marker.write_text("existing data")
        with self.assertRaises(ValueError):
            bootstrap.start_command(self.lima, self.home)
        self.assertEqual(marker.read_text(), "existing data")

    def test_repeat_start_keeps_config_and_uses_same_socket(self):
        instance = self.existing_instance()
        before = (instance / "lima.yaml").read_text()
        with patch.object(bootstrap.subprocess, "call", return_value=0) as call, \
             patch.object(bootstrap.subprocess, "run") as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bootstrap.main([], self.runtime), 0)
            self.assertEqual(bootstrap.main([], self.runtime), 0)
        self.assertEqual(call.call_count, 2)
        self.assertTrue(all(c.args[0][-1] == "fresh-eyes" for c in call.call_args_list))
        self.assertEqual(run.call_count, 2)
        self.assertEqual((instance / "lima.yaml").read_text(), before)
        self.assertTrue((self.runtime / "docker-env.json").is_file())

    def test_failed_start_does_not_pull_image_or_claim_ready(self):
        self.existing_instance()
        output = io.StringIO()
        with patch.object(bootstrap.subprocess, "call", return_value=1), \
             patch.object(bootstrap.subprocess, "run") as run, contextlib.redirect_stdout(output):
            self.assertEqual(bootstrap.main([], self.runtime), 1)
        run.assert_not_called()
        self.assertNotIn("Runtime ready", output.getvalue())

    def test_failed_pull_can_be_retried_without_recreating_vm(self):
        self.existing_instance()
        with patch.object(bootstrap.subprocess, "call", return_value=0), \
             patch.object(bootstrap.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["docker", "pull"])), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bootstrap.main([], self.runtime), 1)
        self.assertEqual(bootstrap.start_command(self.lima, self.home)[-1], "fresh-eyes")

    def test_lost_or_empty_temporary_state_uses_persistent_location(self):
        persistent = self.root / "persistent"
        self.assertEqual(bootstrap.choose_home(self.home, persistent), persistent)
        self.assertEqual(bootstrap.choose_home(self.root / "missing", persistent), persistent)
        with patch.object(bootstrap.subprocess, "call", return_value=0) as call, \
             patch.object(bootstrap.subprocess, "run"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bootstrap.main([], self.runtime, persistent_home=persistent), 0)
        self.assertEqual(Path((self.runtime / "lima-home.txt").read_text()).resolve(), persistent.resolve())
        self.assertEqual(Path(call.call_args.kwargs["env"]["LIMA_HOME"]).resolve(), persistent.resolve())

    def test_partial_instance_is_not_discarded_for_a_new_location(self):
        instance = self.home / "fresh-eyes"
        instance.mkdir()
        (instance / "keep-disk").write_text("preserve")
        self.assertEqual(bootstrap.choose_home(self.home, self.root / "persistent"), self.home)
        with self.assertRaises(ValueError):
            bootstrap.start_command(self.lima, self.home)
        self.assertEqual((instance / "keep-disk").read_text(), "preserve")
