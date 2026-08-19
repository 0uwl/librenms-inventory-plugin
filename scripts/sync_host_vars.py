#!/usr/bin/env python3
"""Create a host_vars/<hostname>.yml stub for every host the librenms inventory plugin
currently discovers, for hosts that don't already have one.

Existing host_vars files are never modified or overwritten, so anything you've already
filled in (eg. deploy_environment: prod) is safe - this only fills in the gap of having
to notice a new device exists and create its file by hand. Run it again whenever new
devices show up in LibreNMS.

Usage:
    python3 scripts/sync_host_vars.py -i librenms.yml
    python3 scripts/sync_host_vars.py -i librenms.yml --host-vars-dir path/to/host_vars
    python3 scripts/sync_host_vars.py -i librenms.yml --dry-run
"""

import argparse
import os
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(REPO_ROOT, "inventory_plugins")

STUB_TEMPLATE = """\
# Auto-generated stub for {hostname} by scripts/sync_host_vars.py.
# This file is only ever created, never overwritten by that script - edit freely.
#
# Example:
# deploy_environment: prod
"""


def get_plugin_instance():
    from ansible.plugins.loader import inventory_loader

    inventory_loader.add_directory(PLUGIN_DIR)
    plugin = inventory_loader.get("librenms")
    if plugin is None:
        raise RuntimeError("librenms inventory plugin could not be loaded from " + PLUGIN_DIR)
    return plugin


def discover_hostnames(inventory_path):
    from ansible.inventory.data import InventoryData
    from ansible.parsing.dataloader import DataLoader

    plugin = get_plugin_instance()
    inventory = InventoryData()
    loader = DataLoader()
    plugin.parse(inventory, loader, inventory_path, cache=True)
    return sorted(inventory.hosts.keys())


def write_stub(target_path, hostname):
    content = STUB_TEMPLATE.format(hostname=hostname)
    target_dir = os.path.dirname(target_path)
    fd, tmp_path = tempfile.mkstemp(dir=target_dir, prefix=".sync_host_vars-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp_path, target_path)
    except BaseException:
        os.unlink(tmp_path)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-i", "--inventory", required=True, help="Path to the librenms inventory source file.")
    parser.add_argument(
        "--host-vars-dir",
        help="Directory to write host_vars/<hostname>.yml into. "
        "Defaults to a host_vars/ directory next to --inventory.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print what would be created without writing anything."
    )
    args = parser.parse_args()

    host_vars_dir = args.host_vars_dir or os.path.join(
        os.path.dirname(os.path.abspath(args.inventory)), "host_vars"
    )
    host_vars_dir = os.path.abspath(host_vars_dir)

    hostnames = discover_hostnames(args.inventory)
    if not hostnames:
        print("No hosts discovered - nothing to do.")
        return

    if not args.dry_run:
        os.makedirs(host_vars_dir, exist_ok=True)

    created, skipped, unsafe = 0, 0, 0
    for hostname in hostnames:
        target_path = os.path.abspath(os.path.join(host_vars_dir, hostname + ".yml"))
        # Defends against a hostname (sourced from LibreNMS device data, not something
        # this script controls) containing path separators/".." and escaping
        # host_vars_dir, eg. a device sysName of "../../etc/cron.d/evil".
        if os.path.commonpath([target_path, host_vars_dir]) != host_vars_dir:
            print("Skipping {0}: derived an unsafe file path ({1})".format(hostname, target_path), file=sys.stderr)
            unsafe += 1
            continue

        if os.path.exists(target_path):
            skipped += 1
            continue

        if args.dry_run:
            print("Would create {0}".format(target_path))
        else:
            write_stub(target_path, hostname)
            print("Created {0}".format(target_path))
        created += 1

    summary = "{0} created, {1} already existed".format(created, skipped)
    if unsafe:
        summary += ", {0} skipped as unsafe".format(unsafe)
    print(summary)


if __name__ == "__main__":
    main()
