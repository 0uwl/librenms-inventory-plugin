import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

from ansible.errors import AnsibleError
from ansible.inventory.data import InventoryData
from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import inventory_loader

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
PLUGIN_DIR = os.path.join(HERE, "..", "..", "inventory_plugins")


def get_plugin_instance():
    # Go through the real inventory plugin loader (rather than importlib) so that the
    # DOCUMENTATION-defined options get registered with Ansible's config manager exactly
    # like a normal `ansible-inventory` run does.
    inventory_loader.add_directory(PLUGIN_DIR)
    plugin = inventory_loader.get("librenms")
    if plugin is None:
        raise RuntimeError("librenms inventory plugin could not be loaded from " + PLUGIN_DIR)
    return plugin


def load_fixture(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


DEFAULT_ROUTES = {
    "/devices": "devices.json",
    "/devicegroups/Core": "devicegroups_core.json",
    "/devicegroups/Edge": "devicegroups_edge.json",
    "/devicegroups/Decommissioned": "devicegroups_decommissioned.json",
    # order matters: must be checked after the more specific /devicegroups/<name> routes
    "/devicegroups": "devicegroups.json",
}


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body


class HttpErrorRoute:
    """Route marker: respond with a raised HTTPError instead of a 200 body, so tests can
    simulate LibreNMS returning a non-2xx status (eg. 404) for "nothing found" cases."""

    def __init__(self, status, payload=None, body=None):
        self.status = status
        self.payload = payload
        self.body = body

    def raise_for(self, url):
        if self.body is not None:
            body = self.body
        else:
            body = json.dumps(self.payload).encode("utf-8")
        raise urllib.error.HTTPError(url, self.status, "Error", {}, io.BytesIO(body))


def make_open_url(routes=None, call_log=None):
    routes = routes or DEFAULT_ROUTES

    def _open_url(url, headers=None, timeout=None, validate_certs=None):
        if call_log is not None:
            call_log.append(url)
        for suffix in sorted(routes, key=len, reverse=True):
            if url.endswith(suffix) or suffix in url:
                route = routes[suffix]
                if isinstance(route, HttpErrorRoute):
                    route.raise_for(url)
                return FakeResponse(load_fixture(route))
        raise AssertionError("Unexpected URL requested: {0}".format(url))

    return _open_url


def write_config(**overrides):
    config = {
        "plugin": "librenms",
        "api_endpoint": "https://fake-librenms.example.com/api/v0",
        "api_token": "test-token",
        "cache": False,
    }
    config.update(overrides)

    lines = []
    for key, value in config.items():
        lines.append("{0}: {1}".format(key, json.dumps(value)))
    content = "\n".join(lines) + "\n"

    handle = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yml", delete=False, dir=tempfile.gettempdir()
    )
    handle.write(content)
    handle.close()
    return handle.name


class LibrenmsInventoryTestCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(self._cleanup_configs)
        self._configs = []

    def _cleanup_configs(self):
        for path in self._configs:
            try:
                os.unlink(path)
            except OSError:
                pass

    def build_plugin(self, routes=None, call_log=None, **config_overrides):
        config_path = write_config(**config_overrides)
        self._configs.append(config_path)

        plugin = get_plugin_instance()
        modname = type(plugin).__module__
        inventory = InventoryData()
        loader = DataLoader()

        with mock.patch(modname + ".open_url", make_open_url(routes, call_log)):
            plugin.parse(inventory, loader, config_path, cache=True)
            if plugin.get_option("cache"):
                # Normally done by InventoryManager after parse() returns; replicate it
                # here since the test calls parse() directly.
                plugin.update_cache_if_changed()

        return plugin, inventory


class TestBasicParsing(LibrenmsInventoryTestCase):
    def test_default_excludes_disabled_and_ignored_devices(self):
        _, inventory = self.build_plugin()

        hosts = set(inventory.hosts.keys())
        # device 3 (disabled) and device 4 (ignored) must be excluded by default
        self.assertEqual(hosts, {"core-sw1", "core-sw2"})

    def test_libre_prefixed_vars_and_mapped_vars_are_set(self):
        _, inventory = self.build_plugin()

        host_vars = inventory.get_host("core-sw1").get_vars()
        self.assertEqual(host_vars["libre_hardware"], "C9300")
        self.assertEqual(host_vars["libre_hostname"], "core-sw1.example.com")
        self.assertEqual(host_vars["ansible_host"], "core-sw1.example.com")
        # os "ios" maps to network_os "ios" via the default os_name_map
        self.assertEqual(host_vars["ansible_network_os"], "ios")

    def test_iosxe_is_translated_to_ios_network_os(self):
        _, inventory = self.build_plugin()

        host_vars = inventory.get_host("core-sw2").get_vars()
        self.assertEqual(host_vars["libre_os"], "iosxe")
        self.assertEqual(host_vars["ansible_network_os"], "ios")

    def test_unicode_hostname_is_ascii_normalized(self):
        _, inventory = self.build_plugin(exclude_disabled=False)

        self.assertIn("edge-fw1", inventory.hosts)
        self.assertNotIn("édge-fw1", inventory.hosts)

    def test_default_exclude_fields_strips_sensitive_snmp_data(self):
        _, inventory = self.build_plugin()

        host_vars = inventory.get_host("core-sw1").get_vars()
        self.assertNotIn("libre_community", host_vars)
        self.assertNotIn("libre_authpass", host_vars)
        self.assertNotIn("libre_cryptopass", host_vars)
        # unrelated fields must still be present
        self.assertIn("libre_hardware", host_vars)

    def test_exclude_fields_can_be_overridden_to_empty(self):
        _, inventory = self.build_plugin(exclude_fields=[])

        host_vars = inventory.get_host("core-sw1").get_vars()
        self.assertEqual(host_vars["libre_community"], "public")
        self.assertEqual(host_vars["libre_authpass"], "authsecret")
        self.assertEqual(host_vars["libre_cryptopass"], "cryptosecret")


class TestFiltering(LibrenmsInventoryTestCase):
    def test_exclude_ignored_false_includes_ignored_device(self):
        _, inventory = self.build_plugin(exclude_ignored=False)

        self.assertIn("old-switch", inventory.hosts)

    def test_host_name_regex_filter(self):
        _, inventory = self.build_plugin(
            exclude_disabled=False,
            exclude_ignored=False,
            host_name_regex_filter=["^core-"],
        )

        self.assertEqual(set(inventory.hosts.keys()), {"core-sw1", "core-sw2"})


class TestGrouping(LibrenmsInventoryTestCase):
    def test_group_by_string_property(self):
        _, inventory = self.build_plugin(group_by=["os", "location"])

        os_ios_hosts = {h.name for h in inventory.groups["os_ios"].get_hosts()}
        self.assertEqual(os_ios_hosts, {"core-sw1"})

        location_hosts = {h.name for h in inventory.groups["location_DC1"].get_hosts()}
        self.assertEqual(location_hosts, {"core-sw1", "core-sw2"})

    def test_group_by_boolean_property_only_creates_true_group(self):
        _, inventory = self.build_plugin(
            exclude_disabled=False, exclude_ignored=False, group_by=["disabled", "ignored"]
        )

        self.assertIn("disabled", inventory.groups)
        self.assertIn("ignored", inventory.groups)
        disabled_hosts = {h.name for h in inventory.groups["disabled"].get_hosts()}
        self.assertEqual(disabled_hosts, {"edge-fw1"})

    def test_group_names_raw_drops_prefix(self):
        _, inventory = self.build_plugin(group_by=["os"], group_names_raw=True)

        self.assertIn("ios", inventory.groups)
        self.assertNotIn("os_ios", inventory.groups)

    def test_device_group_membership_respects_regex_filter(self):
        call_log = []
        _, inventory = self.build_plugin(
            call_log=call_log,
            group_name_regex_filter=["^Core$"],
        )

        self.assertIn("Core", inventory.groups)
        self.assertNotIn("Edge", inventory.groups)
        # Only the Core membership endpoint should have been queried, not Edge/Decommissioned
        self.assertTrue(any(url.endswith("/devicegroups/Core") for url in call_log))
        self.assertFalse(any(url.endswith("/devicegroups/Edge") for url in call_log))

    def test_devicegroups_no_members_quirk_does_not_raise(self):
        # The "Decommissioned" fixture returns LibreNMS' odd error-shaped empty response.
        _, inventory = self.build_plugin(group_name_regex_filter=["^Decommissioned$"])

        self.assertNotIn("Decommissioned", inventory.groups)

    def test_404_with_no_devices_found_message_is_treated_as_empty(self):
        # Some LibreNMS versions/endpoints return HTTP 404 (instead of 200 +
        # {"status": "error"}) for "nothing found" cases - this must not be mistaken for
        # the endpoint/resource not existing.
        routes = dict(DEFAULT_ROUTES)
        routes["/devicegroups/Edge"] = HttpErrorRoute(
            404, payload={"status": "error", "message": "No devices found in group 'Edge'"}
        )

        _, inventory = self.build_plugin(routes=routes, group_name_regex_filter=["^Core$", "^Edge$"])

        self.assertIn("Core", inventory.groups)
        self.assertNotIn("Edge", inventory.groups)

    def test_404_with_real_error_message_still_raises(self):
        routes = dict(DEFAULT_ROUTES)
        routes["/devicegroups/Edge"] = HttpErrorRoute(
            404, payload={"status": "error", "message": "Invalid API token supplied"}
        )

        with self.assertRaises(AnsibleError):
            self.build_plugin(routes=routes, group_name_regex_filter=["^Core$", "^Edge$"])

    def test_404_with_non_json_body_still_raises(self):
        routes = dict(DEFAULT_ROUTES)
        routes["/devicegroups/Edge"] = HttpErrorRoute(404, body=b"<html>not found</html>")

        with self.assertRaises(AnsibleError):
            self.build_plugin(routes=routes, group_name_regex_filter=["^Core$", "^Edge$"])

    def test_constructed_compose_and_keyed_groups(self):
        _, inventory = self.build_plugin(
            compose={"libre_env": "'prod'"},
            keyed_groups=[{"prefix": "type", "key": "libre_type"}],
        )

        host_vars = inventory.get_host("core-sw1").get_vars()
        self.assertEqual(host_vars["libre_env"], "prod")
        self.assertIn("type_network", inventory.groups)


class TestCaching(LibrenmsInventoryTestCase):
    def test_second_parse_uses_cache_without_hitting_api(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            first_call_log = []
            self.build_plugin(
                call_log=first_call_log,
                cache=True,
                cache_plugin="jsonfile",
                cache_connection=cache_dir,
                cache_timeout=3600,
            )
            self.assertTrue(first_call_log, "expected the first run to hit the fake API")

            second_call_log = []
            _, inventory = self.build_plugin(
                call_log=second_call_log,
                cache=True,
                cache_plugin="jsonfile",
                cache_connection=cache_dir,
                cache_timeout=3600,
            )

            self.assertEqual(second_call_log, [], "second run should be served entirely from cache")
            self.assertEqual(set(inventory.hosts.keys()), {"core-sw1", "core-sw2"})

    def test_cache_force_update_bypasses_cache(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            self.build_plugin(
                cache=True,
                cache_plugin="jsonfile",
                cache_connection=cache_dir,
                cache_timeout=3600,
            )

            forced_call_log = []
            self.build_plugin(
                call_log=forced_call_log,
                cache=True,
                cache_plugin="jsonfile",
                cache_connection=cache_dir,
                cache_timeout=3600,
                cache_force_update=True,
            )

            self.assertTrue(forced_call_log, "cache_force_update should bypass the cache")


class TestApiTokenTemplating(LibrenmsInventoryTestCase):
    # Exercises _resolve_api_token() directly rather than through a full parse(), since
    # simulating real --extra-vars CLI plumbing (ansible.context.CLIARGS) is fragile and
    # process-global (load_extra_vars() memoizes its result on the function object for
    # the lifetime of the process).

    def _prepare(self, api_token):
        plugin = get_plugin_instance()
        plugin.loader = DataLoader()
        config_path = write_config(api_token=api_token)
        self._configs.append(config_path)
        plugin._read_config_data(path=config_path)
        return plugin

    def test_plain_token_passes_through_unchanged(self):
        plugin = self._prepare("plain-token-value")
        plugin._vars = {}

        self.assertEqual(plugin._resolve_api_token(), "plain-token-value")

    def test_jinja_token_is_templated_against_vars(self):
        plugin = self._prepare("{{ my_vaulted_token }}")
        plugin._vars = {"my_vaulted_token": "resolved-secret"}

        self.assertEqual(plugin._resolve_api_token(), "resolved-secret")


if __name__ == "__main__":
    unittest.main()
