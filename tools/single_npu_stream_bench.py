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


def report_html(payload):
    """Render a self-contained, offline HTML report for a completed scan."""
    data = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
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
  .panel {{ overflow-x: auto; }} svg {{ min-width: 480px; display: block; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
  th, td {{ border-bottom: 1px solid #2b3a53; padding: 9px; text-align: right; }} th:first-child, td:first-child {{ text-align: left; }}
  th {{ color: #aab7ce; }} .ok {{ color: #70dc9b; }} .skip {{ color: #ffcf70; }}
</style>
</head>
<body>
<h1>XDS single-NPU stream benchmark</h1>
<div id="meta" class="muted"></div>
<section id="summary" class="cards"></section>
<section class="panels">
  <div class="panel"><h2>Throughput heatmap</h2><div id="heatmap"></div></div>
  <div class="panel"><h2>Throughput by I/O depth</h2><div id="lines"></div></div>
</section>
<section class="panel" style="margin-top:16px"><h2>All results</h2><div id="table"></div></section>
<script>
const report = {data};
const rows = report.results;
const gib = value => (value / 1024 ** 3).toFixed(2);
const sizeLabel = value => value >= 1024 ** 2 ? (value / 1024 ** 2) + 'M' : (value / 1024) + 'K';
const escapeHtml = value => String(value).replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
const sizes = [...new Set(rows.map(row => row.size))].sort((a, b) => a - b);
const depths = [...new Set(rows.map(row => row.io_depth))].sort((a, b) => a - b);
const lookup = new Map(rows.map(row => [row.size + ':' + row.io_depth, row]));
const best = report.best;

document.querySelector('#meta').textContent = `file size: ${{gib(report.file_size)}} GiB · ${{rows.length}} parameter combinations`;
document.querySelector('#summary').innerHTML = [
  ['Best throughput', `${{gib(best.bandwidth_bytes_per_sec)}} GiB/s`],
  ['Best request size', sizeLabel(best.size)],
  ['Best I/O depth', best.io_depth],
  ['Verification', best.verify.status],
].map(([name, value]) => `<div class="card"><span class="muted">${{name}}</span><strong>${{value}}</strong></div>`).join('');

function heatmap() {{
  const cellW = 86, cellH = 48, left = 72, top = 32;
  const width = left + depths.length * cellW + 12, height = top + sizes.length * cellH + 50;
  const max = Math.max(...rows.map(row => row.bandwidth_bytes_per_sec));
  let svg = `<svg viewBox="0 0 ${{width}} ${{height}}" role="img" aria-label="Throughput heatmap">`;
  svg += `<text x="${{left}}" y="16" fill="#aab7ce">I/O depth</text>`;
  depths.forEach((depth, x) => {{ svg += `<text x="${{left + x * cellW + cellW / 2}}" y="${{top - 8}}" fill="#aab7ce" text-anchor="middle">${{depth}}</text>`; }});
  sizes.forEach((size, y) => {{
    svg += `<text x="${{left - 9}}" y="${{top + y * cellH + cellH / 2 + 4}}" fill="#aab7ce" text-anchor="end">${{sizeLabel(size)}}</text>`;
    depths.forEach((depth, x) => {{
      const row = lookup.get(size + ':' + depth);
      const fraction = row.bandwidth_bytes_per_sec / max;
      const color = `hsl(${{220 - fraction * 170}}, 78%, ${{23 + fraction * 31}}%)`;
      const px = left + x * cellW, py = top + y * cellH;
      svg += `<rect x="${{px}}" y="${{py}}" width="${{cellW - 3}}" height="${{cellH - 3}}" rx="4" fill="${{color}}"><title>size=${{sizeLabel(size)}}, io-depth=${{depth}}: ${{gib(row.bandwidth_bytes_per_sec)}} GiB/s</title></rect>`;
      svg += `<text x="${{px + (cellW - 3) / 2}}" y="${{py + 28}}" fill="white" font-size="12" text-anchor="middle">${{gib(row.bandwidth_bytes_per_sec)}}</text>`;
    }});
  }});
  return svg + '</svg>';
}}

function lineChart() {{
  const width = 650, height = 390, left = 65, right = 16, top = 20, bottom = 48;
  const max = Math.max(...rows.map(row => row.bandwidth_bytes_per_sec)) / 1024 ** 3 * 1.1;
  const x = index => left + index * (width - left - right) / Math.max(depths.length - 1, 1);
  const y = value => top + (max - value) * (height - top - bottom) / max;
  const colors = ['#6ba8ff','#71dc9c','#ffba69','#d595ff','#51d6d6','#ff7d9a','#d8d870'];
  let svg = `<svg viewBox="0 0 ${{width}} ${{height}}" role="img" aria-label="Throughput line chart">`;
  for (let tick = 0; tick <= 4; tick++) {{
    const value = max * tick / 4, py = y(value);
    svg += `<line x1="${{left}}" x2="${{width-right}}" y1="${{py}}" y2="${{py}}" stroke="#2b3a53"/>`;
    svg += `<text x="${{left-8}}" y="${{py+4}}" fill="#aab7ce" text-anchor="end" font-size="12">${{value.toFixed(1)}}</text>`;
  }}
  depths.forEach((depth, index) => {{ svg += `<text x="${{x(index)}}" y="${{height-22}}" fill="#aab7ce" text-anchor="middle">${{depth}}</text>`; }});
  svg += `<text x="${{width/2}}" y="${{height-5}}" fill="#aab7ce" text-anchor="middle">I/O depth</text>`;
  sizes.forEach((size, series) => {{
    const values = depths.map(depth => lookup.get(size + ':' + depth).bandwidth_bytes_per_sec / 1024 ** 3);
    const points = values.map((value, index) => `${{x(index)}},${{y(value)}}`).join(' ');
    svg += `<polyline points="${{points}}" fill="none" stroke="${{colors[series % colors.length]}}" stroke-width="2.5"><title>${{sizeLabel(size)}}</title></polyline>`;
    values.forEach((value, index) => {{ svg += `<circle cx="${{x(index)}}" cy="${{y(value)}}" r="3" fill="${{colors[series % colors.length]}}"><title>${{sizeLabel(size)}}, depth=${{depths[index]}}: ${{value.toFixed(2)}} GiB/s</title></circle>`; }});
  }});
  sizes.forEach((size, index) => {{
    const lx = left + (index % 3) * 110, ly = top + Math.floor(index / 3) * 18;
    svg += `<rect x="${{lx}}" y="${{ly}}" width="10" height="10" fill="${{colors[index % colors.length]}}"/><text x="${{lx+15}}" y="${{ly+10}}" fill="#dfe7f5" font-size="12">${{sizeLabel(size)}}</text>`;
  }});
  return svg + '</svg>';
}}

document.querySelector('#heatmap').innerHTML = heatmap();
document.querySelector('#lines').innerHTML = lineChart();
document.querySelector('#table').innerHTML = `<table><thead><tr><th>Size</th><th>I/O depth</th><th>Bytes</th><th>Elapsed</th><th>Throughput</th><th>Verify</th></tr></thead><tbody>${{rows.map(row => `<tr><td>${{sizeLabel(row.size)}}</td><td>${{row.io_depth}}</td><td>${{row.bytes.toLocaleString()}}</td><td>${{(row.elapsed_ns / 1e9).toFixed(3)}} s</td><td>${{gib(row.bandwidth_bytes_per_sec)}} GiB/s</td><td class="${{row.verify.status === 'ok' ? 'ok' : 'skip'}}">${{escapeHtml(row.verify.status)}}</td></tr>`).join('')}}</tbody></table>`;
</script>
</body>
</html>
"""


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
