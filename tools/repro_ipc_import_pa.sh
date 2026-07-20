#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT="${ROOT_DIR}/build/repro_ipc_import_pa"

usage() {
    cat <<EOF
Usage: sudo -E $0 INPUT BDEV [OFFSET [SIZE [DEVICE [VFID]]]]

Examples:
  sudo -E $0 /mnt/nvme/xds-smoke.bin /dev/nvme0n1 0 4096 0 0
  sudo -E $0 /dev/nvme0n1 /dev/nvme0n1 0 4096 0 0

INPUT may be a regular file on BDEV, or BDEV itself for a raw read-only smoke.
The program performs two reads into the same exported HBM allocation:
  1. allocator PID + allocator VA
  2. importer PID  + IPC-import VA
EOF
}

if (($# < 2 || $# > 6)); then
    usage >&2
    exit 2
fi

if [[ -f "$1" ]] && command -v findmnt >/dev/null 2>&1; then
    input_source="$(findmnt -n -o SOURCE -T "$1" 2>/dev/null || true)"
    if [[ -n "${input_source}" && "${input_source}" != "$2" ]]; then
        echo "WARNING: INPUT is on '${input_source}', but BDEV is '$2'." >&2
        echo "         FIEMAP offsets are only valid for the matching backing block device." >&2
        echo "         For an unambiguous smoke, pass BDEV as both INPUT and BDEV." >&2
    fi
fi

find_ascend_include() {
    local candidate
    for candidate in \
        "${ASCEND_HOME_PATH:-}/include" \
        "${ASCEND_HOME_PATH:-}/runtime/include" \
        /usr/local/Ascend/ascend-toolkit/latest/include \
        /usr/local/Ascend/ascend-toolkit/latest/runtime/include \
        /usr/local/Ascend/latest/include \
        /usr/local/Ascend/latest/runtime/include; do
        if [[ "${candidate}" != /include && "${candidate}" != /runtime/include &&
              -f "${candidate}/acl/acl.h" ]]; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    done
    return 1
}

ASCEND_INCLUDE="$(find_ascend_include)" || {
    echo "Cannot find acl/acl.h. Source CANN set_env.sh or set ASCEND_HOME_PATH." >&2
    exit 2
}

ASCEND_LIB=""
for candidate in \
    "${ASCEND_HOME_PATH:-}/lib64" \
    "${ASCEND_HOME_PATH:-}/runtime/lib64" \
    /usr/local/Ascend/ascend-toolkit/latest/lib64 \
    /usr/local/Ascend/ascend-toolkit/latest/runtime/lib64 \
    /usr/local/Ascend/latest/lib64 \
    /usr/local/Ascend/latest/runtime/lib64; do
    if [[ "${candidate}" != /lib64 && "${candidate}" != /runtime/lib64 &&
          -e "${candidate}/libascendcl.so" ]]; then
        ASCEND_LIB="${candidate}"
        break
    fi
done
if [[ -z "${ASCEND_LIB}" ]]; then
    echo "Cannot find libascendcl.so. Source CANN set_env.sh or set ASCEND_HOME_PATH." >&2
    exit 2
fi

mkdir -p "$(dirname "${OUTPUT}")"
cc -std=gnu11 -O2 -Wall -Wextra -I"${ROOT_DIR}/file_p2p" \
    -c "${ROOT_DIR}/file_p2p/file_p2p_api.c" -o "${OUTPUT}.file_p2p.o"
c++ -std=c++17 -O2 -Wall -Wextra \
    -I"${ASCEND_INCLUDE}" -I"${ROOT_DIR}/file_p2p" \
    "${ROOT_DIR}/tools/repro_ipc_import_pa.cpp" "${OUTPUT}.file_p2p.o" \
    -L"${ASCEND_LIB}" -Wl,-rpath,"${ASCEND_LIB}" -lascendcl -o "${OUTPUT}"

before_lines="$( { dmesg --color=never 2>/dev/null || true; } | wc -l)"
set +e
"${OUTPUT}" "$@"
result=$?
set -e

echo
echo "Kernel messages added by this run:"
dmesg --color=never 2>/dev/null | tail -n "+$((before_lines + 1))" | \
    grep -E 'p2p_dev|get svm_proc|Invalid svm_proc|Find pa cache' || \
    echo "(none found; use: sudo dmesg -T | grep -E 'p2p_dev|get svm_proc|Invalid svm_proc|Find pa cache' | tail -50)"

exit "${result}"
