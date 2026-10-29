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

Development (linting, tests) uses [uv](https://docs.astral.sh/uv/): `uv sync` installs
`ansible-core` plus the dev tooling from `uv.lock`.

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

Some host vars are derived rather than reported, and only exist when the option behind
them is configured: `libre_hardware_family` and `libre_hardware_variant` from
[hardware trimming](#trimming-hardware-to-a-product-family), and `libre_location_site`,
`libre_location_room`, `libre_location_positions` from
[location parsing](#parsing-the-location-field-into-rackunit-positions). Every other
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

This **adds** two host vars and leaves `libre_hardware` unchanged:

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
- **Named groups place the variant anywhere.** `(?P<family>...)` and `(?P<variant>...)`
  set each variable explicitly, for values where the variant comes first. Under `junos`,
  `(?:(?P<variant>node\d+) )?Juniper (?P<family>SRX\d+)` gives
  `node0 Juniper SRX340 Internet Router` a family of `SRX340` and a variant of `node0`, and
  a standalone `Juniper SRX340 Internet Router` the same family with no variant.
- **Order matters.** Patterns are tried top to bottom, so put the specific ones first.
  Under `nxos`, `^C\d+[A-Z]*` before `^[NC]?\dK-C\d+[A-Z]*` would give `C9K-C93180YC` a
  family of `C9` rather than `C9K-C93180YC`.

Keying on `os` rather than a vendor is deliberate: LibreNMS always populates `os`, and it
already separates Catalyst from Nexus, whose product IDs need different patterns. Use a
YAML anchor, as above, when several `os` values share a list. Nothing here is
Cisco-specific - `junos: ['^EX\d+']` gives `EX4300-48T` a family of `EX4300`.

An invalid regular expression fails the inventory run immediately and names the pattern,
rather than silently skipping devices.

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
`libre_hardware` remains unchanged.

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

## Parsing the location field into rack/unit positions

If your LibreNMS instance encodes a device's precise physical position in its
`location` field (LibreNMS' copy of SNMP `sysLocation`), `parse_location_field` can
break that back out into host vars instead of leaving it as one opaque string. It only
understands one fixed format - there is nothing to configure beyond turning it on:

```
<site>;[<room>];[<rack>];[[<stack_nr>]u<u_nr>];...
```

```yaml
# librenms.yml
parse_location_field: true
```

`site` is required and is everything before the first `;`. `room` is an optional second
field. Everything after that is a sequence of rack names and rack-unit positions, told
apart by shape rather than position: a segment that is an optional number followed by
`u` followed by a number (`u10`, `2u5`) is a unit position; anything else non-empty
names the rack that the unit positions after it belong to, until the next rack name.
This is what lets a stack of physically separate devices that LibreNMS reports as a
single device (eg. a switch stack) record where each member sits - one unit position
per member, across racks if the stack spans more than one.

| `location` | Result |
| --- | --- |
| `DC1` | site `DC1` - the minimal valid form, not a mismatch |
| `DC1;Room2` | site `DC1`, room `Room2` |
| `DC1;Room2;RackA;u10` | site `DC1`, room `Room2`, one position: rack `RackA`, unit `10` |
| `DC1;Room2;RackA;1u10;2u11` | two positions in `RackA`: unit `10` (stack member `1`), unit `11` (stack member `2`) |
| `DC1;Room2;RackA;RackB;u5` | two positions: rack `RackA` alone (no unit given yet), then rack `RackB` with unit `5` |

Sets `libre_location_site`, `libre_location_room` (only when given), and
`libre_location_positions` - a list of dicts, one per rack and/or unit position found,
each holding whichever of `rack`, `unit`, `stack_nr` were given for it:

```yaml
# constructed.yml, over a device with location "DC1;Room2;RackA;1u10;2u11"
compose:
  primary_rack: libre_location_positions[0].rack   # "RackA"
  primary_unit: libre_location_positions[0].unit   # 10
```

A rack name with no unit position before the next rack (or the end of the value) still
gets its own entry - `{"rack": "RackA"}` with no `unit` key - rather than being dropped,
so "racked but the exact unit isn't tracked" is representable. A unit position with no
rack before it (eg. a bare `DC1;Room2;u5`) is the reverse: `{"unit": 5}` with no `rack`
key.

A `location` with no site at all (starting with `;`) doesn't match the format and is
skipped, with a `-v` warning - see [Troubleshooting](#troubleshooting) - since not every
device in a fleet necessarily uses this scheme. `libre_location` itself is always left
exactly as LibreNMS reported it, whether or not it was parsed.

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

`exclude_fields` is also what keeps these fields out of a file written by `create_snapshot`,
which puts every host var it is given on disk in plaintext - see
[Writing the inventory to a file](#writing-the-inventory-to-a-file).

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

### Group names

LibreNMS device group names are free text, so they regularly contain characters an
Ansible group name cannot: anything outside letters, digits and underscores, and a
leading digit. Each rejected character becomes an underscore before the group is created:

| LibreNMS device group | Ansible group | Why |
|---|---|---|
| `Core` | `Core` | already valid, left alone |
| `Network Core` | `Network_Core` | |
| `Site-1` | `Site_1` | `-` is rejected just like whitespace |
| `Core & Dist` | `Core___Dist` | one underscore per rejected character |
| `1st Floor` | `_1st_Floor` | a leading digit is prefixed, not overwritten |
| `10.0.0.0/8` | `_10_0_0_0_8` | |
| `Café Backup` | `Café_Backup` | non-ASCII letters are accepted as-is |

The conversion is Ansible's own `to_safe_group_name()` - the hook the inventory base
class exposes for exactly this - so these group names follow the same convention as any
other inventory source. Calling it here rather than leaving it to Ansible means the names
don't depend on the controller's `TRANSFORM_INVALID_GROUP_CHARS` setting, which defaults
to `never`: by default Ansible only *warns* about such a name and leaves a group that can
only be addressed by quoting it.

Two things differ from what that setting would give you, both to avoid losing characters:

- **A leading digit is prefixed, not overwritten.** Ansible's own substitution replaces
  it, turning `1st Floor` into `_st_Floor`; prefixing gives `_1st_Floor`, which satisfies
  the same rule with the name still readable.
- **Surrounding whitespace is dropped** rather than becoming leading/trailing
  underscores, since it is invisible in the LibreNMS UI and practically always accidental.

`group_name_regex_filter` is matched against the name **as LibreNMS reports it**, before
the conversion - write `^Network Core$`, not `^Network_Core$`.

Two cases are warned about rather than silently resolved:

- Device groups whose names differ only in converted characters (`Network Core` and
  `Network-Core`) become **one** Ansible group holding both memberships.
- A device group named `all` or `ungrouped` merges into the group Ansible creates for
  every inventory - a host in a LibreNMS group called `ungrouped` is then reported as
  ungrouped despite being grouped.

Non-ASCII letters survive the conversion because Ansible's rule is Unicode-aware and
accepts them, even though this plugin folds *hostnames* down to ASCII.

### Renaming groups

A LibreNMS group name is often not the name you want to play against. `group_name_mapping`
renames them on the way in:

```yaml
group_name_mapping:
  Cisco devices: os_iosxe
  Network Core: core
```

Keys match the LibreNMS name **exactly**, case included, and before any conversion. Values
are used as the Ansible group name **as written** - no conversion is applied to them, so
they have to be valid Ansible group names already. One that isn't fails the run
immediately, naming the entry and how it would have had to be written:

```
group_name_mapping['Cisco devices'] is not a valid Ansible group name: 'os iosxe' would
have to be written 'os_iosxe'.
```

Device groups that aren't in the mapping keep their own name, converted as above. Things
worth knowing:

- **A key that matches no device group is reported at `-v`.** A mistyped key (`Cisco
  Devices` for `Cisco devices`) would otherwise do nothing at all, silently.
- **The regex filter runs first**, and still matches the LibreNMS name, so a group has to
  survive `group_name_regex_filter` before it can be renamed - filter on `^Cisco devices$`,
  not on `os_iosxe`.
- **Mapping two device groups to one name merges them**, deliberately, and isn't warned
  about. A mapped name landing on a name some *other* group was converted to still is.

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

### Nested site/room/rack groups from a parsed location

`examples/constructed.yml.dist` also has a worked `keyed_groups` example that turns
[a parsed location](#parsing-the-location-field-into-rackunit-positions) into nested
groups: `site_NYC`, a `site_NYC_room_Room2` child of it, and a
`site_NYC_room_Room2_rack_RackA` child of that - one rack group per rack a device's
`libre_location_positions` mentions. Each child's group name is prefixed with its
parent's *full* name, not just Ansible's `parent_group` nesting on its own (which keeps
the child's own short name) - `compose` precomputes each level's full name once, since
building it inline in every `keyed_groups` entry that needs it would repeat the same
concatenation three times. A host missing a level (no room, or a rack never paired with
a unit) simply gets no group at that level or below, rather than an empty one - see the
comments in the example file for how each entry produces that fallback.

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

## Writing the inventory to a file

Set `create_snapshot` and the plugin writes the inventory it just built as a static
YAML inventory file, as well as handing it to the run in progress:

```yaml
# librenms.yml
plugin: librenms
api_endpoint: https://librenms.example.com/api/v0

create_snapshot: true
snapshot_file: ./inventory/librenms.yml
```

The result is Ansible's own inventory format, hosts under `all` with their host vars
inline and each group listed as a child of `all`:

```yaml
# inventory/librenms.yml
---
all:
  children:
    Core:
      hosts:
        core-sw1: null
        core-sw2: null
  hosts:
    core-sw1:
      libre_hardware: C9300
      libre_os: ios
      libre_sysName: core-sw1
    core-sw2:
      libre_hardware: C9300
      libre_os: iosxe
      libre_sysName: core-sw2
```

Group membership is written separately from the host vars, so a host in two device
groups is listed under both without its vars being repeated, and a host in no group at
all is listed under `all` only - where Ansible puts it back into `ungrouped` on the way
in, exactly as this plugin does.

### Using it as a cache

Because the file is a real inventory, Ansible reads it with its own built-in `yaml`
plugin. Nothing from this repository has to be installed on the machine that consumes
it, and no API token has to be available there:

```bash
# ran once against the API and writes inventory/librenms.yml
ansible-inventory --list -i librenms.yml

# from then on
ansible-playbook -i inventory/librenms.yml site.yml
```

It is **not** the plugin's cache, it is not read again by the plugin. The plugin uses
`cache`, `cache_plugin` and `cache_timeout` within a run, and the two are not
interchangeable:

| | What it stores | When it goes stale |
| --- | --- | --- |
| `cache` (see [Configuration](#configuration)) | The raw LibreNMS API responses | After `cache_timeout`. A changed option (`group_name_mapping`, `exclude_fields`, `hardware_trimming_patterns`) is honoured on the next run, because the inventory is re-derived from the cached responses |
| `create_snapshot` | The already-rendered inventory | Whenever LibreNMS changes. A changed option has no effect on it until a run with API access rewrites it |

So: use the cache to make runs fast, and the file to make runs possible without the API.

### What is and is not in the file

Everything in [Host variables](#host-variables) is there, including the vars derived from
the Notes and location fields, and the groups from [Grouping](#grouping). Ansible's magic
vars (`inventory_hostname`, `group_names`, `inventory_file`) are not since Ansible recomputes
those per source. Writing them would pin each host to the run that wrote the file.

A value taken from LibreNMS is reported as *untrusted* on a live run, so Ansible never treats
it as a template, while a value read from a static inventory file is trusted.
To make sure that a device whose Notes field contains `{{ ... }}` stays literal in the snapshot too,
any host var value holding a brace - the start of every Jinja2 delimiter - is written with
YAML's `!unsafe` tag, which Ansible reads back as untrusted, nested mapping keys included.
Values without a brace are written plain: they cannot be templates, and Ansible resolves an
`!unsafe` variable's type again, which would turn a version string like `'15.2'` into a
number.

Only what this plugin added is written. A `constructed` source chained after it is parsed
*later*, so its groups and composed vars are not in the file - chain it over the file the
same way you chain it over the plugin, which works unchanged:

```bash
ansible-inventory --list -i inventory/librenms.yml -i constructed.yml
```

### Paths

`snapshot_file` is a full path including the filename. A relative one resolves against
the directory holding the inventory source file rather than the working directory, so the
output lands in the same place whether you run `ansible` from the project root or from
somewhere below it. `~` and environment variables are expanded, missing directories are
created, and `-vvvv` reports the absolute path it settled on. An environment variable that
is not set fails the run. A symlink is followed: the file it points to is updated and the
link is left in place.

Leave `snapshot_file` out and the name is derived from the inventory source: `librenms.yml`
writes `.librenms.generated.yml` beside itself. It is hidden on purpose: when `-i` names a
directory, Ansible parses every file in it but skips hidden ones, so a visible snapshot
would be read as a second inventory source on the next run. Point `-i` at the file itself
to use it (`-i inventory/.librenms.generated.yml`). If you set `snapshot_file` to a visible
name, keep it outside any directory you pass to `-i`.

It only ever overwrites a file it wrote itself, recognised by its header line, or an empty
one. Anything else - the inventory source file, another source the run has already parsed,
a directory, a hand-written inventory, a `group_vars` file, a plugin configuration - fails
the run and names the path, rather than losing a file the plugin cannot recreate. The write
goes through a temporary file in the same directory that is flushed to disk and then moved
into place, so an interrupted run or a crash cannot leave half an inventory behind. An
existing file keeps its permissions; a new one gets the mode your umask gives a new file.

### Committing it

Hosts, groups and var keys are sorted and no timestamp is written, so a run that found
nothing new rewrites exactly the same bytes and a diff means the fleet really changed.

One caveat before committing one: the file holds every host var the run set. With
`exclude_fields` emptied (see [Sensitive fields](#sensitive-fields)) that includes the
SNMP community string and SNMPv3 passphrases, in plaintext, in your repository.

A note on hostnames: a host key in a YAML inventory is a *pattern*, so a name containing
`:` (an IPv6 address, or something that looks like `host:port`) or `[`...`]` does not read
back as the same host - Ansible expands it. The plugin reports any such host at `-v` when
it writes the file. Quoting does not help, the expansion happens after the YAML is parsed.
Use `hostname_field` to pick a field that gives plain names if LibreNMS reports addresses
for your devices.

## Troubleshooting

The plugin reports what it is doing at two verbosity levels, and every message it emits
is prefixed with `[librenms]` so it can be picked out of Ansible's own output:

```bash
# -v: only the things that went wrong
ansible-inventory -v --list -i librenms.yml

# -vvvv: every step of the run, in order
ansible-inventory -vvvv --list -i librenms.yml | grep '\[librenms\]'
```

At `-v` the plugin reports problems that quietly degrade the inventory rather than stop
it, so a run that "worked" but produced the wrong thing explains itself:

| Message | Usual cause |
| --- | --- |
| `LibreNMS returned no devices` | `device_status_filter` or `query_filters` matched nothing, or the token cannot see devices |
| `every device was filtered out` | `exclude_disabled`, `exclude_ignored` or `host_name_regex_filter` is too strict |
| `group_name_regex_filter matched none of the device groups` | The regexes do not match any LibreNMS device group name |
| `device <name> has no <field>, naming it <uuid>` | `hostname_field` names a field this device does not have |
| `api_token did not resolve to a value` | The vault/extra var referenced by `api_token` is not defined |
| `<host>: notes field is YAML but not a mapping of vars` | The Notes field parses, but not into `key: value` pairs |
| `<host>: location field has no site` | `location` doesn't start with a site before the first `;` - see [Parsing the location field](#parsing-the-location-field-into-rackunit-positions) |
| `no hosts were added, leaving <path> as it is` | Every device was filtered out or none was returned, so an existing snapshot file is deliberately left alone - see [Writing the inventory to a file](#writing-the-inventory-to-a-file) |
| `snapshot_file <path> does not end in .yml, .yaml or .json` | The file is written, but `ansible -i` will not read that name back |
| `host <name> contains one of [ ] :` | The snapshot is written, but a YAML inventory reads that name as a port or a host range rather than as this host |

At `-vvvv` every step is reported as it happens - options being applied, each API request
and whether it came from the cache, each device that was skipped and why, the hardware
family and variant derived for each host, and each group a host was added to.

Failures that stop the run name the option to look at, for example:

```
Could not reach the LibreNMS API at https://librenms.example.com/api/v0/devices.
Check api_endpoint, validate_certs and timeout: <urlopen error [Errno -2] Name or service not known>

LibreNMS rejected the API token (HTTP 401): Unauthenticated.
Check api_token, or the LIBRENMS_TOKEN environment variable.

snapshot_file resolved to /etc/ansible/librenms.yml, which is the inventory source file
itself. Writing the inventory there would destroy the configuration that produced it.
Point it at a different file.
```

## Migrating from mschedrin/librenms-ansible-inventory-plugin

- The plugin file now lives at `inventory_plugins/librenms.py` instead of the repo root.
- `requests` and `unidecode` are no longer required - the plugin uses Ansible's
  built-in `open_url` and the standard library.
- The standalone `librenms-inventory-script.py` dynamic-inventory script has been
  dropped in favor of the plugin (as the old README itself recommended).
- `host_name_regex_filter`, `group_name_regex_filter`, `regex_ignore_case`,
  `exclude_disabled`, and `cache_force_update` keep the same names and behavior.
- New: `exclude_ignored`, `hostname_field`, `device_status_filter`, `query_filters`,
  `create_snapshot`/`snapshot_file` (see
  [Writing the inventory to a file](#writing-the-inventory-to-a-file)).
- Device group names containing whitespace now become valid Ansible group names
  (`Network Core` -> `Network_Core`), see [Group names](#group-names).
- Property-based grouping and vars like `ansible_host`/`ansible_network_os` are no
  longer built into this plugin - chain Ansible's standard `constructed` inventory
  plugin as a second source instead (see [Grouping](#grouping)).

## Testing

```bash
uv run pytest
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
uv run pytest tests/integration -v
```

Every assertion is checked against a "ground truth" fetched directly from the same API
at test time (no hardcoded fixture values), so the suite keeps working as the target
instance's devices/groups change. It covers: default `exclude_disabled`/`exclude_ignored`
filtering against the real `disabled`/`ignore` flags, `libre_*` hostvars matching the raw
device payload, LibreNMS device-group membership
(including the case where an instance has zero device groups, which some LibreNMS
versions signal with an HTTP 404 rather than an empty list), and a real `ansible-inventory
--list` subprocess run.

## Linting

```bash
uv sync

uv run ruff check .
uv run yamllint .github/workflows examples/*.yml.dist
```

Configuration lives in `ruff.toml` and `.yamllint.yml`. Both files carry the reasoning
for each ignore, the notable one being that `inventory_plugins/*.py` is exempt from
`E402`: an Ansible plugin has to define `DOCUMENTATION` and `EXAMPLES` before its
imports.

## Continuous integration

`.github/workflows/ci.yml` runs on every pull request against `main`, and on pushes to
`main`. It has three jobs:

| Job | What it checks |
| --- | --- |
| **Lint** | `ruff check` over the plugin and tests, `yamllint` over the workflows and the example inventory sources |
| **Plugin documentation** | `DOCUMENTATION` and `EXAMPLES` parse as YAML, and `ansible-doc -t inventory librenms` renders - a malformed docstring stops Ansible registering the plugin's options at all |
| **Unit tests** | `tests/unit` on Python 3.12 and 3.13 |

Only the unit tests run in CI. `tests/integration` needs a reachable LibreNMS instance
and skips itself without one, so running it on a GitHub runner would prove nothing.

CI installs from `uv.lock` (`uv sync --locked`), the same lockfile `uv sync` uses locally,
so a clean local run means a clean CI run.

### Requiring CI before a merge

The workflow alone does not block merges - that is a repository setting. The jobs
aggregate into a single `CI` check so only one entry has to be required:

**Settings → Branches → Add branch ruleset**, targeting `main`:

- Enable **Require a pull request before merging**
- Enable **Require status checks to pass**, and add `CI` as a required check
- Enable **Block force pushes**

The `CI` job fails if any of Lint, Plugin documentation, or Unit tests fails or is
cancelled, so requiring it covers all of them - including new matrix entries added
later, which would otherwise each need adding to the required list by hand.
