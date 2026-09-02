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
import yaml
from collections import defaultdict

from ansible.errors import AnsibleError
from ansible.module_utils.common.text.converters import to_text
from ansible.module_utils.urls import open_url
from ansible.plugins.inventory import BaseInventoryPlugin, Cacheable


class InventoryModule(BaseInventoryPlugin, Cacheable):
    NAME = "librenms"

    # --- HTTP / caching -------------------------------------------------

    def _http_request(self, url):
        self.display.vvv(f"Fetching: {url}")
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
            return self._parse_response(url, e, http_status=e.code)

        return self._parse_response(url, response, http_status=None)

    def _parse_response(self, url, response, http_status):
        try:
            raw = to_text(response.read(), errors="surrogate_or_strict")
        except UnicodeError:
            raise AnsibleError("Incorrect encoding of response from LibreNMS API.")

        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None

        if isinstance(payload, dict) and payload.get("status") == "error":
            message = payload.get("message", "")
            # eg. "No devices found in group 'x'", "No devices found", "No groups found".
            if re.search(r"no (devices|device groups?|groups?) found", message, re.IGNORECASE):
                return {}
            raise AnsibleError(f"LibreNMS API error: {message}")

        if http_status is not None:
            raise AnsibleError(
                f"LibreNMS API request to {url} failed with HTTP {http_status}: {raw}"
            )

        if payload is None:
            raise AnsibleError(f"Incorrect JSON payload from LibreNMS API: {raw}")

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
            try:
                payload = self._cache[cache_key]
            except KeyError:
                payload = None

        if payload is None:
            payload = self._http_request(url)
            if user_cache_setting:
                self._cache[cache_key] = payload

        return payload

    # --- Data retrieval ---------------------------------------------------

    def _get_devices(self):
        """Get all devices. Appends optional filters to the request URL that the user
        might have defined in the plugin file

        Returns:
            devices(list): A list of all devices, empty if no devices are found
        """
        url = self.api_endpoint + "/devices"
        query_params = []
        if self.device_status_filter and self.device_status_filter != "all":
            query_params.append("type=" + self.device_status_filter)
        query_params.extend(self.query_filters)
        if query_params:
            url += "?" + "&".join(query_params)

        payload = self._fetch(url)
        return payload.get("devices", [])

    def _get_device_group_membership(self):
        """Get a dictionary of group memberships per device ID. Apply optional regex filters
        the user might have defined in the plugin file

        Returns:
            membership(defaultdict): A dict keyed with the device IDs with a list of their group memberships.
                                    If a device ID is not in this dict, it has no group memberships
        """
        # Fetch all defined groups
        payload = self._fetch(self.api_endpoint + "/devicegroups")
        all_groups = payload.get("groups", [])

        # Filter on the defined regex patterns, if any
        if self.group_name_regex_filter:
            groups = [
                group
                for group in all_groups
                if any(re.match(pattern, group["name"], self.re_flags) for pattern in self.group_name_regex_filter)
            ]
        else:
            groups = all_groups

        membership = defaultdict(list)
        # Fetch the members of each fetched group and map them together
        for group in groups:
            member_payload = self._fetch(self.api_endpoint + "/devicegroups/" + group["name"])
            for device in member_payload.get("devices", []):
                membership[device["device_id"]].append(group["name"])

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
            return True
        if self.exclude_ignored and self._is_flag_set(device.get("ignore")):
            return True
        if self.host_name_regex_filter:
            candidate = device.get("sysName") or device.get("hostname") or ""
            if not any(re.match(pattern, candidate, self.re_flags) for pattern in self.host_name_regex_filter):
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
            return self._ascii_hostname(str(value)) if value else str(uuid.uuid4())

        for field in ("sysName", "hostname"):
            value = device.get(field)
            if value:
                return self._ascii_hostname(value)

        return str(uuid.uuid4())

    def _set_host_variables(self, hostname, device):
        """Convert libreNMS API data into Ansible inventory variables. Prefixes 'libre_' to the
        data field in LibreNMS. Also sets variables taken from the notes field if that is configured
        in the plugin file

        Args:
            hostname (str): The hostname of the device
            device (dict): The full device dictionary fetched from the API
        """

        # Parse notes if option is enabled
        if self.parse_notes_field and device.get('notes'):
            notes_variables = self._parse_notes(device.get('notes'))
            if notes_variables is not None:
                for field, value in notes_variables.items():
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

        self.inventory.set_variable(hostname, "libre_hardware_family", family)

        # Left unset rather than set to None, so that a device whose product ID is only a
        # family does not end up in a group of its own when keyed on the variant.
        if variant is not None:
            self.inventory.set_variable(hostname, "libre_hardware_variant", variant)

    def _parse_notes(self, notes_value: str):
        """Parses the LibreNMS notes field as YAML

        Args:
            notes_value (str): the raw notes field taken from the API

        Returns:
            parsed_variables(dict): A dictionary of key-value parsed from the notes field
        """
        if not notes_value.splitlines()[0].strip() == '---':
            self.display.vvv("Notes field does not start with '---', not parsing")
            return None

        try:
            parsed = yaml.safe_load(notes_value)
        except yaml.YAMLError as e:
            self.display.warning(f"Notes field is not valid YAML, skipping: {e}")
            return None

        if not isinstance(parsed, dict):
            self.display.vvv("Notes field did not parse into a mapping, skipping")
            return None

        return parsed

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
        compiled = {}

        for os_name, patterns in (configured or {}).items():
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

        self.display.vvv(f"No hardware pattern matched: {hardware_value}")
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
            # Add the host to the group
            self.inventory.add_host(group=transformed_group_name, host=hostname)

    # --- Main flow ------------------------------------------------------

    def _populate(self):
        """Populate the dynamic inventory using data fetched from the LibreNMS API
        """
        # Get devices using any filters the user defined in the plugin file
        devices = self._get_devices()

        # Get group memberships of all devices
        membership = {}
        if self.device_groups_as_ansible_groups:
            membership = self._get_device_group_membership()

        # For every fetched device...
        for device in devices:
            # Skip the device if it should be excluded according to user configuration
            if self._device_excluded(device):
                continue

            # Derive the hostname
            hostname = self._derive_hostname(device)

            # Add the host to the dynamic inventory
            self.inventory.add_host(hostname)

            # Set the host's variables
            self._set_host_variables(hostname, device)
            
            # Add the host to all the groups it should be a member of
            if self.device_groups_as_ansible_groups:
                self._add_host_to_device_groups(device.get("device_id"), hostname, membership)

    def _resolve_api_token(self):
        """Use Ansible's Templar to get the api_token from a separate Ansible variable.
        Supports api_token being a Jinja2 expression (eg. "{{ vaulted_librenms_token }}")
        evaluated against extra vars, so the token can live in an Ansible Vault-encrypted
        variable instead of plaintext in the inventory source file or an env var.

        Returns:
            api_token: The rendered API token
        """
        self.templar.available_variables = self._vars
        return self.templar.template(self.get_option("api_token"), fail_on_undefined=False)

    def parse(self, inventory, loader, path, cache=True):
        # Let the Ansible plugin base class do its initialization of this module first
        super(InventoryModule, self).parse(inventory, loader, path)

        # Parse the plugin file to get all properties defined
        self._read_config_data(path=path)

        # Set the properties of the inventory plugin model
        self.api_endpoint = self.get_option("api_endpoint").rstrip("/")
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
        self.hardware_trimming_patterns = self._compile_trimming_patterns(
            self.get_option("hardware_trimming_patterns")
        )
        self.lowercase_hardware_family = self.get_option("lowercase_hardware_family")

        self.cache_force_update = self.get_option("cache_force_update")
        self.use_cache = cache

        # Populate the dynamic inventory
        self._populate()
