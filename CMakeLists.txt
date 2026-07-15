cmake_minimum_required(VERSION 3.16)

project(xds LANGUAGES C)

option(XDS_ASCEND "Build the Ascend kernel-module backend" ON)
option(XDS_BUILD_PYTHON "Build the Python module for the selected backend" OFF)
option(XDS_BUILD_TESTS "Register tests for the selected backend" OFF)

execute_process(
    COMMAND uname -r
    OUTPUT_VARIABLE XDS_KERNEL_RELEASE
    OUTPUT_STRIP_TRAILING_WHITESPACE
)
set(
    XDS_KERNEL_BUILD_DIR
    "/lib/modules/${XDS_KERNEL_RELEASE}/build"
    CACHE PATH
    "Linux kernel build directory used for external modules"
)
set(XDS_PYTHON_OUTPUT_DIR "${CMAKE_BINARY_DIR}/python")
set(
    XDS_ASCEND_MODULE_SYMVERS
    ""
    CACHE FILEPATH
    "Module.symvers exported by the real Ascend devmm provider"
)

if(XDS_ASCEND)
    if(NOT EXISTS "${XDS_KERNEL_BUILD_DIR}/Makefile")
        message(FATAL_ERROR
            "Kernel build directory is unavailable: ${XDS_KERNEL_BUILD_DIR}. "
            "Install matching kernel headers or set KDIR before running build.sh.")
    endif()

    find_program(XDS_KERNEL_MAKE NAMES gmake make REQUIRED)

    set(XDS_KERNEL_OUTPUT_DIR "${CMAKE_BINARY_DIR}/kernel")
    set(XDS_P2P_MODULE_DIR "${XDS_KERNEL_OUTPUT_DIR}/p2p_dev")
    set(XDS_STUB_MODULE_DIR "${XDS_KERNEL_OUTPUT_DIR}/stub")
    file(MAKE_DIRECTORY "${XDS_P2P_MODULE_DIR}" "${XDS_STUB_MODULE_DIR}")

    configure_file("${CMAKE_CURRENT_SOURCE_DIR}/p2p_dev.c"
                   "${XDS_P2P_MODULE_DIR}/p2p_dev.c" COPYONLY)
    configure_file("${CMAKE_CURRENT_SOURCE_DIR}/p2p_dev_uapi.h"
                   "${XDS_P2P_MODULE_DIR}/p2p_dev_uapi.h" COPYONLY)
    configure_file("${CMAKE_CURRENT_SOURCE_DIR}/p2p_mem_query.h"
                   "${XDS_P2P_MODULE_DIR}/p2p_mem_query.h" COPYONLY)
    file(WRITE "${XDS_P2P_MODULE_DIR}/Makefile" "obj-m += p2p_dev.o\n")

    configure_file("${CMAKE_CURRENT_SOURCE_DIR}/stub.c"
                   "${XDS_STUB_MODULE_DIR}/stub.c" COPYONLY)
    configure_file("${CMAKE_CURRENT_SOURCE_DIR}/p2p_mem_query.h"
                   "${XDS_STUB_MODULE_DIR}/p2p_mem_query.h" COPYONLY)
    file(WRITE "${XDS_STUB_MODULE_DIR}/Makefile" "obj-m += stub.o\n")

    set(XDS_P2P_KBUILD_ARGS)
    if(XDS_ASCEND_MODULE_SYMVERS)
        if(NOT EXISTS "${XDS_ASCEND_MODULE_SYMVERS}")
            message(FATAL_ERROR
                "XDS_ASCEND_MODULE_SYMVERS does not exist: ${XDS_ASCEND_MODULE_SYMVERS}")
        endif()
        list(APPEND XDS_P2P_KBUILD_ARGS
            "KBUILD_EXTRA_SYMBOLS=${XDS_ASCEND_MODULE_SYMVERS}")
    else()
        # The provider is normally loaded on the target host.  Keep its symbols
        # unresolved here so p2p_dev.ko never acquires a dependency on stub.ko.
        list(APPEND XDS_P2P_KBUILD_ARGS KBUILD_MODPOST_WARN=1)
    endif()

    add_custom_target(kernel_modules ALL
        COMMAND "${XDS_KERNEL_MAKE}" -C "${XDS_KERNEL_BUILD_DIR}"
                "M=${XDS_P2P_MODULE_DIR}" ${XDS_P2P_KBUILD_ARGS} modules
        COMMAND "${XDS_KERNEL_MAKE}" -C "${XDS_KERNEL_BUILD_DIR}"
                "M=${XDS_STUB_MODULE_DIR}" modules
        BYPRODUCTS
            "${XDS_P2P_MODULE_DIR}/p2p_dev.ko"
            "${XDS_STUB_MODULE_DIR}/stub.ko"
        COMMENT "Building p2p_dev.ko and stub.ko"
        VERBATIM
    )
endif()

if(NOT XDS_ASCEND)
    add_custom_target(mock_python ALL
        COMMAND "${CMAKE_COMMAND}" -E make_directory "${XDS_PYTHON_OUTPUT_DIR}"
        COMMAND "${CMAKE_COMMAND}" -E copy_if_different
                "${CMAKE_CURRENT_SOURCE_DIR}/file_p2p/mock_file_p2p.py"
                "${XDS_PYTHON_OUTPUT_DIR}/file_p2p.py"
        BYPRODUCTS "${XDS_PYTHON_OUTPUT_DIR}/file_p2p.py"
        COMMENT "Preparing the file_p2p mock module"
        VERBATIM
    )
elseif(XDS_BUILD_PYTHON)
    find_package(Python3 3.8 REQUIRED COMPONENTS Interpreter Development)
    execute_process(
        COMMAND "${Python3_EXECUTABLE}" -c
                "import sysconfig; print(sysconfig.get_config_var('EXT_SUFFIX') or '.so')"
        OUTPUT_VARIABLE XDS_PYTHON_EXTENSION_SUFFIX
        OUTPUT_STRIP_TRAILING_WHITESPACE
    )

    add_library(file_p2p MODULE
        file_p2p/file_p2p_api.c
        file_p2p/py_file_p2p_api.c
    )
    target_include_directories(file_p2p PRIVATE
        "${CMAKE_CURRENT_SOURCE_DIR}/file_p2p"
        ${Python3_INCLUDE_DIRS}
    )
    target_link_libraries(file_p2p PRIVATE ${Python3_LIBRARIES})
    set_target_properties(file_p2p PROPERTIES
        PREFIX ""
        SUFFIX "${XDS_PYTHON_EXTENSION_SUFFIX}"
        LIBRARY_OUTPUT_DIRECTORY "${XDS_PYTHON_OUTPUT_DIR}"
    )
endif()

if(XDS_BUILD_TESTS)
    find_package(Python3 3.8 REQUIRED COMPONENTS Interpreter)
    enable_testing()

    if(XDS_ASCEND)
        enable_language(CXX)
        if(NOT XDS_BUILD_PYTHON)
            message(FATAL_ERROR "XDS_BUILD_TESTS requires XDS_BUILD_PYTHON for the Ascend backend")
        endif()

        find_path(XDS_ACL_INCLUDE_DIR
            NAMES acl/acl.h
            HINTS ENV ASCEND_HOME_PATH
            PATH_SUFFIXES include runtime/include
        )
        find_library(XDS_ASCENDCL_LIBRARY
            NAMES ascendcl
            HINTS ENV ASCEND_HOME_PATH
            PATH_SUFFIXES lib64 runtime/lib64
        )
        if(NOT XDS_ACL_INCLUDE_DIR OR NOT XDS_ASCENDCL_LIBRARY)
            message(FATAL_ERROR
                "Ascend ACL development files were not found. Source the CANN "
                "set_env.sh before running ./build.sh -t build.")
        endif()

        add_executable(single_npu_stream_bench
            tools/single_npu_stream_bench.cpp
            file_p2p/file_p2p_api.c
        )
        target_compile_features(single_npu_stream_bench PRIVATE cxx_std_17)
        target_include_directories(single_npu_stream_bench PRIVATE
            "${CMAKE_CURRENT_SOURCE_DIR}/file_p2p"
            "${CMAKE_CURRENT_SOURCE_DIR}"
            "${XDS_ACL_INCLUDE_DIR}"
        )
        target_link_libraries(single_npu_stream_bench PRIVATE "${XDS_ASCENDCL_LIBRARY}")
        set_target_properties(single_npu_stream_bench PROPERTIES
            RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin"
        )

        add_test(
            NAME python_extension_smoke
            COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/real/test_import.py"
        )
        add_test(
            NAME cpp_stream_bench_help
            COMMAND single_npu_stream_bench --help
        )
        add_test(
            NAME cpp_stream_bench_invalid_size
            COMMAND single_npu_stream_bench --file-size 0
        )
        add_test(
            NAME cpp_stream_bench_invalid_batch_delay
            COMMAND single_npu_stream_bench --batch-delay -1
        )
        set_tests_properties(
            cpp_stream_bench_invalid_size
            cpp_stream_bench_invalid_batch_delay
            PROPERTIES WILL_FAIL TRUE
        )
        set(XDS_BACKEND_TESTS
            python_extension_smoke
            cpp_stream_bench_help
            cpp_stream_bench_invalid_size
            cpp_stream_bench_invalid_batch_delay
        )
    else()
        add_test(
            NAME mock_file_p2p
            COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/mock/test_file_p2p.py"
        )
        set(XDS_BACKEND_TESTS mock_file_p2p)
    endif()

    add_test(
        NAME stream_report
        COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/test_stream_report.py"
    )

    set_tests_properties(${XDS_BACKEND_TESTS} PROPERTIES
        ENVIRONMENT "PYTHONPATH=${XDS_PYTHON_OUTPUT_DIR}"
    )
endif()
