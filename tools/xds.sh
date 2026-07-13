#!/usr/bin/env bash
# Real-hardware test entry point.  This script deliberately never falls back to
# the mock backend: a successful smoke/bench result must mean real P2P I/O ran.
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
KDIR="${KDIR:-/lib/modules/$(uname -r)/build}"
MODULE="${ROOT_DIR}/build/kernel/p2p_dev/p2p_dev.ko"
MODULE_SIGN_KEY="${MODULE_SIGN_KEY:-}"
MODULE_SIGN_CERT="${MODULE_SIGN_CERT:-}"
MODULE_SIGN_HASH="${MODULE_SIGN_HASH:-sha256}"

die() { echo "xds.sh: $*" >&2; exit 1; }

sign_module() {
    # A signature is useful only when its certificate is trusted by the host
    # kernel. Therefore signing is opt-in and the key is supplied by the user.
    [[ -n "$MODULE_SIGN_KEY" && -n "$MODULE_SIGN_CERT" ]] || return 0
    [[ -f "$MODULE_SIGN_KEY" ]] || die "module signing key is unavailable: $MODULE_SIGN_KEY"
    [[ -f "$MODULE_SIGN_CERT" ]] || die "module signing certificate is unavailable: $MODULE_SIGN_CERT"

    if command -v modinfo >/dev/null 2>&1 && [[ -n "$(modinfo -F signer "$MODULE" 2>/dev/null)" ]]; then
        echo "Kernel module is already signed: $MODULE"
        return 0
    fi

    local sign_file="${KDIR}/scripts/sign-file"
    [[ -x "$sign_file" ]] || die "kernel sign-file is unavailable: $sign_file"
    "$sign_file" "$MODULE_SIGN_HASH" "$MODULE_SIGN_KEY" "$MODULE_SIGN_CERT" "$MODULE" \
        || die "failed to sign kernel module: $MODULE"
    echo "Signed kernel module: $MODULE"
}

usage() {
    cat <<'EOF'
Usage: ./tools/xds.sh setup|smoke|bench|cleanup [options]

Commands:
  setup                  Check the Ascend environment, build and load p2p_dev.
  smoke [options]        Run one real P2P read and verify all bytes.
  bench [options]        Benchmark real P2P reads; verification is enabled by default.
  cleanup                Remove p2p_dev without forcing an unload.

Run "./tools/xds.sh smoke --help" or "./tools/xds.sh bench --help" for
test parameters. Environment overrides: CANN_ENV, KDIR,
ASCEND_MODULE_SYMVERS, PYTHON, BUILD_JOBS, MODULE_SIGN_KEY,
MODULE_SIGN_CERT, MODULE_SIGN_HASH.
EOF
}

source_cann() {
    local env_file="${CANN_ENV:-}"
    if [[ -z "$env_file" ]]; then
        for env_file in /usr/local/Ascend/ascend-toolkit/set_env.sh /usr/local/Ascend/ascend-toolkit/latest/set_env.sh; do
            [[ -f "$env_file" ]] && break
        done
    fi
    [[ -n "$env_file" && -f "$env_file" ]] || die "CANN environment is unavailable; set CANN_ENV=/path/to/set_env.sh"
    # shellcheck disable=SC1090
    source "$env_file"
}

setup() {
    [[ "$(uname -s)" == Linux ]] || die "Linux is required"
    command -v bash >/dev/null || die "bash is required"
    command -v cmake >/dev/null || die "cmake is required"
    "$PYTHON" - <<'PY'
import sys
if sys.version_info < (3, 8):
    raise SystemExit("Python 3.8 or newer is required")
PY
    source_cann
    [[ -f "$KDIR/Makefile" ]] || die "Kernel build directory is unavailable: $KDIR"
    local trace_addr
    trace_addr="$(awk '/__tracepoint_nvme_setup_cmd/ {print $1; exit}' /proc/kallsyms)"
    [[ -n "$trace_addr" && "$trace_addr" != 0000000000000000 ]] || die "nvme_setup_cmd tracepoint is unavailable"
    "$PYTHON" - <<'PY'
import torch
try:
    import torch_npu  # noqa: F401
except ImportError as exc:
    raise SystemExit("torch_npu cannot be imported: %s" % exc)
if not hasattr(torch, "npu") or torch.npu.device_count() < 1:
    raise SystemExit("no usable Ascend NPU device found")
PY
    local symbol
    for symbol in devmm_get_mem_pa_list devmm_put_mem_pa_list devmm_get_mem_page_size; do
        grep -qw "$symbol" /proc/kallsyms || die "Ascend driver symbol is unavailable: $symbol"
    done
    (cd "$ROOT_DIR" && ./build.sh -X on -P -i on)
    [[ -f "$MODULE" ]] || die "module was not built: $MODULE"
    sign_module
    if ! lsmod | awk '$1 == "p2p_dev" { found=1 } END { exit !found }'; then
        sudo insmod "$MODULE" "tp_nvme_setup_cmd_addr=0x${trace_addr}"
    fi
    "$PYTHON" - <<'PY'
import os
try:
    fd = os.open("/dev/p2p_device", os.O_RDWR)
except OSError as exc:
    raise SystemExit("cannot open /dev/p2p_device: %s" % exc)
else:
    os.close(fd)
PY
    echo "PASS setup module=p2p_dev device=/dev/p2p_device"
}

cleanup() {
    if ! lsmod | awk '$1 == "p2p_dev" { found=1 } END { exit !found }'; then
        echo "PASS cleanup module=not-loaded"
        return
    fi
    if command -v fuser >/dev/null && fuser -s /dev/p2p_device; then
        die "/dev/p2p_device is still in use; close the listed test processes before cleanup"
    fi
    sudo rmmod p2p_dev || die "p2p_dev could not be unloaded (it may still be in use); refusing to force unload"
    echo "PASS cleanup module=p2p_dev"
}

main() {
    (($# >= 1)) || { usage; exit 2; }
    local command="$1"; shift
    case "$command" in
        setup) (($# == 0)) || die "setup takes no arguments"; setup ;;
        smoke|bench)
            if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
                exec "$PYTHON" "${ROOT_DIR}/tools/xds_test.py" "$command" "$@"
            fi
            source_cann
            export PYTHONPATH="${ROOT_DIR}/build/python${PYTHONPATH:+:$PYTHONPATH}"
            exec "$PYTHON" "${ROOT_DIR}/tools/xds_test.py" "$command" "$@"
            ;;
        cleanup) (($# == 0)) || die "cleanup takes no arguments"; cleanup ;;
        -h|--help|help) usage ;;
        *) usage >&2; die "unknown command: $command" ;;
    esac
}
main "$@"
