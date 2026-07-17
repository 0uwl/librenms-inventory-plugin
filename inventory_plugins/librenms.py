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
        - Devices can be grouped by LibreNMS device group membership and/or by device
          properties (os, hardware, location, ...), and further refined with Ansible's
          standard C(compose)/C(groups)/C(keyed_groups) options.
    extends_documentation_fragment:
        - constructed
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
        group_by:
            description:
                - List of device properties to group hosts by. For each device, a group named
                  C(<property>_<value>) (or just C(<value>) if I(group_names_raw) is set) is
                  created and the host is added to it.
            type: list
            elements: str
            choices:
                - os
                - os_version
                - hardware
                - type
                - status
                - location
                - vendor
                - disabled
                - ignored
            default: []
        group_names_raw:
            description: Do not prefix I(group_by) group names with the property name.
            type: bool
            default: false
        variable_name_map:
            description:
                - Mapping of raw LibreNMS device field names to the additional host var name
                  they should be exposed as, on top of the always-present C(libre_<field>).
            type: dict
            default:
                hostname: ansible_host
                os: ansible_network_os
        os_name_map:
            description:
                - Mapping applied to the value written to C(ansible_network_os), translating
                  LibreNMS' C(os) field into the network_os value expected by installed
                  collections (e.g. C(iosxe) -> C(ios)).
            type: dict
            default:
                asa: asa
                ios: ios
                iosxe: ios
        exclude_fields:
            description:
                - LibreNMS device fields to leave out of the C(libre_<field>) host vars
                  entirely, and out of any I(variable_name_map) mapping.
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

group_by:
  - os
  - type
  - location

# Power-user grouping/vars on top of the built-in group_by choices, using Ansible's
# standard Constructable options:
compose:
  ansible_port: libre_ssh_port | default(22)

groups:
  network_edge: "'router' in libre_purpose | default('')"

keyed_groups:
  - prefix: vendor
    key: libre_vendor
"""

import json
import re
import unicodedata
import urllib.error
import uuid
from collections import defaultdict

from ansible.errors import AnsibleError
from ansible.module_utils.common.text.converters import to_text
from ansible.module_utils.urls import open_url
from ansible.plugins.inventory import BaseInventoryPlugin, Cacheable, Constructable


class InventoryModule(BaseInventoryPlugin, Constructable, Cacheable):
    NAME = "librenms"

    def _require_inventory(self):
        # self.inventory is only populated once BaseInventoryPlugin.parse() has run; every
        # caller of this helper runs from within/after our own parse(), so this should
        # never actually trigger, it exists to fail loudly instead of with a bare
        # AttributeError, and to give type checkers a non-Optional value to work with.
        if self.inventory is None:
            raise AnsibleError("Inventory data is not initialized; parse() has not run yet.")
        return self.inventory

    # --- HTTP / caching -------------------------------------------------

    def _http_request(self, url):
        self.display.vvv("Fetching: {0}".format(url))
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
            raise AnsibleError("LibreNMS API error: {0}".format(message))

        if http_status is not None:
            raise AnsibleError(
                "LibreNMS API request to {0} failed with HTTP {1}: {2}".format(url, http_status, raw)
            )

        if payload is None:
            raise AnsibleError("Incorrect JSON payload from LibreNMS API: {0}".format(raw))

        return payload

    def _fetch(self, url):
        cache_key = self.get_cache_key(url)
        user_cache_setting = self.get_option("cache")
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
        payload = self._fetch(self.api_endpoint + "/devicegroups")
        all_groups = payload.get("groups", [])

        if self.group_name_regex_filter:
            groups = [
                g
                for g in all_groups
                if any(re.match(f, g["name"], self.re_flags) for f in self.group_name_regex_filter)
            ]
        else:
            groups = all_groups

        membership = defaultdict(list)
        for group in groups:
            member_payload = self._fetch(self.api_endpoint + "/devicegroups/" + group["name"])
            for device in member_payload.get("devices", []):
                membership[device["device_id"]].append(group["name"])

        return membership

    # --- Filtering ----------------------------------------------------

    @staticmethod
    def _is_flag_set(value):
        if value is None:
            return False
        return str(value) not in ("0", "")

    def _device_excluded(self, device):
        if self.exclude_disabled and self._is_flag_set(device.get("disabled")):
            return True
        if self.exclude_ignored and self._is_flag_set(device.get("ignore")):
            return True
        if self.host_name_regex_filter:
            candidate = device.get("sysName") or device.get("hostname") or ""
            if not any(re.match(f, candidate, self.re_flags) for f in self.host_name_regex_filter):
                return True
        return False

    # --- Hostname / hostvars -------------------------------------------

    @staticmethod
    def _ascii_hostname(value):
        return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")

    def _derive_hostname(self, device):
        if self.hostname_field:
            value = device.get(self.hostname_field)
            return self._ascii_hostname(str(value)) if value else str(uuid.uuid4())

        for field in ("sysName", "hostname"):
            value = device.get(field)
            if value:
                return self._ascii_hostname(value)

        return str(uuid.uuid4())

    def _set_host_variables(self, hostname, device):
        for field, value in device.items():
            if field in self.exclude_fields:
                continue
            self._require_inventory().set_variable(hostname, "libre_" + field, value)

        for field, mapped_name in self.variable_name_map.items():
            if field in self.exclude_fields:
                continue
            value = device.get(field)
            if value in (None, ""):
                continue
            if mapped_name == "ansible_network_os":
                value = self.os_name_map.get(value, value)
            self._require_inventory().set_variable(hostname, mapped_name, value)

    # --- Grouping -----------------------------------------------------

    @property
    def group_extractors(self):
        return {
            "os": lambda device: device.get("os"),
            "os_version": lambda device: device.get("os_version"),
            "hardware": lambda device: device.get("hardware"),
            "type": lambda device: device.get("type"),
            "status": lambda device: "up" if self._is_flag_set(device.get("status")) else "down",
            "location": lambda device: device.get("location"),
            "vendor": lambda device: device.get("vendor"),
            "disabled": lambda device: self._is_flag_set(device.get("disabled")),
            "ignored": lambda device: self._is_flag_set(device.get("ignore")),
        }

    def generate_group_name(self, grouping, value):
        # Booleans are special-cased so eg. "disabled" produces a group named "disabled",
        # not "disabled_True", and "False" produces no group at all.
        if isinstance(value, bool):
            return grouping if value else None

        if value in (None, ""):
            return None

        sanitized = re.sub(r"\W+", "_", str(value)).strip("_")
        if not sanitized:
            return None

        if self.group_names_raw:
            return sanitized
        return "{0}_{1}".format(grouping, sanitized)

    def _add_host_to_property_groups(self, device, hostname):
        for grouping in self.group_by:
            extractor = self.group_extractors.get(grouping)
            if extractor is None:
                continue

            value = extractor(device)
            group_name = self.generate_group_name(grouping, value)
            if not group_name:
                continue

            transformed_group_name = self._require_inventory().add_group(group_name)
            self._require_inventory().add_host(group=transformed_group_name, host=hostname)

    def _add_host_to_device_groups(self, device_id, hostname, membership):
        for group_name in membership.get(device_id, []):
            transformed_group_name = self._require_inventory().add_group(group_name)
            self._require_inventory().add_host(group=transformed_group_name, host=hostname)

    # --- Main flow ------------------------------------------------------

    def _populate(self):
        devices = self._get_devices()

        membership = {}
        if self.device_groups_as_ansible_groups:
            membership = self._get_device_group_membership()

        strict = self.get_option("strict")

        for device in devices:
            if self._device_excluded(device):
                continue

            hostname = self._derive_hostname(device)
            self._require_inventory().add_host(hostname)
            self._set_host_variables(hostname, device)

            if self.device_groups_as_ansible_groups:
                self._add_host_to_device_groups(device.get("device_id"), hostname, membership)

            self._add_host_to_property_groups(device, hostname)

            self._set_composite_vars(self.get_option("compose"), device, hostname, strict=strict)
            self._add_host_to_composed_groups(self.get_option("groups"), device, hostname, strict=strict)
            self._add_host_to_keyed_groups(self.get_option("keyed_groups"), device, hostname, strict=strict)

    def _resolve_api_token(self):
        # Supports api_token being a Jinja2 expression (eg. "{{ vaulted_librenms_token }}")
        # evaluated against extra vars, so the token can live in an Ansible Vault-encrypted
        # variable instead of plaintext in the inventory source file or an env var.
        self.templar.available_variables = self._vars
        return self.templar.template(self.get_option("api_token"), fail_on_undefined=False)

    def parse(self, inventory, loader, path, cache=True):
        super(InventoryModule, self).parse(inventory, loader, path)
        self._read_config_data(path=path)

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
        self.group_by = self.get_option("group_by")
        self.group_names_raw = self.get_option("group_names_raw")
        self.variable_name_map = self.get_option("variable_name_map")
        self.os_name_map = self.get_option("os_name_map")

        self.cache_force_update = self.get_option("cache_force_update")
        self.use_cache = cache

        self._populate()
