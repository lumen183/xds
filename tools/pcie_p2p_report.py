#!/usr/bin/env python3
"""Collect PCIe/NVMe/Ascend topology and render an offline XDS P2P report.

The collector only needs Python 3.8 and standard Linux utilities.  It reads
PCIe link state from sysfs, so the calculated limit reflects the negotiated
speed/width on the machine rather than a product-name assumption.
"""

import argparse
import datetime as dt
import html
import json
import math
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import sys
from pathlib import Path


GIB = 1024 ** 3
BDF_RE = re.compile(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$")


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except (OSError, PermissionError):
        return None


def run_command(argv, timeout=15):
    if not shutil.which(argv[0]):
        return {"command": argv, "status": "missing", "returncode": None, "output": ""}
    try:
        proc = subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", timeout=timeout, check=False,
        )
        return {
            "command": argv,
            "status": "ok" if proc.returncode == 0 else "failed",
            "returncode": proc.returncode,
            "output": proc.stdout.strip(),
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"command": argv, "status": "failed", "returncode": None, "output": str(exc)}


def parse_speed(value):
    """Return PCIe transfer rate in GT/s from a sysfs link-speed string."""
    if not value or value.lower() in {"unknown", "invalid"}:
        return None
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*GT/s", value, re.I)
    return float(match.group(1)) if match else None


def parse_width(value):
    if not value:
        return None
    match = re.search(r"(?:Width\s*)?x?([0-9]+)", value, re.I)
    return int(match.group(1)) if match else None


def pcie_gib_per_sec(speed_gt, width):
    """PCIe one-direction data-bit ceiling after line encoding, before TLP overhead."""
    if not speed_gt or not width:
        return None
    encoding_efficiency = 0.8 if speed_gt <= 5.0 else 128.0 / 130.0
    return speed_gt * 1e9 * width * encoding_efficiency / 8.0 / GIB


def pcie_generation(speed_gt):
    if speed_gt is None:
        return None
    generations = [(2.5, 1), (5.0, 2), (8.0, 3), (16.0, 4), (32.0, 5), (64.0, 6)]
    return min(generations, key=lambda item: abs(item[0] - speed_gt))[1]


def parse_size(value):
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?i?B?)?\s*", value, re.I)
    if not match:
        raise argparse.ArgumentTypeError("size must look like 512K, 1G, or 1073741824")
    number = float(match.group(1))
    suffix = (match.group(2) or "").upper().replace("IB", "").replace("B", "")
    powers = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4}
    if suffix not in powers:
        raise argparse.ArgumentTypeError("unsupported size suffix")
    return int(number * 1024 ** powers[suffix])


def parse_lspci_names(output):
    names = {}
    for line in output.splitlines():
        try:
            fields = shlex.split(line)
        except ValueError:
            continue
        if fields and BDF_RE.match(fields[0]):
            names[fields[0].lower()] = " · ".join(fields[1:4])
    return names


def parse_lspci_verbose(output):
    details, current = {}, None
    for line in output.splitlines():
        match = re.match(r"^([0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7])\s", line)
        if match:
            current = match.group(1).lower()
            details.setdefault(current, {})
            continue
        if current and "ACSCtl:" in line:
            value = line.split("ACSCtl:", 1)[1].strip()
            details[current]["acs_control"] = value
            direct = "DirectTrans+" in value
            details[current]["acs_redirect"] = not direct and (
                "ReqRedir+" in value or "CmpltRedir+" in value
            )
    return details


def pci_parent(path):
    resolved = path.resolve()
    found_self = False
    for part in reversed(resolved.parts):
        if not BDF_RE.match(part):
            continue
        if not found_self:
            found_self = True
            continue
        return part.lower()
    return None


def iommu_group(path):
    try:
        target = (path / "iommu_group").resolve(strict=True)
        return int(target.name)
    except (OSError, ValueError):
        return None


def collect_pci(sysfs_root=Path("/sys/bus/pci/devices")):
    lspci = run_command(["lspci", "-Dmmnn"])
    verbose = run_command(["lspci", "-Dvv"], timeout=30)
    names = parse_lspci_names(lspci["output"])
    verbose_details = parse_lspci_verbose(verbose["output"])
    devices = {}
    if not sysfs_root.exists():
        return devices, lspci
    for path in sorted(sysfs_root.iterdir()):
        bdf = path.name.lower()
        if not BDF_RE.match(bdf):
            continue
        speed_text = read_text(path / "current_link_speed")
        max_speed_text = read_text(path / "max_link_speed")
        width_text = read_text(path / "current_link_width")
        max_width_text = read_text(path / "max_link_width")
        speed, width = parse_speed(speed_text), parse_width(width_text)
        max_speed, max_width = parse_speed(max_speed_text), parse_width(max_width_text)
        class_code = (read_text(path / "class") or "").lower()
        vendor = (read_text(path / "vendor") or "").lower()
        devices[bdf] = {
            "bdf": bdf,
            "parent": pci_parent(path),
            "name": names.get(bdf, bdf),
            "vendor": vendor,
            "device": (read_text(path / "device") or "").lower(),
            "class": class_code,
            "numa_node": _int_or_none(read_text(path / "numa_node")),
            "iommu_group": iommu_group(path),
            "current_link_speed": speed_text,
            "current_link_width": width,
            "max_link_speed": max_speed_text,
            "max_link_width": max_width,
            "speed_gt": speed,
            "max_speed_gt": max_speed,
            "generation": pcie_generation(speed),
            "wire_gib_s": pcie_gib_per_sec(speed, width),
            "max_wire_gib_s": pcie_gib_per_sec(max_speed, max_width),
            "acs_control": verbose_details.get(bdf, {}).get("acs_control"),
            "acs_redirect": verbose_details.get(bdf, {}).get("acs_redirect"),
        }
    return devices, [lspci, verbose]


def _int_or_none(value):
    try:
        number = int(value)
        return number if number >= 0 else None
    except (TypeError, ValueError):
        return None


def sysfs_bdf(path):
    try:
        for part in reversed(path.resolve(strict=True).parts):
            if BDF_RE.match(part):
                return part.lower()
    except OSError:
        pass
    return None


def collect_nvme(pci_devices, selected=None):
    selected_names = None
    if selected:
        selected_names = set()
        for item in selected.split(","):
            name = Path(item).name
            match = re.match(r"^(nvme[0-9]+)n[0-9]+", name)
            selected_names.add(match.group(1) if match else name)
    controllers = []
    root = Path("/sys/class/nvme")
    if root.exists():
        for path in sorted(root.glob("nvme[0-9]*")):
            if selected_names and path.name not in selected_names:
                continue
            bdf = sysfs_bdf(path / "device")
            namespaces = sorted(item.name for item in Path("/sys/block").glob(path.name + "n*"))
            controllers.append({
                "controller": path.name,
                "bdf": bdf,
                "model": read_text(path / "model"),
                "firmware": read_text(path / "firmware_rev"),
                "serial": read_text(path / "serial"),
                "namespaces": namespaces,
                "pci": pci_devices.get(bdf),
            })
    return controllers


def collect_npus(pci_devices, explicit=None):
    explicit_bdfs = {item.strip().lower() for item in explicit.split(",")} if explicit else set()
    npus = []
    for bdf, dev in pci_devices.items():
        class_base = dev["class"][2:4] if dev["class"].startswith("0x") else ""
        auto = dev["vendor"] == "0x19e5" and class_base == "12"
        if (explicit_bdfs and bdf in explicit_bdfs) or (not explicit_bdfs and auto):
            npus.append({"index": len(npus), "bdf": bdf, "pci": dev})
    return npus


def ancestor_chain(bdf, devices):
    chain, seen = [], set()
    while bdf and bdf in devices and bdf not in seen:
        seen.add(bdf)
        chain.append(bdf)
        bdf = devices[bdf].get("parent")
    return chain


def analyze_path(source_bdf, target_bdf, devices, efficiency=0.90):
    source_chain = ancestor_chain(source_bdf, devices)
    target_chain = ancestor_chain(target_bdf, devices)
    target_set = set(target_chain)
    lca = next((item for item in source_chain if item in target_set), None)
    if lca:
        path = source_chain[:source_chain.index(lca)] + [lca]
        target_leg = target_chain[:target_chain.index(lca)]
        path += list(reversed(target_leg))
        link_nodes = source_chain[:source_chain.index(lca)] + target_leg
        relation = "same-tree"
    else:
        path = source_chain + list(reversed(target_chain))
        link_nodes = source_chain + target_chain
        relation = "cross-root-or-unknown"
    known = [(bdf, devices[bdf].get("wire_gib_s")) for bdf in link_nodes]
    known = [(bdf, value) for bdf, value in known if value is not None]
    bottleneck = min(known, key=lambda item: item[1]) if known else (None, None)
    return {
        "source": source_bdf,
        "target": target_bdf,
        "relation": relation,
        "lca": lca,
        "nodes": path,
        "link_nodes": link_nodes,
        "wire_gib_s": bottleneck[1],
        "engineering_gib_s": bottleneck[1] * efficiency if bottleneck[1] is not None else None,
        "bottleneck_bdf": bottleneck[0],
        "all_links_known": len(known) == len(link_nodes) and bool(link_nodes),
    }


def collect_raw_commands():
    commands = [
        ["lspci", "-Dtv"],
        ["npu-smi", "info"],
        ["npu-smi", "info", "-m"],
        ["nvme", "list"],
        ["lsblk", "-e7", "-o", "NAME,TYPE,SIZE,MODEL,TRAN,PKNAME,MOUNTPOINTS"],
        ["lscpu"],
        ["numactl", "--hardware"],
        ["uname", "-a"],
    ]
    return [run_command(command, timeout=20) for command in commands]


def relevant_nodes(devices, endpoints):
    result = set()
    for bdf in endpoints:
        result.update(ancestor_chain(bdf, devices))
    return result


def layout_tree(devices, included):
    children = {bdf: [] for bdf in included}
    roots = []
    for bdf in included:
        parent = devices[bdf].get("parent")
        if parent in included:
            children[parent].append(bdf)
        else:
            roots.append(bdf)
    for values in children.values():
        values.sort()
    roots.sort()
    positions = {}
    next_x = [0]

    def visit(node, depth):
        kids = children[node]
        if kids:
            for child in kids:
                visit(child, depth + 1)
            x = sum(positions[child][0] for child in kids) / len(kids)
        else:
            x = next_x[0]
            next_x[0] += 1
        positions[node] = (x, depth)

    for root in roots:
        visit(root, 0)
        next_x[0] += 0.6
    return positions


def topology_svg(payload):
    devices = payload["pci_devices"]
    npu_bdfs = {item["bdf"] for item in payload["npus"]}
    nvme_bdfs = {item["bdf"] for item in payload["nvme"] if item.get("bdf")}
    included = relevant_nodes(devices, npu_bdfs | nvme_bdfs)
    if not included:
        return '<svg viewBox="0 0 800 120"><text x="20" y="55" fill="#aab7ce">未发现 NPU/NVMe PCIe 端点；请查看诊断信息。</text></svg>'
    positions = layout_tree(devices, included)
    max_x = max((x for x, _ in positions.values()), default=0)
    max_y = max((y for _, y in positions.values()), default=0)
    cell_w, cell_h, margin = 205, 112, 30
    width = max(850, int((max_x + 1) * cell_w + margin * 2))
    height = max(180, int((max_y + 1) * cell_h + margin * 2))

    def xy(bdf):
        x, y = positions[bdf]
        return margin + x * cell_w, margin + y * cell_h

    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="PCIe topology">']
    for bdf in sorted(included):
        parent = devices[bdf].get("parent")
        if parent not in included:
            continue
        x1, y1 = xy(parent)
        x2, y2 = xy(bdf)
        parts.append(f'<path d="M{x1+82:.1f},{y1+64:.1f} V{(y1+y2)/2+32:.1f} H{x2+82:.1f} V{y2:.1f}" fill="none" stroke="#52647f" stroke-width="2"/>')
    for bdf in sorted(included):
        dev = devices[bdf]
        x, y = xy(bdf)
        if bdf in npu_bdfs:
            color, kind = "#8b5cf6", "NPU"
        elif bdf in nvme_bdfs:
            color, kind = "#10b981", "NVMe"
        else:
            color, kind = "#334155", "Bridge/Root"
        link = link_label(dev)
        label = short_name(dev.get("name") or bdf, 24)
        parts.append(f'<g><rect x="{x:.1f}" y="{y:.1f}" width="164" height="68" rx="8" fill="{color}" stroke="#8291a8"/>')
        parts.append(f'<text x="{x+8:.1f}" y="{y+17:.1f}" fill="white" font-size="12" font-weight="700">{html.escape(kind)} · {bdf}</text>')
        parts.append(f'<text x="{x+8:.1f}" y="{y+37:.1f}" fill="#f2f5fa" font-size="11">{html.escape(label)}</text>')
        parts.append(f'<text x="{x+8:.1f}" y="{y+56:.1f}" fill="#d3dbea" font-size="11">{html.escape(link)}</text></g>')
    parts.append("</svg>")
    return "".join(parts)


def short_name(value, limit):
    return value if len(value) <= limit else value[:limit - 1] + "…"


def link_label(dev):
    if dev.get("speed_gt") and dev.get("current_link_width"):
        bw = dev.get("wire_gib_s")
        return f"Gen{dev.get('generation')} x{dev['current_link_width']} · {bw:.2f} GiB/s"
    return "link unknown"


def fmt_bw(value):
    return "未知" if value is None else f"{value:.2f} GiB/s"


def status_for(measured, ceiling):
    if measured is None or ceiling is None:
        return "unknown", "数据不足"
    ratio = measured / ceiling if ceiling else math.inf
    if ratio > 1.02:
        return "bad", f"超过上限 {ratio:.2f}×"
    if ratio > 0.90:
        return "warn", f"达到上限 {ratio:.0%}，需严格复核"
    return "ok", f"达到上限 {ratio:.0%}"


def effective_ceiling(path, args):
    limits = [("PCIe 工程上限", path.get("engineering_gib_s"))]
    if args.nvme_read_gib is not None:
        limits.append(("NVMe 介质读取", args.nvme_read_gib))
    if args.hbm_write_gib is not None:
        limits.append(("NPU HBM 写入", args.hbm_write_gib))
    known = [(name, value) for name, value in limits if value is not None]
    return min(known, key=lambda item: item[1]) if known else (None, None)


def render_html(payload):
    measured = payload["benchmark"].get("measured_gib_s")
    paths = payload["paths"]
    npu_endpoint_sum = sum(
        item.get("pci", {}).get("wire_gib_s") or 0 for item in payload["npus"]
    )
    nvme_endpoint_sum = sum(
        item.get("pci", {}).get("wire_gib_s") or 0 for item in payload["nvme"]
    )
    best_wire = max((item.get("wire_gib_s") for item in paths if item.get("wire_gib_s") is not None), default=None)
    best_known = max((item.get("effective_gib_s") for item in paths if item.get("effective_gib_s") is not None), default=None)
    if measured is None:
        overall_class, overall_text = "ok", "仅理论模式"
    else:
        overall_class, overall_text = status_for(measured, best_known)
    rows = []
    for item in paths:
        klass, verdict = status_for(measured, item.get("effective_gib_s"))
        rows.append(
            f'<tr><td>{html.escape(item["nvme_controller"])}</td><td>{item["npu_index"]}</td>'
            f'<td>{item["source"]} → {item["target"]}</td><td>{html.escape(item["relation"])}</td>'
            f'<td>{fmt_bw(item.get("wire_gib_s"))}</td><td>{fmt_bw(item.get("engineering_gib_s"))}</td>'
            f'<td>{fmt_bw(item.get("effective_gib_s"))}</td><td class="{klass}">{html.escape(verdict)}</td></tr>'
        )
    if not rows:
        rows.append('<tr><td colspan="8" class="unknown">没有可分析的 NVMe→NPU 路径</td></tr>')
    benchmark = payload["benchmark"]
    batch_bytes = benchmark.get("batch_bytes")
    transfer_bytes = benchmark.get("transfer_bytes")
    drains = math.ceil(transfer_bytes / batch_bytes) if batch_bytes and transfer_bytes else None
    warnings = list(payload["warnings"])
    if benchmark.get("request_size") and benchmark.get("io_depth"):
        warnings.append(
            f"该参数每次 drain 前最多积累 {batch_bytes / GIB:.2f} GiB；"
            f"完成测试约需 {drains} 次 drain。这里的软件 io-depth 不是 NVMe 原生队列深度。"
        )
    warning_html = "".join(f"<li>{html.escape(item)}</li>" for item in warnings)
    raw = html.escape(json.dumps(payload["raw_commands"], ensure_ascii=False, indent=2))
    measured_text = "未提供" if measured is None else f"{measured:.2f} GiB/s"
    best_text = fmt_bw(best_known)
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>XDS PCIe→HBM 拓扑分析</title><style>
:root{{color-scheme:dark;font-family:system-ui,-apple-system,sans-serif;background:#0b1020;color:#e6edf7}}
body{{max-width:1500px;margin:auto;padding:24px}} h1,h2{{margin:.2em 0 .6em}} .muted{{color:#9dafc8}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:20px 0}}
.card,.panel{{background:#131d30;border:1px solid #2b3b56;border-radius:10px;padding:16px}}
.card strong{{display:block;font-size:1.35rem;margin-top:6px}} .panel{{margin:14px 0;overflow:auto}}
svg{{min-width:800px;width:100%;height:auto}} table{{width:100%;border-collapse:collapse;white-space:nowrap}}
th,td{{padding:9px;border-bottom:1px solid #2b3b56;text-align:right}} th:first-child,td:first-child{{text-align:left}}
.ok{{color:#62d894}}.warn{{color:#ffd166}}.bad{{color:#ff6b78;font-weight:700}}.unknown{{color:#9dafc8}}
code,pre{{font-family:ui-monospace,monospace}} pre{{font-size:12px;white-space:pre-wrap}} li{{margin:.5em 0}}
</style></head><body>
<h1>XDS PCIe→HBM 拓扑分析</h1><div class="muted">{html.escape(payload["host"]["hostname"])} · {html.escape(payload["host"]["collected_at"])} · 所有带宽均为单向</div>
<section class="cards">
<div class="card"><span class="muted">Ascend 端点</span><strong>{len(payload["npus"])}</strong></div>
<div class="card"><span class="muted">NVMe 控制器</span><strong>{len(payload["nvme"])}</strong></div>
<div class="card"><span class="muted">NPU 端点链路求和</span><strong>{npu_endpoint_sum:.2f} GiB/s</strong></div>
<div class="card"><span class="muted">NVMe 端点链路求和</span><strong>{nvme_endpoint_sum:.2f} GiB/s</strong></div>
<div class="card"><span class="muted">最佳路径 PCIe 理论最大值</span><strong>{fmt_bw(best_wire)}</strong></div>
<div class="card"><span class="muted">实测</span><strong>{measured_text}</strong></div>
<div class="card"><span class="muted">最佳路径工程参考值</span><strong>{best_text}</strong></div>
<div class="card"><span class="muted">总体判定</span><strong class="{overall_class}">{html.escape(overall_text)}</strong></div>
</section>
<section class="panel"><h2>PCIe 拓扑</h2><p class="muted">节点文字来自该设备的 PCIe Link Status；桥端口方向以端口类型为准。路径上最慢的相关链路决定 PCIe 上限。</p>{topology_svg(payload)}</section>
<section class="panel"><h2>NVMe → NPU 路径矩阵</h2><table><thead><tr><th>NVMe</th><th>NPU</th><th>BDF 路径</th><th>关系</th><th>PCIe 硬上限</th><th>工程上限</th><th>端到端已知上限</th><th>对比实测</th></tr></thead><tbody>{''.join(rows)}</tbody></table></section>
<section class="panel"><h2>快速判断</h2><ul>{warning_html or '<li>未发现额外警告。</li>'}</ul></section>
<section class="panel"><h2>计算口径</h2><p>PCIe 1/2 代使用 8b/10b；3 代及以后使用 128b/130b。硬上限尚未扣除 TLP/DLLP、流控、地址路由和实现开销；工程上限 = 硬上限 × {payload['assumptions']['pcie_efficiency']:.0%}。端到端上限取 PCIe 工程上限、手工提供的 NVMe 顺序读上限和 HBM 写入上限中的最小值。端点链路求和只是多设备完全独立时的宽松上界，未扣除共享 switch/upstream/root 带宽。跨 Root Complex 路径仅展示已知链路，不能据此证明 P2P 可达。</p></section>
<details class="panel"><summary>原始诊断命令输出</summary><pre>{raw}</pre></details>
</body></html>'''


def build_payload(args):
    devices, lspci_diagnostics = collect_pci()
    nvmes = collect_nvme(devices, args.nvme)
    npus = collect_npus(devices, args.npu_bdf)
    raw_commands = collect_raw_commands()
    raw_commands[0:0] = lspci_diagnostics
    paths = []
    for nvme in nvmes:
        if not nvme.get("bdf"):
            continue
        for npu in npus:
            item = analyze_path(nvme["bdf"], npu["bdf"], devices, args.pcie_efficiency)
            item["nvme_controller"] = nvme["controller"]
            item["npu_index"] = npu["index"]
            name, ceiling = effective_ceiling(item, args)
            item["effective_limit"] = name
            item["effective_gib_s"] = ceiling
            paths.append(item)
    warnings = []
    if len(npus) != 8:
        warnings.append(f"自动识别到 {len(npus)} 个 Ascend PCIe 加速器端点，不是预期的 8 个；必要时使用 --npu-bdf。")
    if not nvmes:
        warnings.append("没有识别到 NVMe 控制器；检查 sysfs，或用 --nvme 指定控制器名。")
    if any(item["relation"] != "same-tree" for item in paths):
        warnings.append("存在跨 PCI root/未知公共上游的路径；此类路径可能绕经 CPU/互联，不能仅按端点链路计算 P2P 上限。")
    if any(not item["all_links_known"] for item in paths):
        warnings.append("部分 bridge 不公开链路状态，路径上限只是已知链路的上界，不是完整证明。")
    redirected = sorted({
        bdf for item in paths for bdf in item["nodes"]
        if devices.get(bdf, {}).get("acs_redirect")
    })
    if redirected:
        warnings.append(
            "以下路径节点启用了 ACS Request/Completion Redirect，P2P 流量可能被重定向到 Root Complex："
            + ", ".join(redirected)
        )
    if args.hbm_write_gib is None:
        warnings.append("未提供本机 NPU HBM 持续写带宽；报告只把 PCIe/NVMe 当作已知约束。可用 --hbm-write-gib 补充可靠数据。")
    request_size = args.request_size
    transfer_size = args.transfer_size
    return {
        "schema_version": 1,
        "host": {
            "hostname": socket.gethostname(),
            "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "kernel": platform.release(),
            "architecture": platform.machine(),
        },
        "assumptions": {
            "pcie_efficiency": args.pcie_efficiency,
            "nvme_read_gib_s": args.nvme_read_gib,
            "hbm_write_gib_s": args.hbm_write_gib,
        },
        "benchmark": {
            "measured_gib_s": args.measured_gib,
            "request_size": request_size,
            "io_depth": args.io_depth,
            "transfer_bytes": transfer_size,
            "batch_bytes": request_size * args.io_depth if request_size and args.io_depth else None,
        },
        "pci_devices": devices,
        "nvme": nvmes,
        "npus": npus,
        "paths": paths,
        "warnings": warnings,
        "raw_commands": raw_commands,
    }


def parser():
    root = argparse.ArgumentParser(
        description=__doc__,
        epilog="最简用法（只看理论值）：python3 tools/pcie_p2p_report.py",
    )
    root.add_argument("--output", default="xds-pcie-topology", help="output prefix (default: xds-pcie-topology)")
    root.add_argument("--nvme", help="comma-separated controllers, for example nvme0,nvme1")
    root.add_argument("--npu-bdf", help="override Ascend auto-detection with comma-separated PCI BDFs")
    root.add_argument("--measured-gib", type=float, help="measured XDS bandwidth in GiB/s")
    root.add_argument("--request-size", type=parse_size, help="XDS logical request size, for example 512K")
    root.add_argument("--io-depth", type=int, help="XDS submissions before drain, for example 2048")
    root.add_argument("--transfer-size", type=parse_size, help="bytes timed by the benchmark, for example 1G")
    root.add_argument("--pcie-efficiency", type=float, default=0.90, help="engineering/TLP efficiency factor (default: 0.90)")
    root.add_argument("--nvme-read-gib", type=float, help="verified sustained NVMe/media read ceiling in GiB/s")
    root.add_argument("--hbm-write-gib", type=float, help="verified sustained NPU HBM write ceiling in GiB/s")
    return root


def main():
    args = parser().parse_args()
    if args.io_depth is not None and args.io_depth <= 0:
        raise SystemExit("--io-depth must be positive")
    if not 0 < args.pcie_efficiency <= 1:
        raise SystemExit("--pcie-efficiency must be in (0, 1]")
    for name in ("measured_gib", "nvme_read_gib", "hbm_write_gib"):
        value = getattr(args, name)
        if value is not None and value <= 0:
            raise SystemExit(f"--{name.replace('_', '-')} must be positive")
    payload = build_payload(args)
    prefix = Path(args.output)
    json_path = prefix.with_suffix(".json")
    html_path = prefix.with_suffix(".html")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    html_path.write_text(render_html(payload), encoding="utf-8")
    print(f"REPORT json={json_path} html={html_path} npus={len(payload['npus'])} nvme={len(payload['nvme'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
