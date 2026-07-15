#include <acl/acl.h>

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cctype>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <system_error>
#include <vector>

#include <fcntl.h>
#include <linux/fiemap.h>
#include <linux/fs.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>

extern "C" {
#include "file_p2p_api.h"
}

namespace fs = std::filesystem;
using Clock = std::chrono::steady_clock;

namespace {

constexpr const char *kDefaultSizes = "32K,64K,128K,256K,512K,1M";
constexpr const char *kDefaultDepths = "4,8,16,32,64,128";
constexpr std::size_t kFiemapExtentsPerQuery = 256;

class Failure : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

struct Args {
    std::string bdev;
    fs::path data_dir;
    std::uint64_t file_size = 3ULL * 1024 * 1024 * 1024;
    std::vector<std::uint32_t> sizes;
    std::vector<std::uint32_t> io_depths;
    std::uint64_t offset = 0;
    std::uint32_t devid = 0;
    std::uint32_t vfid = 0;
    bool verify = true;
    bool verbose = false;
    fs::path json;
};

struct Verification {
    bool enabled = false;
    std::string status = "skipped";
    std::size_t samples = 0;
    std::uint64_t sample_size = 0;
};

struct Result {
    std::uint32_t size;
    std::uint32_t io_depth;
    std::uint64_t elapsed_ns;
    double bandwidth;
    Verification verification;
};

Clock::time_point g_started;

void log(const Args &args, const std::string &phase, const std::string &message)
{
    if (!args.verbose)
        return;
    const auto elapsed = std::chrono::duration<double>(Clock::now() - g_started).count();
    std::cerr << "INFO phase=" << phase << " elapsed=" << std::fixed << std::setprecision(3)
              << elapsed << "s " << message << std::endl;
}

std::string errno_message(int error)
{
    return std::error_code(error, std::generic_category()).message();
}

void check_io(const char *operation, int result)
{
    if (result < 0) {
        const int error = -result;
        throw Failure(std::string(operation) + " failed: errno=" + std::to_string(error) +
                      " (" + errno_message(error) + ")");
    }
}

void check_acl(const char *operation, aclError result)
{
    if (result != ACL_SUCCESS)
        throw Failure(std::string(operation) + " failed: ACL error=" + std::to_string(result));
}

std::uint64_t parse_size(const std::string &value)
{
    std::string text;
    text.reserve(value.size());
    for (unsigned char character : value)
        text.push_back(static_cast<char>(std::tolower(character)));
    while (!text.empty() && std::isspace(static_cast<unsigned char>(text.front())))
        text.erase(text.begin());
    while (!text.empty() && std::isspace(static_cast<unsigned char>(text.back())))
        text.pop_back();
    if (text.size() >= 2 && text.substr(text.size() - 2) == "ib")
        text.resize(text.size() - 2);
    else if (!text.empty() && text.back() == 'b')
        text.pop_back();

    std::uint64_t multiplier = 1;
    if (!text.empty()) {
        switch (text.back()) {
        case 'k': multiplier = 1024ULL; break;
        case 'm': multiplier = 1024ULL * 1024; break;
        case 'g': multiplier = 1024ULL * 1024 * 1024; break;
        case 't': multiplier = 1024ULL * 1024 * 1024 * 1024; break;
        default: break;
        }
        if (multiplier != 1)
            text.pop_back();
    }
    if (text.empty() || text.front() == '-')
        throw Failure("size must be an integer with optional K/M/G/T suffix: " + value);
    std::size_t parsed = 0;
    unsigned long long number;
    try {
        number = std::stoull(text, &parsed, 10);
    } catch (const std::exception &) {
        throw Failure("size must be an integer with optional K/M/G/T suffix: " + value);
    }
    if (parsed != text.size() || number == 0 || number > std::numeric_limits<std::uint64_t>::max() / multiplier)
        throw Failure("invalid size: " + value);
    const std::uint64_t result = number * multiplier;
    if (result > std::numeric_limits<std::uint32_t>::max())
        throw Failure("size must be between 1 and 4GiB: " + value);
    return result;
}

std::vector<std::string> split_list(const std::string &value, const char *label)
{
    std::vector<std::string> items;
    std::size_t start = 0;
    while (start <= value.size()) {
        const auto end = value.find(',', start);
        std::string item = value.substr(start, end == std::string::npos ? std::string::npos : end - start);
        const auto first = item.find_first_not_of(" \t\r\n");
        const auto last = item.find_last_not_of(" \t\r\n");
        if (first == std::string::npos)
            throw Failure(std::string(label) + " list must contain non-empty values");
        items.push_back(item.substr(first, last - first + 1));
        if (end == std::string::npos)
            break;
        start = end + 1;
    }
    return items;
}

std::vector<std::uint32_t> parse_size_list(const std::string &value)
{
    std::vector<std::uint32_t> result;
    for (const auto &item : split_list(value, "size"))
        result.push_back(static_cast<std::uint32_t>(parse_size(item)));
    return result;
}

std::vector<std::uint32_t> parse_depth_list(const std::string &value)
{
    std::vector<std::uint32_t> result;
    for (const auto &item : split_list(value, "io-depth")) {
        std::size_t parsed = 0;
        unsigned long number;
        try {
            number = std::stoul(item, &parsed, 10);
        } catch (const std::exception &) {
            throw Failure("io-depth list must contain positive integers");
        }
        if (parsed != item.size() || number == 0 || number > std::numeric_limits<std::uint32_t>::max())
            throw Failure("io-depth list must contain positive integers");
        result.push_back(static_cast<std::uint32_t>(number));
    }
    return result;
}

void usage(std::ostream &stream, const char *program)
{
    stream << "Usage: " << program << " --bdev DEVICE --data-dir DIR [options]\n\n"
           << "Sequential single-NPU P2P streaming benchmark.\n\n"
           << "  --bdev PATH          block device backing --data-dir (required)\n"
           << "  --data-dir PATH      directory for the temporary stream file (required)\n"
           << "  --file-size SIZE     stream size (default: 3G)\n"
           << "  --size LIST          request sizes (default: " << kDefaultSizes << ")\n"
           << "  --io-depth LIST      requests per drain (default: " << kDefaultDepths << ")\n"
           << "  --offset BYTES       starting file offset (default: 0)\n"
           << "  --devid ID           Ascend NPU device id (default: 0)\n"
           << "  --vfid ID            Ascend virtual device id (default: 0)\n"
           << "  --verify             verify first/middle/last samples (default)\n"
           << "  --no-verify          skip sample verification\n"
           << "  --json PATH          write result JSON\n"
           << "  --verbose            print phase diagnostics\n"
           << "  -h, --help           show this help\n";
}

Args parse_args(int argc, char **argv)
{
    Args args;
    args.sizes = parse_size_list(kDefaultSizes);
    args.io_depths = parse_depth_list(kDefaultDepths);
    auto value = [&](int &index, const std::string &name) -> std::string {
        if (++index >= argc)
            throw Failure(name + " requires a value");
        return argv[index];
    };
    for (int i = 1; i < argc; ++i) {
        const std::string option = argv[i];
        if (option == "-h" || option == "--help") {
            usage(std::cout, argv[0]);
            std::exit(0);
        } else if (option == "--bdev") {
            args.bdev = value(i, option);
        } else if (option == "--data-dir") {
            args.data_dir = value(i, option);
        } else if (option == "--file-size") {
            args.file_size = parse_size(value(i, option));
        } else if (option == "--size") {
            args.sizes = parse_size_list(value(i, option));
        } else if (option == "--io-depth") {
            args.io_depths = parse_depth_list(value(i, option));
        } else if (option == "--offset" || option == "--devid" || option == "--vfid") {
            const std::string input = value(i, option);
            if (!input.empty() && input.front() == '-')
                throw Failure(option + " must be non-negative");
            std::size_t parsed = 0;
            unsigned long long number;
            try {
                number = std::stoull(input, &parsed, 10);
            } catch (const std::exception &) {
                throw Failure(option + " must be a non-negative integer");
            }
            if (parsed != input.size())
                throw Failure(option + " must be a non-negative integer");
            if (option == "--offset")
                args.offset = number;
            else if (option == "--devid" && number <= std::numeric_limits<std::uint32_t>::max())
                args.devid = static_cast<std::uint32_t>(number);
            else if (option == "--vfid" && number <= std::numeric_limits<std::uint32_t>::max())
                args.vfid = static_cast<std::uint32_t>(number);
            else
                throw Failure(option + " is too large");
        } else if (option == "--verify") {
            args.verify = true;
        } else if (option == "--no-verify") {
            args.verify = false;
        } else if (option == "--json") {
            args.json = value(i, option);
        } else if (option == "--verbose") {
            args.verbose = true;
        } else {
            throw Failure("unknown option: " + option);
        }
    }
    if (args.bdev.empty() || args.data_dir.empty())
        throw Failure("--bdev and --data-dir are required");
    if (args.devid > std::numeric_limits<unsigned short>::max() ||
        args.vfid > std::numeric_limits<unsigned short>::max())
        throw Failure("device ids must fit in 16 bits");
    if (args.offset > std::numeric_limits<std::uint64_t>::max() - args.file_size)
        throw Failure("offset plus file size overflows");
    for (auto size : args.sizes) {
        for (auto depth : args.io_depths) {
            if (size > std::numeric_limits<std::size_t>::max() / depth)
                throw Failure("request size multiplied by io-depth is too large");
        }
    }
    return args;
}

fs::path canonical_path(const fs::path &path)
{
    std::error_code error;
    const auto result = fs::canonical(path, error);
    if (error)
        throw Failure("cannot resolve " + path.string() + ": " + error.message());
    return result;
}

void fiemap_check(const fs::path &path, std::uint64_t offset, std::uint64_t length, const Args &args)
{
    const int fd = ::open(path.c_str(), O_RDONLY);
    if (fd < 0)
        throw Failure("open " + path.string() + " failed: " + errno_message(errno));
    struct Close { int fd; ~Close() { ::close(fd); } } close{fd};
    std::uint64_t cursor = offset;
    const std::uint64_t end = offset + length;
    while (cursor < end) {
        std::vector<unsigned char> storage(sizeof(struct fiemap) +
                                           kFiemapExtentsPerQuery * sizeof(struct fiemap_extent), 0);
        auto *map = reinterpret_cast<struct fiemap *>(storage.data());
        map->fm_start = cursor;
        map->fm_length = end - cursor;
        map->fm_flags = FIEMAP_FLAG_SYNC;
        map->fm_extent_count = kFiemapExtentsPerQuery;
        if (::ioctl(fd, FS_IOC_FIEMAP, map) < 0)
            throw Failure("FIEMAP failed for " + path.string() + ": " + errno_message(errno));
        log(args, "fiemap", "path=" + path.string() + " offset=" + std::to_string(cursor) +
            " mapped_extents=" + std::to_string(map->fm_mapped_extents));
        if (!map->fm_mapped_extents)
            throw Failure("FIEMAP returned no extents; the file may be sparse");
        std::uint64_t next = cursor;
        for (std::uint32_t i = 0; i < map->fm_mapped_extents; ++i) {
            const auto &extent = map->fm_extents[i];
            if ((extent.fe_flags & FIEMAP_EXTENT_UNWRITTEN) || extent.fe_logical > next)
                throw Failure("FIEMAP shows an unwritten or sparse extent");
            next = std::max(next, static_cast<std::uint64_t>(extent.fe_logical + extent.fe_length));
            if (next >= end)
                return;
        }
        if (next <= cursor)
            throw Failure("FIEMAP made no progress while checking the read range");
        cursor = next;
    }
    throw Failure("FIEMAP extents do not cover the requested read range");
}

bool parent_device_matches(dev_t filesystem_device, dev_t requested_device)
{
    const fs::path link = fs::path("/sys/dev/block") /
        (std::to_string(major(filesystem_device)) + ":" + std::to_string(minor(filesystem_device)));
    std::error_code error;
    const fs::path device_path = fs::canonical(link, error);
    if (error)
        return false;
    std::ifstream parent_dev(device_path.parent_path() / "dev");
    std::string value;
    if (!(parent_dev >> value))
        return false;
    return value == std::to_string(major(requested_device)) + ":" + std::to_string(minor(requested_device));
}

void check_bdev(const fs::path &path, const Args &args)
{
    struct stat file_stat {};
    struct stat bdev_stat {};
    if (::stat(path.c_str(), &file_stat) < 0)
        throw Failure("stat " + path.string() + " failed: " + errno_message(errno));
    if (::stat(args.bdev.c_str(), &bdev_stat) < 0)
        throw Failure("stat " + args.bdev + " failed: " + errno_message(errno));
    if (!S_ISBLK(bdev_stat.st_mode))
        throw Failure("--bdev is not a block device: " + args.bdev);
    if (file_stat.st_dev != bdev_stat.st_rdev && !parent_device_matches(file_stat.st_dev, bdev_stat.st_rdev))
        throw Failure("file filesystem does not match --bdev");
}

class InputFile {
public:
    InputFile(const Args &args, std::uint64_t required_size)
    {
        const fs::path directory = canonical_path(args.data_dir);
        if (!fs::is_directory(directory))
            throw Failure("--data-dir is not a directory: " + directory.string());
        std::string pattern = (directory / "xds-test-XXXXXX.bin").string();
        std::vector<char> name(pattern.begin(), pattern.end());
        name.push_back('\0');
        const int fd = ::mkstemps(name.data(), 4);
        if (fd < 0)
            throw Failure("cannot create temporary input: " + errno_message(errno));
        path_ = name.data();
        generated_ = true;
        bool fd_open = true;
        try {
            log(args, "input.write", "path=" + path_.string() + " bytes=" + std::to_string(required_size));
            constexpr std::size_t chunk_size = 8 * 1024 * 1024;
            std::vector<unsigned char> buffer(static_cast<std::size_t>(std::min<std::uint64_t>(chunk_size, required_size)));
            std::uint64_t position = 0;
            while (position < required_size) {
                const auto amount = static_cast<std::size_t>(std::min<std::uint64_t>(buffer.size(), required_size - position));
                for (std::size_t i = 0; i < amount; ++i)
                    buffer[i] = static_cast<unsigned char>((position + i) & 0xff);
                std::size_t written = 0;
                while (written < amount) {
                    const ssize_t count = ::write(fd, buffer.data() + written, amount - written);
                    if (count < 0) {
                        if (errno == EINTR)
                            continue;
                        throw Failure("write " + path_.string() + " failed: " + errno_message(errno));
                    }
                    written += static_cast<std::size_t>(count);
                    position += static_cast<std::size_t>(count);
                }
            }
            if (::fsync(fd) < 0)
                throw Failure("fsync " + path_.string() + " failed: " + errno_message(errno));
            const int close_result = ::close(fd);
            fd_open = false;
            if (close_result < 0)
                throw Failure("close " + path_.string() + " failed: " + errno_message(errno));
        } catch (...) {
            if (fd_open)
                ::close(fd);
            cleanup();
            throw;
        }
        try {
            struct stat info {};
            if (::stat(path_.c_str(), &info) < 0)
                throw Failure("stat " + path_.string() + " failed: " + errno_message(errno));
            if (static_cast<std::uint64_t>(info.st_size) < required_size)
                throw Failure("generated file is too small");
            if (static_cast<std::uint64_t>(info.st_blocks) * 512 < required_size)
                throw Failure("test file is sparse; use a filesystem with allocated local extents");
            fiemap_check(path_, args.offset, args.file_size, args);
            check_bdev(path_, args);
        } catch (...) {
            cleanup();
            throw;
        }
    }

    ~InputFile() { cleanup(); }
    InputFile(const InputFile &) = delete;
    InputFile &operator=(const InputFile &) = delete;
    const fs::path &path() const { return path_; }

private:
    void cleanup() noexcept
    {
        if (generated_ && !path_.empty()) {
            std::error_code ignored;
            fs::remove(path_, ignored);
            generated_ = false;
        }
    }
    fs::path path_;
    bool generated_ = false;
};

class AclRuntime {
public:
    explicit AclRuntime(std::uint32_t device) : device_(device)
    {
        check_acl("aclInit", aclInit(nullptr));
        initialized_ = true;
        try {
            std::uint32_t count = 0;
            check_acl("aclrtGetDeviceCount", aclrtGetDeviceCount(&count));
            if (device >= count)
                throw Failure("invalid NPU device id: " + std::to_string(device));
            check_acl("aclrtSetDevice", aclrtSetDevice(static_cast<int32_t>(device)));
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
            aclrtResetDevice(static_cast<int32_t>(device_));
        if (initialized_)
            aclFinalize();
    }
    AclRuntime(const AclRuntime &) = delete;
    AclRuntime &operator=(const AclRuntime &) = delete;

private:
    std::uint32_t device_;
    bool initialized_ = false;
    bool device_set_ = false;
};

class DeviceBuffer {
public:
    explicit DeviceBuffer(std::size_t bytes) : bytes_(bytes)
    {
        check_acl("aclrtMalloc", aclrtMalloc(&address_, bytes, ACL_MEM_MALLOC_HUGE_FIRST));
    }
    ~DeviceBuffer() { if (address_) aclrtFree(address_); }
    DeviceBuffer(const DeviceBuffer &) = delete;
    DeviceBuffer &operator=(const DeviceBuffer &) = delete;
    void *data() const { return address_; }
    std::size_t size() const { return bytes_; }

private:
    void *address_ = nullptr;
    std::size_t bytes_;
};

class P2pFd {
public:
    P2pFd()
    {
        if (::access("/dev/p2p_device", R_OK | W_OK) < 0)
            throw Failure("/dev/p2p_device is unavailable: " + errno_message(errno));
        fd_ = new_p2p_fd();
        check_io("new_p2p_fd", fd_);
    }
    ~P2pFd() { if (fd_ >= 0) close_p2p_fd(fd_); }
    int get() const { return fd_; }

private:
    int fd_ = -1;
};

std::uint64_t stream_once(int fd, const Args &args, const fs::path &input,
                          std::uint32_t request_size, std::uint32_t io_depth)
{
    const std::size_t buffer_size = static_cast<std::size_t>(request_size) * io_depth;
    DeviceBuffer buffer(buffer_size);
    const auto base = reinterpret_cast<std::uintptr_t>(buffer.data());
    std::uint64_t cursor = args.offset;
    const std::uint64_t end = args.offset + args.file_size;
    log(args, "stream.alloc", "size=" + std::to_string(request_size) +
        " io_depth=" + std::to_string(io_depth) + " buffer_bytes=" + std::to_string(buffer_size));
    const auto started = Clock::now();
    while (cursor < end) {
        const std::uint64_t remaining = end - cursor;
        const std::uint64_t needed = (remaining + request_size - 1) / request_size;
        const std::uint32_t count = static_cast<std::uint32_t>(std::min<std::uint64_t>(io_depth, needed));
        for (std::uint32_t index = 0; index < count; ++index) {
            const std::uint64_t file_offset = cursor + static_cast<std::uint64_t>(index) * request_size;
            const auto length = static_cast<std::uint32_t>(std::min<std::uint64_t>(request_size, end - file_offset));
            read_parameter parameter {
                input.c_str(), args.bdev.c_str(), static_cast<unsigned long>(file_offset),
                static_cast<unsigned short>(args.devid), static_cast<unsigned short>(args.vfid),
                length, static_cast<unsigned long>(base + static_cast<std::uintptr_t>(index) * request_size)
            };
            check_io("read_file", read_file(fd, &parameter));
        }
        check_io("drain_read", drain_read(fd));
        cursor += static_cast<std::uint64_t>(count) * request_size;
    }
    return static_cast<std::uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now() - started).count());
}

Verification verify_samples(int fd, const Args &args, const fs::path &input, std::uint32_t request_size)
{
    const std::uint64_t sample_size = std::min<std::uint64_t>(args.file_size, request_size);
    const std::uint64_t end = args.offset + args.file_size;
    const std::uint64_t middle = args.offset + ((args.file_size / 2) / request_size) * request_size;
    std::vector<std::uint64_t> candidates {args.offset, middle, end - sample_size};
    std::vector<std::uint64_t> offsets;
    for (auto offset : candidates) {
        if (std::find(offsets.begin(), offsets.end(), offset) == offsets.end())
            offsets.push_back(offset);
    }
    DeviceBuffer raw(static_cast<std::size_t>(sample_size + 4095));
    const auto raw_address = reinterpret_cast<std::uintptr_t>(raw.data());
    const auto address = (raw_address + 4095) & ~static_cast<std::uintptr_t>(4095);
    std::vector<unsigned char> actual(static_cast<std::size_t>(sample_size));
    for (auto offset : offsets) {
        read_parameter parameter {
            input.c_str(), args.bdev.c_str(), static_cast<unsigned long>(offset),
            static_cast<unsigned short>(args.devid), static_cast<unsigned short>(args.vfid),
            static_cast<unsigned int>(sample_size), static_cast<unsigned long>(address)
        };
        check_io("read_file", read_file(fd, &parameter));
        check_io("drain_read", drain_read(fd));
        check_acl("aclrtSynchronizeDevice", aclrtSynchronizeDevice());
        check_acl("aclrtMemcpy", aclrtMemcpy(actual.data(), actual.size(),
                                             reinterpret_cast<const void *>(address), actual.size(),
                                             ACL_MEMCPY_DEVICE_TO_HOST));
        std::size_t mismatch_count = 0;
        std::size_t first = actual.size();
        for (std::size_t index = 0; index < actual.size(); ++index) {
            const auto wanted = static_cast<unsigned char>((offset + index) & 0xff);
            if (actual[index] != wanted) {
                if (first == actual.size())
                    first = index;
                ++mismatch_count;
            }
        }
        if (mismatch_count) {
            std::ostringstream message;
            message << "data verification failed: sample_offset=" << offset
                    << " sample_size=" << sample_size << " request_size=" << request_size
                    << " first_index=" << first << " first_file_offset=" << offset + first
                    << " device_address=0x" << std::hex << address + first << std::dec
                    << " mismatch_count=" << mismatch_count;
            throw Failure(message.str());
        }
    }
    return {true, "ok", offsets.size(), sample_size};
}

std::string json_escape(const std::string &value)
{
    std::ostringstream output;
    for (unsigned char character : value) {
        switch (character) {
        case '"': output << "\\\""; break;
        case '\\': output << "\\\\"; break;
        case '\b': output << "\\b"; break;
        case '\f': output << "\\f"; break;
        case '\n': output << "\\n"; break;
        case '\r': output << "\\r"; break;
        case '\t': output << "\\t"; break;
        default:
            if (character < 0x20)
                output << "\\u" << std::hex << std::setw(4) << std::setfill('0') << static_cast<int>(character) << std::dec;
            else
                output << character;
        }
    }
    return output.str();
}

void write_verification(std::ostream &output, const Verification &verification)
{
    output << "{\"enabled\":" << (verification.enabled ? "true" : "false")
           << ",\"status\":\"" << json_escape(verification.status) << "\"";
    if (verification.enabled)
        output << ",\"samples\":" << verification.samples << ",\"sample_size\":" << verification.sample_size;
    output << '}';
}

void write_json(const fs::path &path, const Args &args, const fs::path &input,
                const std::vector<Result> &results, std::size_t best)
{
    std::ofstream output(path);
    if (!output)
        throw Failure("cannot write JSON: " + path.string());
    output << std::setprecision(17);
    output << "{\n  \"status\": \"PASS\",\n  \"file_size\": " << args.file_size << ",\n  \"results\": [\n";
    for (std::size_t index = 0; index < results.size(); ++index) {
        const auto &result = results[index];
        output << "    {\"status\":\"PASS\",\"api\":\"single\",\"bdev\":\""
               << json_escape(args.bdev) << "\",\"file\":\"" << json_escape(input.string())
               << "\",\"devid\":" << args.devid << ",\"vfid\":" << args.vfid
               << ",\"offset\":" << args.offset << ",\"file_size\":" << args.file_size
               << ",\"size\":" << result.size << ",\"io_depth\":" << result.io_depth
               << ",\"bytes\":" << args.file_size << ",\"elapsed_ns\":" << result.elapsed_ns
               << ",\"bandwidth_bytes_per_sec\":" << result.bandwidth << ",\"verify\":";
        write_verification(output, result.verification);
        output << '}' << (index + 1 == results.size() ? "\n" : ",\n");
    }
    const auto &winner = results[best];
    output << "  ],\n  \"best\": {\"status\":\"PASS\",\"api\":\"single\",\"bdev\":\""
           << json_escape(args.bdev) << "\",\"file\":\"" << json_escape(input.string())
           << "\",\"devid\":" << args.devid << ",\"vfid\":" << args.vfid
           << ",\"offset\":" << args.offset << ",\"file_size\":" << args.file_size
           << ",\"size\":" << winner.size << ",\"io_depth\":" << winner.io_depth
           << ",\"bytes\":" << args.file_size << ",\"elapsed_ns\":" << winner.elapsed_ns
           << ",\"bandwidth_bytes_per_sec\":" << winner.bandwidth << ",\"verify\":";
    write_verification(output, winner.verification);
    output << "}\n}\n";
    if (!output)
        throw Failure("failed while writing JSON: " + path.string());
}

int run(const Args &args)
{
    g_started = Clock::now();
    log(args, "run.start", "bdev=" + args.bdev + " data_dir=" + args.data_dir.string());
    AclRuntime runtime(args.devid);
    InputFile input(args, args.offset + args.file_size);
    P2pFd p2p;
    std::vector<Result> results;
    for (auto request_size : args.sizes) {
        for (auto io_depth : args.io_depths) {
            const auto elapsed_ns = stream_once(p2p.get(), args, input.path(), request_size, io_depth);
            const double bandwidth = elapsed_ns ?
                static_cast<double>(args.file_size) * 1'000'000'000.0 / static_cast<double>(elapsed_ns) : 0.0;
            const Verification verification = args.verify ?
                verify_samples(p2p.get(), args, input.path(), request_size) : Verification{};
            results.push_back({request_size, io_depth, elapsed_ns, bandwidth, verification});
            std::cout << "PASS size=" << request_size << "B io_depth=" << io_depth
                      << " bytes=" << args.file_size << " bandwidth=" << std::fixed << std::setprecision(2)
                      << bandwidth / (1024.0 * 1024 * 1024) << "GiB/s verify=" << verification.status
                      << std::endl;
        }
    }
    const auto best = static_cast<std::size_t>(std::distance(results.begin(),
        std::max_element(results.begin(), results.end(), [](const Result &left, const Result &right) {
            return left.bandwidth < right.bandwidth;
        })));
    std::cout << "BEST size=" << results[best].size << "B io_depth=" << results[best].io_depth
              << " bandwidth=" << std::fixed << std::setprecision(2)
              << results[best].bandwidth / (1024.0 * 1024 * 1024) << "GiB/s" << std::endl;
    if (!args.json.empty()) {
        write_json(args.json, args, input.path(), results, best);
        std::cout << "REPORT json=" << args.json << std::endl;
    }
    return 0;
}

} // namespace

int main(int argc, char **argv)
{
    try {
        return run(parse_args(argc, argv));
    } catch (const Failure &error) {
        std::cerr << "FAIL " << error.what() << std::endl;
        return 1;
    } catch (const std::exception &error) {
        std::cerr << "FAIL unexpected error: " << error.what() << std::endl;
        return 1;
    }
}
