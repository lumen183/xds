#!/usr/bin/env python3
"""Sequential single-NPU P2P streaming benchmark.

Create one allocated test file, then scan it sequentially for every requested
P2P request-size and queue-depth combination.  This intentionally uses only
the stable single-request file_p2p API.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
# Direct execution does not receive tools/xds.sh's PYTHONPATH setup.
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "build" / "python"))

from tools.xds_test import (  # noqa: E402
    InputFile,
    TestFailure,
    check_result,
    parse_size,
    require_runtime,
    verify,
)


DEFAULT_SIZES = "32K,64K,128K,256K,512K,1M"
DEFAULT_IO_DEPTHS = "4,8,16,32,64,128"


def parse_size_list(value):
    """Parse a comma-separated list of the project's normal size values."""
    values = [item.strip() for item in value.split(",")]
    if not values or any(not item for item in values):
        raise argparse.ArgumentTypeError("size list must be comma-separated non-empty values")
    return [parse_size(item) for item in values]


def parse_io_depth_list(value):
    values = [item.strip() for item in value.split(",")]
    if not values or any(not item for item in values):
        raise argparse.ArgumentTypeError("io-depth list must be comma-separated positive integers")
    try:
        depths = [int(item) for item in values]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("io-depth list must contain positive integers") from exc
    if any(depth < 1 for depth in depths):
        raise argparse.ArgumentTypeError("io-depth values must be positive")
    return depths


def stream_once(torch, file_p2p, fd, args, request_size, io_depth):
    """Read the configured stream range once and return its measured metrics."""
    buffer = torch.empty(
        request_size * io_depth,
        dtype=torch.uint8,
        device=torch.device(f"npu:{args.devid}"),
    )
    address = buffer.data_ptr()
    cursor = args.offset
    end = args.offset + args.file_size
    started = time.perf_counter_ns()
    try:
        while cursor < end:
            remaining = end - cursor
            count = min(io_depth, (remaining + request_size - 1) // request_size)
            # The final request may be smaller, so the stream covers the whole
            # generated file even when --file-size is not a multiple of --size.
            for index in range(count):
                length = min(request_size, end - (cursor + index * request_size))
                check_result(
                    "read_file",
                    file_p2p.read_file(
                        fd,
                        str(args.input.path),
                        args.bdev,
                        cursor + index * request_size,
                        address + index * request_size,
                        length,
                        args.devid,
                        args.vfid,
                    ),
                )
            check_result("drain_read", file_p2p.drain_read(fd))
            cursor += count * request_size
    finally:
        elapsed_ns = time.perf_counter_ns() - started
    return buffer, elapsed_ns


def sample_offsets(offset, length, request_size):
    """Return deterministic first/middle/last reads without duplicate offsets."""
    sample_size = min(length, request_size)
    end = offset + length
    middle = offset + ((length // 2) // request_size) * request_size
    candidates = (offset, middle, end - sample_size)
    return sample_size, list(dict.fromkeys(candidates))


def verify_samples(torch, file_p2p, fd, args, request_size):
    """Verify three representative locations outside the measured stream pass."""
    sample_size, offsets = sample_offsets(args.offset, args.file_size, request_size)
    buffer = torch.empty(sample_size, dtype=torch.uint8, device=torch.device(f"npu:{args.devid}"))
    address = buffer.data_ptr()
    for offset in offsets:
        check_result(
            "read_file",
            file_p2p.read_file(fd, str(args.input.path), args.bdev, offset, address, sample_size, args.devid, args.vfid),
        )
        check_result("drain_read", file_p2p.drain_read(fd))
        torch.npu.synchronize()
        verify(torch, buffer, offset, sample_size)
    return {"enabled": True, "status": "ok", "samples": len(offsets), "sample_size": sample_size}


def run(args):
    torch, file_p2p = require_runtime(args)
    args.input = InputFile(args, args.offset + args.file_size)
    fd = None
    try:
        fd = file_p2p.new_p2p_fd()
        check_result("new_p2p_fd", fd)
        results = []
        for request_size in args.sizes:
            for io_depth in args.io_depths:
                buffer, elapsed_ns = stream_once(torch, file_p2p, fd, args, request_size, io_depth)
                bandwidth = args.file_size * 1_000_000_000 / elapsed_ns if elapsed_ns else 0
                verification = (
                    verify_samples(torch, file_p2p, fd, args, request_size)
                    if args.verify
                    else {"enabled": False, "status": "skipped"}
                )
                result = {
                    "status": "PASS",
                    "api": "single",
                    "bdev": args.bdev,
                    "file": str(args.input.path),
                    "devid": args.devid,
                    "vfid": args.vfid,
                    "offset": args.offset,
                    "file_size": args.file_size,
                    "size": request_size,
                    "io_depth": io_depth,
                    "bytes": args.file_size,
                    "elapsed_ns": elapsed_ns,
                    "bandwidth_bytes_per_sec": bandwidth,
                    "verify": verification,
                }
                results.append(result)
                print(
                    f"PASS size={request_size}B io_depth={io_depth} bytes={args.file_size} "
                    f"bandwidth={bandwidth / 1024**3:.2f}GiB/s verify={verification['status']}",
                    flush=True,
                )
                # Keep this reference alive through drain and optional validation.
                del buffer
        best = max(results, key=lambda item: item["bandwidth_bytes_per_sec"])
        print(
            f"BEST size={best['size']}B io_depth={best['io_depth']} "
            f"bandwidth={best['bandwidth_bytes_per_sec'] / 1024**3:.2f}GiB/s",
            flush=True,
        )
        payload = {"status": "PASS", "file_size": args.file_size, "results": results, "best": best}
        if args.json:
            Path(args.json).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return payload
    finally:
        if fd is not None:
            file_p2p.close_p2p_fd(fd)
        args.input.close()


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--bdev", required=True, help="block device backing --data-dir")
    root.add_argument("--data-dir", required=True, help="directory in which to create the temporary stream file")
    root.add_argument("--file-size", type=parse_size, default=parse_size("3G"), help="temporary file and sequential stream size (default: 3G)")
    root.add_argument("--size", dest="sizes", type=parse_size_list, default=parse_size_list(DEFAULT_SIZES), help=f"P2P request size(s), comma-separated (default: {DEFAULT_SIZES})")
    root.add_argument("--io-depth", dest="io_depths", type=parse_io_depth_list, default=parse_io_depth_list(DEFAULT_IO_DEPTHS), help=f"concurrent request count(s), comma-separated (default: {DEFAULT_IO_DEPTHS})")
    root.add_argument("--offset", type=int, default=0, help="file offset at which the stream starts (default: 0)")
    root.add_argument("--devid", type=int, default=0, help="Ascend NPU device id (default: 0)")
    root.add_argument("--vfid", type=int, default=0, help="Ascend virtual device id (default: 0)")
    root.add_argument("--verify", action="store_true", default=True, help="verify first/middle/last samples after each scan (default)")
    root.add_argument("--no-verify", dest="verify", action="store_false", help="skip post-scan sample verification")
    root.add_argument("--json", help="write all results as JSON")
    root.add_argument("--verbose", action="store_true", help="accepted for consistency with tools/xds_test.py")
    return root


def main():
    args = parser().parse_args()
    args.command = "stream-bench"
    args.file = None
    if args.offset < 0 or args.devid < 0 or args.vfid < 0:
        print("FAIL offset and device ids must be non-negative", file=sys.stderr)
        return 1
    try:
        run(args)
    except TestFailure as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
