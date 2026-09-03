import io
import json
import os
import tempfile
import unittest
import urllib.error
import uuid
from unittest import mock

from ansible.errors import AnsibleError
from ansible.inventory.data import InventoryData
from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import inventory_loader
from ansible.utils.display import Display

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


class RawResponse:
    """Route marker: respond with an arbitrary byte string instead of a JSON-encoded
    fixture, so tests can simulate a 200 OK with a non-JSON or non-UTF-8 body."""

    def __init__(self, body):
        self._body = body

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


def make_open_url(routes=None, call_log=None, request_log=None):
    routes = routes or DEFAULT_ROUTES

    def _open_url(url, headers=None, timeout=None, validate_certs=None):
        if call_log is not None:
            call_log.append(url)
        if request_log is not None:
            request_log.append({"url": url, "headers": headers, "timeout": timeout})
        for suffix in sorted(routes, key=len, reverse=True):
            if url.endswith(suffix) or suffix in url:
                route = routes[suffix]
                if isinstance(route, HttpErrorRoute):
                    route.raise_for(url)
                if isinstance(route, RawResponse):
                    return route
                return FakeResponse(load_fixture(route))
        raise AssertionError(f"Unexpected URL requested: {url}")

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
        lines.append(f"{key}: {json.dumps(value)}")
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

    def build_plugin(self, routes=None, call_log=None, request_log=None, **config_overrides):
        config_path = write_config(**config_overrides)
        self._configs.append(config_path)

        plugin = get_plugin_instance()
        modname = type(plugin).__module__
        inventory = InventoryData()
        loader = DataLoader()

        with mock.patch(modname + ".open_url", make_open_url(routes, call_log, request_log)):
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

    def test_libre_prefixed_vars_are_set(self):
        _, inventory = self.build_plugin()

        host_vars = inventory.get_host("core-sw1").get_vars()
        self.assertEqual(host_vars["libre_hardware"], "C9300")
        self.assertEqual(host_vars["libre_hostname"], "core-sw1.example.com")

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

    def test_notes_field_ignored_by_default(self):
        routes = dict(DEFAULT_ROUTES)
        routes["/devices"] = "devices_notes.json"

        _, inventory = self.build_plugin(routes=routes)

        host_vars = inventory.get_host("custom-sw1").get_vars()
        self.assertNotIn("var1", host_vars)
        self.assertNotIn("var2", host_vars)
        self.assertNotIn("var3", host_vars)

    def test_parse_notes_field(self):
        routes = dict(DEFAULT_ROUTES)
        routes["/devices"] = "devices_notes.json"

        _, inventory = self.build_plugin(routes=routes, parse_notes_field=True)

        host_vars = inventory.get_host("custom-sw1").get_vars()
        self.assertTrue(host_vars["var1"])
        self.assertFalse(host_vars["var2"])
        self.assertEqual(host_vars["var3"], "test")

    def test_notes_no_parse_without_leading_yaml_prefix(self):
        routes = dict(DEFAULT_ROUTES)
        routes["/devices"] = "devices_notes.json"

        _, inventory = self.build_plugin(routes=routes, parse_notes_field=True)

        host_vars = inventory.get_host("custom-sw2").get_vars()
        self.assertIsNone(host_vars.get("var1"))
        self.assertIsNone(host_vars.get("var2"))
        self.assertIsNone(host_vars.get("var3"))

    def test_notes_no_parse_non_dict(self):
        routes = dict(DEFAULT_ROUTES)
        routes["/devices"] = "devices_notes.json"

        _, inventory = self.build_plugin(routes=routes, parse_notes_field=True)

        host_vars = inventory.get_host("custom-fw1").get_vars()
        self.assertIsNone(host_vars.get("hello"))

    def test_notes_field_supports_nested_lists_and_dicts(self):
        routes = dict(DEFAULT_ROUTES)
        routes["/devices"] = "devices_notes.json"

        _, inventory = self.build_plugin(routes=routes, parse_notes_field=True)

        host_vars = inventory.get_host("custom-sw3").get_vars()
        self.assertEqual(host_vars["a_list"], ["one", "two"])
        self.assertEqual(host_vars["a_dict"], {"nested_key": "nested_value"})

    def test_notes_field_missing_does_not_crash(self):
        routes = dict(DEFAULT_ROUTES)
        routes["/devices"] = "devices_notes.json"

        _, inventory = self.build_plugin(routes=routes, parse_notes_field=True)

        host_vars = inventory.get_host("custom-sw4").get_vars()
        self.assertEqual(host_vars["libre_hardware"], "C9300")

    def test_notes_field_tolerates_trailing_whitespace_on_marker_line(self):
        routes = dict(DEFAULT_ROUTES)
        routes["/devices"] = "devices_notes.json"

        _, inventory = self.build_plugin(routes=routes, parse_notes_field=True)

        host_vars = inventory.get_host("custom-sw5").get_vars()
        self.assertTrue(host_vars["var1"])


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

class TestHardwareTrimming(LibrenmsInventoryTestCase):
    # Keyed on the LibreNMS os rather than a vendor, since os is always populated and
    # already separates Catalyst from Nexus, whose product IDs need different patterns.
    PATTERNS = {
        "ios": [r"^WS-(C\d+[A-Z]*)", r"^C\d+[A-Z]*"],
        "iosxe": [r"^ASR-?\d+", r"^ISR\d+", r"^C\d+[A-Z]*"],
        "nxos": [r"^[NC]?\dK-C\d+[A-Z]*"],
    }

    def host_vars(self, patterns=None, **overrides):
        """Run the plugin over the hardware fixture and return {hostname: host vars}"""
        routes = dict(DEFAULT_ROUTES)
        routes["/devices"] = "devices_hardware.json"
        if patterns is not None:
            overrides["hardware_trimming_patterns"] = patterns

        _, inventory = self.build_plugin(routes=routes, **overrides)

        return {
            hostname: inventory.get_host(hostname).get_vars()
            for hostname in inventory.hosts
        }

    def collect(self, variable, patterns=None, **overrides):
        """{hostname: <variable>} over the hardware fixture, hosts without it omitted"""
        return {
            hostname: host_vars[variable]
            for hostname, host_vars in self.host_vars(patterns, **overrides).items()
            if variable in host_vars
        }

    def families(self, patterns=None, **overrides):
        return self.collect("libre_hardware_family", patterns, **overrides)

    def variants(self, patterns=None, **overrides):
        return self.collect("libre_hardware_variant", patterns, **overrides)

    # --- libre_hardware is never modified ---------------------------------

    def test_libre_hardware_always_holds_the_api_value(self):
        reported = {
            "core-sw1": "C9300-48P",
            "core-sw2": "N9K-C93180YC-EX",
            "core-sw3": "WS-C3850-24T",
            "core-sw4": "ASR-920-24SZ-M",
            "core-sw5": "ISR4321/K9",
            "core-sw6": "C9200CX-12P-2X2G",
            "core-sw7": None,
            "edge-sw1": "EX4300-48T",
        }

        self.assertEqual(self.collect("libre_hardware"), reported)
        self.assertEqual(self.collect("libre_hardware", self.PATTERNS), reported)
        self.assertEqual(
            self.collect("libre_hardware", self.PATTERNS, lowercase_hardware_family=True),
            reported,
        )

    def test_product_id_survives_a_pattern_that_drops_a_prefix(self):
        # C3850 + 24T cannot be recombined into WS-C3850-24T, so libre_hardware is the
        # only place the full product ID exists
        host_vars = self.host_vars(self.PATTERNS)["core-sw3"]

        self.assertEqual(host_vars["libre_hardware"], "WS-C3850-24T")
        self.assertEqual(host_vars["libre_hardware_family"], "C3850")
        self.assertEqual(host_vars["libre_hardware_variant"], "24T")

    # --- libre_hardware_family --------------------------------------------

    def test_each_os_is_trimmed_with_its_own_patterns(self):
        self.assertEqual(
            self.families(self.PATTERNS),
            {
                "core-sw1": "C9300",
                "core-sw2": "N9K-C93180YC",
                "core-sw3": "C3850",
                "core-sw4": "ASR-920",
                "core-sw5": "ISR4321",
                "core-sw6": "C9200CX",
                # junos has no patterns, so the family is the full product ID
                "edge-sw1": "EX4300-48T",
            },
        )

    def test_first_matching_pattern_wins(self):
        families = self.families({"iosxe": [r"^(C9)\d+", r"^C\d+[A-Z]*"]})

        self.assertEqual(families["core-sw1"], "C9")

    def test_os_key_is_matched_case_insensitively(self):
        families = self.families({"IOSXE": [r"^C\d+[A-Z]*"]})

        self.assertEqual(families["core-sw1"], "C9300")

    def test_non_cisco_hardware_can_be_trimmed(self):
        families = self.families({"junos": [r"^EX\d+"]})

        self.assertEqual(families["edge-sw1"], "EX4300")

    def test_family_falls_back_to_the_reported_value_when_nothing_matches(self):
        families = self.families({"junos": [r"^SRX\d+"]})

        self.assertEqual(families["edge-sw1"], "EX4300-48T")

    def test_no_derived_variables_without_patterns(self):
        self.assertEqual(self.families(), {})
        self.assertEqual(self.variants(), {})

    def test_no_derived_variables_when_hardware_is_missing(self):
        host_vars = self.host_vars(self.PATTERNS)["core-sw7"]

        self.assertIsNone(host_vars["libre_hardware"])
        self.assertNotIn("libre_hardware_family", host_vars)
        self.assertNotIn("libre_hardware_variant", host_vars)

    def test_no_derived_variables_when_hardware_is_excluded(self):
        host_vars = self.host_vars(self.PATTERNS, exclude_fields=["hardware"])["core-sw1"]

        self.assertNotIn("libre_hardware", host_vars)
        self.assertNotIn("libre_hardware_family", host_vars)
        self.assertNotIn("libre_hardware_variant", host_vars)

    # --- libre_hardware_variant -------------------------------------------

    def test_variant_holds_what_the_pattern_left_behind(self):
        self.assertEqual(
            self.variants(self.PATTERNS),
            {
                "core-sw1": "48P",
                "core-sw2": "EX",
                "core-sw3": "24T",
                "core-sw4": "24SZ-M",
                "core-sw5": "K9",
                "core-sw6": "12P-2X2G",
            },
        )

    def test_variant_is_unset_when_the_pattern_consumes_everything(self):
        variants = self.variants({"iosxe": [r"^C\d+[A-Z]*-\d+P$"]})

        self.assertNotIn("core-sw1", variants)

    def test_variant_is_unset_when_no_pattern_matches(self):
        variants = self.variants({"junos": [r"^SRX\d+"]})

        self.assertNotIn("edge-sw1", variants)

    # --- lowercase_hardware_family ----------------------------------------

    def test_lowercase_applies_to_the_derived_variables(self):
        host_vars = self.host_vars(self.PATTERNS, lowercase_hardware_family=True)["core-sw1"]

        self.assertEqual(host_vars["libre_hardware_family"], "c9300")
        self.assertEqual(host_vars["libre_hardware_variant"], "48p")

    def test_lowercase_applies_to_untrimmed_families(self):
        # junos has no patterns, so without this it would land in a differently-cased
        # group from the devices that were trimmed
        families = self.families(self.PATTERNS, lowercase_hardware_family=True)

        self.assertEqual(families["edge-sw1"], "ex4300-48t")

    def test_lowercase_has_no_effect_without_patterns(self):
        self.assertEqual(self.families(lowercase_hardware_family=True), {})

    # --- option validation -------------------------------------------------

    def test_invalid_pattern_is_rejected(self):
        bad_pattern = r"^C(9\d+"

        with self.assertRaises(AnsibleError) as raised:
            self.host_vars({"ios": [r"^C\d+", bad_pattern]})

        message = str(raised.exception)
        self.assertIn("invalid regular expression", message)
        # the offending pattern must be named, otherwise it is unfindable in a big config
        self.assertIn(repr(bad_pattern), message)

    def test_patterns_must_be_a_list(self):
        with self.assertRaises(AnsibleError) as raised:
            self.host_vars({"ios": r"^C\d+"})

        self.assertIn("must be a list", str(raised.exception))


class TestGrouping(LibrenmsInventoryTestCase):
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


class TestIsFlagSet(unittest.TestCase):
    def test_none_is_not_set(self):
        self.assertFalse(get_plugin_instance()._is_flag_set(None))

    def test_zero_and_empty_string_are_not_set(self):
        plugin = get_plugin_instance()
        self.assertFalse(plugin._is_flag_set("0"))
        self.assertFalse(plugin._is_flag_set(""))

    def test_one_is_set(self):
        plugin = get_plugin_instance()
        self.assertTrue(plugin._is_flag_set("1"))
        self.assertTrue(plugin._is_flag_set(1))


class TestDeviceQueryParams(LibrenmsInventoryTestCase):
    def test_device_status_filter_adds_type_query_param(self):
        call_log = []
        self.build_plugin(call_log=call_log, device_status_filter="up")

        devices_calls = [u for u in call_log if "/devices" in u and "/devicegroups" not in u]
        self.assertTrue(any(u.endswith("/devices?type=up") for u in devices_calls), devices_calls)

    def test_device_status_filter_all_omits_query_param(self):
        call_log = []
        self.build_plugin(call_log=call_log, device_status_filter="all")

        devices_calls = [u for u in call_log if "/devices" in u and "/devicegroups" not in u]
        self.assertTrue(any(u.endswith("/devices") for u in devices_calls), devices_calls)
        self.assertFalse(any("type=" in u for u in devices_calls), devices_calls)

    def test_query_filters_appended_to_devices_url(self):
        call_log = []
        self.build_plugin(call_log=call_log, query_filters=["os=ios"])

        devices_calls = [u for u in call_log if "/devices" in u and "/devicegroups" not in u]
        self.assertTrue(any("os=ios" in u for u in devices_calls), devices_calls)

    def test_device_status_filter_and_query_filters_combine(self):
        call_log = []
        self.build_plugin(call_log=call_log, device_status_filter="up", query_filters=["os=ios"])

        devices_calls = [u for u in call_log if "/devices" in u and "/devicegroups" not in u]
        self.assertTrue(
            any(u.endswith("/devices?type=up&os=ios") for u in devices_calls), devices_calls
        )


class TestResponseParsingErrors(LibrenmsInventoryTestCase):
    def test_non_json_200_response_raises(self):
        routes = dict(DEFAULT_ROUTES)
        routes["/devices"] = RawResponse(b"<html>not json</html>")

        with self.assertRaises(AnsibleError):
            self.build_plugin(routes=routes)


class TestHostnameDerivation(LibrenmsInventoryTestCase):
    def test_hostname_field_option_selects_custom_field(self):
        _, inventory = self.build_plugin(hostname_field="serial")

        self.assertIn("ABC123", inventory.hosts)
        self.assertIn("ABC124", inventory.hosts)

    def test_hostname_field_falls_back_to_uuid_when_empty(self):
        _, inventory = self.build_plugin(
            hostname_field="notes",
            exclude_disabled=False,
            exclude_ignored=False,
            host_name_regex_filter=["^old-switch$"],
        )

        self.assertEqual(len(inventory.hosts), 1)
        (hostname,) = inventory.hosts.keys()
        self.assertNotEqual(hostname, "")
        uuid.UUID(hostname)  # raises ValueError if not a valid uuid string

    def test_falls_back_to_hostname_when_sysname_missing(self):
        routes = dict(DEFAULT_ROUTES)
        routes["/devices"] = "devices_hostname_fallback.json"

        _, inventory = self.build_plugin(routes=routes, device_groups_as_ansible_groups=False)

        self.assertIn("hostname-only.example.com", inventory.hosts)

    def test_falls_back_to_uuid_when_sysname_and_hostname_missing(self):
        routes = dict(DEFAULT_ROUTES)
        routes["/devices"] = "devices_hostname_fallback.json"

        _, inventory = self.build_plugin(routes=routes, device_groups_as_ansible_groups=False)

        other_host = "hostname-only.example.com"
        (fallback_host,) = [h for h in inventory.hosts if h != other_host]
        uuid.UUID(fallback_host)  # raises ValueError if not a valid uuid string


class TestGroupingToggle(LibrenmsInventoryTestCase):
    def test_disabling_device_groups_skips_devicegroups_endpoint(self):
        call_log = []
        _, inventory = self.build_plugin(call_log=call_log, device_groups_as_ansible_groups=False)

        self.assertFalse(any("/devicegroups" in u for u in call_log), call_log)
        self.assertEqual(set(inventory.groups.keys()), {"all", "ungrouped"})


class TestRequestConstruction(LibrenmsInventoryTestCase):
    def test_custom_headers_are_merged_with_auth_token(self):
        request_log = []
        self.build_plugin(request_log=request_log, headers={"X-Extra": "value"}, timeout=45)

        devices_requests = [r for r in request_log if r["url"].endswith("/devices")]
        self.assertTrue(devices_requests)
        headers = devices_requests[0]["headers"]
        self.assertEqual(headers["X-Auth-Token"], "test-token")
        self.assertEqual(headers["X-Extra"], "value")
        self.assertEqual(devices_requests[0]["timeout"], 45)


class TestDisplayMessages(LibrenmsInventoryTestCase):
    """The plugin narrates each step at -vvvv and reports degraded runs at -v, so a user
    can follow what it did without reading the source."""

    def capture_display(self, routes=None, **config_overrides):
        """Run the plugin and collect what it sent to each Display level

        Returns:
            (steps, problems, warnings): The -vvvv, -v and warning messages, in order
        """
        # Display is a singleton shared by every plugin instance, so patching the class
        # captures whatever the plugin emits regardless of verbosity.
        steps, problems, warnings = [], [], []
        with mock.patch.object(Display, "vvvv", steps.append), \
                mock.patch.object(Display, "v", problems.append), \
                mock.patch.object(Display, "warning", warnings.append):
            self.build_plugin(routes=routes, **config_overrides)

        return steps, problems, warnings

    def test_every_stage_of_a_run_is_reported_at_vvvv(self):
        steps, _, _ = self.capture_display()
        joined = "\n".join(steps)

        for expected in (
            "reading inventory source",
            "applying options",
            "api endpoint is",
            "resolving the api token",
            "populating the inventory",
            "fetching devices",
            "fetching device groups",
            "adding host core-sw1",
            "setting host vars for core-sw1",
            "adding core-sw1 to group Core",
            "done",
        ):
            self.assertIn(expected, joined)

    def test_step_messages_are_prefixed_so_they_can_be_grepped(self):
        steps, _, _ = self.capture_display()

        self.assertTrue(steps)
        self.assertTrue(all(m.startswith("[librenms] ") for m in steps), steps)

    def test_excluded_devices_say_why_they_were_skipped(self):
        steps, _, _ = self.capture_display()
        joined = "\n".join(steps)

        self.assertIn("skipping \u00e9dge-fw1, it is disabled", joined)
        self.assertIn("skipping old-switch, it is ignored", joined)

    def test_filtering_every_device_out_is_reported_at_v(self):
        _, problems, _ = self.capture_display(host_name_regex_filter=["^nothing-matches$"])

        self.assertTrue(any("every device was filtered out" in m for m in problems), problems)

    def test_group_filter_matching_nothing_is_reported_at_v(self):
        _, problems, _ = self.capture_display(group_name_regex_filter=["^nothing-matches$"])

        self.assertTrue(
            any("matched none of the device groups" in m for m in problems), problems
        )

    def test_empty_device_list_is_reported_at_v(self):
        routes = dict(DEFAULT_ROUTES)
        routes["/devices"] = HttpErrorRoute(
            404, payload={"status": "error", "message": "No devices found"}
        )

        _, problems, _ = self.capture_display(routes=routes)

        self.assertTrue(any("returned no devices" in m for m in problems), problems)

    def test_generated_hostname_is_reported_at_v(self):
        _, problems, _ = self.capture_display(hostname_field="does_not_exist")

        self.assertTrue(
            any("has no does_not_exist, naming it" in m for m in problems), problems
        )

    def test_unresolved_api_token_is_reported_at_v(self):
        _, problems, _ = self.capture_display(api_token="{{ never_defined }}")

        self.assertTrue(
            any("api_token did not resolve" in m for m in problems), problems
        )


class TestErrorMessages(LibrenmsInventoryTestCase):
    """Failures that stop the run name the option the user should look at."""

    def test_unreachable_endpoint_names_the_options_to_check(self):
        plugin = get_plugin_instance()
        modname = type(plugin).__module__

        def unreachable(url, **kwargs):
            raise urllib.error.URLError("Name or service not known")

        config_path = write_config()
        self._configs.append(config_path)

        with mock.patch(modname + ".open_url", unreachable), self.assertRaises(AnsibleError) as raised:
            plugin.parse(InventoryData(), DataLoader(), config_path, cache=False)

        message = str(raised.exception)
        self.assertIn("Could not reach the LibreNMS API", message)
        self.assertIn("api_endpoint", message)

    def test_rejected_token_names_the_token_options(self):
        routes = dict(DEFAULT_ROUTES)
        routes["/devices"] = HttpErrorRoute(
            401, payload={"status": "error", "message": "Unauthenticated"}
        )

        with self.assertRaises(AnsibleError) as raised:
            self.build_plugin(routes=routes)

        message = str(raised.exception)
        self.assertIn("rejected the API token", message)
        self.assertIn("LIBRENMS_TOKEN", message)

    def test_non_json_body_error_names_the_endpoint_option(self):
        routes = dict(DEFAULT_ROUTES)
        routes["/devices"] = RawResponse(b"<html>not json</html>")

        with self.assertRaises(AnsibleError) as raised:
            self.build_plugin(routes=routes)

        self.assertIn("api_endpoint", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
