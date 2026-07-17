# librenms-inventory-plugin

An Ansible inventory plugin for LibreNMS integration. Pulls devices from a LibreNMS
instance and exposes them as an Ansible dynamic inventory, with grouping by LibreNMS
device groups, by device properties (os, hardware, location, ...), and via Ansible's
standard `compose`/`groups`/`keyed_groups` options.

This started as a rewrite of
[mschedrin/librenms-ansible-inventory-plugin](https://github.com/mschedrin/librenms-ansible-inventory-plugin),
inspired by the design of the
[NetBox inventory plugin](https://github.com/netbox-community/ansible_modules). See
[plan.md](plan.md) for the design rationale.

## Requirements

- `ansible-core` (no extra Python dependencies — the plugin only uses the standard
  library and Ansible's built-in `open_url`).
- A LibreNMS instance with API access enabled and an API token.

## Installation

Clone this repository, then point Ansible at the `inventory_plugins/` directory and
enable the plugin, either via `ansible.cfg` (see `examples/ansible.cfg.example`):

```ini
[defaults]
inventory_plugins = ./inventory_plugins

[inventory]
enable_plugins = librenms
```

or via environment variables:

```
export ANSIBLE_INVENTORY_PLUGINS=./inventory_plugins
export ANSIBLE_INVENTORY_ENABLED=librenms
```

## Configuration

Create an inventory source file — see `examples/librenms.yml.dist` for a starting
point:

```yaml
plugin: librenms
api_endpoint: https://librenms.example.com/api/v0
# api_token is best provided via the LIBRENMS_TOKEN environment variable instead of
# committing it to this file.
validate_certs: true

cache: true
cache_plugin: jsonfile
cache_connection: /tmp/librenms_inventory_cache
cache_timeout: 600

exclude_disabled: true
exclude_ignored: true

group_name_regex_filter:
  - ^Core$
  - ^Edge$

group_by:
  - os
  - type
  - location
```

Export your API token and test it:

```
export LIBRENMS_TOKEN=your-token-here
ansible-inventory -v --list -i librenms.yml
```

Full list of options: `ansible-doc -t inventory librenms`.

## Host variables

Every field returned by the LibreNMS API for a device is set as a `libre_<field>`
host var (e.g. `libre_hardware`, `libre_os`, `libre_location`). On top of that, a
small default mapping (configurable via `variable_name_map`) sets:

- `ansible_host` from `hostname`
- `ansible_network_os` from `os`, translated through `os_name_map` (e.g. `iosxe` -> `ios`)

## Grouping

Three mechanisms, usable together:

1. **LibreNMS device groups** — enabled by default (`device_groups_as_ansible_groups`),
   each device is added to an Ansible group per LibreNMS device group it belongs to.
   Restrict which device groups are considered with `group_name_regex_filter`.
2. **`group_by`** — a curated list of device properties (`os`, `os_version`, `hardware`,
   `type`, `status`, `location`, `vendor`, `disabled`, `ignored`). Each produces a group
   named `<property>_<value>` (or just `<value>` with `group_names_raw: true`).
3. **`compose` / `groups` / `keyed_groups`** — Ansible's standard
   [constructed](https://docs.ansible.com/ansible/latest/collections/ansible/builtin/constructed_inventory.html)
   options, for arbitrary Jinja2-based host vars and grouping beyond the built-in
   `group_by` choices.

## Migrating from mschedrin/librenms-ansible-inventory-plugin

- The plugin file now lives at `inventory_plugins/librenms.py` instead of the repo root.
- `requests` and `unidecode` are no longer required — the plugin uses Ansible's
  built-in `open_url` and the standard library.
- The standalone `librenms-inventory-script.py` dynamic-inventory script has been
  dropped in favor of the plugin (as the old README itself recommended).
- `host_name_regex_filter`, `group_name_regex_filter`, `regex_ignore_case`,
  `exclude_disabled`, and `cache_force_update` keep the same names and behavior.
- New: `exclude_ignored`, `hostname_field`, `device_status_filter`, `query_filters`,
  `group_by`, `group_names_raw`, `variable_name_map`, `os_name_map`, and the standard
  `compose`/`groups`/`keyed_groups` options.

## Testing

```
python3 -m unittest discover -s tests/unit
```

Unit tests mock the LibreNMS HTTP API using fixtures under `tests/unit/fixtures/` and
exercise the real plugin through Ansible's inventory plugin loader, so they cover the
same option-parsing and grouping code paths as a real `ansible-inventory` run.

### Integration tests (optional, against a real LibreNMS instance)

`tests/integration/` runs the plugin against an actual LibreNMS instance instead of
mocked responses. It's skipped automatically unless credentials are available, so it
never runs as part of a normal offline `tests/unit` run.

Provide credentials either as real environment variables, or via a gitignored `env`
file at the repo root:

```
LIBRENMS_API=http://your-test-instance/api/v0
LIBRENMS_TOKEN=your-token
```

Then run:

```
python3 -m unittest discover -s tests/integration -v
```

Every assertion is checked against a "ground truth" fetched directly from the same API
at test time (not hardcoded fixture values), so the suite keeps working as the target
instance's devices/groups change. It covers: default `exclude_disabled`/`exclude_ignored`
filtering against the real `disabled`/`ignore` flags, `libre_*` hostvars matching the raw
device payload, `group_by` grouping consistency, LibreNMS device-group membership
(including the case where an instance has zero device groups, which some LibreNMS
versions signal with an HTTP 404 rather than an empty list), and a real `ansible-inventory
--list` subprocess run.

Only use this against a disposable/test LibreNMS instance — never production, since it
requires a real API token with read access to it.
