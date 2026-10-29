import io
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.error
import uuid
from unittest import mock

import yaml
from ansible.errors import AnsibleError
from ansible.inventory.data import InventoryData
from ansible.inventory.manager import InventoryManager
from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import inventory_loader
from ansible.template import Templar
from ansible.utils.display import Display
from ansible.vars.manager import VariableManager

# Private, so guarded: a core that renames either of these should skip the one test that
# needs them rather than fail to import the whole module. In a real run InventoryManager
# wraps InventoryData in this and tags every value set through it, which is the only way
# to reproduce the value types a snapshot has to be able to serialize.
try:
    from ansible._internal._datatag._tags import Origin
    from ansible.inventory.manager import _InventoryDataWrapper
except ImportError:  # pragma: no cover
    Origin = _InventoryDataWrapper = None

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


class _SnapshotLoader(yaml.SafeLoader):
    """SafeLoader that reads the !unsafe tag the snapshot writes as the plain value.
    Whether Ansible itself reads it back as untrusted is tested through InventoryManager."""


def _construct_unsafe(loader, node):
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_scalar(node)


_SnapshotLoader.add_constructor("!unsafe", _construct_unsafe)


def load_snapshot(path):
    with open(path) as handle:
        return yaml.load(handle, Loader=_SnapshotLoader)


def read_generated(path):
    """Parse a generated snapshot with Ansible's own yaml inventory plugin

    Going through the real plugin is the point: it is what a user pointing `ansible -i`
    at the file gets, so it proves the file is a usable inventory rather than merely
    well-formed YAML.

    Returns:
        InventoryData: The inventory the file reads back as, reconciled
    """
    plugin = inventory_loader.get("yaml")
    inventory = InventoryData()
    plugin.parse(inventory, DataLoader(), path, cache=False)
    inventory.reconcile_inventory()
    return inventory


# The LibreNMS group "Network Core" has a space in its name, so it reaches the API
# URL-quoted and lands in the inventory as the group "Network_Core".
NETWORK_CORE_ROUTE = "/devicegroups/Network%20Core"

DEFAULT_ROUTES = {
    "/devices": "devices.json",
    "/devicegroups/Core": "devicegroups_core.json",
    "/devicegroups/Edge": "devicegroups_edge.json",
    "/devicegroups/Decommissioned": "devicegroups_decommissioned.json",
    NETWORK_CORE_ROUTE: "devicegroups_network_core.json",
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


def write_config(config_dir=None, **overrides):
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
        mode="w", suffix=".yml", delete=False, dir=config_dir or tempfile.gettempdir()
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

    def build_plugin(
        self,
        routes=None,
        call_log=None,
        request_log=None,
        config_dir=None,
        wrap_inventory=False,
        **config_overrides,
    ):
        config_path = write_config(config_dir=config_dir, **config_overrides)
        self._configs.append(config_path)
        # Kept so a test can assert on, or point an option at, the inventory source file
        self.last_config = config_path

        plugin = get_plugin_instance()
        modname = type(plugin).__module__
        inventory = InventoryData()
        loader = DataLoader()

        # A real run gets InventoryManager's wrapper rather than a bare InventoryData, and
        # the wrapper tags every value the plugin sets. Tests that care what the host vars
        # actually are at write time ask for it.
        target = inventory
        if wrap_inventory:
            target = _InventoryDataWrapper(
                inventory, target_plugin=plugin, origin=Origin(description="<test>")
            )

        with mock.patch(modname + ".open_url", make_open_url(routes, call_log, request_log)):
            plugin.parse(target, loader, config_path, cache=True)
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


class TestLocationParsing(LibrenmsInventoryTestCase):
    def test_location_field_ignored_by_default(self):
        routes = dict(DEFAULT_ROUTES)
        routes["/devices"] = "devices_locations.json"

        _, inventory = self.build_plugin(routes=routes)

        host_vars = inventory.get_host("loc-site-only").get_vars()
        self.assertNotIn("libre_location_site", host_vars)
        self.assertEqual(host_vars["libre_location"], "DC1")

    def test_site_only_location_is_the_minimal_valid_form(self):
        routes = dict(DEFAULT_ROUTES)
        routes["/devices"] = "devices_locations.json"

        _, inventory = self.build_plugin(routes=routes, parse_location_field=True)

        host_vars = inventory.get_host("loc-site-only").get_vars()
        self.assertEqual(host_vars["libre_location_site"], "DC1")
        self.assertNotIn("libre_location_room", host_vars)
        self.assertNotIn("libre_location_positions", host_vars)

    def test_site_and_room(self):
        routes = dict(DEFAULT_ROUTES)
        routes["/devices"] = "devices_locations.json"

        _, inventory = self.build_plugin(routes=routes, parse_location_field=True)

        host_vars = inventory.get_host("loc-site-room").get_vars()
        self.assertEqual(host_vars["libre_location_site"], "DC1")
        self.assertEqual(host_vars["libre_location_room"], "Room2")
        self.assertNotIn("libre_location_positions", host_vars)

    def test_bare_rack_is_recorded_without_a_unit(self):
        # DC1;Room2;RackA;RackB;u5;RackC - RackA and RackC never get a unit spec before
        # being superseded (or the value ending), RackB does.
        routes = dict(DEFAULT_ROUTES)
        routes["/devices"] = "devices_locations.json"

        _, inventory = self.build_plugin(routes=routes, parse_location_field=True)

        host_vars = inventory.get_host("loc-bare-racks").get_vars()
        self.assertEqual(
            host_vars["libre_location_positions"],
            [{"rack": "RackA"}, {"rack": "RackB", "unit": 5}, {"rack": "RackC"}],
        )

    def test_stack_members_across_racks_with_stack_numbers(self):
        # DC1;;RackA;1u10;2u11;RackB;u5 - two stack members in RackA, a third in RackB
        # with no stack number given.
        routes = dict(DEFAULT_ROUTES)
        routes["/devices"] = "devices_locations.json"

        _, inventory = self.build_plugin(routes=routes, parse_location_field=True)

        host_vars = inventory.get_host("loc-stack").get_vars()
        self.assertEqual(host_vars["libre_location_site"], "DC1")
        self.assertNotIn("libre_location_room", host_vars)
        self.assertEqual(
            host_vars["libre_location_positions"],
            [
                {"rack": "RackA", "stack_nr": 1, "unit": 10},
                {"rack": "RackA", "stack_nr": 2, "unit": 11},
                {"rack": "RackB", "unit": 5},
            ],
        )

    def test_unit_without_a_rack(self):
        routes = dict(DEFAULT_ROUTES)
        routes["/devices"] = "devices_locations.json"

        _, inventory = self.build_plugin(routes=routes, parse_location_field=True)

        host_vars = inventory.get_host("loc-unit-no-rack").get_vars()
        self.assertEqual(host_vars["libre_location_positions"], [{"unit": 5}])

    def test_blank_site_is_skipped_with_a_warning(self):
        # ;Room2;RackA;u5 - no site, so the whole value is skipped rather than guessing.
        routes = dict(DEFAULT_ROUTES)
        routes["/devices"] = "devices_locations.json"

        _, inventory = self.build_plugin(routes=routes, parse_location_field=True)

        host_vars = inventory.get_host("loc-blank-site").get_vars()
        self.assertNotIn("libre_location_site", host_vars)
        self.assertNotIn("libre_location_positions", host_vars)
        # libre_location itself is untouched even when parsing is skipped.
        self.assertEqual(host_vars["libre_location"], ";Room2;RackA;u5")

    def test_non_string_location_is_skipped_without_crashing(self):
        routes = dict(DEFAULT_ROUTES)
        routes["/devices"] = "devices_locations.json"

        _, inventory = self.build_plugin(routes=routes, parse_location_field=True)

        host_vars = inventory.get_host("loc-non-string").get_vars()
        self.assertNotIn("libre_location_site", host_vars)
        self.assertEqual(host_vars["libre_location"], 12345)

    def test_empty_location_sets_no_derived_vars(self):
        routes = dict(DEFAULT_ROUTES)
        routes["/devices"] = "devices_locations.json"

        _, inventory = self.build_plugin(routes=routes, parse_location_field=True)

        host_vars = inventory.get_host("loc-empty").get_vars()
        self.assertNotIn("libre_location_site", host_vars)
        self.assertEqual(host_vars["libre_location"], "")


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
            "edge-fw1": "node0 Juniper SRX340 Internet Router",
            "edge-fw2": "Juniper SRX340 Internet Router",
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
                "edge-fw1": "node0 Juniper SRX340 Internet Router",
                "edge-fw2": "Juniper SRX340 Internet Router",
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

    # --- named groups -----------------------------------------------------

    SRX_PATTERNS = {"junos": [r"(?:(?P<variant>node\d+) )?Juniper (?P<family>SRX\d+)"]}

    def test_named_groups_take_a_variant_that_precedes_the_family(self):
        host_vars = self.host_vars(self.SRX_PATTERNS, lowercase_hardware_family=True)["edge-fw1"]

        self.assertEqual(host_vars["libre_hardware_family"], "srx340")
        self.assertEqual(host_vars["libre_hardware_variant"], "node0")

    def test_variant_is_unset_when_its_named_group_does_not_take_part(self):
        host_vars = self.host_vars(self.SRX_PATTERNS)["edge-fw2"]

        self.assertEqual(host_vars["libre_hardware_family"], "SRX340")
        self.assertNotIn("libre_hardware_variant", host_vars)

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

    def test_group_name_whitespace_becomes_underscores(self):
        _, inventory = self.build_plugin(group_name_regex_filter=["^Network Core$"])

        self.assertIn("Network_Core", inventory.groups)
        self.assertEqual(
            [h.name for h in inventory.groups["Network_Core"].get_hosts()], ["core-sw1"]
        )

    def test_group_name_is_url_quoted_when_fetching_members(self):
        call_log = []
        self.build_plugin(call_log=call_log, group_name_regex_filter=["^Network Core$"])

        # Raw whitespace in the path would make the HTTP request itself invalid
        self.assertTrue(any(url.endswith(NETWORK_CORE_ROUTE) for url in call_log), call_log)

    def test_every_character_ansible_rejects_is_converted(self):
        routes = dict(DEFAULT_ROUTES)
        routes["/devicegroups"] = "devicegroups_unsafe_names.json"
        for quoted in ("%20Site%201%20", "Site-2", "1st%20Floor", "Core%20%26%20Dist", "10.0.0.0%2F8"):
            routes["/devicegroups/" + quoted] = "devicegroups_core.json"

        _, inventory = self.build_plugin(routes=routes)

        self.assertEqual(
            set(inventory.groups) - {"all", "ungrouped"},
            {
                "Site_1",        # " Site 1 " - padding dropped, not turned into underscores
                "Site_2",        # "-" is rejected just like whitespace is
                "_1st_Floor",    # a leading digit is prefixed, never overwritten
                "Core___Dist",   # each rejected character becomes one underscore
                "_10_0_0_0_8",
            },
        )

    def test_whitespace_only_group_name_is_skipped(self):
        routes = dict(DEFAULT_ROUTES)
        routes["/devicegroups"] = "devicegroups_blank_name.json"
        routes["/devicegroups/%20%20"] = "devicegroups_core.json"

        _, inventory = self.build_plugin(routes=routes)

        # Nothing is left to name a group with, and an empty name would fail the run
        self.assertEqual(set(inventory.groups.keys()), {"all", "ungrouped"})
        self.assertIn("core-sw1", inventory.hosts)

    def test_names_that_convert_to_the_same_group_are_merged(self):
        routes = dict(DEFAULT_ROUTES)
        routes["/devicegroups"] = "devicegroups_colliding.json"
        routes[NETWORK_CORE_ROUTE] = "devicegroups_core.json"
        routes["/devicegroups/Network-Core"] = "devicegroups_edge.json"

        _, inventory = self.build_plugin(routes=routes)

        # Ansible cannot tell the two apart, so their members end up together - the run
        # warns about it (see TestDisplayMessages) rather than dropping either group
        self.assertEqual(set(inventory.groups) - {"all", "ungrouped"}, {"Network_Core"})
        self.assertEqual(
            {h.name for h in inventory.groups["Network_Core"].get_hosts()},
            {"core-sw1", "core-sw2"},
        )

    def test_group_named_after_an_implicit_group_still_adds_its_members(self):
        routes = dict(DEFAULT_ROUTES)
        routes["/devicegroups"] = "devicegroups_implicit_name.json"
        routes["/devicegroups/ungrouped"] = "devicegroups_core.json"

        _, inventory = self.build_plugin(routes=routes)

        # "ungrouped" is a valid Ansible group name, so it is left alone and merges into
        # the group Ansible makes itself - which the run warns about
        self.assertEqual(set(inventory.groups.keys()), {"all", "ungrouped"})
        self.assertIn("core-sw1", [h.name for h in inventory.groups["ungrouped"].get_hosts()])

    def test_mapped_group_gets_the_name_it_was_mapped_to(self):
        _, inventory = self.build_plugin(group_name_mapping={"Core": "os_iosxe"})

        self.assertIn("os_iosxe", inventory.groups)
        self.assertNotIn("Core", inventory.groups)
        self.assertEqual(
            {h.name for h in inventory.groups["os_iosxe"].get_hosts()}, {"core-sw1", "core-sw2"}
        )

    def test_mapping_applies_to_names_that_would_be_converted(self):
        _, inventory = self.build_plugin(group_name_mapping={"Network Core": "core"})

        # The mapped name is used as written, the conversion never sees it
        self.assertIn("core", inventory.groups)
        self.assertNotIn("Network_Core", inventory.groups)

    def test_mapping_keys_match_the_librenms_name_exactly(self):
        _, inventory = self.build_plugin(
            group_name_mapping={"core": "wrong_case", "Network_Core": "converted_name"}
        )

        # Neither key is a device group name: LibreNMS reports "Core" and "Network Core"
        self.assertEqual(set(inventory.groups) - {"all", "ungrouped"}, {"Core", "Network_Core"})

    def test_unmapped_groups_keep_their_own_name(self):
        _, inventory = self.build_plugin(group_name_mapping={"Core": "os_iosxe"})

        self.assertIn("Network_Core", inventory.groups)

    def test_mapping_is_applied_after_the_regex_filter(self):
        _, inventory = self.build_plugin(
            group_name_regex_filter=["^Core$"], group_name_mapping={"Core": "os_iosxe"}
        )

        # The filter matches the LibreNMS name, the mapping renames what survives it
        self.assertEqual(set(inventory.groups) - {"all", "ungrouped"}, {"os_iosxe"})

    def test_mapping_two_groups_to_one_name_merges_them(self):
        _, inventory = self.build_plugin(
            group_name_mapping={"Core": "cisco", "Network Core": "cisco"}
        )

        self.assertEqual(set(inventory.groups) - {"all", "ungrouped"}, {"cisco"})
        self.assertEqual(
            {h.name for h in inventory.groups["cisco"].get_hosts()}, {"core-sw1", "core-sw2"}
        )

    def test_group_name_regex_filter_matches_the_librenms_name(self):
        # The filter is documented to run against the name as LibreNMS reports it, so a
        # pattern written against the converted name must not match.
        _, inventory = self.build_plugin(group_name_regex_filter=["^Network_Core$"])

        self.assertNotIn("Network_Core", inventory.groups)

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


class TestSnapshotFile(LibrenmsInventoryTestCase):
    """create_snapshot writes the populated inventory out as a static YAML inventory file.
    It sits next to the caching tests deliberately: it is the other feature that leaves
    something on disk, and the one it keeps being mistaken for."""

    def snapshot(self, work_dir, routes=None, call_log=None, name="snap.yml", **overrides):
        """Run the plugin with create_snapshot on, from an inventory source inside work_dir

        Args:
            work_dir (str): Directory to put the inventory source, and the snapshot, in
            name (str): snapshot_file, relative to work_dir unless overridden

        Returns:
            (plugin, inventory, path): The plugin, the inventory and the snapshot's path
        """
        overrides.setdefault("create_snapshot", True)
        overrides.setdefault("snapshot_file", name)
        plugin, inventory = self.build_plugin(
            routes=routes, call_log=call_log, config_dir=work_dir, **overrides
        )
        return plugin, inventory, os.path.join(work_dir, name)

    @staticmethod
    def keys_in_file_order(text, section):
        """The keys the file lists directly under all.<section>, in the order it lists them

        Asserted on the text rather than the parsed document because parsing is exactly
        what throws the ordering away, and the ordering is the point of a file meant to be
        committed and diffed.

        Args:
            text (str): The snapshot file's contents
            section (str): "children" or "hosts"

        Returns:
            list: The keys, in file order
        """
        body = text.split(f"\n  {section}:\n", 1)[1]
        # Stops at the next key of `all`, which is indented two spaces
        body = re.split(r"^  \S", body, maxsplit=1, flags=re.MULTILINE)[0]
        return re.findall(r"^    (\S+):", body, re.MULTILINE)

    @staticmethod
    def host_var_keys_in_file_order(text, hostname):
        """The host var keys the file lists for one host, in the order it lists them"""
        body = text.split(f"\n    {hostname}:\n", 1)[1]
        # Stops at the next host, which is indented four spaces
        body = re.split(r"^    \S", body, maxsplit=1, flags=re.MULTILINE)[0]
        return re.findall(r"^      (\S+):", body, re.MULTILINE)

    # --- what gets written ---

    def test_no_file_is_written_by_default(self):
        with tempfile.TemporaryDirectory() as work:
            self.build_plugin(config_dir=work)

            self.assertEqual(
                os.listdir(work),
                [os.path.basename(self.last_config)],
                "create_snapshot defaults to off, so nothing but the source should be there",
            )

    def test_the_document_is_a_single_top_level_all_group(self):
        with tempfile.TemporaryDirectory() as work:
            _, _, path = self.snapshot(work)
            document = load_snapshot(path)

            self.assertEqual(list(document), ["all"])
            self.assertEqual(sorted(document["all"]), ["children", "hosts"])
            # Ansible's yaml inventory plugin refuses any file carrying a plugin key
            self.assertNotIn("plugin", document)

    def test_hosts_carry_their_host_vars_inline(self):
        with tempfile.TemporaryDirectory() as work:
            _, _, path = self.snapshot(work)
            hosts = load_snapshot(path)["all"]["hosts"]

            self.assertEqual(sorted(hosts), ["core-sw1", "core-sw2"])
            self.assertEqual(hosts["core-sw1"]["libre_hardware"], "C9300")
            self.assertEqual(hosts["core-sw1"]["libre_hostname"], "core-sw1.example.com")

    def test_vars_parsed_out_of_the_notes_field_are_written_too(self):
        routes = dict(DEFAULT_ROUTES)
        routes["/devices"] = "devices_notes.json"

        with tempfile.TemporaryDirectory() as work:
            _, inventory, path = self.snapshot(work, routes=routes, parse_notes_field=True)
            hosts = load_snapshot(path)["all"]["hosts"]

            hostname = next(iter(inventory.hosts))
            expected = {
                key: value
                for key, value in inventory.get_host(hostname).vars.items()
                if key not in ("inventory_file", "inventory_dir")
            }
            self.assertEqual(hosts[hostname], expected)

    def test_group_membership_is_written_as_children_of_all(self):
        with tempfile.TemporaryDirectory() as work:
            _, _, path = self.snapshot(work)
            children = load_snapshot(path)["all"]["children"]

            # Edge's only device is disabled, so exclude_disabled leaves it with no members
            self.assertEqual(sorted(children), ["Core", "Network_Core"])
            self.assertEqual(sorted(children["Core"]["hosts"]), ["core-sw1", "core-sw2"])
            # Membership only - the vars live under all.hosts, once
            self.assertIsNone(children["Core"]["hosts"]["core-sw1"])

    def test_a_host_in_several_groups_is_listed_under_each_of_them(self):
        with tempfile.TemporaryDirectory() as work:
            _, _, path = self.snapshot(work)
            document = load_snapshot(path)

            self.assertIn("core-sw1", document["all"]["children"]["Core"]["hosts"])
            self.assertIn("core-sw1", document["all"]["children"]["Network_Core"]["hosts"])
            # and its vars are not repeated per group
            self.assertIn("libre_hardware", document["all"]["hosts"]["core-sw1"])

    def test_a_host_in_no_group_is_listed_under_all_only(self):
        with tempfile.TemporaryDirectory() as work:
            # old-switch is ignored in LibreNMS and in none of the device groups
            _, _, path = self.snapshot(work, exclude_ignored=False)
            document = load_snapshot(path)

            self.assertIn("old-switch", document["all"]["hosts"])
            for group, entry in document["all"]["children"].items():
                self.assertNotIn("old-switch", entry.get("hosts", {}), f"should not be in {group}")

    def test_implicit_groups_are_not_written_as_groups_of_their_own(self):
        with tempfile.TemporaryDirectory() as work:
            plugin, _, path = self.snapshot(work)
            children = load_snapshot(path)["all"]["children"]

            for implicit in sys.modules[type(plugin).__module__].IMPLICIT_GROUPS:
                self.assertNotIn(implicit, children)

    def test_children_is_omitted_when_grouping_is_off(self):
        with tempfile.TemporaryDirectory() as work:
            _, _, path = self.snapshot(work, device_groups_as_ansible_groups=False)
            document = load_snapshot(path)

            self.assertEqual(list(document["all"]), ["hosts"])

    def test_the_vars_ansible_adds_itself_are_not_written(self):
        with tempfile.TemporaryDirectory() as work:
            _, _, path = self.snapshot(work)
            hosts = load_snapshot(path)["all"]["hosts"]

            for host_vars in hosts.values():
                for implicit in ("inventory_file", "inventory_dir", "inventory_hostname",
                                 "inventory_hostname_short", "group_names"):
                    self.assertNotIn(implicit, host_vars)

    def test_only_the_hosts_this_plugin_added_are_written(self):
        # InventoryData is shared by every source in a run, so a host another source
        # contributed must not end up in this plugin's snapshot
        with tempfile.TemporaryDirectory() as work:
            config_path = write_config(
                config_dir=work, create_snapshot=True, snapshot_file="snap.yml"
            )
            self._configs.append(config_path)

            plugin = get_plugin_instance()
            modname = type(plugin).__module__
            inventory = InventoryData()
            inventory.add_host("a-host-from-another-source")

            with mock.patch(modname + ".open_url", make_open_url()):
                plugin.parse(inventory, DataLoader(), config_path, cache=False)

            hosts = load_snapshot(os.path.join(work, "snap.yml"))["all"]["hosts"]
            self.assertEqual(sorted(hosts), ["core-sw1", "core-sw2"])

    def test_vars_another_source_set_on_a_shared_host_are_not_written(self):
        with tempfile.TemporaryDirectory() as work:
            config_path = write_config(
                config_dir=work, create_snapshot=True, snapshot_file="snap.yml"
            )
            self._configs.append(config_path)

            plugin = get_plugin_instance()
            modname = type(plugin).__module__
            inventory = InventoryData()
            inventory.add_host("core-sw1")
            inventory.set_variable("core-sw1", "ansible_user", "admin")
            inventory.set_variable("core-sw1", "libre_retired_field", "stale")

            with mock.patch(modname + ".open_url", make_open_url()):
                plugin.parse(inventory, DataLoader(), config_path, cache=False)

            host_vars = load_snapshot(os.path.join(work, "snap.yml"))["all"]["hosts"]["core-sw1"]
            self.assertNotIn("ansible_user", host_vars)
            self.assertNotIn("libre_retired_field", host_vars)
            self.assertEqual(host_vars["libre_hardware"], "C9300")

    # --- the file is a usable inventory ---

    def test_the_file_round_trips_to_an_equivalent_inventory(self):
        with tempfile.TemporaryDirectory() as work:
            _, live, path = self.snapshot(work, exclude_ignored=False)
            live.reconcile_inventory()
            reread = read_generated(path)

            def groups_of(inventory):
                return {name: sorted(hosts) for name, hosts in inventory.get_groups_dict().items()}

            def vars_of(inventory):
                return {
                    name: {
                        key: value
                        for key, value in host.vars.items()
                        if key not in ("inventory_file", "inventory_dir")
                    }
                    for name, host in inventory.hosts.items()
                }

            self.assertEqual(groups_of(live), groups_of(reread))
            self.assertEqual(vars_of(live), vars_of(reread))

    @unittest.skipIf(_InventoryDataWrapper is None, "InventoryManager's wrapper moved")
    def test_the_values_a_real_run_produces_can_be_serialized(self):
        # The one test that would catch yaml.safe_dump being used here. A real run gets
        # InventoryManager's wrapper, which tags every value, and safe_dump raises
        # RepresenterError on a tagged str - while the bare InventoryData every other test
        # uses holds plain values that safe_dump handles. Without this, swapping the dumper
        # back passes the whole suite and breaks every user on their first run.
        with tempfile.TemporaryDirectory() as work:
            _, _, path = self.snapshot(work, wrap_inventory=True)

            hosts = load_snapshot(path)["all"]["hosts"]
            self.assertEqual(hosts["core-sw1"]["libre_hardware"], "C9300")
            self.assertEqual(sorted(read_generated(path).hosts), ["core-sw1", "core-sw2"])

    # --- the file is worth committing ---

    def test_the_header_names_the_source_and_warns_against_editing(self):
        with tempfile.TemporaryDirectory() as work:
            _, _, path = self.snapshot(work)
            header = "".join(open(path).readlines()[:2])

            self.assertIn("librenms", header)
            # The basename only, so two checkouts at different paths still match
            self.assertIn(os.path.basename(self.last_config), header)
            self.assertNotIn(work, header)
            self.assertIn("regenerate", header)

    def test_two_runs_over_unchanged_data_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as work:
            _, _, path = self.snapshot(work)
            first = open(path, "rb").read()

            # Same source file, so the same default name and the same header
            plugin = get_plugin_instance()
            modname = type(plugin).__module__
            with mock.patch(modname + ".open_url", make_open_url()):
                plugin.parse(InventoryData(), DataLoader(), self.last_config, cache=False)

            self.assertEqual(open(path, "rb").read(), first, "no timestamp should be written")

    def test_hosts_groups_and_var_keys_are_sorted(self):
        with tempfile.TemporaryDirectory() as work:
            _, _, path = self.snapshot(work, exclude_ignored=False)
            text = open(path).read()

            groups = self.keys_in_file_order(text, "children")
            hosts = self.keys_in_file_order(text, "hosts")
            host_vars = self.host_var_keys_in_file_order(text, "core-sw1")

            self.assertEqual(groups, sorted(groups))
            self.assertEqual(hosts, sorted(hosts))
            self.assertEqual(host_vars, sorted(host_vars))

    def test_an_existing_mode_is_preserved(self):
        with tempfile.TemporaryDirectory() as work:
            _, _, path = self.snapshot(work)
            os.chmod(path, 0o600)

            plugin = get_plugin_instance()
            modname = type(plugin).__module__
            with mock.patch(modname + ".open_url", make_open_url()):
                plugin.parse(InventoryData(), DataLoader(), self.last_config, cache=False)

            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    # --- where the file goes ---

    def test_the_default_path_is_the_hidden_source_name_with_generated_yml(self):
        with tempfile.TemporaryDirectory() as work:
            self.build_plugin(config_dir=work, create_snapshot=True)

            stem = os.path.splitext(os.path.basename(self.last_config))[0]
            expected = os.path.join(work, "." + stem + ".generated.yml")
            self.assertTrue(os.path.exists(expected), os.listdir(work))

    def test_a_directory_source_with_the_default_path_runs_more_than_once(self):
        # The snapshot sits beside the source, so with -i naming the directory a visible
        # one would be parsed as a second source on the next run - and refused as one
        with tempfile.TemporaryDirectory() as work:
            self.build_plugin(config_dir=work, create_snapshot=True)
            plugin = get_plugin_instance()
            modname = type(plugin).__module__

            for _ in range(2):
                with mock.patch(modname + ".open_url", make_open_url()):
                    manager = InventoryManager(DataLoader(), sources=[work])

                parsed = [
                    os.path.basename(source)
                    for source in manager._inventory.processed_sources
                    if source
                ]
                self.assertEqual(parsed, [os.path.basename(self.last_config)])
                self.assertIn("core-sw1", manager.hosts)

    def test_a_symlinked_path_updates_the_link_target_and_keeps_the_link(self):
        with tempfile.TemporaryDirectory() as work:
            real = os.path.join(work, "shared", "snap.yml")
            os.makedirs(os.path.dirname(real))
            os.symlink(real, os.path.join(work, "link.yml"))

            self.snapshot(work, name="link.yml")

            self.assertTrue(os.path.islink(os.path.join(work, "link.yml")))
            self.assertIn("core-sw1", load_snapshot(real)["all"]["hosts"])

    def test_a_relative_path_is_anchored_to_the_inventory_source_directory(self):
        with tempfile.TemporaryDirectory() as work, tempfile.TemporaryDirectory() as elsewhere:
            self.addCleanup(os.chdir, os.getcwd())
            os.chdir(elsewhere)

            _, _, path = self.snapshot(work, name="snap.yml")

            self.assertTrue(os.path.exists(path), "should land beside the inventory source")
            self.assertEqual(os.listdir(elsewhere), [], "and not in the working directory")

    def test_a_missing_directory_in_the_path_is_created(self):
        with tempfile.TemporaryDirectory() as work:
            _, _, path = self.snapshot(work, name=os.path.join("generated", "snap.yml"))

            self.assertTrue(os.path.exists(path))

    def test_a_tilde_is_expanded(self):
        with tempfile.TemporaryDirectory() as work:
            with mock.patch.dict(os.environ, {"HOME": work}):
                self.snapshot(work, name="~/snap.yml")

            self.assertTrue(os.path.exists(os.path.join(work, "snap.yml")))

    def test_an_unset_environment_variable_fails_naming_it(self):
        with tempfile.TemporaryDirectory() as work:
            with mock.patch.dict(os.environ, clear=False) as environ:
                environ.pop("LIBRENMS_UNSET_DIR", None)
                with self.assertRaisesRegex(AnsibleError, r"\$LIBRENMS_UNSET_DIR.*not set"):
                    self.snapshot(work, name="$LIBRENMS_UNSET_DIR/snap.yml")

            self.assertNotIn("$LIBRENMS_UNSET_DIR", os.listdir(work))

    def test_environment_variables_are_expanded(self):
        with tempfile.TemporaryDirectory() as work:
            with mock.patch.dict(os.environ, {"LIBRENMS_SNAPSHOT_DIR": work}):
                self.snapshot(work, name="$LIBRENMS_SNAPSHOT_DIR/snap.yml")

            self.assertTrue(os.path.exists(os.path.join(work, "snap.yml")))

    # --- what it refuses to do ---

    def test_it_refuses_to_write_over_the_inventory_source_file(self):
        # Written by hand rather than through write_config, since snapshot_file has to
        # name the source's own filename and write_config picks a random one
        with tempfile.TemporaryDirectory() as work:
            config_path = os.path.join(work, "librenms.yml")
            with open(config_path, "w") as handle:
                handle.write(
                    "plugin: librenms\n"
                    "api_endpoint: https://fake-librenms.example.com/api/v0\n"
                    "api_token: test-token\n"
                    "cache: false\n"
                    "create_snapshot: true\n"
                    "snapshot_file: librenms.yml\n"
                )
            before = open(config_path, "rb").read()

            plugin = get_plugin_instance()
            modname = type(plugin).__module__
            with mock.patch(modname + ".open_url", make_open_url()), \
                    self.assertRaisesRegex(AnsibleError, "inventory source file"):
                plugin.parse(InventoryData(), DataLoader(), config_path, cache=False)

            self.assertEqual(open(config_path, "rb").read(), before)

    def test_it_refuses_to_write_over_a_file_it_did_not_write(self):
        cases = {
            "hand-written inventory": "all:\n  hosts:\n    web1:\n",
            "plugin configuration": "plugin: constructed\nstrict: false\n",
            "quoted plugin key": '"plugin": constructed\n',
            "flow-style plugin key": "{plugin: constructed, strict: false}\n",
        }
        for case, content in cases.items():
            with self.subTest(case), tempfile.TemporaryDirectory() as work:
                other = os.path.join(work, "other.yml")
                with open(other, "w") as handle:
                    handle.write(content)

                with self.assertRaisesRegex(AnsibleError, "not written by this plugin"):
                    self.snapshot(work, name="other.yml")

                self.assertEqual(open(other).read(), content)

    def test_it_overwrites_a_file_it_wrote_itself_or_an_empty_one(self):
        with tempfile.TemporaryDirectory() as work:
            _, _, path = self.snapshot(work)
            self.snapshot(work)

            empty = os.path.join(work, "empty.yml")
            open(empty, "w").close()
            self.snapshot(work, name="empty.yml")

            self.assertIn("core-sw1", load_snapshot(path)["all"]["hosts"])
            self.assertIn("core-sw1", load_snapshot(empty)["all"]["hosts"])

    def test_it_refuses_a_directory_as_the_target(self):
        with tempfile.TemporaryDirectory() as work:
            os.mkdir(os.path.join(work, "inventory"))

            with self.assertRaisesRegex(AnsibleError, "existing directory"):
                self.snapshot(work, name="inventory")

    def test_the_path_is_vetted_before_any_api_call(self):
        with tempfile.TemporaryDirectory() as work:
            os.mkdir(os.path.join(work, "inventory"))
            call_log = []

            with self.assertRaises(AnsibleError):
                self.snapshot(work, call_log=call_log, name="inventory")

            self.assertEqual(call_log, [], "a path that cannot work should cost no requests")

    def test_a_path_that_cannot_be_written_names_the_option(self):
        with tempfile.TemporaryDirectory() as work:
            # A file where a directory would have to be: makedirs fails with an OSError,
            # which has to surface as an AnsibleError naming the option, not a traceback
            with open(os.path.join(work, "blocker"), "w") as handle:
                handle.write("not a directory\n")

            with self.assertRaisesRegex(AnsibleError, "snapshot_file"):
                self.snapshot(work, name=os.path.join("blocker", "snap.yml"))

    def test_a_value_holding_a_template_reads_back_untemplated(self):
        # A LibreNMS value is untrusted on a live run and never templated; the same value
        # read back out of a static inventory file is trusted and would be evaluated, unless
        # the file marks it !unsafe
        routes = dict(DEFAULT_ROUTES)
        routes["/devices"] = "devices_template.json"
        expected = {
            ("tpl-sw1", "libre_notes"): "owner {{ lookup('env', 'USER') }}",
            ("tpl-sw2", "nested"): {"{{ lookup('env', 'HOME') }}": "key"},
            ("tpl-sw2", "comment"): "{# not a comment #}",
        }

        with tempfile.TemporaryDirectory() as work:
            _, _, path = self.snapshot(work, routes=routes, parse_notes_field=True)
            text = open(path).read()
            self.assertIn("!unsafe", text)

            loader = DataLoader()
            manager = InventoryManager(loader, sources=[path])
            variables = VariableManager(loader=loader, inventory=manager)
            for (hostname, field), value in expected.items():
                host_vars = variables.get_vars(host=manager.get_host(hostname))
                templar = Templar(loader=loader, variables=host_vars)
                self.assertEqual(templar.template(host_vars[field]), value, (hostname, field))

    def test_a_value_with_no_brace_is_not_tagged(self):
        # Ansible resolves an !unsafe scalar's type afresh, so "15.2" would read back as
        # a float - only a value that could be a template is tagged
        with tempfile.TemporaryDirectory() as work:
            _, _, path = self.snapshot(work)

            self.assertNotIn("!unsafe", open(path).read())

    def test_a_new_file_gets_the_mode_the_umask_gives(self):
        with tempfile.TemporaryDirectory() as work:
            previous = os.umask(0o077)
            try:
                _, _, path = self.snapshot(work)
            finally:
                os.umask(previous)

            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    # --- a run that found nothing must not destroy a good snapshot ---

    def test_an_empty_inventory_leaves_an_existing_file_alone(self):
        with tempfile.TemporaryDirectory() as work:
            _, _, path = self.snapshot(work)
            before = open(path, "rb").read()

            self.snapshot(work, host_name_regex_filter=["^nothing-matches-this$"])

            self.assertEqual(open(path, "rb").read(), before)

    def test_an_empty_inventory_writes_no_file_at_all(self):
        with tempfile.TemporaryDirectory() as work:
            _, _, path = self.snapshot(work, host_name_regex_filter=["^nothing-matches-this$"])

            self.assertFalse(os.path.exists(path))


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

    def test_the_resolved_snapshot_path_is_reported_at_vvvv(self):
        with tempfile.TemporaryDirectory() as work:
            steps, _, _ = self.capture_display(
                config_dir=work, create_snapshot=True, snapshot_file="snap.yml"
            )
            joined = "\n".join(steps)

            # The absolute path, so a relative snapshot_file is not left ambiguous
            self.assertIn(f"the snapshot will be written to {os.path.join(work, 'snap.yml')}", joined)
            self.assertIn("snapshot written to", joined)

    def test_create_snapshot_being_off_is_reported_at_vvvv(self):
        steps, _, _ = self.capture_display()

        self.assertIn("create_snapshot is off, no snapshot file will be written", "\n".join(steps))

    def test_not_writing_an_empty_snapshot_is_reported_at_v(self):
        with tempfile.TemporaryDirectory() as work:
            _, problems, _ = self.capture_display(
                config_dir=work,
                create_snapshot=True,
                snapshot_file="snap.yml",
                host_name_regex_filter=["^nothing-matches-this$"],
            )

            self.assertTrue(
                any("no hosts were added, leaving" in problem for problem in problems),
                problems,
            )

    def test_an_extension_ansible_cannot_read_back_is_reported_at_v(self):
        with tempfile.TemporaryDirectory() as work:
            _, problems, _ = self.capture_display(
                config_dir=work, create_snapshot=True, snapshot_file="snap.txt"
            )

            self.assertTrue(
                any("will not read it back" in problem for problem in problems), problems
            )
            # reported, but still written
            self.assertTrue(os.path.exists(os.path.join(work, "snap.txt")))

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

    def test_unconvertible_group_name_warns(self):
        routes = dict(DEFAULT_ROUTES)
        routes["/devicegroups"] = "devicegroups_blank_name.json"

        _, _, warnings = self.capture_display(routes=routes)

        self.assertTrue(
            any("nothing is left of its name" in m for m in warnings), warnings
        )

    def test_group_names_converting_to_the_same_name_warn(self):
        routes = dict(DEFAULT_ROUTES)
        routes["/devicegroups"] = "devicegroups_colliding.json"
        routes[NETWORK_CORE_ROUTE] = "devicegroups_core.json"
        routes["/devicegroups/Network-Core"] = "devicegroups_edge.json"

        _, _, warnings = self.capture_display(routes=routes)

        self.assertTrue(
            any("both end up as the group Network_Core" in m for m in warnings), warnings
        )

    def test_group_named_after_an_implicit_group_warns(self):
        routes = dict(DEFAULT_ROUTES)
        routes["/devicegroups"] = "devicegroups_implicit_name.json"
        routes["/devicegroups/ungrouped"] = "devicegroups_core.json"

        _, _, warnings = self.capture_display(routes=routes)

        self.assertTrue(
            any("same name as the group Ansible creates" in m for m in warnings), warnings
        )

    def test_mapping_key_matching_no_device_group_is_reported_at_v(self):
        _, problems, _ = self.capture_display(group_name_mapping={"Cisco devices": "os_iosxe"})

        self.assertTrue(
            any("matched no device group for 'Cisco devices'" in m for m in problems), problems
        )

    def test_merging_two_groups_by_mapping_does_not_warn(self):
        _, _, warnings = self.capture_display(
            group_name_mapping={"Core": "cisco", "Network Core": "cisco"}
        )

        # Mapping both onto one name is what the user asked for, unlike two names that
        # happen to convert to the same thing
        self.assertFalse(any("end up as the group" in m for m in warnings), warnings)

    def test_mapping_onto_a_converted_name_still_warns(self):
        _, _, warnings = self.capture_display(group_name_mapping={"Core": "Network_Core"})

        self.assertTrue(
            any("both end up as the group Network_Core" in m for m in warnings), warnings
        )

    def test_mapped_group_name_is_reported_at_vvvv(self):
        steps, _, _ = self.capture_display(group_name_mapping={"Core": "os_iosxe"})

        self.assertTrue(any("Core is named os_iosxe" in m for m in steps), steps)

    def test_converted_group_name_is_reported_at_vvvv(self):
        steps, _, _ = self.capture_display()

        self.assertTrue(
            any("Network Core is named Network_Core" in m for m in steps), steps
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

    def test_invalid_mapping_target_names_the_entry(self):
        with self.assertRaises(AnsibleError) as raised:
            self.build_plugin(group_name_mapping={"Cisco devices": "os iosxe"})

        message = str(raised.exception)
        self.assertIn("group_name_mapping['Cisco devices']", message)
        # the name it would have had to be written as, so the fix is in the message
        self.assertIn("'os_iosxe'", message)

    def test_empty_mapping_target_is_rejected(self):
        with self.assertRaises(AnsibleError) as raised:
            self.build_plugin(group_name_mapping={"Cisco devices": ""})

        self.assertIn("group_name_mapping['Cisco devices']", str(raised.exception))

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
