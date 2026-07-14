#!/usr/bin/env python3
"""Real Ascend P2P smoke and benchmark runner used by tools/xds.sh."""
import argparse
import errno
import fcntl
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

FS_IOC_FIEMAP = 0xC020660B
FIEMAP_FLAG_SYNC = 0x00000001
FIEMAP_EXTENT_UNWRITTEN = 0x00000800
_MIB = 1024 * 1024
_FIEMAP_HEADER_SIZE = 32
_FIEMAP_EXTENT_SIZE = 56
# Keep each ioctl allocation modest, but never mistake a full page for the end
# of a fragmented file's extent map.
_FIEMAP_EXTENTS_PER_QUERY = 256


class TestFailure(RuntimeError):
    pass


def log(args, phase, message):
    """Emit phase logs for smoke, or for any command explicitly made verbose."""
    if args.command != "smoke" and not getattr(args, "verbose", False):
        return
    started = getattr(args, "log_started", time.monotonic())
    elapsed = time.monotonic() - started
    print(f"INFO phase={phase} elapsed={elapsed:.3f}s {message}", file=sys.stderr, flush=True)


def parse_size(value):
    suffixes = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
    text = value.strip().lower()
    if text.endswith("ib"):
        text = text[:-2]
    elif text.endswith("b"):
        text = text[:-1]
    suffix = text[-1:] if text[-1:] in suffixes else ""
    try:
        result = int(text[:-1] if suffix else text) * suffixes[suffix]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("size must be an integer with optional K/M/G/T suffix") from exc
    if result <= 0 or result > 0xFFFFFFFF:
        raise argparse.ArgumentTypeError("size must be between 1 and 4GiB")
    return result


def percentile(values, fraction):
    values = sorted(values)
    index = max(0, min(len(values) - 1, (len(values) * fraction + 0.999999).__ceil__() - 1))
    return values[index]


def expected(offset, size):
    # Avoid a Python byte-at-a-time loop for multi-GiB generated files.
    base = bytes(range(256))
    prefix = base[offset & 0xFF:] + base[:offset & 0xFF]
    return (prefix * ((size + 255) // 256))[:size]


def write_pattern(fd, size):
    position = 0
    chunk_size = min(8 * _MIB, size)
    while position < size:
        amount = min(chunk_size, size - position)
        payload = memoryview(expected(position, amount))
        while payload:
            written = os.write(fd, payload)
            payload = payload[written:]
            position += written


def fiemap_check(path, offset, length, args):
    """Verify requested bytes have allocated, written FIEMAP extents."""
    import struct
    fd = os.open(path, os.O_RDONLY)
    cursor = offset
    end = offset + length
    total_mapped = 0
    try:
        while cursor < end:
            # FIEMAP returns at most fm_extent_count records.  A full result is
            # not an error: query again from the byte just covered.
            buffer = bytearray(_FIEMAP_HEADER_SIZE + _FIEMAP_EXTENTS_PER_QUERY * _FIEMAP_EXTENT_SIZE)
            # Ask the filesystem to flush pending extent conversion before
            # examining it.  Without FIEMAP_FLAG_SYNC, ext4/XFS can report
            # freshly written extents as UNWRITTEN despite the preceding fsync.
            struct.pack_into("=QQIIII", buffer, 0, cursor, end - cursor, FIEMAP_FLAG_SYNC, 0, _FIEMAP_EXTENTS_PER_QUERY, 0)
            fcntl.ioctl(fd, FS_IOC_FIEMAP, buffer, True)
            _, _, _, mapped, _, _ = struct.unpack_from("=QQIIII", buffer, 0)
            total_mapped += mapped
            log(args, "fiemap", f"path={path} offset={cursor} length={end - cursor} mapped_extents={mapped} total_mapped_extents={total_mapped}")
            if not mapped:
                raise TestFailure("FIEMAP returned no extents; the file may be sparse")

            next_cursor = cursor
            for index in range(mapped):
                logical, _physical, extent_length, _r1, _r2, flags, *_ = struct.unpack_from(
                    "=QQQQQIIII", buffer, _FIEMAP_HEADER_SIZE + index * _FIEMAP_EXTENT_SIZE)
                # Compare with the end of the preceding extent, not the
                # original query cursor.  A fragmented but contiguous file
                # legitimately has later extents whose logical offset is
                # greater than ``cursor``.
                if flags & FIEMAP_EXTENT_UNWRITTEN or logical > next_cursor:
                    raise TestFailure("FIEMAP shows an unwritten or sparse extent")
                next_cursor = max(next_cursor, logical + extent_length)
                if next_cursor >= end:
                    log(args, "fiemap", f"path={path} coverage=complete extents={total_mapped}")
                    return
            if next_cursor <= cursor:
                raise TestFailure("FIEMAP made no progress while checking requested read range")
            cursor = next_cursor
    except OSError as exc:
        raise TestFailure(f"FIEMAP failed for {path}: {exc}") from exc
    finally:
        os.close(fd)
    raise TestFailure("FIEMAP extents do not cover requested read range")


def check_bdev(path, bdev, args):
    log(args, "bdev.check", f"file={path} requested_bdev={bdev}")
    if not stat.S_ISBLK(os.stat(bdev).st_mode):
        raise TestFailure(f"--bdev is not a block device: {bdev}")
    found = subprocess.run(["findmnt", "-n", "-o", "SOURCE", "--target", path], text=True, capture_output=True)
    if found.returncode != 0:
        raise TestFailure(f"cannot determine filesystem backing {path}; it must be local to {bdev}")
    source = os.path.realpath(found.stdout.strip())
    target = os.path.realpath(bdev)
    log(args, "bdev.check", f"filesystem_source={source} requested_source={target}")
    if source == target:
        return
    parent = subprocess.run(["lsblk", "-no", "PKNAME", source], text=True, capture_output=True)
    if parent.returncode == 0 and parent.stdout.strip() and os.path.realpath("/dev/" + parent.stdout.strip()) == target:
        return
    raise TestFailure(f"file filesystem ({source}) does not match --bdev ({target})")


class InputFile:
    def __init__(self, args, required_size):
        self.path = None
        self.generated = False
        log(args, "input.prepare", f"required_size={required_size} data_dir={args.data_dir!r} file={args.file!r}")
        if args.file:
            self.path = Path(args.file).resolve()
            if not self.path.is_file():
                raise TestFailure(f"--file is not a regular file: {self.path}")
        else:
            directory = Path(args.data_dir).resolve()
            if not directory.is_dir():
                raise TestFailure(f"--data-dir is not a directory: {directory}")
            fd, name = tempfile.mkstemp(prefix="xds-test-", suffix=".bin", dir=directory)
            self.path, self.generated = Path(name), True
            log(args, "input.write", f"path={self.path} bytes={required_size}")
            try:
                write_pattern(fd, required_size)
                log(args, "input.fsync", f"path={self.path}")
                os.fsync(fd)
            finally:
                os.close(fd)
        if self.path.stat().st_size < required_size:
            raise TestFailure(f"file is too small: need {required_size} bytes, got {self.path.stat().st_size}")
        allocated = self.path.stat().st_blocks * 512
        log(args, "input.ready", f"path={self.path} size={self.path.stat().st_size} allocated={allocated}")
        if allocated < required_size:
            raise TestFailure("test file is sparse; use a filesystem with allocated local extents")
        fiemap_check(str(self.path), args.offset, required_size - args.offset, args)
        check_bdev(str(self.path), args.bdev, args)
        log(args, "input.checked", f"path={self.path}")

    def close(self):
        if self.generated and self.path:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


def require_runtime(args):
    log(args, "runtime.device", "checking /dev/p2p_device access")
    if not os.access("/dev/p2p_device", os.R_OK | os.W_OK):
        raise TestFailure("/dev/p2p_device is unavailable; run ./tools/xds.sh setup first")
    try:
        log(args, "runtime.import", "importing torch")
        import torch
        log(args, "runtime.import", f"torch imported version={getattr(torch, '__version__', 'unknown')}")
        log(args, "runtime.import", "importing torch_npu")
        import torch_npu  # noqa: F401
        log(args, "runtime.import", "torch_npu imported")
        log(args, "runtime.import", "importing file_p2p")
        import file_p2p
        log(args, "runtime.import", "file_p2p imported")
    except ImportError as exc:
        raise TestFailure(f"runtime import failed: {exc}") from exc
    log(args, "runtime.npu", "querying NPU device count")
    device_count = torch.npu.device_count() if hasattr(torch, "npu") else 0
    log(args, "runtime.npu", f"device_count={device_count} requested_devid={args.devid} requested_vfid={args.vfid}")
    if not hasattr(torch, "npu") or args.devid >= device_count:
        raise TestFailure(f"invalid NPU device id: {args.devid}")
    return torch, file_p2p


def check_result(operation, result):
    if result < 0:
        detail = os.strerror(-result) if -result in errno.errorcode else "unknown error"
        raise TestFailure(f"{operation} failed: errno={-result} ({detail})")


def verify(torch, buf, offset, size):
    actual = buf[:size].cpu().numpy().tobytes()
    wanted = expected(offset, size)
    if actual == wanted:
        return {"enabled": True, "status": "ok"}
    for i, (got, want) in enumerate(zip(actual, wanted)):
        if got != want:
            raise TestFailure(f"data verification failed at file_offset={offset + i}, device_address=0x{buf.data_ptr() + i:x}, actual=0x{got:02x}, expected=0x{want:02x}")
    raise TestFailure("data verification failed: returned buffer length differs")


def submit(file_p2p, fd, args, addr, request_count, label):
    log(args, "io.submit", f"begin label={label} api={args.api} requests={request_count} size={args.size} addr=0x{addr:x}")
    if args.api == "batch":
        requests = [(args.offset + i * args.size, addr + i * args.size, args.size) for i in range(request_count)]
        check_result("read_file_batch", file_p2p.read_file_batch(fd, str(args.input.path), args.bdev, requests, args.devid, args.vfid))
    else:
        for i in range(request_count):
            check_result("read_file", file_p2p.read_file(fd, str(args.input.path), args.bdev, args.offset + i * args.size, addr + i * args.size, args.size, args.devid, args.vfid))
    log(args, "io.submit", f"queued label={label}")
    log(args, "io.drain", f"begin label={label}; waiting for kernel/NVMe completions")
    check_result("drain_read", file_p2p.drain_read(fd))
    log(args, "io.drain", f"complete label={label}")


def run(args):
    log(args, "run.start", f"command={args.command} api={args.api} bdev={args.bdev} size={args.size} offset={args.offset} devid={args.devid} vfid={args.vfid}")
    torch, file_p2p = require_runtime(args)
    request_count = args.batch_size if args.api == "batch" else args.inflight
    required_size = args.offset + args.size * request_count
    args.input = InputFile(args, required_size)
    fd = None
    try:
        device_name = f"npu:{args.devid}"
        log(args, "npu.alloc", f"begin device={device_name} bytes={args.size * request_count}")
        buf = torch.empty(args.size * request_count, dtype=torch.uint8, device=torch.device(device_name))
        log(args, "npu.alloc", f"complete device={device_name} addr=0x{buf.data_ptr():x}")
        log(args, "p2p.open", "opening /dev/p2p_device")
        fd = file_p2p.new_p2p_fd()
        check_result("new_p2p_fd", fd)
        log(args, "p2p.open", f"complete fd={fd}")
        for _ in range(args.warmup):
            submit(file_p2p, fd, args, buf.data_ptr(), request_count, f"warmup-{_ + 1}/{args.warmup}")
        elapsed = []
        for iteration in range(args.iterations):
            log(args, "iteration", f"begin {iteration + 1}/{args.iterations}")
            start = time.perf_counter_ns()
            submit(file_p2p, fd, args, buf.data_ptr(), request_count, f"iteration-{iteration + 1}/{args.iterations}")
            elapsed.append(time.perf_counter_ns() - start)
            log(args, "iteration", f"complete {iteration + 1}/{args.iterations} elapsed_ns={elapsed[-1]}")
        log(args, "npu.sync", "begin torch.npu.synchronize()")
        torch.npu.synchronize()
        log(args, "npu.sync", "complete")
        log(args, "verify", f"begin bytes={args.size * request_count}")
        verification = verify(torch, buf, args.offset, args.size * request_count) if args.verify else {"enabled": False, "status": "skipped"}
        log(args, "verify", f"complete status={verification['status']}")
        total = sum(elapsed)
        byte_count = args.size * request_count * args.iterations
        result = {"status": "PASS", "api": args.api, "bdev": args.bdev, "file": str(args.input.path), "devid": args.devid, "vfid": args.vfid, "size": args.size, "inflight": args.inflight, "warmup": args.warmup, "iterations": args.iterations, "bytes": byte_count, "elapsed_ns": total, "bandwidth_bytes_per_sec": byte_count * 1_000_000_000 / total if total else 0, "latency_ns": {"p50": percentile(elapsed, .50), "p95": percentile(elapsed, .95), "p99": percentile(elapsed, .99)}, "verify": verification}
        bandwidth = result["bandwidth_bytes_per_sec"] / 1024**3
        print(f"PASS api={args.api} size={args.size}B inflight={args.inflight} iterations={args.iterations} bandwidth={bandwidth:.2f}GiB/s p50={result['latency_ns']['p50'] / 1e6:.2f}ms p95={result['latency_ns']['p95'] / 1e6:.2f}ms verify={verification['status']}")
        return result
    finally:
        if fd is not None:
            log(args, "p2p.close", f"closing fd={fd}")
            file_p2p.close_p2p_fd(fd)
        log(args, "input.cleanup", f"removing generated input={args.input.path}")
        args.input.close()
        log(args, "run.end", "resources released")


def parser():
    common = argparse.ArgumentParser(add_help=False)
    source = common.add_mutually_exclusive_group(required=True)
    source.add_argument("--data-dir")
    source.add_argument("--file")
    common.add_argument("--bdev", required=True)
    common.add_argument("--size", required=True, type=parse_size)
    common.add_argument("--offset", type=int, default=0)
    common.add_argument("--devid", type=int, default=0)
    common.add_argument("--vfid", type=int, default=0)
    common.add_argument("--verbose", action="store_true", help="emit phase logs (smoke logs by default)")
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    smoke = commands.add_parser("smoke", parents=[common])
    smoke.set_defaults(api="single", inflight=1, batch_size=1, warmup=0, iterations=1, verify=True, json=None)
    bench = commands.add_parser("bench", parents=[common])
    bench.add_argument("--api", choices=("single", "batch"), help="run one API; omit to run single then batch")
    bench.add_argument("--batch-size", type=int, default=1)
    bench.add_argument("--inflight", type=int, default=1)
    bench.add_argument("--warmup", type=int, default=5)
    bench.add_argument("--iterations", type=int, default=20)
    bench.add_argument("--verify", action="store_true", default=True)
    bench.add_argument("--no-verify", dest="verify", action="store_false")
    bench.add_argument("--json")
    return root


def main():
    args = parser().parse_args()
    args.log_started = time.monotonic()
    try:
        if args.offset < 0 or args.devid < 0 or args.vfid < 0 or args.inflight < 1 or args.batch_size < 1 or args.warmup < 0 or args.iterations < 1:
            raise TestFailure("offset, device ids, batch/inflight, warmup and iterations are out of range")
        apis = (args.api,) if args.command == "smoke" or args.api else ("single", "batch")
        results = []
        for api in apis:
            args.api = api
            if api == "batch" and args.inflight != 1:
                raise TestFailure("batch mode submits one contiguous batch per iteration; use --batch-size instead of --inflight")
            results.append(run(args))
        if args.json:
            payload = results[0] if len(results) == 1 else results
            Path(args.json).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except TestFailure as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
