# librenms-inventory-plugin

An Ansible inventory plugin for LibreNMS integration. Pulls devices from a LibreNMS
instance and exposes them as an Ansible dynamic inventory, with support for grouping by
LibreNMS device groups. Every device field is exposed as a `libre_<field>` host var, so
property-based grouping or composed vars are left to Ansible's standard `constructed`
inventory plugin, chained as a second inventory source - see [Grouping](#grouping).

This started as a rewrite of
[mschedrin/librenms-ansible-inventory-plugin](https://github.com/mschedrin/librenms-ansible-inventory-plugin),
inspired by the design of the
[NetBox inventory plugin](https://github.com/netbox-community/ansible_modules)

## Requirements

- `ansible-core`
- A LibreNMS instance with API access enabled and an API token.

## Installation

Clone this repository, then point Ansible at the `inventory_plugins/` directory and
enable the plugin, either via `ansible.cfg` (see `examples/ansible.cfg.example`):

```ini
[defaults]
inventory_plugins = ./inventory_plugins

[inventory]
# Modifying enable_plugins overrides the default list of enabled plugins so librenms is
# appended here to the default list as per the Ansible documentation.
enable_plugins = host_list, script, auto, yaml, ini, toml, librenms
```

## Configuration

Create an inventory source file - see `examples/librenms.yml.dist` for a starting
point:

```yaml
plugin: librenms
api_endpoint: https://librenms.example.com/api/v0
# api_token is best provided via the LIBRENMS_TOKEN environment variable or through a 
# vault variable (see example bellow) instead of committing it to this file.
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
```

Export your API token and test it:

```bash
export LIBRENMS_TOKEN=your-token-here
ansible-inventory -v --list -i librenms.yml
```

Full list of options: `ansible-doc -t inventory librenms`.

## Host variables

Every field returned by the LibreNMS API for a device is set as a `libre_<field>`
host var (e.g. `libre_hardware`, `libre_os`, `libre_location`), except for the fields
listed in `exclude_fields` (see [Sensitive fields](#sensitive-fields) below).

Two host vars are derived rather than reported - `libre_hardware_family` and
`libre_hardware_variant` - and only exist when
[hardware trimming](#trimming-hardware-to-a-product-family) is configured. Every other
`libre_<field>` is exactly what the API returned; no option modifies one.

Anything derived from those fields - e.g. `ansible_host` from `libre_hostname`, or
`ansible_network_os` translated from `libre_os` (`iosxe` -> `ios`) - is left to a chained
`constructed` inventory source's `compose` option; see [Grouping](#grouping).

## Trimming hardware to a product family

`libre_hardware` is the exact product ID LibreNMS discovered - `C9300-48P`,
`WS-C3850-24T`, `N9K-C93180YC-EX`. That is too specific to group on: two switches of the
same family land in different groups because their port counts differ.

`hardware_trimming_patterns` maps a LibreNMS `os` to a list of regular expressions. For
each device the patterns under its own `os` are tried in order and the first match wins.
The option is off until you set it.

```yaml
# librenms.yml
hardware_trimming_patterns:
  ios: &cisco_catalyst
    - '^WS-(C\d+[A-Z]*)'
    - '^C\d+[A-Z]*'
  iosxe: *cisco_catalyst
  nxos:
    - '^[NC]?\dK-C\d+[A-Z]*'
```

This **adds** two host vars and leaves `libre_hardware` alone:

| `libre_hardware` | `os` | `libre_hardware_family` | `libre_hardware_variant` |
| --- | --- | --- | --- |
| `C9300-48P` | `iosxe` | `C9300` | `48P` |
| `C9200CX-12P-2X2G` | `iosxe` | `C9200CX` | `12P-2X2G` |
| `WS-C3850-24T` | `ios` | `C3850` | `24T` |
| `N9K-C93180YC-EX` | `nxos` | `N9K-C93180YC` | `EX` |
| `N7K-C7010` | `nxos` | `N7K-C7010` | *(unset)* |
| `EX4300-48T` | `junos` | `EX4300-48T` | *(unset)* |

Group on the family instead of the raw value:

```yaml
# constructed.yml
keyed_groups:
  - key: libre_hardware_family
    prefix: hw
```

Two things to know when writing patterns:

- **A capture group drops what precedes it.** Without a group the whole match becomes the
  family; with one, only the first group does. That is how `^WS-(C\d+[A-Z]*)` puts older
  Catalyst switches under `C3850` rather than a `WS-C3850` namespace of their own.
- **Order matters.** Patterns are tried top to bottom, so put the specific ones first.
  Under `nxos`, `^C\d+[A-Z]*` before `^[NC]?\dK-C\d+[A-Z]*` would give `C9K-C93180YC` a
  family of `C9` rather than `C9K-C93180YC`.

Keying on `os` rather than a vendor is deliberate: LibreNMS always populates `os`, and it
already separates Catalyst from Nexus, whose product IDs need different patterns. Use a
YAML anchor, as above, when several `os` values share a list. Nothing here is
Cisco-specific - `junos: ['^EX\d+']` gives `EX4300-48T` a family of `EX4300`.

An invalid regular expression fails the inventory run immediately and names the pattern,
rather than silently skipping devices.

### Why `libre_hardware` is left alone

The family and the variant together are lossy, so the reported product ID has to survive
somewhere. `^WS-(C\d+[A-Z]*)` matches the `WS-` and discards it: `WS-C3850-24T` becomes a
family of `C3850` and a variant of `24T`, and nothing recombines those into the original.
Anything a pattern consumes without capturing is gone from both derived vars, which is
exactly what you want for grouping and exactly what you do not want when looking up a
spare or an end-of-life date.

So `libre_hardware` keeps holding what the API returned, under every combination of these
options, and the derived vars are additive.

### The family and the variant

`libre_hardware_family` is the trimmed value. Devices whose `os` has no patterns, and
values that match none of their patterns, get the full product ID as their family - so a
`keyed_groups` on it covers every device, with the untrimmed ones simply grouped more
finely.

`libre_hardware_variant` is what the pattern left behind, with the separator it broke on
stripped, so `C9300-48P` gives `48P` rather than `-48P`. It is left unset - not empty, not
`None` - for devices whose product ID is only a family, so `keyed_groups` on the variant
cannot invent a group for devices that do not have one. Guard with `is defined`:

```yaml
# constructed.yml
compose:
  # e.g. "C9300 (48P)"
  hardware_description: >-
    libre_hardware_family ~ (' (' ~ libre_hardware_variant ~ ')'
    if libre_hardware_variant is defined else '')
```

Both are unset for devices LibreNMS reports no hardware for, and when
`hardware_trimming_patterns` is not configured at all.

### Lowercasing

The family keeps the casing LibreNMS reported. `lowercase_hardware_family` lowercases the
family and the variant so they can be dropped straight into a group name without a Jinja
filter on the consuming side:

```yaml
# librenms.yml
lowercase_hardware_family: true   # -> hw_c9300, hw_n9k_c93180yc
```

`libre_hardware` is not affected. The option does nothing unless
`hardware_trimming_patterns` is configured.

## Sensitive fields

LibreNMS' device API returns some fields in plaintext that most people would consider
secrets: the SNMP community string and SNMPv3 auth/priv passphrases
(`community`, `authpass`, `cryptopass`). These are excluded from host vars **by
default** via the `exclude_fields` option, so they never end up as Ansible facts,
`-v` output, or entries in the inventory cache file. If you actually need them (e.g. to
feed a poller task), override the option:

```yaml
exclude_fields: []   # or a list with only the fields you want to keep excluded
```

Note this only controls what the plugin exposes as host vars, it can't retroactively
secure a value that already came back from an HTTP API as plaintext JSON. Vault (below)
encrypts values *you* control at rest in files, it has no bearing on live API responses.

## Loading the API token from Ansible Vault

`api_token` supports two ways to keep it out of plaintext:

1. **Inline `!vault` block** in the inventory source file itself loaded through Ansible's normal
   vault-aware YAML loader:

   ```yaml
   plugin: librenms
   api_endpoint: https://librenms.example.com/api/v0
   api_token: !vault |
             $ANSIBLE_VAULT;1.1;AES256
             6162636465...
   ```

   Generate that block with `ansible-vault encrypt_string --name api_token 'your-token'`,
   then run with `--vault-password-file`/`ANSIBLE_VAULT_PASSWORD_FILE`/`--ask-vault-pass`.

2. **Jinja2 reference to an extra var**, templated at parse time - same pattern used by
   the NetBox inventory plugin's `token` option:

   ```yaml
   plugin: librenms
   api_endpoint: https://librenms.example.com/api/v0
   api_token: "{{ librenms_api_token }}"
   ```

   Note this only resolves against **extra vars** (`--extra-vars`), not
   `group_vars`/`host_vars` - those aren't available yet while inventory sources are
   still being parsed. To keep the value out of your shell history/CLI args, pass a
   vault-encrypted *file* as the extra-vars source instead of a literal value:

   ```bash
   ansible-vault encrypt_string --name librenms_api_token 'your-token' > secrets.yml
   ansible-inventory -i librenms.yml --list \
     --extra-vars @secrets.yml --vault-password-file ~/.vault_pass
   ```

   `secrets.yml` can also just be `ansible-vault encrypt`-ed outright instead of using
   an inline `!vault` string - either form works with `--extra-vars @<file>`.

## Grouping

This plugin only groups by **LibreNMS device groups** - enabled by default
(`device_groups_as_ansible_groups`), each device is added to an Ansible group per
LibreNMS device group it belongs to. Restrict which device groups are considered with
`group_name_regex_filter`.

For anything else - grouping by device property (os, location, ...), composed vars
(`ansible_host`, `ansible_network_os`, ...), or arbitrary Jinja2-based conditions - chain
Ansible's builtin
[constructed](https://docs.ansible.com/ansible/latest/collections/ansible/builtin/constructed_inventory.html)
inventory plugin as a **second inventory source** over this one's output, using its
`compose`/`groups`/`keyed_groups` options against the `libre_<field>` host vars this
plugin sets. `constructed` isn't enabled by default, so add it to `enable_plugins`
alongside `librenms` (see `examples/ansible.cfg.example`). See
`examples/constructed.yml.dist` for a starting point, and run both sources together:

```bash
ansible-inventory -v --list -i librenms.yml -i constructed.yml
```

### Grouping by data LibreNMS doesn't have (eg. environment)

For information that has no source in LibreNMS at all - which environment a device is
in, an owning team, etc - nothing can derive it automatically, so a person has to set it
per host. Two ways to do that:

**Option 1: the Notes field.** Set `parse_notes_field: true`, then put YAML in a
device's Notes field in the LibreNMS UI, starting with a `---` line:

```
---
deploy_environment: prod
```

The plugin parses everything after the `---` and sets each key as a host var, directly
on that host. Notes-derived vars are set as-is (not `libre_`-prefixed), so they can be 
used directly, including Ansible special vars like `ansible_host`/`ansible_user`. That 
also means a key that collides with a reserved Ansible variable name (eg. `groups`, 
`hostvars`) will override it for that host, so be mindful of the names you give to variables.
Also know that anyone with LibreNMS edit access to  a device can set arbitrary host vars for 
it with this enabled. Only enable this if that's an acceptable risk. Malformed YAML in Notes
is skipped with a warning rather than failing the whole plugin run.

**Option 2: `host_vars/<hostname>.yml`.** A file matching the Ansible inventory hostname
exactly, which Ansible merges on top of whatever `librenms.yml` set for that host
automatically - no plugin changes needed:

```yaml
# host_vars/core-sw1.yml
deploy_environment: prod
```

This keeps the data out of LibreNMS entirely (useful if Notes is already used for
something else, or LibreNMS edit access is broader than you'd want driving Ansible vars),
at the cost of a file to create by hand for every device.

Either way, to group on `deploy_environment` with `constructed`, set
`use_vars_plugins: true` in `constructed.yml` if you're using the `host_vars` option: by
default `constructed` only sees variables set directly by inventory plugins, not
`host_vars`/`group_vars` files (Notes-field vars don't need this, since the librenms
plugin sets them directly). See `examples/constructed.yml.dist` for a complete example,
and run all sources together:

```bash
ansible-inventory -v --list -i librenms.yml -i constructed.yml
```

## Migrating from mschedrin/librenms-ansible-inventory-plugin

- The plugin file now lives at `inventory_plugins/librenms.py` instead of the repo root.
- `requests` and `unidecode` are no longer required - the plugin uses Ansible's
  built-in `open_url` and the standard library.
- The standalone `librenms-inventory-script.py` dynamic-inventory script has been
  dropped in favor of the plugin (as the old README itself recommended).
- `host_name_regex_filter`, `group_name_regex_filter`, `regex_ignore_case`,
  `exclude_disabled`, and `cache_force_update` keep the same names and behavior.
- New: `exclude_ignored`, `hostname_field`, `device_status_filter`, `query_filters`.
- Property-based grouping and vars like `ansible_host`/`ansible_network_os` are no
  longer built into this plugin - chain Ansible's standard `constructed` inventory
  plugin as a second source instead (see [Grouping](#grouping)).

## Testing

```bash
python3 -m unittest discover -s tests/unit
```

Unit tests mock the LibreNMS HTTP API using fixtures under `tests/unit/fixtures/` and
exercise the real plugin through Ansible's inventory plugin loader, so they cover the
same option-parsing and grouping code paths as a real `ansible-inventory` run.

### Integration tests (optional, against a real LibreNMS instance)

`tests/integration/` runs the plugin against an actual LibreNMS instance instead of
mocked responses. It's skipped automatically unless credentials are available, so it
never runs as part of a normal offline `tests/unit` run.

Provide credentials either as real environment variables, or via a gitignored `.env`
file at the repo root:

```bash
LIBRENMS_API=http://your-test-instance/api/v0
LIBRENMS_TOKEN=your-token
```

Then run:

```bash
python3 -m unittest discover -s tests/integration -v
```

Every assertion is checked against a "ground truth" fetched directly from the same API
at test time (no hardcoded fixture values), so the suite keeps working as the target
instance's devices/groups change. It covers: default `exclude_disabled`/`exclude_ignored`
filtering against the real `disabled`/`ignore` flags, `libre_*` hostvars matching the raw
device payload, LibreNMS device-group membership
(including the case where an instance has zero device groups, which some LibreNMS
versions signal with an HTTP 404 rather than an empty list), and a real `ansible-inventory
--list` subprocess run.
