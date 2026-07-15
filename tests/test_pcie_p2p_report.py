#!/usr/bin/env python3
import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "pcie_p2p_report.py"
SPEC = importlib.util.spec_from_file_location("pcie_p2p_report", SCRIPT)
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)


class PcieP2pReportTest(unittest.TestCase):
    def test_pcie_gen4_bandwidth(self):
        self.assertAlmostEqual(REPORT.pcie_gib_per_sec(16.0, 4), 7.336, places=3)
        self.assertAlmostEqual(REPORT.pcie_gib_per_sec(16.0, 16), 29.344, places=3)

    def test_path_uses_slowest_link(self):
        devices = {
            "0000:00:01.0": {"parent": None, "wire_gib_s": 29.34},
            "0000:01:00.0": {"parent": "0000:00:01.0", "wire_gib_s": 14.67},
            "0000:02:00.0": {"parent": "0000:01:00.0", "wire_gib_s": 7.335},
            "0000:03:00.0": {"parent": "0000:01:00.0", "wire_gib_s": 29.34},
        }
        result = REPORT.analyze_path("0000:02:00.0", "0000:03:00.0", devices, 0.9)
        self.assertEqual(result["lca"], "0000:01:00.0")
        self.assertEqual(result["bottleneck_bdf"], "0000:02:00.0")
        self.assertAlmostEqual(result["engineering_gib_s"], 6.6015)

    def test_size_and_status(self):
        self.assertEqual(REPORT.parse_size("512K"), 512 * 1024)
        self.assertEqual(REPORT.parse_size("1GiB"), 1024 ** 3)
        self.assertEqual(REPORT.status_for(19.46, 6.6)[0], "bad")

    def test_parse_acs_redirect(self):
        output = """0000:01:00.0 PCI bridge: Example\n\t\tACSCtl: SrcValid+ TransBlk- ReqRedir+ CmpltRedir+ DirectTrans-\n0000:02:00.0 Non-Volatile memory controller: Example\n"""
        details = REPORT.parse_lspci_verbose(output)
        self.assertTrue(details["0000:01:00.0"]["acs_redirect"])


if __name__ == "__main__":
    unittest.main()
