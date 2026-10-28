# Copyright (c) 2026 0uwl
# MIT License (see LICENSE)

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
        write_to_file:
            description:
                - Write the inventory the run produced out to a static YAML inventory file,
                  on top of the inventory handed to the run in progress.
                - The file is in Ansible's own inventory format, so it is usable as an
                  inventory source by itself (C(-i inventory/librenms.yml)), which runs
                  entirely offline without calling the LibreNMS API at all.
                - It is a snapshot, never read back by this plugin. Caching within a run is a
                  separate thing and stays with I(cache) and I(cache_timeout), which cache the
                  raw API responses and so still pick up a later change to an option like
                  I(group_name_mapping). This file holds the already rendered result and would
                  not.
                - Hosts are written under C(all) with their host vars inline, and each Ansible
                  group as a child of C(all) listing its members by name, so a host in several
                  device groups is listed under each of them and a host in none is listed under
                  C(all) only. The groups Ansible creates for every inventory (C(all),
                  C(ungrouped)) are not written as groups of their own.
                - Only what this plugin added goes in. An inventory source parsed before this
                  one is not written, and a C(constructed) source chained after it is parsed
                  later, so its groups and composed vars are not either - chain C(constructed)
                  over the file exactly as you chain it over this plugin.
                - The file holds every host var the run set, so with I(exclude_fields) emptied
                  the SNMP credentials LibreNMS reports land in plaintext on disk.
                - One thing the file cannot carry over - a value LibreNMS reported is untrusted
                  on a live run, so Ansible never templates it, while a value read back out of
                  a static inventory file is trusted and would be. A device field holding a
                  Jinja2 expression is therefore left literal on a live run and evaluated when
                  read from the file. Any host carrying one is named at C(-v) as it is written.
                - Nothing is written when the run added no hosts at all, so a run that filtered
                  everything out cannot truncate a good snapshot. Reported at C(-v).
                - Hosts, groups and var keys are sorted and no timestamp is written, so a run
                  that found nothing new rewrites the same bytes and a diff means the inventory
                  really changed. An existing file's permissions are kept.
            type: bool
            default: false
        inventory_file:
            description:
                - Path of the file I(write_to_file) writes, filename included.
                - A relative path resolves against the directory holding the inventory source
                  file rather than the current working directory, so the output lands in the
                  same place however C(ansible) was invoked. C(~) and environment variables are
                  expanded. The path it resolved to is reported at C(-vvvv).
                - Defaults to the inventory source file's own name with C(.generated.yml) in
                  place of its extension, next to it, so I(write_to_file) on its own writes
                  C(librenms.yml) out to C(librenms.generated.yml). Derived rather than fixed so
                  that two LibreNMS sources in one run do not silently overwrite each other.
                - Missing parent directories are created. A path that cannot be written - no
                  permission, a read-only filesystem, a file where a directory would have to be
                  - fails the run naming the path and the reason rather than raising a traceback.
                  The path is vetted before the first API call, so one that cannot work costs no
                  requests.
                - Refuses to write over this inventory source file, over an inventory source the
                  run has already parsed, over a directory, or over any file carrying a
                  C(plugin:) key, since overwriting one of those would destroy the configuration
                  that produced the inventory. Each case fails the run naming the path.
                - Written through a temporary file in the same directory and moved into place, so
                  an interrupted run cannot leave half an inventory where a whole one was.
                - Ansible's C(yaml) inventory plugin only reads back files ending in C(.yml),
                  C(.yaml) or C(.json). Any other extension is reported at C(-v) - the file is
                  still written, but C(-i) will not read it.
            type: str
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
        group_name_mapping:
            description:
                - Mapping of LibreNMS device group name to the Ansible group name it should
                  be created under, so that a device group of C(Cisco devices) can be the
                  Ansible group C(os_iosxe). Device groups that are not in the mapping keep
                  their own name.
                - Keys match the LibreNMS name exactly, case included, and before any
                  conversion. A key matching no device group is reported at C(-v), since a
                  mistyped one would otherwise do nothing at all.
                - Values are used as given, so they must already be valid Ansible group
                  names. One that is not fails the inventory run immediately, naming the
                  entry and what it would have had to become.
                - Applied after I(group_name_regex_filter), which keeps matching against the
                  LibreNMS name - a device group has to survive the filter before it can be
                  renamed.
                - Mapping two device groups to the same name merges them deliberately and is
                  not reported. A mapped name landing on a name some other device group was
                  converted to still is.
            type: dict
            default: {}
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
                - LibreNMS device group names are free text, so they regularly contain
                  characters an Ansible group name cannot - whitespace, but also C(-), C(.),
                  C(/), C(&) and the rest. Each one is replaced with an underscore before the
                  group is created, using Ansible's own conversion, so C(Network Core) becomes
                  the Ansible group C(Network_Core) and C(Site-1) becomes C(Site_1).
                - A name starting with a digit is prefixed with an underscore rather than
                  having the digit overwritten, so C(1st Floor) becomes C(_1st_Floor).
                  Surrounding whitespace is dropped instead of becoming underscores.
                - Two device groups whose names differ only in characters that are converted
                  (eg. C(Network Core) and C(Network-Core)) become one Ansible group holding
                  both memberships, and a device group named C(all) or C(ungrouped) merges
                  into the group Ansible creates itself. Both are reported as warnings.
                - Regex filters in I(group_name_regex_filter) still match against the name as
                  LibreNMS reports it, not the converted one.
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
                - Named groups C((?P<family>...)) and C((?P<variant>...)) set the family
                  and variant explicitly, for values where the variant does not follow the
                  family - a pattern of '(?:(?P<variant>node\d+) )?Juniper (?P<family>SRX\d+)'
                  gives C(node0 Juniper SRX340 Internet Router) a family of C(SRX340) and a
                  variant of C(node0). A named variant group that does not take part in the
                  match leaves the variant unset.
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

# Also write the inventory out as a static YAML inventory file. That file is a real
# inventory source on its own (`-i inventory/librenms.yml`), so a run over it never
# touches the API. It is a snapshot, not a cache - see the README's "Writing the
# inventory to a file" section.
write_to_file: false
# inventory_file: ./inventory/librenms.yml

exclude_disabled: true
exclude_ignored: true

# Matched against the group name as LibreNMS reports it. Whitespace in a matching
# group's name becomes underscores in the Ansible group name, eg. `Network_Core`.
group_name_regex_filter:
  - ^Network Core$
  - ^Site .*$

# Rename device groups on the way in. Keys are the LibreNMS name, exactly as the API
# reports it; values are used as the Ansible group name as given.
group_name_mapping:
  Cisco devices: os_iosxe
  Network Core: core

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

import contextlib
import json
import os
import re
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import uuid
from collections import defaultdict

import yaml
from ansible.errors import AnsibleError
from ansible.inventory.group import to_safe_group_name
from ansible.module_utils.common.text.converters import to_text
from ansible.module_utils.urls import open_url
from ansible.parsing.yaml.dumper import AnsibleDumper
from ansible.plugins.inventory import BaseInventoryPlugin, Cacheable

# Ansible creates these two itself for every inventory (see InventoryData.__init__), so a
# LibreNMS device group of the same name silently merges into one of them instead of
# becoming a group of its own.
IMPLICIT_GROUPS = ("all", "ungrouped")

# Ansible sets these on every Host as it is added (see InventoryData.add_host), so they
# are not host vars this plugin produced and have no business in a snapshot file - they
# describe the inventory source the host came from, which is a different file by then.
IMPLICIT_HOST_VARS = ("inventory_file", "inventory_dir")

# What Ansible's own yaml inventory plugin will read back (its yaml_extensions option,
# plus a name with no extension at all). A snapshot written under any other name is a
# file that nothing can load, which is worth saying out loud.
READABLE_SNAPSHOT_EXTENSIONS = ("", ".yml", ".yaml", ".json")

# A host key in a YAML inventory is a pattern, not a name: Ansible runs it through
# parse_address() and expand_hostname_range(), so "host:8022" reads back as the host
# "host" with ansible_port 8022, and "sw[1:3]" reads back as three hosts. Quoting does
# not help, the expansion happens after the YAML is parsed.
UNROUNDTRIPPABLE_HOSTNAME_CHARS = ("[", "]", ":")

# Jinja2 delimiters. A value LibreNMS reported is untrusted on a live run, so Ansible
# never templates it, while the same value read back out of a static inventory file is
# trusted and would be evaluated. That is Ansible's rule for any static inventory, not
# something the file can opt out of, so the host carrying one gets named instead.
TEMPLATE_MARKERS = ("{{", "{%")


class InventoryModule(BaseInventoryPlugin, Cacheable):
    NAME = "librenms"

    # A location "unit spec" segment: an optional stack number then a literal 'u' then
    # the unit number, eg. "u10", "2u5". Anything else non-empty in that position is a
    # rack name. See parse_location_field for details
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
        matched nothing) are visible on lower debug levels.

        Args:
            message (str): What went wrong and what the plugin did about it
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
            self._step(f"http {e.code} from {url}, checking body for an API message")
            return self._parse_response(url, e, http_status=e.code)
        except urllib.error.URLError as e:
            raise AnsibleError(
                f"Could not reach the LibreNMS API at {url}. "
                f"Check api_endpoint, validate_certs and timeout"
            ) from e

        self._step(f"200 OK http from {url}")
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
                f"Response from {url} is not valid UTF-8. Check api_endpoint correctness"
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
                f"Response from {url} is not JSON. Check api_endpoint correctness. "
                f"Body was: {raw}"
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
            membership(defaultdict): A dict keyed with the device IDs with a list of the
                                    Ansible group names they belong to, already converted
                                    from the LibreNMS ones. If a device ID is not in this
                                    dict, it has no group memberships
        """
        self._step("fetching device groups")

        # Fetch all defined groups
        payload = self._fetch(self.api_endpoint + "/devicegroups")
        all_groups = payload.get("groups", [])
        self._step(f"{len(all_groups)} device groups returned")

        # A key that matches nothing would otherwise be silent, and a name that differs
        # only in case or in a stray space looks right in the inventory source
        unmatched = sorted(set(self.group_name_mapping) - {group["name"] for group in all_groups})
        if unmatched:
            self._problem(
                f"group_name_mapping matched no device group for {', '.join(repr(n) for n in unmatched)}, "
                f"keys have to match the LibreNMS name exactly, case included."
            )

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
        # Names are resolved once per group, rather than once per member, so that a group
        # whose name is a problem is reported once per run
        resolved_names = {}
        # Fetch the members of each fetched group and map them together
        for group in groups:
            # A mapped name is used as written - it was checked when the options were read
            mapped_name = self.group_name_mapping.get(group["name"])
            group_name = mapped_name or self._sanitize_group_name(group["name"])
            if not group_name:
                self.display.warning(
                    f"[librenms] skipping device group {group['name']!r}: nothing is "
                    f"left of its name once it is converted"
                )
                continue

            if group_name != group["name"]:
                self._step(f"device group {group['name']} is named {group_name} in the inventory")

            if group_name in IMPLICIT_GROUPS:
                self.display.warning(
                    f"[librenms] device group {group['name']!r} has the same name as the group "
                    f"Ansible creates for every inventory, its members merge into that one"
                )

            first_seen, first_was_mapped = resolved_names.setdefault(
                group_name, (group["name"], bool(mapped_name))
            )
            # Mapping two device groups to one name is a deliberate merge; every other way
            # of arriving at the same name is an accident of the conversion
            if first_seen != group["name"] and not (first_was_mapped and mapped_name):
                self.display.warning(
                    f"[librenms] device groups {first_seen!r} and {group['name']!r} both end up "
                    f"as the group {group_name}, their members are added to the same group"
                )

            self._step(f"fetching members of device group {group['name']}")
            # The name goes into the URL path, so it has to be quoted - LibreNMS group
            # names are free text and may contain whitespace or other unsafe characters.
            quoted_name = urllib.parse.quote(group["name"], safe="")
            member_payload = self._fetch(self.api_endpoint + "/devicegroups/" + quoted_name)
            members = member_payload.get("devices", [])
            self._step(f"device group {group['name']} has {len(members)} members")
            for device in members:
                membership[device["device_id"]].append(group_name)

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
        return value is not None and str(value) not in ("0", "")

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
        fields = [self.hostname_field] if self.hostname_field else ["sysName", "hostname"]
        for field in fields:
            value = device.get(field)
            if value:
                return self._ascii_hostname(str(value))

        generated = str(uuid.uuid4())
        self._problem(
            f"device {self._device_label(device)} has no {' or '.join(fields)}, "
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

        # Vars parsed out of notes/location are additional - libre_notes and libre_location
        # themselves are set unmodified below by the general field loop.
        for enabled, source, parser in (
            (self.parse_notes_field, "notes", self._parse_notes),
            (self.parse_location_field, "location", self._parse_location),
        ):
            if not enabled or not device.get(source):
                continue
            parsed = parser(hostname, device.get(source))
            if parsed is not None:
                self._step(f"{hostname}: adding {len(parsed)} vars from the {source} field")
                for field, value in parsed.items():
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
        reported.

        Args:
            hostname (str): The hostname of the device
            device (dict): The full device dictionary fetched from the API
            hardware_value: The device's hardware value, as reported by LibreNMS
        """
        self.inventory.set_variable(hostname, "libre_hardware", hardware_value)

        # LibreNMS does not report hardware for devices it can not identify
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

        # Do not set a variable if no variant data was found
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

        # The room-value is the second segment after the site segment, if there are more than one segment
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
                # This is not a unit spec, start a new rack context.
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

    def _validate_group_name_mapping(self, configured):
        """Validates the group_name_mapping option

        The values are used as Ansible group names as given, rather than being converted
        like the LibreNMS names are, so a value that is not already a valid group name is
        a mistake in the inventory source rather than something to quietly fix. Checking
        up front fails the run naming the entry, instead of producing a group under a name
        the user did not write.

        Args:
            configured (dict): The raw option value, keyed on LibreNMS device group name

        Returns:
            mapping (dict): LibreNMS device group name mapped to its Ansible group name
        """
        mapping = {}

        for librenms_name, ansible_name in (configured or {}).items():
            if not isinstance(ansible_name, str) or not ansible_name.strip():
                raise AnsibleError(
                    f"group_name_mapping['{librenms_name}'] must be the Ansible group name "
                    f"to use, got {ansible_name!r}."
                )

            converted = self._sanitize_group_name(ansible_name)
            if converted != ansible_name:
                raise AnsibleError(
                    f"group_name_mapping['{librenms_name}'] is not a valid Ansible group "
                    f"name: {ansible_name!r} would have to be written {converted!r}."
                )

            # A YAML key of 123 parses as an int, while LibreNMS always reports a string
            mapping[str(librenms_name)] = ansible_name

        return mapping

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
                if "family" in pattern.groupindex:
                    family = match.group("family")
                else:
                    family = match.group(1) if pattern.groups else match.group(0)

                # A named variant group is for values where the variant comes before
                # the family, eg. the cluster node on a Juniper SRX. It is None when
                # an optional group did not take part in the match.
                if "variant" in pattern.groupindex:
                    variant = match.group("variant")
                else:
                    # Whatever the pattern did not consume is the variant. The separator
                    # it broke on is an artefact of where the boundary fell, not part of
                    # the value, so it is stripped.
                    variant = hardware_value[match.end():].lstrip("-/_ ") or None
                return family, variant

        self._step(f"no {os_name} hardware pattern matched {hardware_value}, keeping it as is")
        return hardware_value, None

    # --- Grouping -----------------------------------------------------

    @staticmethod
    def _sanitize_group_name(name):
        """Convert a LibreNMS device group name into a safe Ansible group name

        LibreNMS group names are free text, so they regularly contain characters an
        Ansible group name cannot - whitespace, but also "-", ".", "/", "&" and the
        rest. The conversion itself is Ansible's own to_safe_group_name(), the hook the
        inventory base class puts here for plugins to override, so these group names
        follow the same convention as every other inventory source rather than a rule of
        this plugin's own.

        Two things are done that to_safe_group_name() does not:

        - It is called with force=True, so the name is converted whatever the
          controller's TRANSFORM_INVALID_GROUP_CHARS setting is (it defaults to 'never',
          which only warns), and silently, since the conversion is deliberate here and
          the run reports it at -vvvv instead.
        - A leading digit is prefixed rather than replaced. Ansible's rule rejects a name
          that starts with one, and its substitution overwrites the digit, so
          "1st Floor" would become "_st_Floor". Prefixing gives "_1st_Floor", which
          satisfies the same rule without losing a character.

        Args:
            name (str): The device group name as reported by LibreNMS

        Returns:
            str: The converted name, empty if the name held nothing to convert
        """
        # Surrounding whitespace is invisible in the LibreNMS UI and all but always
        # accidental, so it is dropped rather than turned into leading/trailing
        # underscores. Whitespace inside the name is left to Ansible's substitution.
        name = name.strip()
        if not name:
            return name

        if re.match(r"\d", name):
            name = "_" + name

        return to_safe_group_name(name, replacer="_", force=True, silent=True)

    def _add_host_to_device_groups(self, device_id, hostname, membership):
        """Add a host to groups according to its LibreNMS group memberships

        Args:
            device_id (int): The ID of the device
            hostname (str): The hostname of the device
            membership (dict): A mapping of device IDs to their corresponding group memberships
        """
        for group_name in membership.get(device_id, []):
            # The names in the membership map were converted when it was built
            transformed_group_name = self.inventory.add_group(group_name)
            self._step(f"adding {hostname} to group {transformed_group_name}")
            # Add the host to the group
            self.inventory.add_host(group=transformed_group_name, host=hostname)
            self.added_groups[transformed_group_name].add(hostname)

    # --- Snapshot file --------------------------------------------------

    def _resolve_snapshot_path(self, source_path):
        """Work out the absolute path the snapshot should be written to

        A relative inventory_file resolves against the directory holding the inventory
        source file rather than the current working directory, so that the snapshot lands
        in the same place whether ansible was run from the project root or from somewhere
        below it.

        Args:
            source_path (str): Path of the inventory source file being parsed

        Returns:
            str: The absolute path to write the snapshot to
        """
        source_path = os.path.abspath(source_path)
        source_dir = os.path.dirname(source_path)

        configured = self.get_option("inventory_file")
        if configured:
            target = os.path.expanduser(os.path.expandvars(configured))
        else:
            # eg. librenms.yml -> librenms.generated.yml. Derived from the source file's
            # name so that two LibreNMS sources in one run do not land on one file.
            target = os.path.splitext(os.path.basename(source_path))[0] + ".generated.yml"
            self._step(f"inventory_file is unset, defaulting to {target}")

        if not os.path.isabs(target):
            target = os.path.join(source_dir, target)
        target = os.path.normpath(target)

        if os.path.isdir(target):
            raise AnsibleError(
                f"inventory_file resolved to {target}, which is an existing directory. "
                f"It has to name the file to write, filename included."
            )

        # The default name in the source file's own directory is one typo away from the
        # plugin writing its output over the configuration that produced it
        if self._is_same_file(target, source_path):
            raise AnsibleError(
                f"inventory_file resolved to {target}, which is the inventory source file "
                f"itself. Writing the inventory there would destroy the configuration that "
                f"produced it. Point it at a different file."
            )

        # A mistyped path lands in the same directory as the source, where the other
        # inventory sources of the run live too
        for processed in self.inventory.processed_sources:
            if self._is_same_file(target, processed):
                raise AnsibleError(
                    f"inventory_file resolved to {target}, which is an inventory source this "
                    f"run has already parsed. Point it at a file that is not itself an "
                    f"inventory source."
                )

        if self._declares_a_plugin(target):
            raise AnsibleError(
                f"inventory_file resolved to {target}, which is an inventory plugin "
                f"configuration - it has a plugin key. Refusing to overwrite it, since "
                f"Ansible would not read the result back as an inventory anyway."
            )

        if os.path.splitext(target)[1].lower() not in READABLE_SNAPSHOT_EXTENSIONS:
            self._problem(
                f"inventory_file {target} does not end in .yml, .yaml or .json, so "
                f"ansible -i will not read it back. The file is still written."
            )

        self._step(f"write_to_file is on, the snapshot will be written to {target}")
        return target

    @staticmethod
    def _is_same_file(target, other):
        """Test whether two paths name the same file, whether or not the first exists yet

        Args:
            target (str): The path the snapshot would be written to
            other (str): The path to compare it against

        Returns:
            bool: True if the two name one file
        """
        other = os.path.abspath(other)
        if target == other:
            return True
        # samefile() needs both to exist, and the target normally does not yet
        return os.path.exists(target) and os.path.exists(other) and os.path.samefile(target, other)

    @staticmethod
    def _declares_a_plugin(target):
        """Test whether a file is an inventory plugin configuration rather than inventory data

        The default filename sits beside the inventory source, so a mistyped inventory_file
        can land on another source file in that same directory - this plugin's own, or a
        chained constructed one. Ansible's yaml inventory plugin refuses any file with a
        plugin key, so such a file is never a snapshot this plugin wrote, and overwriting
        it costs the user a configuration they cannot get back. Only the head of the file
        is read: a plugin configuration is a handful of lines, a snapshot can be megabytes.

        Args:
            target (str): The path the snapshot would be written to

        Returns:
            bool: True if the file exists and declares a plugin
        """
        try:
            with open(target, encoding="utf-8", errors="replace") as handle:
                head = handle.read(8192)
        except OSError:
            # Absent or unreadable: nothing this check can rule out, and the write itself
            # reports whatever actually stops it
            return False

        return re.search(r"^plugin:\s*\S", head, re.MULTILINE) is not None

    @classmethod
    def _contains_a_template(cls, value):
        """Test whether a host var value holds something Jinja2 would evaluate

        Args:
            value: A host var value, of whatever type the API or the Notes field produced

        Returns:
            bool: True if the value, or anything inside it, holds Jinja2 delimiters
        """
        if isinstance(value, str):
            return any(marker in value for marker in TEMPLATE_MARKERS)
        if isinstance(value, dict):
            return any(cls._contains_a_template(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(cls._contains_a_template(item) for item in value)
        return False

    def _build_snapshot(self):
        """Build the inventory this run produced, in Ansible's own YAML inventory format

        Only what this plugin added goes in. The InventoryData object is shared by every
        inventory source in a run, so reading it whole would put an earlier source's hosts
        - and their vars - into this plugin's snapshot.

        Host vars are written once, under "all", and each group lists its hosts with no
        value of their own. Ansible's yaml inventory plugin reads a null host entry as
        "a member of this group, no vars here", so a host in several groups is listed in
        each without its vars being repeated, and a host in no group at all is still
        covered by being under "all".

        Returns:
            snapshot(dict): The inventory, ready to be serialized as YAML
        """
        self._step(f"building a snapshot of {len(self.added_hosts)} hosts")

        hosts = {}
        for hostname in sorted(self.added_hosts):
            if any(char in hostname for char in UNROUNDTRIPPABLE_HOSTNAME_CHARS):
                self._problem(
                    f"host {hostname} contains one of {' '.join(UNROUNDTRIPPABLE_HOSTNAME_CHARS)}, "
                    f"which a YAML inventory reads as a port or a host range, so the snapshot "
                    f"will not read back as this host."
                )

            host_vars = {
                field: value
                for field, value in self.inventory.get_host(hostname).vars.items()
                if field not in IMPLICIT_HOST_VARS
            }
            for field, value in host_vars.items():
                if self._contains_a_template(value):
                    self._problem(
                        f"{hostname}: {field} contains Jinja2 delimiters, which a live run "
                        f"leaves alone but a static inventory file evaluates, so this value "
                        f"will not mean the same thing read back from the snapshot."
                    )

            # A host with nothing left is still a host, it just carries no vars
            hosts[hostname] = host_vars or None

        snapshot = {"all": {"hosts": hosts}}

        children = {
            group_name: {"hosts": dict.fromkeys(sorted(hostnames))}
            for group_name, hostnames in self.added_groups.items()
            if group_name not in IMPLICIT_GROUPS
        }
        if children:
            self._step(f"snapshot has {len(children)} groups")
            snapshot["all"]["children"] = children
        else:
            self._step("snapshot has no groups")

        return snapshot

    def _write_snapshot(self):
        """Write the snapshot to the path resolved from inventory_file

        The file is replaced atomically, so a run that is interrupted part way through
        the write cannot leave half an inventory behind for the next one to read.
        """
        if not self.added_hosts:
            self._problem(
                f"no hosts were added, leaving {self.snapshot_path} as it is. A snapshot is "
                f"only written for a run that produced hosts."
            )
            return

        snapshot = self._build_snapshot()

        # No timestamp: with the contents sorted and nothing else moving, the file is a
        # diff only when the inventory itself changed, which is what makes a committed
        # snapshot worth reviewing
        header = (
            f"# Ansible inventory written by the librenms inventory plugin from {self.snapshot_source}.\n"
            "# Generated file - regenerate it rather than editing it, the next run overwrites it.\n"
        )

        try:
            # AnsibleDumper, not safe_dump: in a real run InventoryManager hands the plugin
            # a wrapper that tags every value it sets, so host vars come back as
            # _AnsibleTaggedStr/_AnsibleTaggedInt and friends, which safe_dump cannot
            # represent. AnsibleDumper writes them as the plain scalars they are. It also
            # covers the datetime a Notes field can parse into.
            body = yaml.dump(
                snapshot,
                Dumper=AnsibleDumper,
                sort_keys=True,
                default_flow_style=False,
                allow_unicode=True,
                # Keeps PyYAML from reflowing at 80 columns, where one value getting longer
                # rewrites the lines around it
                width=4096,
                explicit_start=True,
            )
        except yaml.YAMLError as e:
            raise AnsibleError(
                f"The inventory could not be serialized as YAML for {self.snapshot_path}: {e}"
            ) from e

        target_dir = os.path.dirname(self.snapshot_path) or "."
        # A snapshot can hold whatever exclude_fields did not strip, so a mode the user
        # already chose for the file is kept rather than widened back to the default
        try:
            mode = os.stat(self.snapshot_path).st_mode & 0o777
        except OSError:
            mode = 0o644

        temp_path = None
        try:
            os.makedirs(target_dir, exist_ok=True)
            # Written in the target's own directory so the replace below stays on one
            # filesystem, where it is atomic
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=target_dir,
                prefix=".librenms-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = handle.name
                handle.write(header)
                handle.write(body)
            # NamedTemporaryFile creates the file 0600; an inventory is an ordinary file
            os.chmod(temp_path, mode)
            os.replace(temp_path, self.snapshot_path)
        except OSError as e:
            if temp_path:
                with contextlib.suppress(OSError):
                    os.unlink(temp_path)
            raise AnsibleError(
                f"Could not write the inventory snapshot to {self.snapshot_path}: {e}. "
                f"Check inventory_file, and that {target_dir} is writable."
            ) from e

        self._step(f"snapshot written to {self.snapshot_path}")

    # --- Main flow ------------------------------------------------------

    def _populate(self):
        """Populate the dynamic inventory using data fetched from the LibreNMS API
        """
        self._step("populating the inventory")

        devices = self._get_devices()

        membership = {}
        if self.device_groups_as_ansible_groups:
            membership = self._get_device_group_membership()
        else:
            self._step("device_groups_as_ansible_groups is off, skipping device groups")

        self._step(f"processing {len(devices)} devices")
        added = 0

        for device in devices:
            if self._device_excluded(device):
                continue

            hostname = self._derive_hostname(device)

            self._step(f"adding host {hostname}")
            self.inventory.add_host(hostname)
            self.added_hosts.add(hostname)
            added += 1

            self._set_host_variables(hostname, device)

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
        super().parse(inventory, loader, path)

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
        self.group_name_mapping = self._validate_group_name_mapping(
            self.get_option("group_name_mapping")
        )
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

        # Tracked while populating so that the snapshot holds the hosts and groups this
        # plugin added and nothing else - the InventoryData object is shared with every
        # other inventory source in the run
        self.added_hosts = set()
        self.added_groups = defaultdict(set)

        self.write_to_file = self.get_option("write_to_file")
        # Named relative so that two people regenerating the file from different checkouts
        # still produce the same bytes
        self.snapshot_source = os.path.basename(path)
        self.snapshot_path = None
        if self.write_to_file:
            # Resolved before the API is called, so a path that can never be written to
            # fails the run now rather than after a full set of requests
            self.snapshot_path = self._resolve_snapshot_path(path)
        else:
            self._step("write_to_file is off, no snapshot file will be written")

        self._step("options applied")

        # Populate the dynamic inventory
        self._populate()

        if self.write_to_file:
            self._write_snapshot()

        self._step("done")
