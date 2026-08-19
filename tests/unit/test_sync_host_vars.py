import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.join(HERE, "..", "..")
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")

sys.path.insert(0, HERE)
sys.path.insert(0, SCRIPTS_DIR)

import sync_host_vars  # noqa: E402
from test_librenms_inventory import DEFAULT_ROUTES, make_open_url, write_config  # noqa: E402


class SyncHostVarsTestCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(self._cleanup_configs)
        self._configs = []

    def _cleanup_configs(self):
        for path in self._configs:
            try:
                os.unlink(path)
            except OSError:
                pass

    def run_sync(self, host_vars_dir, routes=None, extra_args=None, **config_overrides):
        config_path = write_config(**config_overrides)
        self._configs.append(config_path)

        plugin = sync_host_vars.get_plugin_instance()
        modname = type(plugin).__module__
        argv = ["sync_host_vars.py", "-i", config_path, "--host-vars-dir", host_vars_dir]
        argv.extend(extra_args or [])

        stdout = io.StringIO()
        with mock.patch(modname + ".open_url", make_open_url(routes)):
            with mock.patch.object(sys, "argv", argv):
                with contextlib.redirect_stdout(stdout):
                    sync_host_vars.main()

        return stdout.getvalue()


class TestSyncHostVars(SyncHostVarsTestCase):
    def test_creates_stub_for_each_discovered_host(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            host_vars_dir = os.path.join(tmp_dir, "host_vars")
            self.run_sync(host_vars_dir)

            self.assertEqual(set(os.listdir(host_vars_dir)), {"core-sw1.yml", "core-sw2.yml"})

    def test_stub_contains_no_active_yaml_keys(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            host_vars_dir = os.path.join(tmp_dir, "host_vars")
            self.run_sync(host_vars_dir)

            with open(os.path.join(host_vars_dir, "core-sw1.yml")) as f:
                content = f.read()
            self.assertTrue(all(line.startswith("#") or not line.strip() for line in content.splitlines()))

    def test_does_not_overwrite_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            host_vars_dir = os.path.join(tmp_dir, "host_vars")
            os.makedirs(host_vars_dir)
            existing = os.path.join(host_vars_dir, "core-sw1.yml")
            with open(existing, "w") as f:
                f.write("deploy_environment: prod\n")

            output = self.run_sync(host_vars_dir)

            with open(existing) as f:
                self.assertEqual(f.read(), "deploy_environment: prod\n")
            self.assertIn("1 created, 1 already existed", output)

    def test_dry_run_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            host_vars_dir = os.path.join(tmp_dir, "host_vars")
            self.run_sync(host_vars_dir, extra_args=["--dry-run"])

            self.assertFalse(os.path.exists(host_vars_dir))

    def test_second_run_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            host_vars_dir = os.path.join(tmp_dir, "host_vars")
            self.run_sync(host_vars_dir)
            output = self.run_sync(host_vars_dir)

            self.assertIn("0 created, 2 already existed", output)

    def test_hostname_with_path_traversal_is_not_written_outside_target_dir(self):
        routes = dict(DEFAULT_ROUTES)
        routes["/devices"] = "devices_traversal.json"

        with tempfile.TemporaryDirectory() as tmp_dir:
            host_vars_dir = os.path.join(tmp_dir, "host_vars")
            output = self.run_sync(host_vars_dir, routes=routes)

            self.assertIn("skipped as unsafe", output)
            escaped_target = os.path.abspath(os.path.join(tmp_dir, "..", "etc", "evil.yml"))
            self.assertFalse(os.path.exists(escaped_target))


if __name__ == "__main__":
    unittest.main()
