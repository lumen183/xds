#include <acl/acl.h>

#include "file_p2p_api.h"

#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <iostream>
#include <limits>
#include <spawn.h>
#include <stdexcept>
#include <string>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#include <vector>

extern char **environ;

namespace {

constexpr std::size_t kIpcKeyLength = 65;
constexpr std::uint64_t kDisablePidValidation = 1;

enum ExitCode {
    kPass = 0,
    kUsage = 2,
    kSetupFailure = 10,
    kImportFailure = 11,
    kXdsFailure = 20,
    kVerifyFailure = 21,
};

struct Options {
    std::string input;
    std::string bdev;
    std::uint64_t offset = 0;
    std::uint32_t size = 4096;
    std::uint32_t device = 0;
    std::uint16_t vfid = 0;
};

struct WireMessage {
    char key[kIpcKeyLength];
    std::uint64_t exporter_address;
    std::int32_t exporter_pid;
};

std::string acl_error(aclError error)
{
    return std::to_string(static_cast<long long>(error));
}

bool write_exact(int fd, const void *data, std::size_t size)
{
    const auto *cursor = static_cast<const unsigned char *>(data);
    while (size != 0) {
        const ssize_t count = ::write(fd, cursor, size);
        if (count < 0 && errno == EINTR)
            continue;
        if (count <= 0)
            return false;
        cursor += count;
        size -= static_cast<std::size_t>(count);
    }
    return true;
}

bool read_exact(int fd, void *data, std::size_t size)
{
    auto *cursor = static_cast<unsigned char *>(data);
    while (size != 0) {
        const ssize_t count = ::read(fd, cursor, size);
        if (count < 0 && errno == EINTR)
            continue;
        if (count <= 0)
            return false;
        cursor += count;
        size -= static_cast<std::size_t>(count);
    }
    return true;
}

bool parse_u64(const char *text, std::uint64_t *value)
{
    errno = 0;
    char *end = nullptr;
    const unsigned long long parsed = std::strtoull(text, &end, 0);
    if (errno != 0 || end == text || *end != '\0')
        return false;
    *value = parsed;
    return true;
}

bool parse_options(int argc, char **argv, int first, Options *options)
{
    if (argc - first < 2 || argc - first > 6)
        return false;
    options->input = argv[first];
    options->bdev = argv[first + 1];
    std::uint64_t value = 0;
    if (argc > first + 2) {
        if (!parse_u64(argv[first + 2], &options->offset))
            return false;
    }
    if (argc > first + 3) {
        if (!parse_u64(argv[first + 3], &value) || value < 4096 || (value & 4095) != 0 ||
            value > std::numeric_limits<std::uint32_t>::max())
            return false;
        options->size = static_cast<std::uint32_t>(value);
    }
    if (argc > first + 4) {
        if (!parse_u64(argv[first + 4], &value) || value > std::numeric_limits<std::uint32_t>::max())
            return false;
        options->device = static_cast<std::uint32_t>(value);
    }
    if (argc > first + 5) {
        if (!parse_u64(argv[first + 5], &value) || value > std::numeric_limits<std::uint16_t>::max())
            return false;
        options->vfid = static_cast<std::uint16_t>(value);
    }
    return (options->offset & 4095) == 0 &&
           options->offset <= std::numeric_limits<unsigned long>::max() &&
           options->offset + options->size >= options->offset;
}

void usage(const char *program)
{
    std::cerr << "Usage: " << program
              << " INPUT BDEV [OFFSET [SIZE [DEVICE [VFID]]]]\n"
              << "  Example (regular file): " << program
              << " /mnt/nvme/xds-smoke.bin /dev/nvme0n1 0 4096 0 0\n"
              << "  Example (raw device):   " << program
              << " /dev/nvme0n1 /dev/nvme0n1 0 4096 0 0\n";
}

class AclRuntime {
public:
    explicit AclRuntime(std::uint32_t device) : device_(device)
    {
        aclError error = aclInit(nullptr);
        if (error != ACL_ERROR_NONE)
            throw std::runtime_error("aclInit failed: " + acl_error(error));
        initialized_ = true;
        error = aclrtSetDevice(static_cast<std::int32_t>(device));
        if (error != ACL_ERROR_NONE)
            throw std::runtime_error("aclrtSetDevice failed: " + acl_error(error));
        device_set_ = true;
    }

    ~AclRuntime()
    {
        if (device_set_)
            aclrtResetDevice(static_cast<std::int32_t>(device_));
        if (initialized_)
            aclFinalize();
    }

private:
    std::uint32_t device_;
    bool initialized_ = false;
    bool device_set_ = false;
};

bool load_expected(const Options &options, std::vector<unsigned char> *expected)
{
    const int fd = ::open(options.input.c_str(), O_RDONLY);
    if (fd < 0) {
        std::cerr << "SETUP_FAIL open input: " << std::strerror(errno) << '\n';
        return false;
    }
    expected->resize(options.size);
    std::size_t done = 0;
    while (done != expected->size()) {
        const ssize_t count = ::pread(fd, expected->data() + done, expected->size() - done,
                                      static_cast<off_t>(options.offset + done));
        if (count < 0 && errno == EINTR)
            continue;
        if (count <= 0) {
            std::cerr << "SETUP_FAIL pread input at offset " << options.offset + done
                      << ": " << (count == 0 ? "unexpected EOF" : std::strerror(errno)) << '\n';
            ::close(fd);
            return false;
        }
        done += static_cast<std::size_t>(count);
    }
    ::close(fd);
    return true;
}

int run_xds_case(const char *name, const Options &options, void *device_address,
                 const std::vector<unsigned char> &expected)
{
    std::cout << "CASE_BEGIN name=" << name << " pid=" << ::getpid()
              << " va=" << device_address << " size=" << options.size << std::endl;

    std::vector<unsigned char> poison(expected.size());
    for (std::size_t index = 0; index < expected.size(); ++index)
        poison[index] = static_cast<unsigned char>(expected[index] ^ 0xff);
    aclError acl_ret = aclrtMemcpy(device_address, options.size, poison.data(), poison.size(),
                                   ACL_MEMCPY_HOST_TO_DEVICE);
    if (acl_ret != ACL_ERROR_NONE) {
        std::cerr << "CASE_FAIL name=" << name << " stage=poison acl=" << acl_ret << std::endl;
        return kSetupFailure;
    }

    const int p2p_fd = new_p2p_fd();
    if (p2p_fd < 0) {
        std::cerr << "CASE_FAIL name=" << name << " stage=open_xds ret=" << p2p_fd << std::endl;
        return kSetupFailure;
    }
    read_parameter parameter {
        options.input.c_str(), options.bdev.c_str(), static_cast<unsigned long>(options.offset),
        static_cast<unsigned short>(options.device), options.vfid, options.size,
        reinterpret_cast<unsigned long>(device_address)
    };
    int ret = read_file(p2p_fd, &parameter);
    if (ret == 0)
        ret = drain_read(p2p_fd);
    close_p2p_fd(p2p_fd);
    if (ret != 0) {
        std::cerr << "CASE_FAIL name=" << name << " stage=xds ret=" << ret << std::endl;
        return kXdsFailure;
    }
    acl_ret = aclrtSynchronizeDevice();
    if (acl_ret != ACL_ERROR_NONE) {
        std::cerr << "CASE_FAIL name=" << name << " stage=synchronize acl=" << acl_ret << std::endl;
        return kXdsFailure;
    }

    std::vector<unsigned char> actual(options.size);
    acl_ret = aclrtMemcpy(actual.data(), actual.size(), device_address, options.size,
                          ACL_MEMCPY_DEVICE_TO_HOST);
    if (acl_ret != ACL_ERROR_NONE) {
        std::cerr << "CASE_FAIL name=" << name << " stage=copy_back acl=" << acl_ret << std::endl;
        return kVerifyFailure;
    }
    if (actual != expected) {
        std::size_t first = 0;
        while (first != actual.size() && actual[first] == expected[first])
            ++first;
        std::cerr << "CASE_FAIL name=" << name << " stage=verify first_mismatch=" << first;
        if (first != actual.size())
            std::cerr << " actual=" << static_cast<unsigned int>(actual[first])
                      << " expected=" << static_cast<unsigned int>(expected[first]);
        std::cerr << std::endl;
        return kVerifyFailure;
    }
    std::cout << "CASE_PASS name=" << name << std::endl;
    return kPass;
}

int importer_main(int argc, char **argv)
{
    if (argc < 5)
        return kUsage;
    std::uint64_t fd_value = 0;
    if (!parse_u64(argv[2], &fd_value) || fd_value > std::numeric_limits<int>::max())
        return kUsage;
    Options options;
    if (!parse_options(argc, argv, 3, &options))
        return kUsage;

    WireMessage message {};
    const int control_fd = static_cast<int>(fd_value);
    if (!read_exact(control_fd, &message, sizeof(message))) {
        std::cerr << "IMPORT_FAIL stage=receive errno=" << errno << std::endl;
        return kSetupFailure;
    }
    ::close(control_fd);
    std::vector<unsigned char> expected;
    if (!load_expected(options, &expected))
        return kSetupFailure;

    try {
        AclRuntime runtime(options.device);
        void *imported = nullptr;
        const aclError error = aclrtIpcMemImportByKey(&imported, message.key, 0);
        if (error != ACL_ERROR_NONE) {
            std::cerr << "IMPORT_FAIL stage=aclrtIpcMemImportByKey acl=" << error << std::endl;
            return kImportFailure;
        }
        std::cout << "IPC_MAP exporter_pid=" << message.exporter_pid
                  << " exporter_va=0x" << std::hex << message.exporter_address << std::dec
                  << " importer_pid=" << ::getpid() << " importer_va=" << imported << std::endl;
        const int result = run_xds_case("ipc_import", options, imported, expected);
        const aclError close_error = aclrtIpcMemClose(message.key);
        if (close_error != ACL_ERROR_NONE)
            std::cerr << "IMPORT_WARN aclrtIpcMemClose=" << close_error << std::endl;
        return result;
    } catch (const std::exception &error) {
        std::cerr << "IMPORT_FAIL stage=runtime error=" << error.what() << std::endl;
        return kSetupFailure;
    }
}

std::string executable_path(const char *argv0)
{
    std::vector<char> path(4096);
    const ssize_t count = ::readlink("/proc/self/exe", path.data(), path.size() - 1);
    if (count > 0) {
        path[static_cast<std::size_t>(count)] = '\0';
        return path.data();
    }
    return argv0;
}

int parent_main(int argc, char **argv)
{
    Options options;
    if (!parse_options(argc, argv, 1, &options)) {
        usage(argv[0]);
        return kUsage;
    }
    std::vector<unsigned char> expected;
    if (!load_expected(options, &expected))
        return kSetupFailure;

    try {
        AclRuntime runtime(options.device);
        void *allocated = nullptr;
        aclError error = aclrtMalloc(&allocated, options.size, ACL_MEM_MALLOC_HUGE_FIRST);
        if (error != ACL_ERROR_NONE) {
            std::cerr << "SETUP_FAIL aclrtMalloc=" << error << std::endl;
            return kSetupFailure;
        }
        std::cout << "EXPORTER pid=" << ::getpid() << " va=" << allocated << std::endl;
        const int direct_result = run_xds_case("same_process", options, allocated, expected);

        WireMessage message {};
        message.exporter_address = reinterpret_cast<std::uintptr_t>(allocated);
        message.exporter_pid = static_cast<std::int32_t>(::getpid());
        error = aclrtIpcMemGetExportKey(allocated, options.size, message.key, sizeof(message.key),
                                        kDisablePidValidation);
        if (error != ACL_ERROR_NONE) {
            std::cerr << "SETUP_FAIL aclrtIpcMemGetExportKey=" << error << std::endl;
            aclrtFree(allocated);
            return kSetupFailure;
        }

        int sockets[2];
        if (::socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) != 0) {
            std::cerr << "SETUP_FAIL socketpair: " << std::strerror(errno) << std::endl;
            aclrtIpcMemClose(message.key);
            aclrtFree(allocated);
            return kSetupFailure;
        }
        const std::string executable = executable_path(argv[0]);
        const std::string fd = std::to_string(sockets[1]);
        std::vector<char *> child_argv {
            const_cast<char *>(executable.c_str()), const_cast<char *>("--importer"),
            const_cast<char *>(fd.c_str()), argv[1], argv[2]
        };
        for (int index = 3; index < argc; ++index)
            child_argv.push_back(argv[index]);
        child_argv.push_back(nullptr);
        posix_spawn_file_actions_t actions;
        ::posix_spawn_file_actions_init(&actions);
        ::posix_spawn_file_actions_addclose(&actions, sockets[0]);
        pid_t child = -1;
        const int spawn_error = ::posix_spawn(&child, executable.c_str(), &actions, nullptr,
                                              child_argv.data(), environ);
        ::posix_spawn_file_actions_destroy(&actions);
        ::close(sockets[1]);
        int importer_result = kSetupFailure;
        if (spawn_error != 0) {
            std::cerr << "SETUP_FAIL posix_spawn: " << std::strerror(spawn_error) << std::endl;
        } else if (!write_exact(sockets[0], &message, sizeof(message))) {
            std::cerr << "SETUP_FAIL send IPC key: " << std::strerror(errno) << std::endl;
        }
        ::close(sockets[0]);
        if (spawn_error == 0) {
            int status = 0;
            if (::waitpid(child, &status, 0) > 0 && WIFEXITED(status))
                importer_result = WEXITSTATUS(status);
        }

        const aclError close_error = aclrtIpcMemClose(message.key);
        if (close_error != ACL_ERROR_NONE)
            std::cerr << "EXPORT_WARN aclrtIpcMemClose=" << close_error << std::endl;
        aclrtFree(allocated);

        std::cout << "SUMMARY same_process=" << direct_result
                  << " ipc_import=" << importer_result << std::endl;
        if (direct_result == kPass && importer_result == kXdsFailure) {
            std::cout << "VERDICT=CONFIRMED same-process PA query passed, "
                         "IPC-import PID+VA PA query failed" << std::endl;
            return kPass;
        }
        if (direct_result == kPass && importer_result == kPass) {
            std::cout << "VERDICT=NOT_REPRODUCED both PA queries passed on this stack" << std::endl;
            return 1;
        }
        std::cout << "VERDICT=INCONCLUSIVE inspect CASE_FAIL/IMPORT_FAIL and kernel log" << std::endl;
        return 1;
    } catch (const std::exception &error) {
        std::cerr << "SETUP_FAIL runtime: " << error.what() << std::endl;
        return kSetupFailure;
    }
}

} // namespace

int main(int argc, char **argv)
{
    if (argc > 1 && std::strcmp(argv[1], "--importer") == 0)
        return importer_main(argc, argv);
    return parent_main(argc, argv);
}
