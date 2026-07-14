#!/usr/bin/env python3
"""Sequential single-NPU P2P streaming benchmark.

Create one allocated test file, then scan it sequentially for every requested
P2P request-size and queue-depth combination.  This intentionally uses only
the stable single-request file_p2p API.
"""
import argparse
import html
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
    expected,
    log,
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
    log(
        args,
        "stream.alloc",
        f"size={request_size} io_depth={io_depth} buffer_bytes={request_size * io_depth} "
        f"addr=0x{address:x} range=[{args.offset},{end})",
    )
    started = time.perf_counter_ns()
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
    # The P2P driver builds NVMe SGLs from the NPU virtual address.  Use a
    # page-aligned sub-buffer here so verification does not accidentally test
    # the driver's handling of an address such as 0x...0200 instead of the
    # actual read path.
    raw_buffer = torch.empty(sample_size + 4095, dtype=torch.uint8, device=torch.device(f"npu:{args.devid}"))
    raw_address = raw_buffer.data_ptr()
    aligned_offset = (-raw_address) & 0xFFF
    buffer = raw_buffer[aligned_offset:aligned_offset + sample_size]
    address = buffer.data_ptr()
    log(
        args,
        "verify.alloc",
        f"sample_size={sample_size} raw_address=0x{raw_address:x} "
        f"aligned_offset={aligned_offset} aligned_address=0x{address:x} "
        f"alignment={address & 0xFFF}",
    )
    if address & 0xFFF:
        raise TestFailure(f"failed to create 4KiB-aligned verification buffer: address=0x{address:x}")
    for offset in offsets:
        log(
            args,
            "verify.submit",
            f"file_offset={offset} device_address=0x{address:x} length={sample_size}",
        )
        check_result(
            "read_file",
            file_p2p.read_file(fd, str(args.input.path), args.bdev, offset, address, sample_size, args.devid, args.vfid),
        )
        log(args, "verify.drain", f"file_offset={offset}")
        check_result("drain_read", file_p2p.drain_read(fd))
        torch.npu.synchronize()
        actual = buffer[:sample_size].cpu().numpy().tobytes()
        wanted = expected(offset, sample_size)
        if actual != wanted:
            first = next((index for index, (got, want) in enumerate(zip(actual, wanted)) if got != want), None)
            if first is None:
                first = min(len(actual), len(wanted))
            mismatch_count = sum(got != want for got, want in zip(actual, wanted))
            window_start = max(0, first - 8)
            window_end = min(sample_size, first + 8)
            diagnostic = (
                f"data verification failed: sample_offset={offset} sample_size={sample_size} "
                f"request_size={request_size} first_index={first} first_file_offset={offset + first} "
                f"device_address=0x{address + first:x} mismatch_count={mismatch_count} "
                f"window=[{window_start},{window_end}) "
                f"actual_window=0x{actual[window_start:window_end].hex()} "
                f"expected_window=0x{wanted[window_start:window_end].hex()} "
                f"actual_head=0x{actual[:32].hex()} expected_head=0x{wanted[:32].hex()} "
                f"actual_tail=0x{actual[-32:].hex()} expected_tail=0x{wanted[-32:].hex()}"
            )
            # Keep this visible even without --verbose: the old verify() error
            # only exposed the first differing byte and hid whether the whole
            # sample was stale, zero-filled, shifted, or partially transferred.
            print(f"DEBUG phase=verify.mismatch {diagnostic}", file=sys.stderr, flush=True)
            raise TestFailure(diagnostic)
        else:
            log(
                args,
                "verify.ok",
                f"file_offset={offset} length={sample_size} "
                f"head={actual[:16].hex()} tail={actual[-16:].hex()}",
            )
        verify(torch, buffer, offset, sample_size)
    return {"enabled": True, "status": "ok", "samples": len(offsets), "sample_size": sample_size}


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
    expected_head = expected(args.offset, sample_size)
    expected_tail = expected(tail_offset, sample_size)
    log(
        args,
        "input.sample",
        f"path={args.input.path} offset={args.offset} length={sample_size} "
        f"head_match={head == expected_head} tail_offset={tail_offset} tail_match={tail == expected_tail} "
        f"head=0x{head.hex()} expected_head=0x{expected_head.hex()} "
        f"tail=0x{tail.hex()} expected_tail=0x{expected_tail.hex()}",
    )


def _gib(value):
    return f"{value / 1024 ** 3:.2f}"


def _size_label(value):
    return f"{value / 1024 ** 2:g}M" if value >= 1024 ** 2 else f"{value / 1024:g}K"


def _heatmap_svg(rows, sizes, depths):
    cell_w, cell_h, left, top = 86, 48, 72, 32
    width = left + len(depths) * cell_w + 12
    height = top + len(sizes) * cell_h + 50
    maximum = max((row["bandwidth_bytes_per_sec"] for row in rows), default=1)
    lookup = {(row["size"], row["io_depth"]): row for row in rows}
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Throughput heatmap">']
    parts.append(f'<text x="{left}" y="16" fill="#aab7ce">I/O depth</text>')
    for x, depth in enumerate(depths):
        parts.append(f'<text x="{left + x * cell_w + cell_w / 2}" y="24" fill="#aab7ce" text-anchor="middle">{depth}</text>')
    for y, size in enumerate(sizes):
        py = top + y * cell_h
        parts.append(f'<text x="{left - 9}" y="{py + cell_h / 2 + 4}" fill="#aab7ce" text-anchor="end">{_size_label(size)}</text>')
        for x, depth in enumerate(depths):
            row = lookup.get((size, depth))
            if row is None:
                continue
            fraction = row["bandwidth_bytes_per_sec"] / maximum
            color = f"hsl({220 - fraction * 170:.1f}, 78%, {23 + fraction * 31:.1f}%)"
            px = left + x * cell_w
            label = f"size={_size_label(size)}, io-depth={depth}: {_gib(row['bandwidth_bytes_per_sec'])} GiB/s"
            parts.append(f'<rect x="{px}" y="{py}" width="{cell_w - 3}" height="{cell_h - 3}" rx="4" fill="{color}"><title>{html.escape(label)}</title></rect>')
            parts.append(f'<text x="{px + (cell_w - 3) / 2}" y="{py + 28}" fill="white" font-size="12" text-anchor="middle">{_gib(row["bandwidth_bytes_per_sec"])}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _line_chart_svg(rows, sizes, depths):
    width, height, left, right, top, bottom = 650, 390, 65, 16, 20, 48
    maximum = max((row["bandwidth_bytes_per_sec"] for row in rows), default=1) / 1024 ** 3 * 1.1
    x = lambda index: left + index * (width - left - right) / max(len(depths) - 1, 1)
    y = lambda value: top + (maximum - value) * (height - top - bottom) / maximum
    colors = ["#6ba8ff", "#71dc9c", "#ffba69", "#d595ff", "#51d6d6", "#ff7d9a", "#d8d870"]
    lookup = {(row["size"], row["io_depth"]): row for row in rows}
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Throughput line chart">']
    for tick in range(5):
        value = maximum * tick / 4
        py = y(value)
        parts.append(f'<line x1="{left}" x2="{width - right}" y1="{py:.1f}" y2="{py:.1f}" stroke="#2b3a53"/>')
        parts.append(f'<text x="{left - 8}" y="{py + 4:.1f}" fill="#aab7ce" text-anchor="end" font-size="12">{value:.1f}</text>')
    for index, depth in enumerate(depths):
        parts.append(f'<text x="{x(index):.1f}" y="{height - 22}" fill="#aab7ce" text-anchor="middle">{depth}</text>')
    parts.append(f'<text x="{width / 2}" y="{height - 5}" fill="#aab7ce" text-anchor="middle">I/O depth</text>')
    for series, size in enumerate(sizes):
        values = [lookup[(size, depth)]["bandwidth_bytes_per_sec"] / 1024 ** 3 for depth in depths if (size, depth) in lookup]
        points = " ".join(f"{x(index):.1f},{y(value):.1f}" for index, value in enumerate(values))
        color = colors[series % len(colors)]
        parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2.5"><title>{_size_label(size)}</title></polyline>')
        for index, value in enumerate(values):
            parts.append(f'<circle cx="{x(index):.1f}" cy="{y(value):.1f}" r="3" fill="{color}"><title>{_size_label(size)}, depth={depths[index]}: {value:.2f} GiB/s</title></circle>')
    for index, size in enumerate(sizes):
        lx, ly = left + (index % 3) * 110, top + (index // 3) * 18
        color = colors[index % len(colors)]
        parts.append(f'<rect x="{lx}" y="{ly}" width="10" height="10" fill="{color}"/><text x="{lx + 15}" y="{ly + 10}" fill="#dfe7f5" font-size="12">{_size_label(size)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _results_table(rows):
    body = []
    for row in rows:
        status = html.escape(str(row["verify"]["status"]))
        body.append(
            f'<tr><td>{_size_label(row["size"])}</td><td>{row["io_depth"]}</td>'
            f'<td>{row["bytes"]:,}</td><td>{row["elapsed_ns"] / 1e9:.3f} s</td>'
            f'<td>{_gib(row["bandwidth_bytes_per_sec"])} GiB/s</td>'
            f'<td class="{"ok" if status == "ok" else "skip"}">{status}</td></tr>'
        )
    return "".join(body)


def report_html(payload):
    """Render a self-contained HTML report without requiring browser JavaScript."""
    rows = payload["results"]
    sizes = sorted({row["size"] for row in rows})
    depths = sorted({row["io_depth"] for row in rows})
    best = payload["best"]
    meta = f"file size: {_gib(payload['file_size'])} GiB · {len(rows)} parameter combinations"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>XDS single-NPU stream benchmark</title>
<style>
  :root {{ color-scheme: dark; font-family: system-ui, sans-serif; background: #10151f; color: #e8edf7; }}
  body {{ max-width: 1280px; margin: 0 auto; padding: 28px; }}
  h1 {{ margin: 0 0 6px; }} .muted {{ color: #aab7ce; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 22px 0; }}
  .card, .panel {{ background: #182131; border: 1px solid #2b3a53; border-radius: 10px; padding: 16px; }}
  .card strong {{ display: block; font-size: 1.35rem; margin-top: 5px; }}
  .panels {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(500px, 1fr)); gap: 16px; }}
  .panel {{ overflow-x: auto; }} svg {{ min-width: 480px; max-width: 100%; height: auto; display: block; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
  th, td {{ border-bottom: 1px solid #2b3a53; padding: 9px; text-align: right; }} th:first-child, td:first-child {{ text-align: left; }}
  th {{ color: #aab7ce; }} .ok {{ color: #70dc9b; }} .skip {{ color: #ffcf70; }}
</style>
</head>
<body>
<h1>XDS single-NPU stream benchmark</h1>
<div class="muted">{html.escape(meta)}</div>
<section class="cards">
  <div class="card"><span class="muted">Best throughput</span><strong>{_gib(best['bandwidth_bytes_per_sec'])} GiB/s</strong></div>
  <div class="card"><span class="muted">Best request size</span><strong>{_size_label(best['size'])}</strong></div>
  <div class="card"><span class="muted">Best I/O depth</span><strong>{best['io_depth']}</strong></div>
  <div class="card"><span class="muted">Verification</span><strong>{html.escape(str(best['verify']['status']))}</strong></div>
</section>
<section class="panels">
  <div class="panel"><h2>Throughput heatmap</h2>{_heatmap_svg(rows, sizes, depths)}</div>
  <div class="panel"><h2>Throughput by I/O depth</h2>{_line_chart_svg(rows, sizes, depths)}</div>
</section>
<section class="panel" style="margin-top:16px"><h2>All results</h2><table><thead><tr><th>Size</th><th>I/O depth</th><th>Bytes</th><th>Elapsed</th><th>Throughput</th><th>Verify</th></tr></thead><tbody>{_results_table(rows)}</tbody></table></section>
</body>
</html>
"""


def run(args):
    args.log_started = time.monotonic()
    log(
        args,
        "run.start",
        f"bdev={args.bdev} data_dir={args.data_dir} file_size={args.file_size} "
        f"sizes={args.sizes} io_depths={args.io_depths} offset={args.offset} "
        f"devid={args.devid} vfid={args.vfid} verify={args.verify}",
    )
    torch, file_p2p = require_runtime(args)
    args.input = InputFile(args, args.offset + args.file_size)
    log_host_file_samples(args)
    fd = None
    try:
        fd = file_p2p.new_p2p_fd()
        check_result("new_p2p_fd", fd)
        log(args, "p2p.open", f"fd={fd}")
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
