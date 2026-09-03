# Copyright (c) 2026 0uwl
# MIT License (see LICENSE)

from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = r"""
    name: librenms
    author:
        - 0uwl
    short_description: LibreNMS inventory source
    description:
        - Get inventory hosts from a LibreNMS instance.
        - Every device field is exposed as a C(libre_<field>) host var.
        - Devices can be grouped by LibreNMS device group membership. For property-based
          grouping or composed vars, chain Ansible's standard C(constructed) inventory
          plugin as a second inventory source over this one's output.
    extends_documentation_fragment:
        - inventory_cache
    options:
        plugin:
            description: Token that ensures this is a source file for the C(librenms) plugin.
            required: true
            choices: ['librenms']
        api_endpoint:
            description: Endpoint of the LibreNMS API, e.g. C(https://librenms.example.com/api/v0).
            required: true
            env:
                - name: LIBRENMS_API
        api_token:
            description:
                - LibreNMS API token.
                - Supports Jinja2 templating (eg. C({{ my_vaulted_token }})) evaluated against
                  extra vars, so the token can be kept in an Ansible Vault-encrypted variable
                  instead of plaintext in the inventory source file or an environment variable.
            required: true
            env:
                - name: LIBRENMS_TOKEN
                - name: LIBRENMS_API_KEY
        validate_certs:
            description: Verify TLS certificates when calling the LibreNMS API.
            type: bool
            default: true
        timeout:
            description: Timeout for LibreNMS API requests, in seconds.
            type: int
            default: 60
        headers:
            description: Extra HTTP headers to send with every request to the LibreNMS API.
            type: dict
            default: {}
        cache_force_update:
            description:
                - Force a cache refresh for this run, regardless of I(cache_timeout).
                - Unlike the C(--flush-cache) CLI flag, this can be set from the inventory
                  source file itself, which is useful under AWX/Tower where extra CLI flags
                  cannot be passed to C(ansible-inventory).
            type: bool
            default: false
        exclude_disabled:
            description: Exclude devices that are disabled in LibreNMS.
            type: bool
            default: true
        exclude_ignored:
            description: Exclude devices that are marked ignored in LibreNMS.
            type: bool
            default: true
        device_status_filter:
            description:
                - Filter devices server-side using LibreNMS' C(type) query parameter on the
                  C(/devices) endpoint.
            type: str
            choices: [all, active, ignored, up, down, disabled]
            default: all
        query_filters:
            description:
                - Extra raw C(key=value) query string parameters appended to the C(/devices)
                  API request, e.g. C(os=ios).
            type: list
            elements: str
            default: []
        group_name_regex_filter:
            description:
                - List of regexes. Only LibreNMS device groups whose name matches at least one
                  regex are turned into Ansible groups. An empty list means all device groups
                  are considered.
            type: list
            elements: str
            default: []
        host_name_regex_filter:
            description:
                - List of regexes. Only devices whose C(sysName)/C(hostname) matches at least
                  one regex are included. An empty list means all devices are included.
            type: list
            elements: str
            default: []
        regex_ignore_case:
            description: Perform regex filter matches case-insensitively.
            type: bool
            default: true
        hostname_field:
            description:
                - LibreNMS device field to use as the Ansible inventory hostname.
                - By default C(sysName) is used, falling back to C(hostname) if C(sysName) is empty.
            type: str
        device_groups_as_ansible_groups:
            description:
                - Add each device to an Ansible group per LibreNMS device group it belongs to
                  (subject to I(group_name_regex_filter)).
            type: bool
            default: true
        exclude_fields:
            description:
                - LibreNMS device fields to leave out of the C(libre_<field>) host vars
                  entirely.
                - Defaults to fields LibreNMS returns in plaintext that many would consider
                  secrets (SNMP community string and SNMPv3 auth/priv passphrases). These are
                  never exposed as Ansible facts, cache entries, or C(-v) output unless you
                  remove them from this list.
            type: list
            elements: str
            default:
                - community
                - authpass
                - cryptopass
        parse_notes_field:
            description:
                - Parse a device's Notes field as YAML and add them as host vars. The field
                  must start with '---'. Only basic variables are allowed
                - Notes-derived vars are set as-is, without a C(libre_) prefix, so they can
                  be used directly (eg. C(ansible_host), C(ansible_user)). This means a key
                  that collides with a reserved Ansible variable name (eg. C(groups),
                  C(hostvars), C(ansible_connection)) will override that variable for the
                  host. Devices with untrusted or multi-admin-edited notes fields should
                  avoid this option, or admins should be made aware of the risk.
            type: bool
            default: false
        parse_location_field:
            description:
                - Parse a device's C(location) field (LibreNMS' copy of the SNMP
                  C(sysLocation) value) into site/room/rack/unit host vars, for sites
                  that encode a device's precise physical position in it.
                - Expects the fixed format
                  C(<site>;[<room>];[<rack>];[[<stack_nr>]u<u_nr>];...) - C(site) is
                  required, C(room) is an optional second field, and everything after
                  that is read as a sequence of rack names and rack-unit positions. A
                  segment made up of an optional number then C(u) then a number (eg.
                  C(u10), C(2u5)) is a unit position; anything else names the rack that
                  the unit positions after it belong to, until the next rack name. A
                  rack with no unit position before the next rack (or the end of the
                  value) is still recorded, on its own. This lets a stack of physically
                  separate devices reported as one LibreNMS device (eg. C(2u5;3u6))
                  record where each member sits, across racks if needed.
                - Sets C(libre_location_site), C(libre_location_room) (when given), and
                  C(libre_location_positions) - a list of dicts, one per rack and/or
                  unit position found, each with any of C(rack), C(unit), C(stack_nr)
                  that were given. A location with no semicolons at all still gets
                  C(libre_location_site) set to the whole value - that is the minimal
                  form of the format, not a mismatch.
                - A location with no site (eg. starting with a C(;)) is skipped with a
                  warning rather than failing the run, since not every device may use
                  this scheme.
                - C(libre_location) itself is never modified - these are additional vars.
            type: bool
            default: false
        hardware_trimming_patterns:
            description:
                - Derive a more general product family from the C(hardware) field, so hosts
                  can be grouped by family instead of by exact product ID.
                - Sets C(libre_hardware_family), and C(libre_hardware_variant) for whatever
                  the pattern leaves behind, eg. C(C9300-48P) gives a family of C(C9300) and
                  a variant of C(48P). C(libre_hardware) is never modified, since the product
                  ID cannot be reconstructed from the two - a pattern may consume text that
                  neither of them keeps.
                - A mapping of LibreNMS C(os) value (eg. C(ios), C(nxos)) to a list of regular
                  expressions. For each device the patterns listed under its own C(os) are
                  tried in order and the first one to match wins. Devices whose C(os) has no
                  entry, and values that match none of its patterns, get a family equal to
                  the value LibreNMS reported and no variant.
                - Patterns are anchored at the start of the value. If a pattern contains a
                  capture group the first group becomes the family, otherwise the whole
                  match does. The capture group is what lets a pattern drop a prefix - a
                  pattern of '^WS-(C\d+[A-Z]*)' gives C(WS-C3850-24T) a family of C(C3850),
                  grouping older Catalyst switches under the same names as the 9K
                  generation. Text consumed without being captured is in neither variable.
                - The separator a pattern broke on is stripped from the variant. Devices
                  with nothing left over get no variant at all, rather than an empty one.
                - Both derived variables are unset for devices LibreNMS reports no hardware
                  for, and when this option is not configured.
                - An invalid regular expression fails the inventory run immediately, naming
                  the offending pattern.
                - The family keeps the casing LibreNMS reported. See
                  C(lowercase_hardware_family) to normalise it.
            type: dict
            default: {}
        lowercase_hardware_family:
            description:
                - Lowercase C(libre_hardware_family) and C(libre_hardware_variant), so that
                  they are safe to use directly in group names without a Jinja filter on the
                  consuming side.
                - C(libre_hardware) is not affected, it always holds what LibreNMS reported.
                - Has no effect unless C(hardware_trimming_patterns) is configured.
            type: bool
            default: false
"""

EXAMPLES = r"""
# librenms.yml
plugin: librenms
api_endpoint: https://librenms.example.com/api/v0
# api_token is better provided via the LIBRENMS_TOKEN environment variable
validate_certs: true
cache: true
cache_plugin: jsonfile
cache_connection: /tmp/librenms_inventory_cache
cache_timeout: 600

exclude_disabled: true
exclude_ignored: true

group_name_regex_filter:
  - ^Network Core$
  - ^Site .*$

# For property-based grouping (os, location, ...) or composed vars (eg. ansible_host,
# ansible_network_os), add a second inventory source using Ansible's standard
# `constructed` plugin over this one's output - see the README's Grouping section.

# Enable this to read the Notes value as YAML and derrive additional host vars from it
parse_notes_field: false

# Parse the location field as <site>;[<room>];[<rack>];[[<stack_nr>]u<u_nr>];... to get
# libre_location_site/_room/_positions - see the README's Location parsing section.
parse_location_field: false

# Trim the hardware value down to a product family, per LibreNMS os. Order matters
# within a list - the first pattern to match wins.
hardware_trimming_patterns:
  ios: &cisco_catalyst
    - '^WS-(C\d+[A-Z]*)'
    - '^C\d+[A-Z]*'
  iosxe: *cisco_catalyst
  nxos:
    - '^[NC]?\dK-C\d+[A-Z]*'

# Lowercase the derived family and variant so they can be used directly in group names
lowercase_hardware_family: true
"""

import json
import re
import unicodedata
import urllib.error
import uuid
from collections import defaultdict

import yaml
from ansible.errors import AnsibleError
from ansible.module_utils.common.text.converters import to_text
from ansible.module_utils.urls import open_url
from ansible.plugins.inventory import BaseInventoryPlugin, Cacheable


class InventoryModule(BaseInventoryPlugin, Cacheable):
    NAME = "librenms"

    # A location "unit spec" segment: an optional stack number then a literal 'u' then
    # the unit number, eg. "u10", "2u5". Anything else non-empty in that position is a
    # rack name. Fixed, not user-configurable - see parse_location_field.
    _LOCATION_UNIT_RE = re.compile(r"^(?P<stack_nr>\d+)?[uU](?P<unit>\d+)$")

    # --- Logging ---------------------------------------------------------

    def _step(self, message):
        """Report a step of the inventory run, shown at -vvvv

        Every step of a run is reported this way so that a -vvvv log reads as a
        sequence of what the plugin did, in order.

        Args:
            message (str): What the plugin is doing, as a short lowercase phrase
        """
        self.display.vvvv(f"[librenms] {message}")

    def _problem(self, message):
        """Report something that went wrong but did not stop the run, shown at -v

        Kept at -v so problems that silently degrade the inventory (a device that
        got a generated hostname, notes that could not be read, a filter that
        matched nothing) are visible without wading through a -vvvv log.

        Args:
            message (str): What went wrong, and what the plugin did about it
        """
        self.display.v(f"[librenms] {message}")

    @staticmethod
    def _device_label(device):
        """Name a device for log messages, without assuming any field is present

        Args:
            device (dict): A device dictionary fetched from the API

        Returns:
            str: The best identifier available for the device
        """
        return str(
            device.get("sysName")
            or device.get("hostname")
            or device.get("device_id")
            or "unknown device"
        )

    # --- HTTP / caching -------------------------------------------------

    def _http_request(self, url):
        """Send a request to the LibreNMS API and hand the response off to be parsed

        Args:
            url (str): The full request URL

        Returns:
            payload(dict): The parsed response body
        """
        self._step(f"requesting {url}")
        try:
            response = open_url(
                url,
                headers=self.headers,
                timeout=self.timeout,
                validate_certs=self.validate_certs,
            )
        except urllib.error.HTTPError as e:
            # LibreNMS signals some "nothing found" cases (eg. an empty device group, or
            # a devices query matching nothing) with a non-2xx HTTP status instead of
            # 200 + {"status": "error"}, so the body has to be inspected before treating
            # this as a real failure - a bare status-code check would misreport those as
            # the endpoint/resource not existing.
            self._step(f"http {e.code} from {url}, checking body for an API message")
            return self._parse_response(url, e, http_status=e.code)
        except urllib.error.URLError as e:
            # No HTTP response at all - a bad endpoint, DNS, TLS or a timeout. Reported
            # as an AnsibleError so the user gets the reason instead of a traceback.
            # AnsibleError appends the cause itself, so it is not repeated here.
            raise AnsibleError(
                f"Could not reach the LibreNMS API at {url}. "
                f"Check api_endpoint, validate_certs and timeout"
            ) from e

        self._step(f"http 200 from {url}")
        return self._parse_response(url, response, http_status=None)

    def _parse_response(self, url, response, http_status):
        """Decode a LibreNMS API response body and turn API-level failures into errors

        Args:
            url (str): The request URL the response came from
            response: The file-like response, or the HTTPError standing in for one
            http_status (int): The status of a non-2xx response, None for a 2xx one

        Returns:
            payload(dict): The decoded response body, or an empty dict when LibreNMS
                reported that nothing matched the request
        """
        self._step(f"reading response body from {url}")
        try:
            raw = to_text(response.read(), errors="surrogate_or_strict")
        except UnicodeError as e:
            raise AnsibleError(
                f"Response from {url} is not valid UTF-8. Check that api_endpoint points "
                f"at a LibreNMS API and not at some other service"
            ) from e

        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None

        if isinstance(payload, dict) and payload.get("status") == "error":
            message = payload.get("message", "")
            # eg. "No devices found in group 'x'", "No devices found", "No groups found".
            if re.search(r"no (devices|device groups?|groups?) found", message, re.IGNORECASE):
                self._step(f"LibreNMS reported nothing found for {url}, treating as empty")
                return {}
            if http_status in (401, 403):
                raise AnsibleError(
                    f"LibreNMS rejected the API token (HTTP {http_status}): {message}. "
                    f"Check api_token, or the LIBRENMS_TOKEN environment variable."
                )
            raise AnsibleError(f"LibreNMS API error on {url}: {message}")

        if http_status is not None:
            raise AnsibleError(
                f"LibreNMS API request to {url} failed with HTTP {http_status}: {raw}"
            )

        if payload is None:
            raise AnsibleError(
                f"Response from {url} is not JSON. Check that api_endpoint points at a "
                f"LibreNMS API. Body was: {raw}"
            )

        self._step(f"response from {url} parsed")
        return payload

    def _fetch(self, url):
        """Fetch data from LibreNMS or the cache if it's enabled and contains data for the request

        Args:
            url (str): The API request sent to LibreNMS or the cache

        Returns:
            payload(dict): The returned value of the fetch
        """
        # Look for the cache key corresponding to the URL
        cache_key = self.get_cache_key(url)
        user_cache_setting = self.get_option("cache")
        # Decide if the cache should be read and used
        attempt_to_read_cache = user_cache_setting and self.use_cache and not self.cache_force_update

        payload = None
        if attempt_to_read_cache:
            self._step(f"checking cache for {url}")
            try:
                payload = self._cache[cache_key]
                self._step(f"cache hit for {url}")
            except KeyError:
                self._step(f"cache miss for {url}")
                payload = None
        elif user_cache_setting:
            self._step(f"skipping cache read for {url}, a refresh was requested")
        else:
            self._step(f"cache disabled, going to the API for {url}")

        if payload is None:
            payload = self._http_request(url)
            if user_cache_setting:
                self._step(f"storing response for {url} in the cache")
                self._cache[cache_key] = payload

        return payload

    # --- Data retrieval ---------------------------------------------------

    def _get_devices(self):
        """Get all devices. Appends optional filters to the request URL that the user
        might have defined in the plugin file

        Returns:
            devices(list): A list of all devices, empty if no devices are found
        """
        self._step("fetching devices")

        url = self.api_endpoint + "/devices"
        query_params = []
        if self.device_status_filter and self.device_status_filter != "all":
            query_params.append("type=" + self.device_status_filter)
        query_params.extend(self.query_filters)
        if query_params:
            self._step(f"applying device query filters: {' '.join(query_params)}")
            url += "?" + "&".join(query_params)

        payload = self._fetch(url)
        devices = payload.get("devices", [])

        if not devices:
            self._problem(
                "LibreNMS returned no devices. Check device_status_filter and "
                "query_filters, and that the token can see devices."
            )
        else:
            self._step(f"{len(devices)} devices returned")

        return devices

    def _get_device_group_membership(self):
        """Get a dictionary of group memberships per device ID. Apply optional regex filters
        the user might have defined in the plugin file

        Returns:
            membership(defaultdict): A dict keyed with the device IDs with a list of their group memberships.
                                    If a device ID is not in this dict, it has no group memberships
        """
        self._step("fetching device groups")

        # Fetch all defined groups
        payload = self._fetch(self.api_endpoint + "/devicegroups")
        all_groups = payload.get("groups", [])
        self._step(f"{len(all_groups)} device groups returned")

        # Filter on the defined regex patterns, if any
        if self.group_name_regex_filter:
            self._step(f"filtering device groups on {len(self.group_name_regex_filter)} regexes")
            groups = [
                group
                for group in all_groups
                if any(re.match(pattern, group["name"], self.re_flags) for pattern in self.group_name_regex_filter)
            ]
            if all_groups and not groups:
                self._problem(
                    "group_name_regex_filter matched none of the device groups, no "
                    "groups will be created."
                )
            else:
                self._step(f"{len(groups)} device groups kept after filtering")
        else:
            groups = all_groups

        membership = defaultdict(list)
        # Fetch the members of each fetched group and map them together
        for group in groups:
            self._step(f"fetching members of device group {group['name']}")
            member_payload = self._fetch(self.api_endpoint + "/devicegroups/" + group["name"])
            members = member_payload.get("devices", [])
            self._step(f"device group {group['name']} has {len(members)} members")
            for device in members:
                membership[device["device_id"]].append(group["name"])

        self._step(f"{len(membership)} devices belong to at least one device group")
        return membership

    # --- Filtering ----------------------------------------------------

    @staticmethod
    def _is_flag_set(value):
        """Convert LibreNMS' way of representing device flags into usable booleans.
        Flags could be "disabled" or "ignored" devices.

        Args:
            value (str): the value of a device property to be converted

        Returns:
            bool: The converted boolean value. False if value is 0 or None, True if anything else
        """
        if value is None:
            return False
        return str(value) not in ("0", "")

    def _device_excluded(self, device):
        """Test if the device should be excluded from the inventory. This can be done by setting
        exclude_disabled or exclude_ignored or defining regex patterns of hostnames to include in
        the plugin file

        Args:
            device (dict): A device dictionary fetched from the API

        Returns:
            bool: True if no patterns match or the device is set to disabled or ignored and the user
                set the exclude_disabled or exclude_ignored to True in the plugin file. False otherwise
        """
        if self.exclude_disabled and self._is_flag_set(device.get("disabled")):
            self._step(f"skipping {self._device_label(device)}, it is disabled")
            return True
        if self.exclude_ignored and self._is_flag_set(device.get("ignore")):
            self._step(f"skipping {self._device_label(device)}, it is ignored")
            return True
        if self.host_name_regex_filter:
            candidate = device.get("sysName") or device.get("hostname") or ""
            if not any(re.match(pattern, candidate, self.re_flags) for pattern in self.host_name_regex_filter):
                self._step(f"skipping {self._device_label(device)}, no host_name_regex_filter matched")
                return True
        return False

    # --- Hostname / hostvars -------------------------------------------

    @staticmethod
    def _ascii_hostname(value):
        """Convert the hostname from LibreNMS to ASCII

        Args:
            value (str): Raw LibreNMS hostname

        Returns:
            converted_hostname(str): The hostname decoded into ASCII
        """
        # Normalize the value into unicode, then encode and decode it to get a converted, ASCII value
        return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")

    def _derive_hostname(self, device):
        """Get the hostname of the device, or generate a random uuid4 if no hostname can be derived
        from the API data or what the user defined in the plugin file.

        Args:
            device (dict): A device dictionary fetched from the API

        Returns:
            str: The derived hostname for the given device
        """
        if self.hostname_field:
            value = device.get(self.hostname_field)
            if value:
                return self._ascii_hostname(str(value))
            generated = str(uuid.uuid4())
            self._problem(
                f"device {self._device_label(device)} has no {self.hostname_field}, "
                f"naming it {generated}"
            )
            return generated

        for field in ("sysName", "hostname"):
            value = device.get(field)
            if value:
                return self._ascii_hostname(value)

        generated = str(uuid.uuid4())
        self._problem(
            f"device {self._device_label(device)} has no sysName or hostname, "
            f"naming it {generated}"
        )
        return generated

    def _set_host_variables(self, hostname, device):
        """Convert libreNMS API data into Ansible inventory variables. Prefixes 'libre_' to the
        data field in LibreNMS. Also sets variables taken from the notes field if that is configured
        in the plugin file

        Args:
            hostname (str): The hostname of the device
            device (dict): The full device dictionary fetched from the API
        """

        self._step(f"setting host vars for {hostname}")

        # Parse notes if option is enabled
        if self.parse_notes_field and device.get('notes'):
            notes_variables = self._parse_notes(hostname, device.get('notes'))
            if notes_variables is not None:
                self._step(f"{hostname}: adding {len(notes_variables)} vars from the notes field")
                for field, value in notes_variables.items():
                    self.inventory.set_variable(hostname, field, value)

        # Parse the location field if option is enabled. libre_location itself is set
        # unmodified below by the general field loop - these are additional vars.
        if self.parse_location_field and device.get('location'):
            location_variables = self._parse_location(hostname, device.get('location'))
            if location_variables is not None:
                self._step(f"{hostname}: adding {len(location_variables)} vars from the location field")
                for field, value in location_variables.items():
                    self.inventory.set_variable(hostname, field, value)

        for field, value in device.items():
            if field in self.exclude_fields:
                continue

            if field == 'hardware':
                self._set_hardware_variables(hostname, device, value)
                continue

            self.inventory.set_variable(hostname, "libre_" + field, value)

    def _set_hardware_variables(self, hostname, device, hardware_value):
        """Sets libre_hardware, plus the derived family and variant vars when trimming is on

        Trimming only ever adds variables. libre_hardware stays exactly what LibreNMS
        reported, because the product ID cannot be reconstructed from the family and the
        variant - a pattern may consume text that neither of them keeps.

        Args:
            hostname (str): The hostname of the device
            device (dict): The full device dictionary fetched from the API
            hardware_value: The device's hardware value, as reported by LibreNMS
        """
        self.inventory.set_variable(hostname, "libre_hardware", hardware_value)

        # LibreNMS reports no hardware for devices it could not identify
        if not self.hardware_trimming_patterns or not isinstance(hardware_value, str):
            return

        family, variant = self._trim_hardware(hardware_value, device.get('os'))

        if self.lowercase_hardware_family:
            family = family.lower()
            if variant is not None:
                variant = variant.lower()

        self._step(
            f"{hostname}: hardware {hardware_value} gives family {family}, "
            f"{'variant ' + variant if variant else 'no variant'}"
        )

        self.inventory.set_variable(hostname, "libre_hardware_family", family)

        # Left unset rather than set to None, so that a device whose product ID is only a
        # family does not end up in a group of its own when keyed on the variant.
        if variant is not None:
            self.inventory.set_variable(hostname, "libre_hardware_variant", variant)

    def _parse_notes(self, hostname, notes_value: str):
        """Parses the LibreNMS notes field as YAML

        Args:
            hostname (str): The hostname of the device, used to name it in messages
            notes_value (str): the raw notes field taken from the API

        Returns:
            parsed_variables(dict): A dictionary of key-value parsed from the notes field
        """
        self._step(f"{hostname}: parsing the notes field")

        if notes_value.splitlines()[0].strip() != '---':
            self._step(f"{hostname}: notes field does not start with '---', not parsing")
            return None

        try:
            parsed = yaml.safe_load(notes_value)
        except yaml.YAMLError as e:
            self.display.warning(f"[librenms] {hostname}: notes field is not valid YAML, skipping it: {e}")
            return None

        if not isinstance(parsed, dict):
            self._problem(
                f"{hostname}: notes field is YAML but not a mapping of vars, skipping it"
            )
            return None

        return parsed

    def _parse_location(self, hostname, location_value):
        """Parses the LibreNMS location field into site/room/rack-unit variables

        Expects the fixed format documented under parse_location_field:
        <site>;[<room>];[<rack>];[[<stack_nr>]u<u_nr>];...  Segments after site/room are
        classified by shape rather than position - one matching an optional stack number
        plus 'u' plus a unit number is a unit spec, anything else non-empty is a rack
        name that applies to every unit spec until the next rack name. A rack with no
        unit specs before the next rack (or the end of the value) is still recorded, on
        its own - a device can be racked without a known unit.

        Args:
            hostname (str): The hostname of the device, used to name it in messages
            location_value (str): the raw location field taken from the API

        Returns:
            parsed_variables(dict): site/room/positions vars to set, or None if the
                value has no site (eg. it starts with ';') or isn't a string
        """
        self._step(f"{hostname}: parsing the location field")

        # location is normally a string, but nothing enforces that API-side so we 
        # should guard it
        if not isinstance(location_value, str):
            self._problem(f"{hostname}: location field is not a string, skipping location parsing")
            return None

        segments = location_value.split(";")

        site = segments[0].strip()
        if not site:
            self._problem(f"{hostname}: location field has no site, skipping location parsing")
            return None

        # A location with no semicolons at all (just a site) is the minimal, still
        # valid, form of the format
        variables = {"libre_location_site": site}

        room = segments[1].strip() if len(segments) > 1 else ""
        if room:
            variables["libre_location_room"] = room

        positions = []
        # The rack name currently in effect
        current_rack = None
        # Whether a unit spec has claimed the current rack yet or not
        current_rack_used = False

        def flush_bare_rack():
            # Record a rack that never got a unit spec before it was superseded (or the
            # value ended), so a rack with no unit is not silently dropped.
            if current_rack is not None and not current_rack_used:
                positions.append({"rack": current_rack})

        # For every segment except the first two (unit specs)
        for raw_segment in segments[2:]:
            segment = raw_segment.strip()
            if not segment:
                continue

            # Match the segment against the defined regex pattern for unit specs
            match = self._LOCATION_UNIT_RE.match(segment)
            if match is None:
                # Not a unit spec - starts a new rack context.
                flush_bare_rack()
                current_rack = segment
                current_rack_used = False
                continue

            # A unit spec - attach it to whatever rack is currently in effect, if any.
            position = {"unit": int(match.group("unit"))}
            if current_rack is not None:
                position["rack"] = current_rack
            if match.group("stack_nr") is not None:
                position["stack_nr"] = int(match.group("stack_nr"))
            positions.append(position)
            current_rack_used = True

        # The last rack in the value never gets superseded by another, so it needs its
        # own flush after the loop ends.
        flush_bare_rack()

        if positions:
            self._step(f"{hostname}: location gives {len(positions)} rack/unit position(s)")
            variables["libre_location_positions"] = positions

        return variables

    def _compile_trimming_patterns(self, configured):
        """Validates the hardware_trimming_patterns option and pre-compiles its patterns

        Ansible only checks that the option is a dict, so the shape of the values is
        checked here. Compiling up front means a bad pattern fails the run immediately,
        naming itself, rather than part way through building the host list.

        Args:
            configured (dict): The raw option value, keyed on LibreNMS os

        Returns:
            compiled (dict): Lowercased os mapped to its list of compiled patterns
        """
        if not configured:
            return {}

        self._step(f"compiling hardware trimming patterns for {len(configured)} os values")
        compiled = {}

        for os_name, patterns in configured.items():
            if not isinstance(patterns, list):
                raise AnsibleError(
                    f"hardware_trimming_patterns['{os_name}'] must be a list of regular "
                    f"expressions, got {type(patterns).__name__}."
                )

            for pattern in patterns:
                try:
                    compiled.setdefault(str(os_name).lower(), []).append(re.compile(pattern))
                except (re.error, TypeError) as e:
                    raise AnsibleError(
                        f"hardware_trimming_patterns['{os_name}'] contains an invalid "
                        f"regular expression {pattern!r}: {e}"
                    ) from e

        self._step(f"hardware trimming patterns compiled for: {', '.join(sorted(compiled))}")
        return compiled

    def _trim_hardware(self, hardware_value, os_name):
        """Splits a hardware value into its product family and the variant that follows it

        Args:
            hardware_value (str): The hardware value as reported by LibreNMS
            os_name (str): The device's LibreNMS os, which selects the pattern list

        Returns:
            (family, variant): The trimmed value and whatever the pattern left behind, the
                latter being None when nothing is left over. When the device's os has no
                patterns configured, or none of them match, the value is returned unchanged
                with no variant.
        """
        patterns = self.hardware_trimming_patterns.get(str(os_name).lower())
        if not patterns:
            self._step(f"no hardware patterns configured for os {os_name}, keeping {hardware_value}")
            return hardware_value, None

        for pattern in patterns:
            if (match := pattern.match(hardware_value)):
                # A capture group lets a pattern drop a prefix, eg. the WS- on older
                # Catalyst switches. Without one, the whole match names the family.
                family = match.group(1) if pattern.groups else match.group(0)
                # Whatever the pattern did not consume is the variant. The separator it
                # broke on is an artefact of where the boundary fell, not part of the
                # value, so it is stripped.
                variant = hardware_value[match.end():].lstrip("-/_ ") or None
                return family, variant

        self._step(f"no {os_name} hardware pattern matched {hardware_value}, keeping it as is")
        return hardware_value, None

    # --- Grouping -----------------------------------------------------

    def _add_host_to_device_groups(self, device_id, hostname, membership):
        """Add a host to groups according to its LibreNMS group memberships

        Args:
            device_id (int): The ID of the device
            hostname (str): The hostname of the device
            membership (dict): A mapping of device IDs to their corresponding group memberships
        """
        for group_name in membership.get(device_id, []):
            # Let Ansible transform the LibreNMS group name
            transformed_group_name = self.inventory.add_group(group_name)
            self._step(f"adding {hostname} to group {transformed_group_name}")
            # Add the host to the group
            self.inventory.add_host(group=transformed_group_name, host=hostname)

    # --- Main flow ------------------------------------------------------

    def _populate(self):
        """Populate the dynamic inventory using data fetched from the LibreNMS API
        """
        self._step("populating the inventory")

        # Get devices using any filters the user defined in the plugin file
        devices = self._get_devices()

        # Get group memberships of all devices
        membership = {}
        if self.device_groups_as_ansible_groups:
            membership = self._get_device_group_membership()
        else:
            self._step("device_groups_as_ansible_groups is off, skipping device groups")

        self._step(f"processing {len(devices)} devices")
        added = 0

        # For every fetched device...
        for device in devices:
            # Skip the device if it should be excluded according to user configuration
            if self._device_excluded(device):
                continue

            # Derive the hostname
            hostname = self._derive_hostname(device)

            # Add the host to the dynamic inventory
            self._step(f"adding host {hostname}")
            self.inventory.add_host(hostname)
            added += 1

            # Set the host's variables
            self._set_host_variables(hostname, device)

            # Add the host to all the groups it should be a member of
            if self.device_groups_as_ansible_groups:
                self._add_host_to_device_groups(device.get("device_id"), hostname, membership)

        self._step(f"{added} of {len(devices)} devices added to the inventory")

        if devices and not added:
            self._problem(
                "every device was filtered out. Check exclude_disabled, exclude_ignored "
                "and host_name_regex_filter."
            )

    def _resolve_api_token(self):
        """Use Ansible's Templar to get the api_token from a separate Ansible variable.
        Supports api_token being a Jinja2 expression (eg. "{{ vaulted_librenms_token }}")
        evaluated against extra vars, so the token can live in an Ansible Vault-encrypted
        variable instead of plaintext in the inventory source file or an env var.

        Returns:
            api_token: The rendered API token
        """
        self._step("resolving the api token")
        self.templar.available_variables = self._vars
        api_token = self.templar.template(self.get_option("api_token"), fail_on_undefined=False)

        # An unresolved Jinja2 expression comes back as the literal template text, which
        # LibreNMS will reject later on with a 401. Say so now, while the cause is clear.
        if not api_token or "{{" in str(api_token):
            self._problem(
                "api_token did not resolve to a value. Check the variable it references, "
                "or set LIBRENMS_TOKEN."
            )
        else:
            self._step("api token resolved")

        return api_token

    def parse(self, inventory, loader, path, cache=True):
        # Let the Ansible plugin base class do its initialization of this module first
        super(InventoryModule, self).parse(inventory, loader, path)

        self._step(f"reading inventory source {path}")

        # Parse the plugin file to get all properties defined
        self._read_config_data(path=path)

        self._step("applying options")

        # Set the properties of the inventory plugin model
        self.api_endpoint = self.get_option("api_endpoint").rstrip("/")
        self._step(f"api endpoint is {self.api_endpoint}")
        self.validate_certs = self.get_option("validate_certs")
        self.timeout = self.get_option("timeout")
        self.headers = {"X-Auth-Token": self._resolve_api_token()}
        self.headers.update(self.get_option("headers") or {})

        self.exclude_disabled = self.get_option("exclude_disabled")
        self.exclude_ignored = self.get_option("exclude_ignored")
        self.device_status_filter = self.get_option("device_status_filter")
        self.query_filters = self.get_option("query_filters")
        self.exclude_fields = set(self.get_option("exclude_fields") or [])

        self.group_name_regex_filter = self.get_option("group_name_regex_filter")
        self.host_name_regex_filter = self.get_option("host_name_regex_filter")
        self.re_flags = re.IGNORECASE if self.get_option("regex_ignore_case") else 0

        self.hostname_field = self.get_option("hostname_field")
        self.device_groups_as_ansible_groups = self.get_option("device_groups_as_ansible_groups")
        self.parse_notes_field = self.get_option("parse_notes_field")
        self.parse_location_field = self.get_option("parse_location_field")
        self.hardware_trimming_patterns = self._compile_trimming_patterns(
            self.get_option("hardware_trimming_patterns")
        )
        self.lowercase_hardware_family = self.get_option("lowercase_hardware_family")

        self.cache_force_update = self.get_option("cache_force_update")
        self.use_cache = cache
        self._step(
            f"cache is {'on' if self.get_option('cache') else 'off'}, "
            f"cache_force_update is {'on' if self.cache_force_update else 'off'}"
        )

        self._step("options applied")

        # Populate the dynamic inventory
        self._populate()

        self._step("done")
