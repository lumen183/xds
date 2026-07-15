import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.plot_single_npu_stream_bench import report_html  # noqa: E402


class StreamReportTest(unittest.TestCase):
    def setUp(self):
        rows = [
            {
                "size": 32768,
                "io_depth": 4,
                "bytes": 1073741824,
                "elapsed_ns": 500000000,
                "bandwidth_bytes_per_sec": 2147483648.0,
                "verify": {"enabled": True, "status": "ok", "samples": 3, "sample_size": 32768},
            },
            {
                "size": 65536,
                "io_depth": 8,
                "bytes": 1073741824,
                "elapsed_ns": 250000000,
                "bandwidth_bytes_per_sec": 4294967296.0,
                "verify": {"enabled": False, "status": "skipped"},
            },
        ]
        self.payload = {
            "status": "PASS",
            "file_size": 1073741824,
            "results": rows,
            "best": rows[1],
        }

    def test_report_contains_charts_and_results(self):
        rendered = report_html(self.payload)
        self.assertIn("Throughput heatmap", rendered)
        self.assertIn("Throughput by I/O depth", rendered)
        self.assertIn("4.00 GiB/s", rendered)
        self.assertIn("64K", rendered)

    def test_command_writes_default_and_explicit_output(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "results.json"
            output_path = Path(directory) / "custom.html"
            input_path.write_text(json.dumps(self.payload), encoding="utf-8")
            default = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "plot_single_npu_stream_bench.py"), str(input_path)],
                text=True,
                capture_output=True,
            )
            self.assertEqual(default.returncode, 0, default.stderr)
            self.assertTrue(input_path.with_suffix(".html").is_file())
            command = [
                sys.executable,
                str(ROOT / "tools" / "plot_single_npu_stream_bench.py"),
                str(input_path),
                "--output",
                str(output_path),
            ]
            completed = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(output_path.is_file())
            self.assertIn(f"REPORT html={output_path}", completed.stdout)


if __name__ == "__main__":
    unittest.main()
