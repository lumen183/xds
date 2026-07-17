#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="${ROOT_DIR}/build"
ASCEND_MODE="on"
INCREMENTAL="off"
BUILD_PYTHON="OFF"
TEST_ACTION=""
KERNEL_PROFILE="release"
KDIR="${KDIR:-/lib/modules/$(uname -r)/build}"
ASCEND_MODULE_SYMVERS="${ASCEND_MODULE_SYMVERS:-}"

usage() {
    cat <<'EOF'
Usage: ./build.sh [-X on|off] [-P] [-t build|run] [-i on|off] [-M release|debug]

  -X on|off     Select the Ascend backend (on by default).  "off" builds only
                 the file_p2p Python mock and never builds kernel modules.
  -P             Build the Python module for the selected backend.
  -t build|run   Build test targets (including the C++ stream benchmark),
                 or build then run tests.
  -i on|off      Preserve build artifacts for incremental builds (off by default).
  -M release|debug
                 Select the kernel-module profile (release by default).
                 release removes hot-path logs; debug keeps them.

Environment:
  KDIR           Matching Linux kernel build directory for -X on.
  ASCEND_MODULE_SYMVERS
                 Optional Module.symvers exported by the real Ascend devmm
                 provider.  Supply this on target builds with modversions.
  BUILD_JOBS     Optional parallel build-job count.
EOF
}

fail_option() {
    echo "build.sh: $1" >&2
    usage >&2
    exit 2
}

while (($#)); do
    case "$1" in
        -X)
            (($# >= 2)) || fail_option "-X requires on or off"
            ASCEND_MODE="$2"
            shift 2
            ;;
        -P)
            BUILD_PYTHON="ON"
            shift
            ;;
        -t)
            (($# >= 2)) || fail_option "-t requires build or run"
            TEST_ACTION="$2"
            shift 2
            ;;
        -i)
            (($# >= 2)) || fail_option "-i requires on or off"
            INCREMENTAL="$2"
            shift 2
            ;;
        -M)
            (($# >= 2)) || fail_option "-M requires release or debug"
            KERNEL_PROFILE="${2,,}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail_option "unknown option: $1"
            ;;
    esac
done

[[ "${ASCEND_MODE}" == "on" || "${ASCEND_MODE}" == "off" ]] || fail_option "-X must be on or off"
[[ "${INCREMENTAL}" == "on" || "${INCREMENTAL}" == "off" ]] || fail_option "-i must be on or off"
[[ "${KERNEL_PROFILE}" == "release" || "${KERNEL_PROFILE}" == "debug" ]] || fail_option "-M must be release or debug"
[[ -z "${TEST_ACTION}" || "${TEST_ACTION}" == "build" || "${TEST_ACTION}" == "run" ]] || fail_option "-t must be build or run"

if [[ "${ASCEND_MODE}" == "off" || -n "${TEST_ACTION}" ]]; then
    BUILD_PYTHON="ON"
fi

if [[ "${INCREMENTAL}" == "off" ]]; then
    "${CMAKE_COMMAND:-cmake}" -E rm -rf "${BUILD_DIR}"
fi

cmake -S "${ROOT_DIR}" -B "${BUILD_DIR}" \
    "-DXDS_ASCEND=${ASCEND_MODE}" \
    "-DXDS_BUILD_PYTHON=${BUILD_PYTHON}" \
    "-DXDS_BUILD_TESTS=$([[ -n "${TEST_ACTION}" ]] && echo ON || echo OFF)" \
    "-DXDS_KERNEL_PROFILE=${KERNEL_PROFILE}" \
    "-DXDS_KERNEL_BUILD_DIR=${KDIR}" \
    "-DXDS_ASCEND_MODULE_SYMVERS=${ASCEND_MODULE_SYMVERS}"

build_args=(--build "${BUILD_DIR}" --parallel)
if [[ -n "${BUILD_JOBS:-}" ]]; then
    build_args+=("${BUILD_JOBS}")
fi
cmake "${build_args[@]}"

if [[ "${TEST_ACTION}" == "run" ]]; then
    ctest --test-dir "${BUILD_DIR}" --output-on-failure
fi
