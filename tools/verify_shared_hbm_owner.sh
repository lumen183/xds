#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT="${ROOT_DIR}/build/verify_shared_hbm_owner"

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
c++ -std=c++17 -O2 -Wall -Wextra \
    -I"${ASCEND_INCLUDE}" -I"${ROOT_DIR}" \
    "${ROOT_DIR}/tools/verify_shared_hbm_owner.cpp" \
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
    echo "(none found; inspect: dmesg -T | grep -E 'p2p_dev|get svm_proc|Invalid svm_proc|Find pa cache' | tail -50)"

exit "${result}"
