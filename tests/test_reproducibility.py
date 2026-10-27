"""
Checks that the evidence bundles shipped in evidence/ reproduce the
deterministic ground truth documented in the report, and that the copilot's
request resolver never fabricates output.

Run from the repository root (no extra dependencies besides `requests`,
which copilot.py imports):

    python3 -m unittest discover -s tests -v
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import baseline_diagnostic as bd  # noqa: E402
import copilot  # noqa: E402

EVIDENCE = ROOT / "evidence"


def load(name, subdir=""):
    with open(EVIDENCE / subdir / (name + ".json")) as f:
        return json.load(f)


def findings(baseline, fault):
    return bd.diagnose(load(baseline), load(fault))


class GroundTruth(unittest.TestCase):
    """Finding counts per fault, paired with the baseline captured in the same lab session."""

    def test_primary_wan_down(self):
        # Report Appendix A: 3 findings. The shipped bundle was re-collected
        # alongside baseline.json, so that is its matching baseline.
        f = findings("baseline", "fault-primary-wan-down")
        self.assertEqual(len(f), 3)
        self.assertTrue(any("INTERFACE DOWN: R-A1 FastEthernet0/1" in x for x in f))

    def test_hsrp_active_down(self):
        f = findings("baseline-final", "fault-hsrp-active-down")
        self.assertEqual(len(f), 5)  # Appendix A: 5 findings
        self.assertTrue(any("HSRP STATE CHANGE: R-A2" in x and "now Active" in x for x in f))

    def test_ospf_area_mismatch(self):
        f = findings("baseline-final", "fault-ospf-area-mismatch")
        self.assertEqual(len(f), 3)  # Appendix A: 3 findings
        self.assertTrue(any("OSPF AREA MISMATCH on subnet 10.10.1.0/24" in x for x in f))

    def test_asymmetric_acl_repeat_trial_bundle(self):
        # The shipped bundle is the repeat-trial collection (corrected
        # procedure), NOT the original run behind Appendix A's 8 findings /
        # 25 deny matches -- see README, "Data provenance".
        f = findings("baseline-final", "fault-asymmetric-acl")
        self.assertEqual(len(f), 3)
        self.assertTrue(any("ACL DENY HITS: R-B1" in x for x in f))

    def test_convergence_race_documented_in_section_9_1(self):
        # Collecting before the OSPF dead timer expired gave 4 findings
        # instead of 5 (R-A2's adjacency change missing); the 45 s delay fixed it.
        self.assertEqual(len(findings("baseline-final", "fault-hsrp-active-down-t2")), 4)
        self.assertEqual(len(findings("baseline-final", "fault-hsrp-active-down-recheck")), 5)

    def test_restore_checks_are_clean(self):
        for name in ("restored-before-wan-trials", "restored-before-ospf-trials", "restored-before-acl-trials"):
            fault = load(name, "restore-checks")
            self.assertEqual(bd.diagnose(load("baseline-final"), fault), [], name)
        fault = load("restored", "restore-checks")
        self.assertEqual(bd.diagnose(load("baseline"), fault), [])


class NoFabrication(unittest.TestCase):
    """The copilot can only see what was collected; everything else is NOT AVAILABLE."""

    @classmethod
    def setUpClass(cls):
        cls.bundle = copilot.EvidenceBundle(EVIDENCE / "fault-ospf-area-mismatch.json")

    def test_collected_command_is_returned_verbatim(self):
        out = copilot.resolve_request(self.bundle, "R-A1", "show ip ospf interface brief")
        self.assertIn("Fa0/0", out)

    def test_configuration_commands_are_never_served(self):
        for cmd in ("enable", "configure terminal", "no shutdown"):
            out = copilot.resolve_request(self.bundle, "R-A1", cmd)
            self.assertTrue(out.startswith("NOT AVAILABLE"), cmd)

    def test_unknown_device_and_uncollected_ping(self):
        self.assertTrue(copilot.resolve_request(self.bundle, "R-X9", "show ip route").startswith("NOT AVAILABLE"))
        self.assertTrue(copilot.resolve_request(self.bundle, "R-A1", "ping 10.99.99.2").startswith("NOT AVAILABLE"))


if __name__ == "__main__":
    unittest.main()
