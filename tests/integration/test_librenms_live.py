"""Optional integration tests that exercise the plugin against a real, running LibreNMS
instance rather than mocked HTTP responses (see tests/unit/ for those).

Skipped automatically unless LIBRENMS_API and LIBRENMS_TOKEN are available - either
already exported, or provided via a gitignored `env` file (KEY=VALUE per line) at the
repo root. Nothing here assumes specific fixture data: every assertion is checked
against a live "ground truth" fetched directly from the same API, so the suite keeps
working as the target instance's devices/groups change.

Run with:
    python3 -m unittest discover -s tests/integration -v
"""

import json
import os
import ssl
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request

from ansible.inventory.data import InventoryData
from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import inventory_loader

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
PLUGIN_DIR = os.path.join(REPO_ROOT, "inventory_plugins")
ENV_FILE = os.path.join(REPO_ROOT, "env")


def _load_env_file(path):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Real env vars (or ones already loaded) win over the file.
            os.environ.setdefault(key, value)


_load_env_file(ENV_FILE)

LIBRENMS_API = os.environ.get("LIBRENMS_API")
LIBRENMS_TOKEN = os.environ.get("LIBRENMS_TOKEN")
HAVE_CREDS = bool(LIBRENMS_API and LIBRENMS_TOKEN)

skip_reason = "LIBRENMS_API / LIBRENMS_TOKEN not set (export them or add an `env` file at the repo root)"


def get_plugin_instance():
    inventory_loader.add_directory(PLUGIN_DIR)
    plugin = inventory_loader.get("librenms")
    if plugin is None:
        raise RuntimeError("librenms inventory plugin could not be loaded from " + PLUGIN_DIR)
    return plugin


def _raw_get(path):
    """Ground truth fetched directly from the LibreNMS API, independent of the plugin's
    own HTTP code, so tests aren't just checking the plugin agrees with itself."""
    request = urllib.request.Request(
        LIBRENMS_API.rstrip("/") + path, headers={"X-Auth-Token": LIBRENMS_TOKEN}
    )
    context = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(request, context=context, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode("utf-8"))


def write_config(**overrides):
    # api_endpoint/api_token are deliberately left out by default so the plugin picks
    # them up from LIBRENMS_API/LIBRENMS_TOKEN, same as a real user relying on the env
    # vars documented in DOCUMENTATION.
    config = {"plugin": "librenms", "validate_certs": False, "cache": False}
    config.update(overrides)

    lines = ["{0}: {1}".format(key, json.dumps(value)) for key, value in config.items()]
    content = "\n".join(lines) + "\n"

    handle = tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False)
    handle.write(content)
    handle.close()
    return handle.name


@unittest.skipUnless(HAVE_CREDS, skip_reason)
class LiveLibrenmsTestCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(self._cleanup_configs)
        self._configs = []

    def _cleanup_configs(self):
        for path in self._configs:
            try:
                os.unlink(path)
            except OSError:
                pass

    def build_plugin(self, **config_overrides):
        config_path = write_config(**config_overrides)
        self._configs.append(config_path)

        plugin = get_plugin_instance()
        inventory = InventoryData()
        loader = DataLoader()
        plugin.parse(inventory, loader, config_path, cache=True)
        return plugin, inventory


class TestLiveDevices(LiveLibrenmsTestCase):
    def test_default_filters_match_raw_disabled_ignore_flags(self):
        plugin = get_plugin_instance()
        raw_devices = _raw_get("/devices").get("devices", [])
        expected_ids = {
            int(d["device_id"])
            for d in raw_devices
            if not plugin._is_flag_set(d.get("disabled")) and not plugin._is_flag_set(d.get("ignore"))
        }

        _, inventory = self.build_plugin()
        actual_ids = {
            inventory.get_host(h).get_vars()["libre_device_id"] for h in inventory.hosts
        }

        self.assertEqual(actual_ids, expected_ids)

    def test_disabling_filters_returns_every_raw_device(self):
        raw_devices = _raw_get("/devices").get("devices", [])
        expected_ids = {int(d["device_id"]) for d in raw_devices}

        _, inventory = self.build_plugin(exclude_disabled=False, exclude_ignored=False)
        actual_ids = {
            inventory.get_host(h).get_vars()["libre_device_id"] for h in inventory.hosts
        }

        self.assertEqual(actual_ids, expected_ids)

    def test_libre_prefixed_hostvars_match_raw_device(self):
        raw_devices = _raw_get("/devices").get("devices", [])
        self.assertTrue(raw_devices, "test instance has no devices to compare against")
        sample = raw_devices[0]

        _, inventory = self.build_plugin(exclude_disabled=False, exclude_ignored=False)
        match = [
            inventory.get_host(h).get_vars()
            for h in inventory.hosts
            if inventory.get_host(h).get_vars().get("libre_device_id") == int(sample["device_id"])
        ]
        self.assertEqual(len(match), 1)
        host_vars = match[0]

        self.assertEqual(host_vars["libre_hostname"], sample["hostname"])
        self.assertEqual(host_vars["libre_os"], sample["os"])
        self.assertEqual(host_vars["ansible_host"], sample["hostname"])


class TestLiveGrouping(LiveLibrenmsTestCase):
    def test_group_by_os_and_status_is_internally_consistent(self):
        _, inventory = self.build_plugin(
            exclude_disabled=False, exclude_ignored=False, group_by=["os", "status"]
        )

        for group_name, group in inventory.groups.items():
            if group_name.startswith("os_"):
                expected_os = group_name[len("os_"):]
                for host in group.get_hosts():
                    self.assertEqual(host.get_vars().get("libre_os"), expected_os)
            elif group_name.startswith("status_"):
                expected_status = group_name[len("status_"):]
                for host in group.get_hosts():
                    is_up = self._is_flag_set_value(host.get_vars().get("libre_status"))
                    self.assertEqual("up" if is_up else "down", expected_status)

    @staticmethod
    def _is_flag_set_value(value):
        return str(value) not in ("0", "", "None")

    def test_device_group_membership_matches_raw_api(self):
        raw_groups_response = _raw_get("/devicegroups")
        raw_group_names = (
            {g["name"] for g in raw_groups_response.get("groups", [])}
            if raw_groups_response.get("status") != "error"
            else set()
        )

        _, inventory = self.build_plugin(device_groups_as_ansible_groups=True)

        non_device_groups = {"all", "ungrouped"}
        ansible_device_groups = set(inventory.groups.keys()) - non_device_groups

        if not raw_group_names:
            # Covers the "no device groups found" (HTTP 404) case: must not be mistaken
            # for a real failure, and must not fabricate any groups.
            self.assertEqual(ansible_device_groups, set())
            return

        self.assertEqual(ansible_device_groups, raw_group_names)

        for group_name in raw_group_names:
            raw_members = _raw_get("/devicegroups/" + group_name).get("devices", [])
            expected_member_ids = {int(d["device_id"]) for d in raw_members}
            actual_member_ids = {
                h.get_vars()["libre_device_id"] for h in inventory.groups[group_name].get_hosts()
            }
            self.assertEqual(actual_member_ids, expected_member_ids)


class TestLiveCli(LiveLibrenmsTestCase):
    def test_ansible_inventory_cli_runs_cleanly(self):
        config_path = write_config(group_by=["os"])
        self._configs.append(config_path)

        env = dict(os.environ)
        env["ANSIBLE_INVENTORY_PLUGINS"] = PLUGIN_DIR
        env["ANSIBLE_INVENTORY_ENABLED"] = "librenms"

        result = subprocess.run(
            ["ansible-inventory", "-i", config_path, "--list"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertNotIn("WARNING", result.stderr)
        self.assertNotIn("ERROR", result.stderr)

        payload = json.loads(result.stdout)
        self.assertTrue(payload.get("_meta", {}).get("hostvars"))


if __name__ == "__main__":
    unittest.main()
