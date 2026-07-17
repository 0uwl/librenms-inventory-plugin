# LibreNMS Ansible Inventory Plugin — Design Plan

## Context

The goal is to build a modern Ansible inventory plugin that pulls hosts from LibreNMS, replacing the abandoned
[mschedrin/librenms-ansible-inventory-plugin](https://github.com/mschedrin/librenms-ansible-inventory-plugin).
That project works but is limited: it can only group hosts by LibreNMS *device groups*, requires `requests` +
`unidecode` as extra pip dependencies, does one HTTP request per device (N+1 pattern), and hand-rolls cache validation. 
Its own trailing TODOs literally ask for the two things being requested here: *"group by option to create ansible-groups 
from libre device properties"* and *"evaluate composited vars... see example in netbox inventory plugin"*.

The [Official NetBox inventory plugin](https://github.com/netbox-community/ansible_modules/blob/devel/plugins/inventory/nb_inventory.py)
(`nb_inventory.py`) is the reference implementation for property-based `group_by`, Ansible's
built-in `Constructable` mixin (`compose` / `groups` / `keyed_groups`), and the standard `inventory_cache`
fragment instead of manual cache bookkeeping. This plan adapts those patterns to LibreNMS's simpler,
non-paginated API.

Decisions already made:
- **Packaging**: standalone plugin file (like the original project), not a full Galaxy collection.
- **Dependencies**: zero extra pip dependencies — use Ansible's built-in `open_url` and stdlib `unicodedata`
  instead of `requests` + `unidecode`.

## Repo layout

```
librenms-inventory-plugin/
├── LICENSE
├── README.md                          # rewritten: install, config, migration notes from old plugin
├── inventory_plugins/
│   └── librenms.py                    # the plugin (NAME = 'librenms')
├── examples/
│   ├── librenms.yml.dist              # example inventory source config
│   └── ansible.cfg.example
└── tests/
    └── unit/
        ├── fixtures/*.json            # sample LibreNMS API payloads (devices, devicegroups, devicegroups/<name>)
        └── test_librenms_inventory.py
```

The old `librenms-inventory-script.py` (dynamic-inventory script) is dropped. The old README itself says
"scripts are probably being deprecated" in favor of the plugin, and maintaining both doubles the surface area
for no real benefit.

## LibreNMS API facts from docs.librenms.org

- `GET /devices` returns full device objects in a single non-paginated response (`{"status","count","devices":[...]}`),
  filterable server-side via `type=` (`all|active|ignored|up|down|disabled`, or search-by-attribute values like
  `os`, `location`, `hardware`, etc. combined with a `query` param).
  - Device object fields include: `device_id`, `hostname`, `sysName`, `os`, `os_version`, `hardware`, `type`,
  `status`, `location`, `location_id`, `disabled`, `ignore`, `purpose`, `vendor`, `serial`, `version`, `uptime`,
  `ip`, `icon`.
- `GET /devicegroups` lists groups; `GET /devicegroups/<name>` returns member device IDs (confirmed from the old
  plugin's working code). Only membership is returned, not the full device data.
- Unlike NetBox, LibreNMS needs no pagination/chunking logic. It also means `/devices` should be fetched directly for'
  full device data instead of the old plugin's fetch chain of group -> id→per-device, which
  turns an O(groups + devices) API call pattern into O(groups). Devices are fetched once in one call.

## Core architecture (`inventory_plugins/librenms.py`)

`class InventoryModule(BaseInventoryPlugin, Constructable, Cacheable)`, `NAME = 'librenms'`.

### 1. Fetching (replaces old group→device-id→per-device-fetch chain)
- `_fetch(url)`: wraps `ansible.module_utils.urls.open_url`, sends `X-Auth-Token` header, raises `AnsibleError`
  on `status == "error"`, with the old plugin's workaround preserved (LibreNMS returns an error payload for
  "No devices found in group" — treat that one message as an empty result, not a failure).
- `_get_devices()`: single `GET /devices`, optionally passing `type=` (from a new `device_status_filter` option)
  and a NetBox-style `query_filters` list of raw query-string params, letting LibreNMS filter server-side when
  possible.
- `_get_device_group_membership()`: `GET /devicegroups` (filtered client-side by `group_name_regex_filter`,
  reusing the old plugin's regex-list-of-filters semantics), then `GET /devicegroups/<name>` per surviving group
  to build a `device_id -> [group names]` map. Groups excluded by the filter never trigger a membership call.
- All fetches go through the `Cacheable` mixin using `extends_documentation_fragment: inventory_cache` +
  `self.get_cache_key(path)` + `self._cache[...]`, replacing the old plugin's hand-rolled cache-key/filter
  revalidation logic. A `cache_force_update` boolean option is kept (not just relying on `--flush-cache`) since
  that's how the old plugin let AWX jobs force a refresh without CLI flags.

### 2. Filtering
- `host_name_regex_filter`, `group_name_regex_filter`, `regex_ignore_case` — same names/semantics as the old
  plugin (list of regexes, `re.match`), for drop-in migration.
- `exclude_disabled` (kept) and new `exclude_ignored` (the old plugin never checked LibreNMS's `ignore` flag,
  which is distinct from `disabled`).

### 3. Hostname derivation
- Same fallback chain as old plugin: prefer `sysName`, fall back to `hostname`; ASCII-normalize via
  `unicodedata.normalize('NFKD', ...).encode('ascii', 'ignore')` instead of `unidecode` (drops the dependency,
  same practical effect for transliteration of accented characters).
- New `hostname_field` option (à la `nb_inventory`) to let users pick any device field as the inventory hostname
  instead of the fixed sysName/hostname chain.

### 4. Hostvars
- Every raw device field is set as `libre_<field>` (unchanged — preserves existing playbooks).
- `variable_name_map` (default: `hostname`/`libre_hostname` → `ansible_host`, `os`/`libre_os` →
  `ansible_network_os`) — kept from old plugin but exposed as a configurable dict option instead of a hardcoded
  constant.
- `os_name_map` (default: `{asa: asa, ios: ios, iosxe: ios}`) — same idea, now user-configurable since
  `ansible_network_os` naming depends on which collections the user has installed.

### 5. Grouping — the main new feature
Three complementary mechanisms, all additive to existing behavior:
1. **LibreNMS device groups as Ansible groups** (old behavior, kept as default): controlled by a new
   `device_groups_as_ansible_groups` bool (default `true`), using the membership map from step 1.
2. **`group_by` extractor list** (new, modeled directly on `nb_inventory.py`'s `group_extractors` property +
   `generate_group_name`): a `group_extractors` dict property mapping option names to small `extract_*(device)`
   methods — e.g. `os`, `hardware`, `type`, `status`, `location`, `vendor`, `version` (`os_version`), `disabled`,
   `ignored`. `generate_group_name(grouping, value)` produces `f"{grouping}_{value}"` (sanitized), with the same
   boolean special-case NetBox uses (a true boolean produces a group named after the grouping itself, e.g.
   `disabled`, not `disabled_True`; false produces no group).
3. **Ansible `Constructable`** (`extends_documentation_fragment: constructed`): wire up `self._set_composite_vars`
   (`compose`), `self._add_host_to_composed_groups` (`groups`), `self._add_host_to_keyed_groups` (`keyed_groups`)
   in the per-host loop, exactly as `nb_inventory.py` does — this is the direct answer to the old plugin's own
   TODO comment and gives power users arbitrary Jinja2-based grouping without waiting on new `group_by` choices.

### 6. `parse()` flow
```
parse() -> read all options into self.* -> main():
  devices = _get_devices()                      # 1 API call (+ cache)
  membership = _get_device_group_membership()    # 1 + N-filtered-groups API calls (+ cache)
  for device in devices:
      apply host_name_regex_filter / exclude_disabled / exclude_ignored -> skip if filtered
      hostname = derive_hostname(device)
      inventory.add_host(hostname)
      set libre_* hostvars + mapped vars
      if device_groups_as_ansible_groups: add to membership[device_id] groups
      add_host_to_groups(device, hostname)        # group_by extractors
      _set_composite_vars / _add_host_to_composed_groups / _add_host_to_keyed_groups
```

## Testing strategy
- Unit tests (`tests/unit/test_librenms_inventory.py`) using `unittest.mock` to stub `open_url`, with JSON
  fixtures under `tests/unit/fixtures/` modeled on the LibreNMS API doc examples (`devices.json`,
  `devicegroups.json`, `devicegroups_<name>.json`). Cover: basic parse, each `group_by` extractor, regex filters
  (case-sensitive/insensitive), `exclude_disabled`/`exclude_ignored`, hostname unicode normalization, and cache
  hit/bypass/`cache_force_update` behavior.
- Manual end-to-end verification: `ansible-inventory -i inventory_plugins/librenms.yml --list -vvv` against a
  real/test LibreNMS instance the user points `LIBRENMS_API`/`LIBRENMS_TOKEN` at — this requires the user's own
  instance/credentials and can't be done from here.

## README updates
- Install/config instructions (same shape as old README: `ansible.cfg` `inventory_plugins` path, `enable_plugins`).
- Migration notes for old-plugin users: file moved to `inventory_plugins/librenms.py`, `requests`/`unidecode` no
  longer needed, standalone script removed, new `group_by`/`compose`/`groups`/`keyed_groups`/`exclude_ignored`
  options documented with examples.
