import argparse
import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import single_npu_stream_bench as bench  # noqa: E402


class SingleNpuStreamBenchArgumentsTest(unittest.TestCase):
    def parse(self, *extra):
        return bench.parser().parse_args(
            ["--bdev", "/dev/nvme0n1", "--data-dir", "/mnt/nvme", *extra]
        )

    def test_safety_controls_default_to_disabled(self):
        args = self.parse()
        self.assertFalse(args.isolate_tests)
        self.assertEqual(args.inter_test_delay, 0.0)
        self.assertEqual(args.batch_delay, 0.0)

    def test_safety_controls_accept_fractional_seconds(self):
        args = self.parse(
            "--isolate-tests",
            "--inter-test-delay", "10.5",
            "--batch-delay", "0.01",
        )
        self.assertTrue(args.isolate_tests)
        self.assertEqual(args.inter_test_delay, 10.5)
        self.assertEqual(args.batch_delay, 0.01)

    def test_delays_reject_negative_and_non_finite_values(self):
        for option, value in (
            ("--inter-test-delay", "-1"),
            ("--batch-delay", "nan"),
            ("--batch-delay", "inf"),
        ):
            with self.subTest(option=option, value=value):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    self.parse(option, value)


class IsolationCleanupTest(unittest.TestCase):
    def test_cleanup_synchronizes_closes_and_releases_cache(self):
        events = []

        class FakeNpu:
            def synchronize(self):
                events.append("sync")

            def empty_cache(self):
                events.append("empty_cache")

        torch = argparse.Namespace(npu=FakeNpu())
        file_p2p = argparse.Namespace(close_p2p_fd=lambda fd: events.append(("close", fd)))
        args = argparse.Namespace(command="stream-bench", verbose=False)
        with mock.patch.object(bench.gc, "collect", side_effect=lambda: events.append("gc")):
            bench.isolate_test_resources(torch, file_p2p, 17, args)

        self.assertEqual(events, ["sync", ("close", 17), "gc", "empty_cache", "sync"])


if __name__ == "__main__":
    unittest.main()
