#!/usr/bin/env python3
"""Concurrent two-NPU/two-NVMe XDS benchmark with per-card verification.

The default topology is:

* NPU user device 0 <- /dev/nvme3n1, mounted at /workspace
* NPU user device 1 <- /dev/nvme4n1, mounted at /data

Each NPU runs in a separate spawned process with its own NPU runtime context,
destination buffer, test file, and /dev/p2p_device fd.  The SVM context ID is
separate from the NPU user device ID because the diagnosed Ascend driver
resolves both NPU0 and NPU1 virtual addresses through SVM devid 0.
"""

import argparse
import json
import multiprocessing
import os
import queue
import sys
import tempfile
import time
import traceback
from pathlib import Path
from types import SimpleNamespace


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "build" / "python"))

from tools.xds_test import (  # noqa: E402
    TestFailure,
    check_bdev,
    check_result,
    fiemap_check,
    parse_size,
    require_runtime,
)


CANARY = 0xA5
DEFAULT_REQUEST_SIZE = "128K"
DEFAULT_IO_DEPTH = 32
DEFAULT_WARMUP = 2
DEFAULT_ITERATIONS = 20
PATTERN_CHUNK_SIZE = 8 * 1024 * 1024


def nonnegative_int(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a non-negative integer") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("value must be a non-negative integer")
    return number


def positive_int(value):
    number = nonnegative_int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return number


def positive_float(value):
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a positive number") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("value must be a positive number")
    return number


def pattern_bytes(offset, size, seed):
    """Return a deterministic, device-specific byte pattern."""
    base = bytes((seed + index) & 0xFF for index in range(256))
    rotation = offset & 0xFF
    cycle = base[rotation:] + base[:rotation]
    return (cycle * ((size + 255) // 256))[:size]


def write_pattern_file(fd, size, seed):
    position = 0
    while position < size:
        amount = min(PATTERN_CHUNK_SIZE, size - position)
        payload = memoryview(pattern_bytes(position, amount, seed))
        while payload:
            written = os.write(fd, payload)
            if written <= 0:
                raise TestFailure("pattern file write returned zero bytes")
            payload = payload[written:]
            position += written


class WorkerInputFile:
    def __init__(self, config, log_args):
        directory = Path(config["data_dir"]).resolve()
        if not directory.is_dir():
            raise TestFailure(f"data directory is unavailable: {directory}")

        self.path = None
        fd, name = tempfile.mkstemp(
            prefix=f"xds-dual-npu{config['npu_devid']}-",
            suffix=".bin",
            dir=directory,
        )
        self.path = Path(name)
        try:
            try:
                write_pattern_file(fd, config["working_set_size"], config["pattern_seed"])
                os.fsync(fd)
            finally:
                os.close(fd)

            allocated = self.path.stat().st_blocks * 512
            if allocated < config["working_set_size"]:
                raise TestFailure("generated test file is sparse")
            fiemap_check(str(self.path), 0, config["working_set_size"], log_args)
            check_bdev(str(self.path), config["bdev"], log_args)
        except Exception:
            self.close()
            raise

    def close(self):
        if self.path is not None:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self.path = None


def submit_one_iteration(file_p2p, fd, config, file_path, address):
    request_size = config["request_size"]
    for index in range(config["io_depth"]):
        check_result(
            "read_file",
            file_p2p.read_file(
                fd,
                str(file_path),
                config["bdev"],
                index * request_size,
                address + index * request_size,
                request_size,
                config["svm_devid"],
                config["vfid"],
            ),
        )
    check_result("drain_read", file_p2p.drain_read(fd))


def verify_buffer(torch, buffer, config):
    torch.npu.synchronize()
    actual = buffer.cpu().numpy().tobytes()
    wanted = pattern_bytes(0, config["working_set_size"], config["pattern_seed"])
    if actual == wanted:
        return

    mismatch = next(
        (index for index, (got, expected) in enumerate(zip(actual, wanted)) if got != expected),
        min(len(actual), len(wanted)),
    )
    got = actual[mismatch] if mismatch < len(actual) else None
    expected = wanted[mismatch] if mismatch < len(wanted) else None
    raise TestFailure(
        "data verification failed: "
        f"npu_devid={config['npu_devid']} svm_devid={config['svm_devid']} "
        f"bdev={config['bdev']} first_index={mismatch} "
        f"actual={got!r} expected={expected!r}"
    )


def worker_main(config, barrier, result_queue, verbose):
    input_file = None
    fd = None
    result = {
        "status": "FAIL",
        "worker": config["name"],
        "npu_devid": config["npu_devid"],
        "svm_devid": config["svm_devid"],
        "vfid": config["vfid"],
        "bdev": config["bdev"],
        "data_dir": config["data_dir"],
    }
    log_args = SimpleNamespace(
        command="dual-bench",
        verbose=verbose,
        log_started=time.monotonic(),
        devid=config["npu_devid"],
        vfid=config["vfid"],
    )
    try:
        torch, file_p2p = require_runtime(log_args)
        torch.npu.set_device(config["npu_devid"])

        input_file = WorkerInputFile(config, log_args)
        buffer = torch.empty(
            config["working_set_size"],
            dtype=torch.uint8,
            device=torch.device(f"npu:{config['npu_devid']}"),
        )
        buffer.fill_(CANARY)
        torch.npu.synchronize()
        address = buffer.data_ptr()

        fd = file_p2p.new_p2p_fd()
        check_result("new_p2p_fd", fd)

        for _ in range(config["warmup"]):
            submit_one_iteration(file_p2p, fd, config, input_file.path, address)

        # Parent and both workers cross this barrier together.  Timed traffic
        # therefore starts only after both files, contexts, buffers and fds are ready.
        barrier.wait(timeout=config["start_timeout"])
        started_ns = time.monotonic_ns()
        for _ in range(config["iterations"]):
            submit_one_iteration(file_p2p, fd, config, input_file.path, address)
        ended_ns = time.monotonic_ns()

        verify_buffer(torch, buffer, config)
        transferred = config["working_set_size"] * config["iterations"]
        elapsed_ns = ended_ns - started_ns
        result.update(
            {
                "status": "PASS",
                "file": str(input_file.path),
                "buffer_address": f"0x{address:x}",
                "pattern_seed": config["pattern_seed"],
                "request_size": config["request_size"],
                "io_depth": config["io_depth"],
                "warmup": config["warmup"],
                "iterations": config["iterations"],
                "bytes": transferred,
                "started_ns": started_ns,
                "ended_ns": ended_ns,
                "elapsed_ns": elapsed_ns,
                "bandwidth_bytes_per_sec": (
                    transferred * 1_000_000_000 / elapsed_ns if elapsed_ns else 0
                ),
                "verify": {"enabled": True, "status": "ok"},
            }
        )
    except Exception as exc:  # Child errors must always reach the parent.
        try:
            barrier.abort()
        except Exception:
            pass
        result["error"] = str(exc)
        result["traceback"] = traceback.format_exc()
    finally:
        if fd is not None:
            try:
                file_p2p.close_p2p_fd(fd)
            except Exception:
                pass
        if input_file is not None:
            input_file.close()
        result_queue.put(result)


def worker_configs(args):
    common = {
        "request_size": args.request_size,
        "io_depth": args.io_depth,
        "working_set_size": args.request_size * args.io_depth,
        "warmup": args.warmup,
        "iterations": args.iterations,
        "vfid": args.vfid,
        "start_timeout": args.start_timeout,
    }
    return [
        {
            **common,
            "name": "npu0-nvme3",
            "npu_devid": args.npu0_devid,
            "svm_devid": args.npu0_svm_devid,
            "bdev": args.npu0_bdev,
            "data_dir": args.npu0_data_dir,
            "pattern_seed": 0x31,
        },
        {
            **common,
            "name": "npu1-nvme4",
            "npu_devid": args.npu1_devid,
            "svm_devid": args.npu1_svm_devid,
            "bdev": args.npu1_bdev,
            "data_dir": args.npu1_data_dir,
            "pattern_seed": 0xA7,
        },
    ]


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--npu0-devid", type=nonnegative_int, default=0)
    root.add_argument(
        "--npu0-svm-devid",
        type=nonnegative_int,
        default=0,
        help="DEVMM SVM context ID for the NPU0 buffer (default: 0)",
    )
    root.add_argument("--npu0-bdev", default="/dev/nvme3n1")
    root.add_argument("--npu0-data-dir", default="/workspace")
    root.add_argument("--npu1-devid", type=nonnegative_int, default=1)
    root.add_argument(
        "--npu1-svm-devid",
        type=nonnegative_int,
        default=0,
        help="DEVMM SVM context ID for the NPU1 buffer (diagnosed default: 0)",
    )
    root.add_argument("--npu1-bdev", default="/dev/nvme4n1")
    root.add_argument("--npu1-data-dir", default="/data")
    root.add_argument("--vfid", type=nonnegative_int, default=0)
    root.add_argument(
        "--request-size",
        type=parse_size,
        default=parse_size(DEFAULT_REQUEST_SIZE),
        help=f"bytes per read_file request (default: {DEFAULT_REQUEST_SIZE})",
    )
    root.add_argument("--io-depth", type=positive_int, default=DEFAULT_IO_DEPTH)
    root.add_argument("--warmup", type=nonnegative_int, default=DEFAULT_WARMUP)
    root.add_argument("--iterations", type=positive_int, default=DEFAULT_ITERATIONS)
    root.add_argument(
        "--start-timeout",
        type=positive_float,
        default=300.0,
        help="seconds to wait for both workers to become ready (default: 300)",
    )
    root.add_argument(
        "--run-timeout",
        type=positive_float,
        default=3600.0,
        help="seconds to wait for workers to finish after launch (default: 3600)",
    )
    root.add_argument("--json", help="write combined and per-card results as JSON")
    root.add_argument("--verbose", action="store_true")
    return root


def main():
    args = parser().parse_args()
    if args.npu0_devid == args.npu1_devid:
        print("FAIL NPU workers must use different --npu*-devid values", file=sys.stderr)
        return 2
    if args.request_size * args.io_depth > sys.maxsize:
        print("FAIL request-size * io-depth is too large", file=sys.stderr)
        return 2

    configs = worker_configs(args)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(len(configs) + 1)
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=worker_main,
            name=config["name"],
            args=(config, barrier, result_queue, args.verbose),
        )
        for config in configs
    ]

    for process in processes:
        process.start()

    barrier_error = None
    try:
        barrier.wait(timeout=args.start_timeout)
    except Exception as exc:
        barrier_error = str(exc) or type(exc).__name__

    results = []
    deadline = time.monotonic() + args.run_timeout
    while len(results) < len(processes) and time.monotonic() < deadline:
        try:
            results.append(result_queue.get(timeout=1.0))
        except queue.Empty:
            if all(not process.is_alive() for process in processes):
                break

    for process in processes:
        process.join(timeout=5.0)
        if process.is_alive():
            process.terminate()
            process.join()

    returned = {result["worker"] for result in results}
    for process, config in zip(processes, configs):
        if config["name"] not in returned:
            results.append(
                {
                    "status": "FAIL",
                    "worker": config["name"],
                    "npu_devid": config["npu_devid"],
                    "svm_devid": config["svm_devid"],
                    "bdev": config["bdev"],
                    "error": f"worker produced no result; exitcode={process.exitcode}",
                }
            )

    results.sort(key=lambda item: item["npu_devid"])
    passed = len(results) == len(configs) and all(item["status"] == "PASS" for item in results)
    payload = {
        "status": "PASS" if passed else "FAIL",
        "barrier_error": barrier_error,
        "request_size": args.request_size,
        "io_depth": args.io_depth,
        "working_set_size_per_card": args.request_size * args.io_depth,
        "warmup": args.warmup,
        "iterations": args.iterations,
        "workers": results,
    }

    if passed:
        global_started = min(item["started_ns"] for item in results)
        global_ended = max(item["ended_ns"] for item in results)
        global_elapsed = global_ended - global_started
        total_bytes = sum(item["bytes"] for item in results)
        payload.update(
            {
                "bytes": total_bytes,
                "elapsed_ns": global_elapsed,
                "aggregate_bandwidth_bytes_per_sec": (
                    total_bytes * 1_000_000_000 / global_elapsed if global_elapsed else 0
                ),
            }
        )

    for result in results:
        if result["status"] == "PASS":
            print(
                f"PASS worker={result['worker']} npu={result['npu_devid']} "
                f"svm={result['svm_devid']} bdev={result['bdev']} "
                f"bandwidth={result['bandwidth_bytes_per_sec'] / 1024**3:.2f}GiB/s "
                f"verify=ok"
            )
        else:
            print(
                f"FAIL worker={result['worker']} npu={result['npu_devid']} "
                f"svm={result['svm_devid']} bdev={result['bdev']} "
                f"error={result.get('error', 'unknown error')}",
                file=sys.stderr,
            )

    if passed:
        print(
            "PASS aggregate "
            f"bandwidth={payload['aggregate_bandwidth_bytes_per_sec'] / 1024**3:.2f}GiB/s "
            f"bytes={payload['bytes']} verify=ok"
        )
    elif barrier_error:
        print(f"FAIL start barrier: {barrier_error}", file=sys.stderr)

    if args.json:
        json_path = Path(args.json)
        json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"REPORT json={json_path}")

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
