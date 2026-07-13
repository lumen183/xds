#!/usr/bin/env bash
set -Eeuo pipefail

module_path="${1:-./build/kernel/p2p_dev/p2p_dev.ko}"
addr=$(awk '/__tracepoint_nvme_setup_cmd/ {print $1}' /proc/kallsyms)

[[ -n "${addr}" ]] || { echo "no nvme_setup_cmd tracepoint" >&2; exit 1; }
[[ -f "${module_path}" ]] || { echo "module not found: ${module_path}" >&2; exit 1; }

rmmod p2p_dev 2>/dev/null || true
insmod "${module_path}" "tp_nvme_setup_cmd_addr=0x${addr}"
chmod 666 /dev/p2p_device
echo "p2p_dev loaded"
