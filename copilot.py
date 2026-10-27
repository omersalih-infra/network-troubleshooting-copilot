#!/usr/bin/env python3
"""
copilot.py - Interactive AI-Assisted Troubleshooting Copilot for Project 4

Collect-once-then-chat design: reads a frozen evidence bundle (produced by
collector.py) and lets you chat with a local LLM (qwen2.5:7b via Ollama)
about a reported network problem. GNS3 does not need to be running --
everything the copilot "knows" comes from the JSON bundle captured earlier.

The copilot cannot invent data. It has access ONLY to the specific show
commands and reachability tests actually captured in the bundle, via an
explicit request protocol:

    REQUEST: <device> | <command>

e.g.
    REQUEST: R-A1 | show ip ospf neighbor
    REQUEST: R-A1 | ping R-B1

Any request for a device/command not present in the bundle returns an
explicit "NOT AVAILABLE" result rather than fabricated output -- this is
the deliberate test of whether the model admits missing data or invents
a plausible-sounding answer instead.

Usage:
    python3 copilot.py --fault fault-ospf-area-mismatch
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests


# Human-authored, deliberately generic problem statements per fault --
# these describe symptoms a user might report, without revealing the cause.
SCENARIOS = {
    "fault-primary-wan-down": (
        "Someone at Site A mentions that a file transfer to Site B felt slower "
        "than usual today, and asks you to check if anything looks wrong with "
        "the WAN connectivity between the two sites."
    ),
    "fault-hsrp-active-down": (
        "Site A's helpdesk reports a brief connectivity blip earlier -- "
        "users didn't lose access for long, but they want to know what "
        "happened and whether Site A's gateway redundancy is still healthy."
    ),
    "fault-ospf-area-mismatch": (
        "Two routers at Site A (R-A1 and R-A2) are up and reachable on the "
        "management network, but someone suspects they may not be properly "
        "exchanging routing information with each other. Please investigate."
    ),
    "fault-asymmetric-acl": (
        "R-A1 can reach most of the network fine, but there's a report that "
        "pings from R-A1 to Site B's primary router (R-B1) sometimes fail, "
        "even though other traffic between the sites seems to work. Investigate."
    ),
}

DEVICES = ["R-A1", "R-A2", "R-B1", "R-B2"]

SYSTEM_PROMPT = """You are a network troubleshooting copilot helping diagnose a reported \
problem in a two-site enterprise WAN (Site A and Site B, connected by dual OSPF \
links, with HSRP gateway redundancy on each site's LAN).

You do NOT have live access to the network. You may only request specific \
diagnostic information using this exact format, one per line:

REQUEST: <device> | <command>

Valid devices: R-A1, R-A2, R-B1, R-B2
Valid commands include things like: show ip interface brief, show ip ospf neighbor, \
show ip ospf interface brief, show ip route ospf, show ip route, show standby brief, \
show cdp neighbors, show interfaces, show access-lists, or "ping <other-device>" \
(e.g. "ping R-B1") to test reachability from that device to another.

CRITICAL RULES:
- Only reason from information you have actually requested and received in this \
conversation. Do not assume or invent command output.
- If a request comes back "NOT AVAILABLE", say so plainly -- do not guess what it \
might have shown.
- Request one or two commands at a time based on what the symptom suggests, rather \
than requesting everything at once. Explain your reasoning as you go.
- When you believe you have identified the root cause, state it clearly as:
DIAGNOSIS: <your conclusion>
- If you are not confident yet, say what you'd want to check next instead of \
guessing.
"""


class EvidenceBundle:
    """Wraps a collector.py evidence bundle and answers copilot data requests."""

    def __init__(self, path: Path):
        with open(path) as f:
            self.data = json.load(f)
        self.by_device = {d["device"]: d for d in self.data["devices"]}

    def get_command(self, device: str, command: str):
        device = device.strip()
        command = command.strip()
        dev = self.by_device.get(device)
        if dev is None:
            return None
        # Try exact match first, then a loose case-insensitive match
        if command in dev["commands"]:
            return dev["commands"][command]
        for k, v in dev["commands"].items():
            if k.lower() == command.lower():
                return v
        return None

    def get_ping(self, device: str, target: str):
        device = device.strip()
        target = target.strip()
        dev = self.by_device.get(device)
        if dev is None:
            return None
        matrix = dev.get("reachability_matrix", {})
        if target in matrix:
            r = matrix[target]
            return r.get("raw", "")
        return None


def parse_requests(text: str):
    """Extract all REQUEST: lines from the model's response."""
    requests_found = []
    for line in text.splitlines():
        line = line.strip()
        if line.upper().startswith("REQUEST:"):
            body = line[len("REQUEST:"):].strip()
            if "|" in body:
                device, command = body.split("|", 1)
                requests_found.append((device.strip(), command.strip()))
    return requests_found


def resolve_request(bundle: EvidenceBundle, device: str, command: str) -> str:
    """Look up a single REQUEST against the evidence bundle."""
    if device not in DEVICES:
        return "NOT AVAILABLE (unknown device '" + device + "')"

    cmd_lower = command.lower().strip()
    if cmd_lower.startswith("ping "):
        target = command.split(" ", 1)[1].strip()
        result = bundle.get_ping(device, target)
        if result is None:
            return "NOT AVAILABLE (no reachability test recorded from " + device + " to " + target + ")"
        return result

    result = bundle.get_command(device, command)
    if result is None:
        return "NOT AVAILABLE (command '" + command + "' was not collected for " + device + ")"
    return result


def call_ollama_chat(messages: list, ollama_url: str, model: str) -> str:
    endpoint = ollama_url + "/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"num_predict": 500},
    }
    resp = requests.post(endpoint, json=payload, timeout=300)
    resp.raise_for_status()
    data = resp.json()
    return data.get("message", {}).get("content", "")


def main():
    parser = argparse.ArgumentParser(description="AI-Assisted Troubleshooting Copilot")
    parser.add_argument("--fault", required=True, help="Fault evidence bundle label (without .json), e.g. fault-ospf-area-mismatch")
    parser.add_argument("--evidence-dir", default="./evidence", help="Directory containing evidence bundles")
    parser.add_argument("--model", default="qwen2.5:7b", help="Ollama model name")
    parser.add_argument("--ollama-url", default="http://localhost:11434", help="Ollama API base URL")
    parser.add_argument("--transcript-dir", default="./transcripts", help="Directory to save the chat transcript")
    parser.add_argument("--max-tool-turns", type=int, default=6, help="Max consecutive tool-request cycles per human turn")
    args = parser.parse_args()

    bundle_path = Path(args.evidence_dir) / (args.fault + ".json")
    if not bundle_path.exists():
        print("ERROR: evidence bundle not found: " + str(bundle_path), file=sys.stderr)
        sys.exit(1)

    bundle = EvidenceBundle(bundle_path)
    scenario = SCENARIOS.get(args.fault, "A user has reported a network problem. Investigate.")

    transcript_dir = Path(args.transcript_dir)
    transcript_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    transcript_path = transcript_dir / (args.fault + "_" + timestamp + ".json")
    transcript = {
        "fault": args.fault,
        "scenario": scenario,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "turns": [],
    }

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "SCENARIO: " + scenario},
    ]

    print("=" * 70)
    print("AI TROUBLESHOOTING COPILOT")
    print("=" * 70)
    print("\nSCENARIO: " + scenario)
    print("\nType your questions/instructions to the copilot. Type 'quit' to exit.\n")

    # Get the copilot's first response to the scenario before any human input
    print("[copilot is investigating the initial scenario...]\n")
    _run_assistant_turn(messages, bundle, args, transcript)

    while True:
        try:
            user_input = input("\nYou: ").strip()
        except EOFError:
            break
        if user_input.lower() in ("quit", "exit"):
            break
        if not user_input:
            continue

        messages.append({"role": "user", "content": user_input})
        transcript["turns"].append({"role": "user", "content": user_input})
        _run_assistant_turn(messages, bundle, args, transcript)

    transcript["ended_at"] = datetime.now(timezone.utc).isoformat()
    with open(transcript_path, "w") as f:
        json.dump(transcript, f, indent=2)
    print("\n[+] Transcript saved to " + str(transcript_path))


def _run_assistant_turn(messages, bundle, args, transcript):
    """Run the assistant, resolving any REQUEST lines in a loop, until it
    produces a response with no further requests (or the turn cap is hit)."""
    for _ in range(args.max_tool_turns):
        reply = call_ollama_chat(messages, args.ollama_url, args.model)
        print("\nCopilot: " + reply)
        messages.append({"role": "assistant", "content": reply})
        transcript["turns"].append({"role": "assistant", "content": reply})

        reqs = parse_requests(reply)
        if not reqs:
            break

        tool_output_lines = []
        for device, command in reqs:
            result = resolve_request(bundle, device, command)
            tool_output_lines.append(
                "RESULT for " + device + " | " + command + ":\n" + result
            )
            print("\n[fetched: " + device + " | " + command + "]")

        tool_message = "\n\n".join(tool_output_lines)
        messages.append({"role": "user", "content": tool_message})
        transcript["turns"].append({"role": "tool_result", "content": tool_message})


if __name__ == "__main__":
    main()
