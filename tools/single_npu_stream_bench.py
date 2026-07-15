#!/usr/bin/env python3
"""Sequential single-NPU P2P streaming benchmark.

Create one allocated test file, then scan it sequentially for every requested
P2P request-size and queue-depth combination.  This intentionally uses only
the stable single-request file_p2p API.
"""
import argparse
import gc
import json
import math
import os
import sys
import tempfile
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
    log,
    parse_size,
    require_runtime,
)
from tools.plot_single_npu_stream_bench import report_html  # noqa: E402


DEFAULT_SIZES = "32K,64K,128K,256K,512K,1M"
DEFAULT_IO_DEPTHS = "4,8,16,32,64,128"
DEFAULT_PATTERN_SEED = 0x58D5A17E20260715
DEFAULT_VERIFY_SAMPLES = 32
PATTERN_NAME = "splitmix64-v1"
CANARY = 0xA5
_MASK64 = (1 << 64) - 1
_SPLITMIX_INCREMENT = 0x9E3779B97F4A7C15
_SPLITMIX_MULTIPLIER_1 = 0xBF58476D1CE4E5B9
_SPLITMIX_MULTIPLIER_2 = 0x94D049BB133111EB
_PATTERN_CHUNK_SIZE = 8 * 1024 * 1024
np = None


def require_numpy():
    """Load NumPy only for real runs so --help works before runtime setup."""
    global np
    if np is not None:
        return
    try:
        import numpy as numpy_module
    except ImportError as exc:
        raise TestFailure("NumPy is required for stream pattern generation and verification") from exc
    np = numpy_module


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


def parse_uint64(value):
    try:
        number = int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a decimal or 0x-prefixed integer") from exc
    if number < 0 or number > _MASK64:
        raise argparse.ArgumentTypeError("value must fit in an unsigned 64-bit integer")
    return number


def parse_positive_integer(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a positive integer") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return number


def parse_nonnegative_float(value):
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a non-negative number") from exc
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("value must be a finite non-negative number")
    return number


def splitmix64(value):
    """Return one deterministic SplitMix64 output using unsigned arithmetic."""
    value = (value + _SPLITMIX_INCREMENT) & _MASK64
    value = ((value ^ (value >> 30)) * _SPLITMIX_MULTIPLIER_1) & _MASK64
    value = ((value ^ (value >> 27)) * _SPLITMIX_MULTIPLIER_2) & _MASK64
    return value ^ (value >> 31)


def pattern_bytes(offset, size, seed):
    """Generate a slice of the position-dependent splitmix64-v1 byte stream."""
    if np is None:
        raise RuntimeError("require_numpy() must be called before generating the stream pattern")
    if offset < 0 or size < 0:
        raise ValueError("pattern offset and size must be non-negative")
    if size == 0:
        return b""
    first_word = offset // 8
    prefix = offset % 8
    word_count = (prefix + size + 7) // 8
    words = np.arange(first_word, first_word + word_count, dtype=np.uint64)
    words ^= np.uint64(seed)
    words += np.uint64(_SPLITMIX_INCREMENT)
    words = (words ^ (words >> np.uint64(30))) * np.uint64(_SPLITMIX_MULTIPLIER_1)
    words = (words ^ (words >> np.uint64(27))) * np.uint64(_SPLITMIX_MULTIPLIER_2)
    words ^= words >> np.uint64(31)
    encoded = words.astype("<u8", copy=False).tobytes()
    return encoded[prefix:prefix + size]


def write_pattern_file(fd, size, seed):
    position = 0
    while position < size:
        amount = min(_PATTERN_CHUNK_SIZE, size - position)
        payload = memoryview(pattern_bytes(position, amount, seed))
        while payload:
            written = os.write(fd, payload)
            if written == 0:
                raise TestFailure("temporary pattern file write returned zero bytes")
            payload = payload[written:]
            position += written


class StreamInputFile:
    """Generate the stream-specific pattern while reusing shared file checks."""

    def __init__(self, args, required_size):
        self.path = None
        self._checked = None
        directory = Path(args.data_dir).resolve()
        if not directory.is_dir():
            raise TestFailure(f"--data-dir is not a directory: {directory}")
        fd, name = tempfile.mkstemp(prefix="xds-stream-", suffix=".bin", dir=directory)
        self.path = Path(name)
        try:
            log(
                args,
                "input.write",
                f"path={self.path} bytes={required_size} pattern={PATTERN_NAME} "
                f"pattern_seed=0x{args.pattern_seed:016x}",
            )
            try:
                write_pattern_file(fd, required_size, args.pattern_seed)
                os.fsync(fd)
            finally:
                os.close(fd)

            original_file = args.file
            args.file = self.path
            try:
                self._checked = InputFile(args, required_size)
                self.path = self._checked.path
            finally:
                args.file = original_file
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            self.close()
            raise

    def close(self):
        if self._checked is not None:
            self._checked.close()
            self._checked = None
        if self.path is not None:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self.path = None


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
    log(
        args,
        "stream.alloc",
        f"size={request_size} io_depth={io_depth} buffer_bytes={request_size * io_depth} "
        f"addr=0x{address:x} range=[{args.offset},{end})",
    )
    started = time.perf_counter_ns()
    throttle_sleep_ns = 0
    try:
        while cursor < end:
            remaining = end - cursor
            count = min(io_depth, (remaining + request_size - 1) // request_size)
            log(
                args,
                "stream.batch",
                f"size={request_size} io_depth={io_depth} cursor={cursor} remaining={remaining} requests={count}",
            )
            # The final request may be smaller, so the stream covers the whole
            # generated file even when --file-size is not a multiple of --size.
            for index in range(count):
                length = min(request_size, end - (cursor + index * request_size))
                file_offset = cursor + index * request_size
                device_address = address + index * request_size
                log(
                    args,
                    "stream.submit",
                    f"index={index} file_offset={file_offset} device_address=0x{device_address:x} length={length}",
                )
                check_result(
                    "read_file",
                    file_p2p.read_file(
                        fd,
                        str(args.input.path),
                        args.bdev,
                        file_offset,
                        device_address,
                        length,
                        args.devid,
                        args.vfid,
                    ),
                )
            log(args, "stream.drain", f"cursor={cursor} requests={count}")
            check_result("drain_read", file_p2p.drain_read(fd))
            cursor += count * request_size
            log(args, "stream.batch", f"complete next_cursor={cursor}")
            if args.batch_delay and cursor < end:
                sleep_started = time.perf_counter_ns()
                time.sleep(args.batch_delay)
                throttle_sleep_ns += time.perf_counter_ns() - sleep_started
    finally:
        wall_elapsed_ns = time.perf_counter_ns() - started
    # Deliberate safety throttling must not make the device's active transfer
    # bandwidth look lower.  Preserve both active and wall-clock durations.
    elapsed_ns = max(0, wall_elapsed_ns - throttle_sleep_ns)
    return buffer, elapsed_ns, wall_elapsed_ns, throttle_sleep_ns


def sample_request_indices(total_requests, target, seed, request_size, io_depth):
    """Select reproducible, spatially stratified request indices."""
    target = min(total_requests, max(target, io_depth, 3))
    if target == total_requests:
        return list(range(total_requests))

    derived = splitmix64(seed ^ request_size ^ (io_depth << 32) ^ total_requests)
    selected = {0, (total_requests - 1) // 2, total_requests - 1}
    for stratum in range(target):
        begin = stratum * total_requests // target
        end = (stratum + 1) * total_requests // target
        choice = begin + splitmix64(derived ^ stratum) % (end - begin)
        selected.add(choice)

    counter = 0
    while len(selected) < target:
        selected.add(splitmix64(derived ^ 0xD1B54A32D192ED03 ^ counter) % total_requests)
        counter += 1

    # Anchors can make the stratified set exceed target.  Keep them, then take
    # a deterministic subset of the remaining stratified values.
    anchors = {0, (total_requests - 1) // 2, total_requests - 1}
    if len(selected) > target:
        others = sorted(
            selected - anchors,
            key=lambda index: splitmix64(derived ^ 0x94D049BB133111EB ^ index),
        )
        selected = anchors | set(others[:target - len(anchors)])
    return sorted(selected, key=lambda index: splitmix64(derived ^ 0xBF58476D1CE4E5B9 ^ index))


def _raise_data_mismatch(args, mode, batch_index, slot, request_index, request_size,
                         file_offset, address, actual, wanted):
    actual_array = np.frombuffer(actual, dtype=np.uint8)
    wanted_array = np.frombuffer(wanted, dtype=np.uint8)
    differing = np.flatnonzero(actual_array != wanted_array)
    first = int(differing[0])
    window_start = max(0, first - 8)
    window_end = min(len(actual), first + 8)
    diagnostic = (
        f"data verification failed: mode={mode} batch={batch_index} slot={slot} "
        f"request_index={request_index} request_size={request_size} file_offset={file_offset} "
        f"first_index={first} first_file_offset={file_offset + first} "
        f"device_address=0x{address + first:x} mismatch_count={len(differing)} "
        f"window=[{window_start},{window_end}) "
        f"actual_window=0x{actual[window_start:window_end].hex()} "
        f"expected_window=0x{wanted[window_start:window_end].hex()} "
        f"pattern={PATTERN_NAME} pattern_seed=0x{args.pattern_seed:016x}"
    )
    print(f"DEBUG phase=verify.mismatch {diagnostic}", file=sys.stderr, flush=True)
    raise TestFailure(diagnostic)


def _check_canary(args, mode, batch_index, label, region, device_address):
    changed = np.flatnonzero(region != CANARY)
    if not len(changed):
        return
    first = int(changed[0])
    diagnostic = (
        f"verification boundary overwritten: mode={mode} batch={batch_index} region={label} "
        f"first_index={first} device_address=0x{device_address + first:x} "
        f"actual=0x{int(region[first]):02x} expected_canary=0x{CANARY:02x} "
        f"changed_bytes={len(changed)} pattern={PATTERN_NAME} "
        f"pattern_seed=0x{args.pattern_seed:016x}"
    )
    print(f"DEBUG phase=verify.boundary {diagnostic}", file=sys.stderr, flush=True)
    raise TestFailure(diagnostic)


def verify_pass(torch, file_p2p, fd, args, request_size, io_depth):
    """Run an untimed-for-bandwidth independent sample or full verification pass."""
    total_requests = (args.file_size + request_size - 1) // request_size
    if args.verify_mode == "full":
        request_indices = range(total_requests)
    else:
        request_indices = sample_request_indices(
            total_requests,
            args.verify_samples,
            args.pattern_seed,
            request_size,
            io_depth,
        )

    started = time.perf_counter_ns()
    buffer_size = request_size * io_depth
    buffer = torch.empty(buffer_size, dtype=torch.uint8, device=torch.device(f"npu:{args.devid}"))
    address = buffer.data_ptr()
    log(
        args,
        "verify.alloc",
        f"mode={args.verify_mode} size={request_size} io_depth={io_depth} "
        f"buffer_bytes={buffer_size} addr=0x{address:x} alignment={address & 0xFFF}",
    )
    verified_requests = 0
    verified_bytes = 0
    batches = 0
    for start in range(0, len(request_indices), io_depth):
        batch = request_indices[start:start + io_depth]
        buffer.fill_(CANARY)
        torch.npu.synchronize()
        for slot, request_index in enumerate(batch):
            file_offset = args.offset + request_index * request_size
            length = min(request_size, args.offset + args.file_size - file_offset)
            check_result(
                "read_file",
                file_p2p.read_file(
                    fd,
                    str(args.input.path),
                    args.bdev,
                    file_offset,
                    address + slot * request_size,
                    length,
                    args.devid,
                    args.vfid,
                ),
            )
        log(
            args,
            "verify.batch",
            f"mode={args.verify_mode} batch={batches} requests={len(batch)}",
        )
        check_result("drain_read", file_p2p.drain_read(fd))
        torch.npu.synchronize()
        actual_batch = buffer.cpu().numpy()
        for slot, request_index in enumerate(batch):
            file_offset = args.offset + request_index * request_size
            length = min(request_size, args.offset + args.file_size - file_offset)
            slot_start = slot * request_size
            actual = actual_batch[slot_start:slot_start + length].tobytes()
            wanted = pattern_bytes(file_offset, length, args.pattern_seed)
            if actual != wanted:
                _raise_data_mismatch(
                    args,
                    args.verify_mode,
                    batches,
                    slot,
                    request_index,
                    request_size,
                    file_offset,
                    address + slot_start,
                    actual,
                    wanted,
                )
            if length < request_size:
                tail = actual_batch[slot_start + length:slot_start + request_size]
                _check_canary(
                    args,
                    args.verify_mode,
                    batches,
                    f"slot-{slot}-short-request-tail",
                    tail,
                    address + slot_start + length,
                )
            verified_requests += 1
            verified_bytes += length
        unused_start = len(batch) * request_size
        if unused_start < buffer_size:
            _check_canary(
                args,
                args.verify_mode,
                batches,
                "unused-slots",
                actual_batch[unused_start:],
                address + unused_start,
            )
        batches += 1
        if args.batch_delay and start + io_depth < len(request_indices):
            time.sleep(args.batch_delay)

    return {
        "enabled": True,
        "status": "ok",
        "mode": args.verify_mode,
        "scope": "independent-pass",
        "pattern": PATTERN_NAME,
        "pattern_seed": f"0x{args.pattern_seed:016x}",
        "requests": verified_requests,
        "batches": batches,
        "bytes": verified_bytes,
        "elapsed_ns": time.perf_counter_ns() - started,
    }


def isolate_test_resources(torch, file_p2p, fd, args):
    """Close one test's P2P context and force NPU/Python resource release."""
    log(args, "test.isolate", f"begin fd={fd}")
    try:
        torch.npu.synchronize()
    finally:
        file_p2p.close_p2p_fd(fd)
    gc.collect()
    empty_cache = getattr(torch.npu, "empty_cache", None)
    if callable(empty_cache):
        empty_cache()
    torch.npu.synchronize()
    log(args, "test.isolate", f"complete fd={fd}")


def log_host_file_samples(args):
    """Check the ordinary host read path before testing P2P/DMA."""
    sample_size = min(args.file_size, 64)
    tail_offset = args.offset + args.file_size - sample_size
    fd = os.open(args.input.path, os.O_RDONLY)
    try:
        head = os.pread(fd, sample_size, args.offset)
        tail = os.pread(fd, sample_size, tail_offset)
    finally:
        os.close(fd)
    expected_head = pattern_bytes(args.offset, sample_size, args.pattern_seed)
    expected_tail = pattern_bytes(tail_offset, sample_size, args.pattern_seed)
    log(
        args,
        "input.sample",
        f"path={args.input.path} offset={args.offset} length={sample_size} "
        f"head_match={head == expected_head} tail_offset={tail_offset} tail_match={tail == expected_tail} "
        f"head=0x{head.hex()} expected_head=0x{expected_head.hex()} "
        f"tail=0x{tail.hex()} expected_tail=0x{expected_tail.hex()}",
    )
    if head != expected_head or tail != expected_tail:
        raise TestFailure(
            f"generated input pattern verification failed: path={args.input.path} "
            f"head_match={head == expected_head} tail_offset={tail_offset} "
            f"tail_match={tail == expected_tail} pattern={PATTERN_NAME} "
            f"pattern_seed=0x{args.pattern_seed:016x}"
        )


def run(args):
    args.log_started = time.monotonic()
    log(
        args,
        "run.start",
        f"bdev={args.bdev} data_dir={args.data_dir} file_size={args.file_size} "
        f"sizes={args.sizes} io_depths={args.io_depths} offset={args.offset} "
        f"devid={args.devid} vfid={args.vfid} verify_mode={args.verify_mode} "
        f"verify_samples={args.verify_samples} pattern={PATTERN_NAME} "
        f"pattern_seed=0x{args.pattern_seed:016x} isolate_tests={args.isolate_tests} "
        f"inter_test_delay={args.inter_test_delay} batch_delay={args.batch_delay}",
    )
    torch, file_p2p = require_runtime(args)
    require_numpy()
    args.input = StreamInputFile(args, args.offset + args.file_size)
    log_host_file_samples(args)
    fd = None
    try:
        if not args.isolate_tests:
            fd = file_p2p.new_p2p_fd()
            check_result("new_p2p_fd", fd)
            log(args, "p2p.open", f"fd={fd}")
        results = []
        combinations = [
            (request_size, io_depth)
            for request_size in args.sizes
            for io_depth in args.io_depths
        ]
        for test_index, (request_size, io_depth) in enumerate(combinations):
            test_fd = fd
            if args.isolate_tests:
                test_fd = file_p2p.new_p2p_fd()
                check_result("new_p2p_fd", test_fd)
                log(args, "p2p.open", f"fd={test_fd} test={test_index + 1}/{len(combinations)}")
            try:
                buffer, elapsed_ns, wall_elapsed_ns, throttle_sleep_ns = stream_once(
                    torch, file_p2p, test_fd, args, request_size, io_depth
                )
                bandwidth = args.file_size * 1_000_000_000 / elapsed_ns if elapsed_ns else 0
                # The measured pass is fully drained; release its destination
                # before allocating the independent verification-pass buffer.
                del buffer
                verification = (
                    verify_pass(torch, file_p2p, test_fd, args, request_size, io_depth)
                    if args.verify_mode != "none"
                    else {"enabled": False, "status": "skipped", "mode": "none"}
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
                    "wall_elapsed_ns": wall_elapsed_ns,
                    "throttle_sleep_ns": throttle_sleep_ns,
                    "bandwidth_bytes_per_sec": bandwidth,
                    "verify": verification,
                }
                results.append(result)
                print(
                    f"PASS size={request_size}B io_depth={io_depth} bytes={args.file_size} "
                    f"bandwidth={bandwidth / 1024**3:.2f}GiB/s verify={verification['status']}",
                    flush=True,
                )
            finally:
                if args.isolate_tests and test_fd is not None:
                    isolate_test_resources(torch, file_p2p, test_fd, args)
            if args.inter_test_delay and test_index + 1 < len(combinations):
                log(args, "test.cooldown", f"seconds={args.inter_test_delay}")
                time.sleep(args.inter_test_delay)
        best = max(results, key=lambda item: item["bandwidth_bytes_per_sec"])
        print(
            f"BEST size={best['size']}B io_depth={best['io_depth']} "
            f"bandwidth={best['bandwidth_bytes_per_sec'] / 1024**3:.2f}GiB/s",
            flush=True,
        )
        payload = {
            "status": "PASS",
            "file_size": args.file_size,
            "pattern": PATTERN_NAME,
            "pattern_seed": f"0x{args.pattern_seed:016x}",
            "isolate_tests": args.isolate_tests,
            "inter_test_delay": args.inter_test_delay,
            "batch_delay": args.batch_delay,
            "results": results,
            "best": best,
        }
        if args.json:
            json_path = Path(args.json)
            json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            html_path = json_path.with_suffix(".html")
            html_path.write_text(report_html(payload), encoding="utf-8")
            print(f"REPORT json={json_path} html={html_path}", flush=True)
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
    root.add_argument(
        "--verify",
        dest="verify_mode",
        nargs="?",
        choices=("sample", "full"),
        const="sample",
        default="sample",
        help="run an independent sample or full verification pass (default: sample)",
    )
    root.add_argument(
        "--no-verify",
        dest="verify_mode",
        action="store_const",
        const="none",
        help="skip the post-scan verification pass",
    )
    root.add_argument(
        "--verify-samples",
        type=parse_positive_integer,
        default=DEFAULT_VERIFY_SAMPLES,
        help=f"minimum requests checked in sample mode (default: {DEFAULT_VERIFY_SAMPLES}; never less than io-depth)",
    )
    root.add_argument(
        "--pattern-seed",
        type=parse_uint64,
        default=DEFAULT_PATTERN_SEED,
        help=f"splitmix64-v1 seed, decimal or 0x-prefixed (default: 0x{DEFAULT_PATTERN_SEED:016x})",
    )
    root.add_argument(
        "--isolate-tests",
        action="store_true",
        help="give every size/io-depth combination a fresh P2P fd and force NPU/cache cleanup afterward",
    )
    root.add_argument(
        "--inter-test-delay",
        type=parse_nonnegative_float,
        default=0.0,
        metavar="SECONDS",
        help="cooldown between size/io-depth combinations (default: 0)",
    )
    root.add_argument(
        "--batch-delay",
        type=parse_nonnegative_float,
        default=0.0,
        metavar="SECONDS",
        help="sleep between drain batches to limit sustained load; excluded from active bandwidth (default: 0)",
    )
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
