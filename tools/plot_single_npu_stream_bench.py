#!/usr/bin/env python3
"""Render a single-NPU stream benchmark JSON result as self-contained HTML."""

import argparse
import html
import json
from pathlib import Path


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
        present_depths = [depth for depth in depths if (size, depth) in lookup]
        values = [lookup[(size, depth)]["bandwidth_bytes_per_sec"] / 1024 ** 3 for depth in present_depths]
        points = " ".join(f"{x(depths.index(depth)):.1f},{y(value):.1f}" for depth, value in zip(present_depths, values))
        color = colors[series % len(colors)]
        parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2.5"><title>{_size_label(size)}</title></polyline>')
        for depth, value in zip(present_depths, values):
            parts.append(f'<circle cx="{x(depths.index(depth)):.1f}" cy="{y(value):.1f}" r="3" fill="{color}"><title>{_size_label(size)}, depth={depth}: {value:.2f} GiB/s</title></circle>')
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
    if not rows:
        raise ValueError("benchmark JSON contains no results")
    sizes = sorted({row["size"] for row in rows})
    depths = sorted({row["io_depth"] for row in rows})
    best = payload.get("best") or max(rows, key=lambda row: row["bandwidth_bytes_per_sec"])
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


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("json", type=Path, help="benchmark JSON produced by the C++ or Python runner")
    root.add_argument("--output", type=Path, help="HTML output path (default: JSON path with .html suffix)")
    return root


def main():
    args = parser().parse_args()
    output = args.output or args.json.with_suffix(".html")
    try:
        payload = json.loads(args.json.read_text(encoding="utf-8"))
        output.write_text(report_html(payload), encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        parser().error(str(exc))
    print(f"REPORT html={output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
