#!/usr/bin/env python3
"""
baseline_diagnostic.py - Deterministic rule-based fault diagnosis for Project 4
(AI-Assisted Troubleshooting Copilot)

This is the non-LLM comparison baseline: a small set of hand-written rules
that diff a fault evidence bundle against the known-good baseline bundle
and report exactly what changed, with no reasoning, no guessing, and no
possibility of hallucination -- it only ever reports differences that are
actually present in the two JSON files.

This exists specifically to answer the question the copilot evaluation
raises: for these four fault types, how much value does an LLM add over
a handful of simple diff rules? Mirrors the "Isolation Forest vs.
threshold detector" comparison from the companion anomaly-detector project.

Usage:
    python3 baseline_diagnostic.py --baseline evidence/baseline-final.json \
                                     --fault evidence/fault-primary-wan-down.json
"""

import argparse
import json
import re
import sys
from pathlib import Path


def parse_interface_brief(text: str) -> dict:
    """Parse 'show ip interface brief' into {interface: (ip, status, protocol)}."""
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("Interface"):
            continue
        parts = line.split()
        if len(parts) < 6:
            continue
        intf = parts[0]
        ip = parts[1]
        status = parts[-2]
        protocol = parts[-1]
        result[intf] = (ip, status, protocol)
    return result


def parse_standby_brief(text: str) -> dict:
    """Parse 'show standby brief' into {interface_group: (state, active, standby, vip)}."""
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("Interface") or line.startswith("P indicates") or line == "|":
            continue
        parts = line.split()
        if len(parts) < 7:
            continue
        # Interface Grp Pri P State Active Standby VirtualIP
        intf, grp = parts[0], parts[1]
        state = parts[4] if parts[3] == "P" else parts[3]
        idx = parts.index(state)
        active = parts[idx + 1]
        standby = parts[idx + 2]
        vip = parts[idx + 3]
        result[intf + " grp" + grp] = {"state": state, "active": active, "standby": standby, "vip": vip}
    return result


def parse_ospf_interface_brief(text: str) -> dict:
    """Parse 'show ip ospf interface brief' into {interface: {area, subnet, nbrs, cost}}."""
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("Interface"):
            continue
        parts = line.split()
        if len(parts) < 7:
            continue
        intf, pid, area, ip_mask, cost, state, nbrs_fc = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], parts[6]
        subnet = ip_mask.split("/")[0].rsplit(".", 1)[0]  # crude /24-ish grouping, good enough here
        result[intf] = {"area": area, "ip_mask": ip_mask, "subnet": subnet, "cost": cost, "state": state, "nbrs_fc": nbrs_fc}
    return result


def parse_access_list_matches(text: str) -> list:
    """Extract (rule, match_count) pairs from 'show access-lists' output."""
    matches = []
    for line in text.splitlines():
        m = re.search(r"\((\d+) matches?\)", line)
        if m and "deny" in line.lower():
            matches.append((line.strip(), int(m.group(1))))
    return matches


def diagnose(baseline: dict, fault: dict) -> list:
    findings = []

    baseline_by_dev = {d["device"]: d for d in baseline["devices"]}
    fault_by_dev = {d["device"]: d for d in fault["devices"]}

    # --- Rule 1: interface state changes (down that was up) ---
    for dev_name, f_dev in fault_by_dev.items():
        b_dev = baseline_by_dev.get(dev_name)
        if not b_dev:
            continue
        b_intf = parse_interface_brief(b_dev["commands"].get("show ip interface brief", ""))
        f_intf = parse_interface_brief(f_dev["commands"].get("show ip interface brief", ""))
        for intf, (b_ip, b_status, b_proto) in b_intf.items():
            f_val = f_intf.get(intf)
            if f_val is None:
                continue
            f_ip, f_status, f_proto = f_val
            was_up = (b_status == "up" and b_proto == "up")
            now_down = (f_status != "up" or f_proto != "up")
            if was_up and now_down:
                findings.append(
                    "INTERFACE DOWN: " + dev_name + " " + intf +
                    " was up/up in baseline, now " + f_status + "/" + f_proto
                )

    # --- Rule 2: HSRP active/standby role changes ---
    for dev_name, f_dev in fault_by_dev.items():
        b_dev = baseline_by_dev.get(dev_name)
        if not b_dev:
            continue
        b_standby = parse_standby_brief(b_dev["commands"].get("show standby brief", ""))
        f_standby = parse_standby_brief(f_dev["commands"].get("show standby brief", ""))
        for key, b_val in b_standby.items():
            f_val = f_standby.get(key)
            if f_val is None:
                continue
            if b_val["state"] != f_val["state"]:
                findings.append(
                    "HSRP STATE CHANGE: " + dev_name + " " + key +
                    " was " + b_val["state"] + " in baseline, now " + f_val["state"]
                )

    # --- Rule 3: OSPF neighbor count drop per interface ---
    for dev_name, f_dev in fault_by_dev.items():
        b_dev = baseline_by_dev.get(dev_name)
        if not b_dev:
            continue
        b_ospf = parse_ospf_interface_brief(b_dev["commands"].get("show ip ospf interface brief", ""))
        f_ospf = parse_ospf_interface_brief(f_dev["commands"].get("show ip ospf interface brief", ""))
        for intf, b_val in b_ospf.items():
            f_val = f_ospf.get(intf)
            if f_val is None:
                continue
            if b_val["nbrs_fc"] != f_val["nbrs_fc"] and b_val["nbrs_fc"] != "0/0":
                findings.append(
                    "OSPF ADJACENCY CHANGE: " + dev_name + " " + intf +
                    " neighbor count was " + b_val["nbrs_fc"] + " in baseline, now " + f_val["nbrs_fc"]
                )

    # --- Rule 4: OSPF area mismatch across devices sharing a subnet ---
    subnet_areas = {}  # subnet -> list of (device, interface, area)
    for dev_name, f_dev in fault_by_dev.items():
        f_ospf = parse_ospf_interface_brief(f_dev["commands"].get("show ip ospf interface brief", ""))
        for intf, val in f_ospf.items():
            if intf.startswith("Lo") or intf.startswith("Gi1"):  # skip loopbacks/mgmt
                continue
            subnet_areas.setdefault(val["subnet"], []).append((dev_name, intf, val["area"]))

    for subnet, entries in subnet_areas.items():
        areas = set(e[2] for e in entries)
        if len(areas) > 1:
            desc = ", ".join(dev + " " + intf + "=area" + area for dev, intf, area in entries)
            findings.append("OSPF AREA MISMATCH on subnet " + subnet + ".0/24: " + desc)

    # --- Rule 5: reachability matrix regressions ---
    for dev_name, f_dev in fault_by_dev.items():
        b_dev = baseline_by_dev.get(dev_name)
        if not b_dev:
            continue
        b_matrix = b_dev.get("reachability_matrix", {})
        f_matrix = f_dev.get("reachability_matrix", {})
        for target, b_result in b_matrix.items():
            f_result = f_matrix.get(target)
            if f_result is None:
                continue
            if b_result.get("success") and not f_result.get("success"):
                findings.append(
                    "REACHABILITY REGRESSION: " + dev_name + " -> " + target +
                    " was OK in baseline, now FAILING"
                )

    # --- Rule 6: ACL deny matches (corroborating evidence for reachability faults) ---
    for dev_name, f_dev in fault_by_dev.items():
        acl_matches = parse_access_list_matches(f_dev["commands"].get("show access-lists", ""))
        for rule_line, count in acl_matches:
            if count > 0:
                findings.append("ACL DENY HITS: " + dev_name + " -- " + rule_line)

    return findings


def main():
    parser = argparse.ArgumentParser(description="Deterministic rule-based fault diagnosis")
    parser.add_argument("--baseline", required=True, help="Path to baseline evidence bundle JSON")
    parser.add_argument("--fault", required=True, help="Path to fault evidence bundle JSON")
    args = parser.parse_args()

    with open(args.baseline) as f:
        baseline = json.load(f)
    with open(args.fault) as f:
        fault = json.load(f)

    print("=" * 70)
    print("DETERMINISTIC DIAGNOSIS")
    print("Baseline: " + args.baseline)
    print("Fault:    " + args.fault + " (label: " + fault.get("label", "?") + ")")
    print("=" * 70)

    findings = diagnose(baseline, fault)

    if not findings:
        print("\nNo differences detected against baseline.")
    else:
        print("\n" + str(len(findings)) + " finding(s):\n")
        for f in findings:
            print("  - " + f)


if __name__ == "__main__":
    main()
