#include <acl/acl.h>

#include "p2p_dev_uapi.h"

#include <cerrno>
#include <cctype>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <iostream>
#include <limits>
#include <spawn.h>
#include <stdexcept>
#include <string>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#include <vector>

extern char **environ;

namespace {

constexpr std::size_t kIpcKeySize = 65;
constexpr std::uint64_t kExportFlagDefault = 0;
constexpr std::uint64_t kImportFlagDefault = 0;

class Failure : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

struct Args {
    std::uint64_t size = 16ULL << 20;
    std::uint32_t devid = 0;
    std::uint32_t vfid = 0;
    bool verbose = false;
};

struct ChildHello {
    std::int32_t namespace_pid;
    std::int32_t bare_pid;
};

struct ShareInfo {
    char key[kIpcKeySize];
    std::uint64_t owner_address;
    std::uint64_t size;
    std::int32_t owner_namespace_pid;
    std::int32_t owner_bare_pid;
    std::uint32_t devid;
    std::uint32_t vfid;
    std::int32_t baseline_result;
};

std::string recent_acl_error()
{
    const char *message = aclGetRecentErrMsg();
    return message == nullptr || *message == '\0' ? "(no ACL detail)" : message;
}

void check_acl(const char *operation, aclError result)
{
    if (result != ACL_SUCCESS) {
        throw Failure(std::string(operation) + " failed: ACL error=" + std::to_string(result) +
                      " detail=" + recent_acl_error());
    }
}

std::uint64_t parse_size(std::string text)
{
    if (text.empty() || text.front() == '-')
        throw Failure("--size must be positive");
    for (char &character : text)
        character = static_cast<char>(std::tolower(static_cast<unsigned char>(character)));
    std::uint64_t multiplier = 1;
    if (text.size() >= 2 && text.substr(text.size() - 2) == "ib")
        text.resize(text.size() - 2);
    if (!text.empty()) {
        if (text.back() == 'k') multiplier = 1ULL << 10;
        else if (text.back() == 'm') multiplier = 1ULL << 20;
        else if (text.back() == 'g') multiplier = 1ULL << 30;
        else if (!std::isdigit(static_cast<unsigned char>(text.back())))
            throw Failure("--size suffix must be K, M, G, KiB, MiB, or GiB");
        if (multiplier != 1)
            text.pop_back();
    }
    if (text.empty())
        throw Failure("--size must be positive");
    std::size_t parsed = 0;
    unsigned long long number = 0;
    try {
        number = std::stoull(text, &parsed, 10);
    } catch (const std::exception &) {
        throw Failure("--size must be a positive integer with an optional K/M/G suffix");
    }
    if (parsed != text.size() || number == 0 || number > std::numeric_limits<std::uint64_t>::max() / multiplier)
        throw Failure("--size is invalid or too large");
    return number * multiplier;
}

std::uint32_t parse_id(const std::string &text, const std::string &option)
{
    if (text.empty() || text.front() == '-')
        throw Failure(option + " must be non-negative");
    std::size_t parsed = 0;
    unsigned long long number = 0;
    try {
        number = std::stoull(text, &parsed, 10);
    } catch (const std::exception &) {
        throw Failure(option + " must be a non-negative integer");
    }
    if (parsed != text.size() || number > std::numeric_limits<unsigned short>::max())
        throw Failure(option + " must fit in 16 bits");
    return static_cast<std::uint32_t>(number);
}

void usage(std::ostream &stream, const char *program)
{
    stream << "Usage: " << program << " [options]\n\n"
           << "Read-only DEVMM PA-query diagnostic for IPC-shared HBM.\n"
           << "It does not access a file, NVMe, or submit DMA.\n\n"
           << "  --size SIZE    HBM allocation/query size (default: 16M)\n"
           << "  --devid ID     Ascend logical device id (default: 0)\n"
           << "  --vfid ID      Ascend virtual device id (default: 0)\n"
           << "  --verbose      print lifecycle diagnostics\n"
           << "  -h, --help     show this help\n";
}

Args parse_args(int argc, char **argv)
{
    Args args;
    auto value = [&](int &index, const std::string &option) -> std::string {
        if (++index >= argc)
            throw Failure(option + " requires a value");
        return argv[index];
    };
    for (int index = 1; index < argc; ++index) {
        const std::string option = argv[index];
        if (option == "-h" || option == "--help") {
            usage(std::cout, argv[0]);
            std::exit(0);
        } else if (option == "--size") {
            args.size = parse_size(value(index, option));
        } else if (option == "--devid") {
            args.devid = parse_id(value(index, option), option);
        } else if (option == "--vfid") {
            args.vfid = parse_id(value(index, option), option);
        } else if (option == "--verbose") {
            args.verbose = true;
        } else {
            throw Failure("unknown option: " + option);
        }
    }
    if (args.size > std::numeric_limits<unsigned long>::max())
        throw Failure("--size does not fit the XDS UAPI");
    return args;
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

class AclRuntime {
public:
    explicit AclRuntime(std::uint32_t device) : device_(device)
    {
        check_acl("aclInit", aclInit(nullptr));
        initialized_ = true;
        try {
            check_acl("aclrtSetDevice", aclrtSetDevice(static_cast<std::int32_t>(device_)));
            device_set_ = true;
        } catch (...) {
            aclFinalize();
            initialized_ = false;
            throw;
        }
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

int query_pa(std::int32_t pid, std::uint64_t address, std::uint64_t size,
             std::uint32_t devid, std::uint32_t vfid)
{
    const int fd = ::open("/dev/p2p_device", O_RDWR);
    if (fd < 0)
        return -errno;
    va_desc desc {};
    desc.hostpid = pid;
    desc.devid = static_cast<unsigned short>(devid);
    desc.vfid = static_cast<unsigned short>(vfid);
    desc.addr = static_cast<unsigned long>(address);
    desc.size = static_cast<unsigned long>(size);
    const int ioctl_result = ::ioctl(fd, IOCTL_DUMP_PA, &desc);
    const int result = ioctl_result < 0 ? -errno : 0;
    ::close(fd);
    return result;
}

std::string executable_path(const char *argv0)
{
    std::vector<char> storage(4096);
    const ssize_t count = ::readlink("/proc/self/exe", storage.data(), storage.size() - 1);
    if (count <= 0)
        return argv0;
    storage[static_cast<std::size_t>(count)] = '\0';
    return storage.data();
}

int child_main(int control_fd, std::uint32_t device, bool verbose)
{
    ShareInfo info {};
    try {
        AclRuntime runtime(device);
        std::int32_t bare_pid = -1;
        check_acl("aclrtDeviceGetBareTgid", aclrtDeviceGetBareTgid(&bare_pid));
        ChildHello hello {static_cast<std::int32_t>(::getpid()), bare_pid};
        if (!write_exact(control_fd, &hello, sizeof(hello)))
            throw Failure("cannot send child PID to exporter");
        if (!read_exact(control_fd, &info, sizeof(info)))
            throw Failure("cannot receive IPC share information from exporter");
        ::close(control_fd);
        control_fd = -1;

        void *imported = nullptr;
        check_acl("aclrtIpcMemImportByKey",
                  aclrtIpcMemImportByKey(&imported, info.key, kImportFlagDefault));
        if (verbose) {
            std::cerr << "INFO child imported key namespace_pid=" << ::getpid()
                      << " bare_pid=" << bare_pid << " imported_va=" << imported << std::endl;
        }
        const int imported_result = query_pa(static_cast<std::int32_t>(::getpid()),
                                             reinterpret_cast<std::uintptr_t>(imported), info.size,
                                             info.devid, info.vfid);
        const int owner_result = query_pa(info.owner_namespace_pid, info.owner_address, info.size,
                                          info.devid, info.vfid);
        int owner_bare_result = owner_result;
        if (info.owner_bare_pid != info.owner_namespace_pid) {
            owner_bare_result = query_pa(info.owner_bare_pid, info.owner_address, info.size,
                                         info.devid, info.vfid);
        }
        const aclError close_result = aclrtIpcMemClose(info.key);
        if (close_result != ACL_SUCCESS)
            std::cerr << "WARN aclrtIpcMemClose(importer)=" << close_result
                      << " detail=" << recent_acl_error() << std::endl;

        std::cout << "QUERY baseline-owner-process pid=" << info.owner_namespace_pid
                  << " va=0x" << std::hex << info.owner_address << std::dec
                  << " result=" << info.baseline_result << '\n';
        std::cout << "QUERY importer-view pid=" << ::getpid()
                  << " va=" << imported << " result=" << imported_result << '\n';
        std::cout << "QUERY owner-view-from-importer pid=" << info.owner_namespace_pid
                  << " va=0x" << std::hex << info.owner_address << std::dec
                  << " result=" << owner_result << '\n';
        if (info.owner_bare_pid != info.owner_namespace_pid) {
            std::cout << "QUERY owner-bare-view-from-importer pid=" << info.owner_bare_pid
                      << " va=0x" << std::hex << info.owner_address << std::dec
                      << " result=" << owner_bare_result << '\n';
        }

        if (info.baseline_result == 0 && imported_result != 0 && owner_result == 0) {
            std::cout << "VERDICT=CONFIRMED importer PID+IPC VA rejected; exporter PID+original VA accepted"
                      << std::endl;
            return 0;
        }
        if (info.baseline_result == 0 && imported_result == 0) {
            std::cout << "VERDICT=NOT_REPRODUCED DEVMM accepted importer PID+IPC VA" << std::endl;
            return 1;
        }
        if (info.baseline_result == 0 && owner_result != 0) {
            std::cout << "VERDICT=OWNER_FALLBACK_REJECTED exporter tuple works in owner process but not importer"
                      << std::endl;
            return 1;
        }
        std::cout << "VERDICT=INCONCLUSIVE baseline owner query failed" << std::endl;
        return 1;
    } catch (const std::exception &error) {
        if (control_fd >= 0)
            ::close(control_fd);
        std::cerr << "FAIL child: " << error.what() << std::endl;
        return 2;
    }
}

int parent_main(const Args &args, const char *argv0)
{
    if (::access("/dev/p2p_device", R_OK | W_OK) < 0)
        throw Failure("/dev/p2p_device is not readable and writable: " + std::string(std::strerror(errno)));

    AclRuntime runtime(args.devid);
    std::int32_t owner_bare_pid = -1;
    check_acl("aclrtDeviceGetBareTgid", aclrtDeviceGetBareTgid(&owner_bare_pid));
    void *allocation = nullptr;
    check_acl("aclrtMalloc", aclrtMalloc(&allocation, args.size, ACL_MEM_MALLOC_HUGE_FIRST));
    bool allocated = true;
    bool exported = false;
    char key[kIpcKeySize] {};
    try {
        const auto address = reinterpret_cast<std::uintptr_t>(allocation);
        const int baseline = query_pa(static_cast<std::int32_t>(::getpid()), address, args.size,
                                      args.devid, args.vfid);
        std::cout << "EXPORTER namespace_pid=" << ::getpid() << " bare_pid=" << owner_bare_pid
                  << " va=" << allocation << " size=" << args.size
                  << " baseline_result=" << baseline << std::endl;

        int sockets[2];
        if (::socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) != 0)
            throw Failure("socketpair failed: " + std::string(std::strerror(errno)));
        const std::string executable = executable_path(argv0);
        const std::string fd_text = std::to_string(sockets[1]);
        const std::string device_text = std::to_string(args.devid);
        std::vector<char *> child_argv {const_cast<char *>(executable.c_str()),
                                        const_cast<char *>("--child"),
                                        const_cast<char *>(fd_text.c_str()),
                                        const_cast<char *>(device_text.c_str())};
        if (args.verbose)
            child_argv.push_back(const_cast<char *>("--verbose"));
        child_argv.push_back(nullptr);
        posix_spawn_file_actions_t actions;
        ::posix_spawn_file_actions_init(&actions);
        ::posix_spawn_file_actions_addclose(&actions, sockets[0]);
        pid_t child = -1;
        const int spawn_result = ::posix_spawn(&child, executable.c_str(), &actions, nullptr,
                                               child_argv.data(), environ);
        ::posix_spawn_file_actions_destroy(&actions);
        ::close(sockets[1]);
        if (spawn_result != 0) {
            ::close(sockets[0]);
            throw Failure("posix_spawn failed: " + std::string(std::strerror(spawn_result)));
        }

        ChildHello hello {};
        if (!read_exact(sockets[0], &hello, sizeof(hello))) {
            ::close(sockets[0]);
            throw Failure("cannot receive child PID");
        }
        if (args.verbose) {
            std::cerr << "INFO child ready namespace_pid=" << hello.namespace_pid
                      << " bare_pid=" << hello.bare_pid << std::endl;
        }
        check_acl("aclrtIpcMemGetExportKey",
                  aclrtIpcMemGetExportKey(allocation, args.size, key, sizeof(key), kExportFlagDefault));
        exported = true;
        std::int32_t import_pid = hello.bare_pid;
        check_acl("aclrtIpcMemSetImportPid", aclrtIpcMemSetImportPid(key, &import_pid, 1));

        ShareInfo info {};
        std::memcpy(info.key, key, sizeof(key));
        info.owner_address = address;
        info.size = args.size;
        info.owner_namespace_pid = static_cast<std::int32_t>(::getpid());
        info.owner_bare_pid = owner_bare_pid;
        info.devid = args.devid;
        info.vfid = args.vfid;
        info.baseline_result = baseline;
        if (!write_exact(sockets[0], &info, sizeof(info))) {
            ::close(sockets[0]);
            throw Failure("cannot send IPC key to child");
        }
        ::close(sockets[0]);

        int status = 0;
        int result = 2;
        if (::waitpid(child, &status, 0) > 0 && WIFEXITED(status))
            result = WEXITSTATUS(status);
        const aclError close_result = aclrtIpcMemClose(key);
        exported = false;
        if (close_result != ACL_SUCCESS)
            std::cerr << "WARN aclrtIpcMemClose(exporter)=" << close_result
                      << " detail=" << recent_acl_error() << std::endl;
        aclrtFree(allocation);
        allocated = false;
        return result;
    } catch (...) {
        if (exported)
            aclrtIpcMemClose(key);
        if (allocated)
            aclrtFree(allocation);
        throw;
    }
}

} // namespace

int main(int argc, char **argv)
{
    try {
        if (argc >= 4 && std::strcmp(argv[1], "--child") == 0) {
            const int control_fd = std::stoi(argv[2]);
            const std::uint32_t device = parse_id(argv[3], "internal device id");
            const bool verbose = argc >= 5 && std::strcmp(argv[4], "--verbose") == 0;
            return child_main(control_fd, device, verbose);
        }
        const Args args = parse_args(argc, argv);
        return parent_main(args, argv[0]);
    } catch (const std::exception &error) {
        std::cerr << "FAIL " << error.what() << std::endl;
        return 2;
    }
}
