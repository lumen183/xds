#!/usr/bin/env python3
"""Single-process, two-thread XDS benchmark for two NPUs and two NVMe devices.

Default topology:

* thread 0: NPU user device 0 <- /dev/nvme3n1 mounted at /workspace
* thread 1: NPU user device 1 <- /dev/nvme4n1 mounted at /data

Both threads use separate NPU buffers, input files, and /dev/p2p_device fds.
They share one host PID and, for the diagnosed Ascend driver, SVM devid 0.
"""

import json
import os
import queue
import sys
import threading
import time
import traceback
from pathlib import Path
from types import SimpleNamespace


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "build" / "python"))

from tools.dual_npu_dual_nvme_bench import (  # noqa: E402
    CANARY,
    WorkerInputFile,
    parser as process_parser,
    submit_one_iteration,
    verify_buffer,
    worker_configs,
)
from tools.xds_test import TestFailure, check_result, require_runtime  # noqa: E402


def thread_worker_main(config, torch, file_p2p, barrier, result_queue, verbose):
    input_file = None
    fd = None
    native_tid = threading.get_native_id()
    result = {
        "status": "FAIL",
        "worker": config["name"],
        "pid": os.getpid(),
        "native_tid": native_tid,
        "npu_devid": config["npu_devid"],
        "svm_devid": config["svm_devid"],
        "vfid": config["vfid"],
        "bdev": config["bdev"],
        "data_dir": config["data_dir"],
    }
    log_args = SimpleNamespace(
        command="dual-thread-bench",
        verbose=verbose,
        log_started=time.monotonic(),
        devid=config["npu_devid"],
        vfid=config["vfid"],
    )
    try:
        # ACL/PyTorch current-device state must be established by each worker
        # thread.  Buffer placement is also explicit, so it does not depend on
        # whichever device another thread selected.
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

        # Never share a p2p fd across cards.  Each fd owns a separate kernel
        # p2p_batch, so drain_read() waits only for this thread's submissions.
        fd = file_p2p.new_p2p_fd()
        check_result("new_p2p_fd", fd)

        for _ in range(config["warmup"]):
            submit_one_iteration(file_p2p, fd, config, input_file.path, address)

        barrier.wait(timeout=config["start_timeout"])
        started_ns = time.monotonic_ns()
        for _ in range(config["iterations"]):
            submit_one_iteration(file_p2p, fd, config, input_file.path, address)
        ended_ns = time.monotonic_ns()

        # Re-establish the thread's device before synchronization and D2H
        # verification in case the runtime implements current device per thread.
        torch.npu.set_device(config["npu_devid"])
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
    except Exception as exc:
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


def parser():
    root = process_parser()
    root.description = __doc__
    return root


def collect_results(threads, configs, result_queue, run_timeout):
    results = []
    deadline = time.monotonic() + run_timeout
    while len(results) < len(threads) and time.monotonic() < deadline:
        try:
            results.append(result_queue.get(timeout=1.0))
        except queue.Empty:
            if all(not thread.is_alive() for thread in threads):
                break

    for thread in threads:
        remaining = max(0.0, deadline - time.monotonic())
        thread.join(timeout=remaining)

    returned = {result["worker"] for result in results}
    for thread, config in zip(threads, configs):
        if config["name"] not in returned:
            results.append(
                {
                    "status": "FAIL",
                    "worker": config["name"],
                    "pid": os.getpid(),
                    "native_tid": thread.native_id,
                    "npu_devid": config["npu_devid"],
                    "svm_devid": config["svm_devid"],
                    "bdev": config["bdev"],
                    "error": "worker thread timed out or produced no result; "
                    f"alive={thread.is_alive()}",
                }
            )
    return sorted(results, key=lambda item: item["npu_devid"])


def main():
    args = parser().parse_args()
    if args.npu0_devid == args.npu1_devid:
        print("FAIL NPU threads must use different --npu*-devid values", file=sys.stderr)
        return 2
    if args.request_size * args.io_depth > sys.maxsize:
        print("FAIL request-size * io-depth is too large", file=sys.stderr)
        return 2

    configs = worker_configs(args)
    runtime_args = SimpleNamespace(
        command="dual-thread-bench",
        verbose=args.verbose,
        log_started=time.monotonic(),
        devid=max(config["npu_devid"] for config in configs),
        vfid=args.vfid,
    )
    try:
        # Import and initialize the shared Python extension/runtime once.  Each
        # worker still selects and uses its own NPU in its own thread.
        torch, file_p2p = require_runtime(runtime_args)
    except TestFailure as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1

    barrier = threading.Barrier(len(configs) + 1)
    result_queue = queue.Queue()
    threads = [
        threading.Thread(
            target=thread_worker_main,
            name=config["name"],
            args=(config, torch, file_p2p, barrier, result_queue, args.verbose),
            daemon=True,
        )
        for config in configs
    ]

    for thread in threads:
        thread.start()

    barrier_error = None
    try:
        barrier.wait(timeout=args.start_timeout)
    except Exception as exc:
        barrier_error = str(exc) or type(exc).__name__

    results = collect_results(threads, configs, result_queue, args.run_timeout)
    passed = len(results) == len(configs) and all(item["status"] == "PASS" for item in results)
    payload = {
        "status": "PASS" if passed else "FAIL",
        "execution_model": "single-process-two-threads",
        "pid": os.getpid(),
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
                f"PASS worker={result['worker']} pid={result['pid']} tid={result['native_tid']} "
                f"npu={result['npu_devid']} svm={result['svm_devid']} "
                f"bdev={result['bdev']} "
                f"bandwidth={result['bandwidth_bytes_per_sec'] / 1024**3:.2f}GiB/s "
                "verify=ok"
            )
        else:
            print(
                f"FAIL worker={result['worker']} pid={result.get('pid')} "
                f"tid={result.get('native_tid')} npu={result['npu_devid']} "
                f"svm={result['svm_devid']} bdev={result['bdev']} "
                f"error={result.get('error', 'unknown error')}",
                file=sys.stderr,
            )

    if passed:
        print(
            "PASS aggregate model=single-process-two-threads "
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
