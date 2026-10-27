#!/usr/bin/env python3
"""
collector.py - Netmiko-based state collector for Project 4
(AI-Assisted Troubleshooting Copilot)

Connects to all four routers over SSH and pulls a set of diagnostic
show commands from each, saving everything into a single structured
JSON "evidence bundle" that the copilot chat script reads later.

This is a collect-once-then-chat design: GNS3 only needs to be running
during collection. Once the bundle is saved, the topology can be shut
down and the copilot chat runs entirely against the frozen JSON file.

These routers only offer legacy SSH key-exchange/cipher algorithms
(diffie-hellman-group14-sha1, aes128-cbc) that modern Paramiko/Netmiko
disable by default -- the same negotiation gap confirmed manually via
OpenSSH CLI overrides during connectivity testing. This script patches
Paramiko's transport preferred lists at runtime to re-enable them.

Credentials and device addresses are NOT stored in this file. Copy
config.example.py to config.py (which is git-ignored) and fill it in.

Usage:
    python3 collector.py --label baseline
    python3 collector.py --label fault-primary-wan-down
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import paramiko
from netmiko import ConnectHandler
try:
    from netmiko.exceptions import NetmikoTimeoutException, NetmikoAuthenticationException
except ImportError:
    from netmiko.ssh_exception import NetmikoTimeoutException, NetmikoAuthenticationException

# --- Patch Paramiko's default algorithm lists to allow legacy IOS crypto ---
# Equivalent to: ssh -oKexAlgorithms=+diffie-hellman-group14-sha1
#                    -oHostKeyAlgorithms=+ssh-rsa -oCiphers=+aes128-cbc
paramiko.Transport._preferred_kex = (
    "diffie-hellman-group14-sha1",
    "diffie-hellman-group-exchange-sha1",
    "diffie-hellman-group1-sha1",
) + paramiko.Transport._preferred_kex
paramiko.Transport._preferred_ciphers = (
    "aes128-cbc",
    "3des-cbc",
    "aes192-cbc",
    "aes256-cbc",
) + paramiko.Transport._preferred_ciphers
paramiko.Transport._preferred_keys = (
    "ssh-rsa",
) + paramiko.Transport._preferred_keys


try:
    from config import DEVICES, USERNAME, PASSWORD, LOOPBACKS
except ImportError:
    sys.exit(
        "ERROR: config.py not found. Copy config.example.py to config.py and "
        "fill in your device addresses and credentials."
    )

# Read-only diagnostic commands pulled from every router.
SHOW_COMMANDS = [
    "show ip interface brief",
    "show ip ospf neighbor",
    "show ip ospf interface brief",
    "show ip route ospf",
    "show ip route",
    "show standby brief",
    "show cdp neighbors",
    "show interfaces",
    "show access-lists",
]

# LOOPBACKS (device name -> loopback IP) comes from config.py. The reachability
# matrix pings these, which is what catches a data-plane-only fault (e.g. an ACL
# silently dropping traffic) that leaves no trace in any control-plane command.


def run_reachability_matrix(conn, source_name: str) -> dict:
    """From the connected device, ping every other router's loopback and
    record pass/fail. This is a real, sourced (via loopback) reachability
    test -- it uses 'ping <ip> source Loopback0' so the test reflects the
    actual routed path, not just a directly-connected-interface ping."""
    results = {}
    for name, ip in LOOPBACKS.items():
        if name == source_name:
            continue
        try:
            output = conn.send_command(
                "ping " + ip + " source Loopback0", read_timeout=15
            )
            # IOS ping summary line looks like "Success rate is 100 percent (5/5)"
            success = "Success rate is 100 percent" in output
            results[name] = {"success": success, "raw": output}
        except Exception as e:
            results[name] = {"success": None, "raw": "ERROR: " + str(e)}
    return results


def collect_device(device: dict) -> dict:
    """Connect to a single device and pull all configured show commands."""
    result = {
        "device": device["name"],
        "host": device["host"],
        "reachable": False,
        "error": None,
        "commands": {},
        "reachability_matrix": {},
    }

    conn_params = {
        "device_type": device["device_type"],
        "host": device["host"],
        "username": USERNAME,
        "password": PASSWORD,
        "timeout": 15,
        "fast_cli": False,
    }

    try:
        conn = ConnectHandler(**conn_params)
        result["reachable"] = True

        for cmd in SHOW_COMMANDS:
            try:
                output = conn.send_command(cmd)
                result["commands"][cmd] = output
            except Exception as e:
                result["commands"][cmd] = "ERROR: " + str(e)

        result["reachability_matrix"] = run_reachability_matrix(conn, device["name"])

        conn.disconnect()

    except (NetmikoTimeoutException, NetmikoAuthenticationException) as e:
        result["error"] = type(e).__name__ + ": " + str(e)
    except Exception as e:
        result["error"] = type(e).__name__ + ": " + str(e)

    return result


def main():
    parser = argparse.ArgumentParser(description="Netmiko collector for Project 4 evidence bundles")
    parser.add_argument("--label", required=True, help="Short label for this snapshot (e.g. 'baseline', 'fault-primary-wan-down')")
    parser.add_argument("--outdir", default="./evidence", help="Output directory for the evidence bundle")
    parser.add_argument("--overwrite", action="store_true", help="Allow replacing an existing bundle with the same label")
    args = parser.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    outfile = outdir / (args.label + ".json")
    if outfile.exists() and not args.overwrite:
        sys.exit(
            "ERROR: " + str(outfile) + " already exists. Evidence bundles are "
            "the ground truth for the evaluation, so they are never replaced "
            "silently. Pick a new --label, or pass --overwrite."
        )

    print("[+] Starting collection: " + args.label)
    timestamp = datetime.now(timezone.utc).isoformat()

    bundle = {
        "label": args.label,
        "collected_at": timestamp,
        "devices": [],
    }

    for device in DEVICES:
        print("    Connecting to " + device["name"] + " (" + device["host"] + ") ...")
        result = collect_device(device)
        status = "OK" if result["reachable"] else ("FAILED (" + str(result["error"]) + ")")
        print("      -> " + status)
        bundle["devices"].append(result)

    with open(outfile, "w") as f:
        json.dump(bundle, f, indent=2)

    reachable_count = sum(1 for d in bundle["devices"] if d["reachable"])
    print("\n[+] Collection complete: " + str(reachable_count) + "/" + str(len(DEVICES)) + " devices reachable")
    print("[+] Evidence bundle written to " + str(outfile))


if __name__ == "__main__":
    main()
