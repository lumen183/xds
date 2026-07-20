# XDS 本次修改说明

本文记录本次对 XDS 仓库完成的构建、文档、mock 和测试改造。目标是让仓库在
没有 Ascend 硬件的开发机上可以完成基本流程验证，同时保留真实 Ascend 内核模块
和 Python C 扩展的构建路径。

## 1. 修改目标

本次工作围绕以下目标展开：

1. 用 CMake 统一组织构建；
2. 用根目录 `build.sh` 作为唯一对外构建入口；
3. 通过开关选择真实 Ascend 后端或纯 Python mock 后端；
4. 为 mock 提供与真实 `file_p2p` 扩展一致的主要 API；
5. 提供无需 Ascend、无需 root、无需 kernel headers 的基本测试；
6. 补充 README、构建说明、运行限制和真机操作步骤；
7. 修复当前 Linux 5.15 headers 下暴露出的内核模块编译问题。

## 2. 构建接口

构建命令统一为：

```bash
./build.sh [-X on|off] [-P] [-t build|run] [-i on|off] [-M release|debug]
```

| 参数 | 默认值 | 行为 |
| --- | --- | --- |
| `-X on\|off` | `on` | `on` 构建真实内核路径；`off` 只构建 mock。 |
| `-P` | 关闭 | 构建当前后端的 Python 模块。mock 模式会自动构建。 |
| `-t build` | 不执行 | 构建测试和测试依赖，但不运行测试。 |
| `-t run` | 不执行 | 自动构建测试依赖，然后运行测试。 |
| `-i on\|off` | `off` | `on` 保留构建缓存；`off` 清理 `build/` 后完整重建。 |
| `-M release\|debug` | `release` | `release` 编译时移除热路径日志；`debug` 保留逐 I/O 日志。 |

参数组合的实际语义如下：

```text
./build.sh
    真实模式，编译 p2p_dev.ko 和 stub.ko

./build.sh -P
    真实模式，编译内核模块和真实 file_p2p Python C 扩展

./build.sh -P -M debug
    真实模式诊断版本，保留逐 I/O 内核日志

./build.sh -X off
    只编译 build/python/file_p2p.py mock

./build.sh -X off -t run
    编译 mock，并运行完整 mock 测试

./build.sh -P -t run
    编译真实内核模块和 Python C 扩展，运行无设备 Python smoke test
```

`-X on` 使用的 kernel build 目录默认为：

```text
/lib/modules/$(uname -r)/build
```

可以通过环境变量覆盖：

```bash
KDIR=/path/to/kernel/build ./build.sh -P
```

目标内核启用 module versions 时，建议额外提供真实 Ascend `devmm` 驱动导出的
符号版本文件：

```bash
ASCEND_MODULE_SYMVERS=/path/to/Module.symvers ./build.sh -P
```

如果未提供该文件，构建会允许 `devmm_*` 符号以 warning 形式保持未解析，供目标机
加载时由真实 Ascend 驱动解析；这不是 mock，也不表示当前机器具备 Ascend 能力。

## 3. 文件变更清单

### 3.1 构建系统

- `CMakeLists.txt`
  - 增加 CMake 项目和后端选项；
  - 真实模式构建内核模块；
  - 真实模式可构建 Python C 扩展；
  - mock 模式复制纯 Python 模块到构建产物目录；
  - 注册 mock 测试和真实扩展 smoke test；
  - 将内核源码复制到 `build/kernel/` 下的独立 Kbuild 目录，避免真实
    `p2p_dev.ko` 自动产生对 `stub.ko` 的模块依赖。

- `build.sh`
  - 解析 `-X`、`-P`、`-t`、`-i`；
  - 负责 CMake 配置、编译和 CTest 执行；
  - `-i off` 清理 CMake 构建目录；
  - 不执行 `insmod`、`rmmod`，不修改内核状态；
  - 支持 `KDIR`、`ASCEND_MODULE_SYMVERS` 和 `BUILD_JOBS` 环境变量。

- `Makefile`
  - 保留 Linux 外部模块所需的 Kbuild 声明；
  - 支持 `make modules` 和 `make clean`；
  - 日常构建入口仍以 `build.sh` 为准。

### 3.2 Python mock 和测试

- `file_p2p/mock_file_p2p.py`
  - 提供 `new_p2p_fd`、`close_p2p_fd`、`read_file`、`read_file_batch`、
    `drain_read`；
  - 使用普通文件和 `bdev_offset` 模拟数据源；
  - 用 `addr` 作为模拟设备内存键；
  - `get_buffer(addr)` 是 mock 专用的只读验证接口；
  - 采用负 errno 返回错误。

- `tests/mock/test_file_p2p.py`
  - 单次读取和 drain；
  - 批量读取；
  - 无效 handle、缺失文件、越界读取和生命周期错误。

- `tests/real/test_import.py`
  - 验证真实 Python C 扩展可以导入；
  - 验证公开函数存在且可调用；
  - 不打开 `/dev/p2p_device`，不访问 Ascend 设备。

### 3.3 真实代码修正

- `p2p_dev.c`
  - 根据 `LINUX_VERSION_CODE` 兼容新旧 `blk_execute_rq_nowait` 调用签名；
  - 修正内核日志格式化类型；
  - 删除未使用的局部变量；
  - 避免统计日志在时间为零时进行无保护除法。

- `file_p2p/file_p2p_api.c`
  - 移除调试输出；
  - 修正用户态大小和 fiemap 长度的格式化输出。

- `file_p2p/py_file_p2p_api.c`
  - 补充 `stdlib.h`；
  - 修正 `read_file_batch` 文档，使其反映实际参数格式
    `(dev_fd, file_name, bdev_name, requests)`。

- `go.sh`
  - 改为显式的手工加载辅助脚本；
  - 默认使用 `build/kernel/p2p_dev/p2p_dev.ko`；
  - 不负责编译，不自动加载 Ascend 依赖。

- `README.md`
  - 增加完整使用手册；
  - 增加构建参数、mock 示例、测试说明和真机限制。

## 4. 产物布局

```text
build/
├── python/
│   └── file_p2p.py                       # -X off 的 mock
│       或 file_p2p.<python-ext-suffix>.so # -X on -P 的真实扩展
└── kernel/
    ├── p2p_dev/p2p_dev.ko                # 真实主模块
    └── stub/stub.ko                       # devmm 占位模块
```

构建不会执行系统安装。使用 Python 产物时显式指定：

```bash
PYTHONPATH="$PWD/build/python" python3 your_program.py
```

## 5. mock 行为约定

mock 保持真实 Python 扩展的主要函数签名，但不模拟真实物理地址和 Ascend 内存：

```python
fd = file_p2p.new_p2p_fd()
file_p2p.read_file(fd, filename, bdev_name, offset, addr, size, devid, vfid)
file_p2p.read_file_batch(fd, filename, bdev_name, requests)
file_p2p.drain_read(fd)
file_p2p.close_p2p_fd(fd)
```

其中：

- `filename` 是普通文件；
- `offset` 是普通文件偏移；
- `bdev_name`、`devid`、`vfid` 只为 API 兼容保留；
- `read_file` 和 `read_file_batch` 先排队；
- `drain_read` 后，数据才可通过 `get_buffer(addr)` 读取；
- 无效 handle 返回 `-EBADF`；
- 文件不存在返回 `-ENOENT`；
- 读取越过文件末尾返回 `-ERANGE`；
- 参数或批请求格式错误返回 `-EINVAL`。

## 6. 已执行验证

在当前开发环境中执行过以下命令：

```bash
bash -n build.sh go.sh
./build.sh -X off -t run
./build.sh -P -t run
./build.sh -X off -i on -t build
./build.sh -i on -P -t run
git diff --check
```

结果：

- mock 测试全部通过；
- 真实 Python C 扩展成功编译并通过导入/API smoke test；
- 当前 Linux 5.15 headers 下 `p2p_dev.ko` 和 `stub.ko` 成功编译；
- `p2p_dev.ko` 的模块依赖显示为 `nvme-core`，不依赖 `stub.ko`；
- 未执行真实 Ascend P2P I/O，也未执行任何模块加载或卸载。

构建真实内核模块时，如果没有 Ascend `Module.symvers`，会看到三个预期的
`devmm_* undefined` warning。它们表示当前机器缺少真实符号版本信息，不能被解释为
硬件运行验证通过。

## 7. 真机使用前检查

在 Ascend 机器上进行真实验证前，需要确认：

1. `KDIR` 与运行内核匹配；
2. Ascend 驱动已加载并导出三个 `devmm_*` 符号；
3. `ASCEND_MODULE_SYMVERS` 与目标驱动版本匹配（如果启用 module versions）；
4. `/proc/kallsyms` 中存在 `__tracepoint_nvme_setup_cmd`；
5. 目标 NVMe 设备和待读文件可访问；
6. 由管理员人工执行模块加载；
7. 测试完成后按目标系统的模块依赖顺序卸载。

推荐加载流程见根目录 [README.md](../README.md) 的“真机模块加载”章节。

## 8. 已知限制与后续工作

当前版本刻意不包含以下行为：

- 不自动加载或卸载内核模块；
- 不在 mock 中复现真实物理地址映射、IOMMU、NVMe 命令提交和 Ascend 内存一致性；
- 不执行真实设备端到端测试；
- 不保证跨 Linux 内核版本的 ABI 兼容；
- 不自动探测所有发行版中的 Ascend `Module.symvers` 路径。

后续接入真实 Ascend 环境时，建议优先补充：

- 目标驱动版本和 kernel headers 的 CI 构建矩阵；
- 真实 `devmm` 符号版本文件的自动发现或显式配置校验；
- 需要 root 和设备的独立硬件测试套件；
- 与目标内核版本绑定的 `blk_execute_rq_nowait` 和 NVMe tracepoint 适配层。
