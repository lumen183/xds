# XDS P2P 文件读取

XDS 提供一个面向 Ascend P2P 读取路径的 Linux 内核模块，以及同名的 Python
接口 `file_p2p`。真实路径将文件在块设备上的数据交给 NVMe/Ascend P2P 流程；
没有 Ascend 的开发机器可使用内置 mock 完成调用链和数据正确性验证。

本次构建、mock、测试和代码修正的完整记录见
[docs/MODIFICATION.md](docs/MODIFICATION.md)。

如果需要将本次工作拆成源码和构建框架两个提交，提交边界与逐处修改理由见
[docs/COMMIT_GUIDE.md](docs/COMMIT_GUIDE.md)。

> 真实内核路径依赖目标内核、NVMe 驱动实现和 Ascend 驱动导出的 `devmm_*` 符号。
> 它不是跨发行版保证可用的通用驱动，必须在目标机器完成集成验证。

## 要求

- Linux、Bash、CMake 3.16+；
- Python mock 和 Python 扩展需要 CPython 3.8+；
- `-X on` 还需要与运行内核匹配的 kernel headers。默认使用
  `/lib/modules/$(uname -r)/build`，也可通过 `KDIR` 指定；
- 真机运行还需要可工作的 Ascend 驱动、匹配的 NVMe 设备与 root 权限。

本机的 CMake 构建目录固定为 `build/`；内核模块输出为
`build/kernel/p2p_dev/p2p_dev.ko` 和 `build/kernel/stub/stub.ko`，Python 模块输出为
`build/python/file_p2p`。构建不会安装到系统或当前 Python 环境。

## 构建

唯一对外构建入口是根目录的 `build.sh`：

```text
./build.sh [-X on|off] [-P] [-t build|run] [-i on|off]
```

| 选项 | 含义 |
| --- | --- |
| `-X on` | 默认值。构建真实路径的 `p2p_dev.ko` 与 `stub.ko`。 |
| `-X off` | 仅构建纯 Python mock，不访问 Ascend、内核 headers 或内核模块。 |
| `-P` | 额外构建当前路径对应的 Python `file_p2p` 模块。mock 模式始终会构建它。 |
| `-t build` | 构建当前路径的测试及其 Python 依赖，不运行测试。 |
| `-t run` | 构建测试依赖后立即运行测试。 |
| `-i off` | 默认值。清理 XDS 生成的构建缓存后完整重建。 |
| `-i on` | 保留 CMake/Kbuild 构建产物，进行增量构建。 |

`-t run` 会自动构建测试需要的 Python 模块，无需另外传 `-P`。可通过
`BUILD_JOBS=8` 控制并行度；真实路径可用 `KDIR=/path/to/kernel/build` 覆盖
内核构建目录。若目标内核启用了 module versions，请把真实 Ascend `devmm` 提供者
导出的 `Module.symvers` 通过 `ASCEND_MODULE_SYMVERS=/path/to/Module.symvers` 传入。

常用命令：

```bash
# 默认：完整重建真实内核模块
./build.sh

# 构建真实内核模块及真实 Python C 扩展
./build.sh -P

# 无 Ascend 环境：构建并运行完整 mock 测试
./build.sh -X off -t run

# 只生成 mock 测试所需产物
./build.sh -X off -t build

# 保留产物进行增量 mock 构建
./build.sh -X off -i on
```

## 无 Ascend mock 流程

mock 与真实扩展使用相同的核心函数名：

```python
import file_p2p

fd = file_p2p.new_p2p_fd()
assert file_p2p.read_file(
    fd, "input.bin", "/dev/mock", 0, 0x1000, 4096, 0, 0
) == 0
assert file_p2p.drain_read(fd) == 0

# 仅 mock 提供，用来验证模拟设备内存内容。
data = file_p2p.get_buffer(0x1000)
file_p2p.close_p2p_fd(fd)
```

mock 将 `file_name` 当作普通文件，将 `bdev_offset` 当作文件偏移；`bdev_name`、
`devid` 和 `vfid` 仅为 API 兼容性保留。`read_file` 先排队，`drain_read` 后数据才
会写入由 `addr` 索引的模拟设备内存。读取越过文件末尾、无效 handle 和不存在的
文件都以负 errno 返回。

批量读取与原生扩展的实际签名一致：

```python
file_p2p.read_file_batch(
    fd,
    "input.bin",
    "/dev/mock",
    [(0, 0x1000, 4096), (4096, 0x2000, 4096)],
)
```

从构建产物目录手工使用模块时：

```bash
PYTHONPATH="$PWD/build/python" python3 your_program.py
```

## 测试

`-X off -t run` 运行标准库 `unittest` mock 测试，覆盖单读、批读、drain 语义、
handle 生命周期、缺失文件和越界读取。它不要求 Ascend、NVMe 设备、root 或 kernel
headers。

`-X on -t run` 只运行真实 Python C 扩展的导入/API smoke test；不会访问设备、不会
申请 Ascend 内存，也不会加载内核模块。实际 P2P I/O 只能在已准备好的目标机器上
人工验证。

## 真机模块加载

`build.sh` 永远不会执行 `insmod`、`rmmod` 或改动内核状态。确认 Ascend 驱动已经
提供真实 `devmm_*` 符号，且目标内核仍包含 `nvme_setup_cmd` tracepoint 后，才可由
管理员手工加载 `build/kernel/p2p_dev/p2p_dev.ko`。模块需要传入该 tracepoint 的地址：

```bash
addr=$(awk '/__tracepoint_nvme_setup_cmd/ {print $1}' /proc/kallsyms)
sudo insmod ./build/kernel/p2p_dev/p2p_dev.ko "tp_nvme_setup_cmd_addr=0x${addr}"
```

仓库保留的 `go.sh` 是同一加载流程的手工快捷方式（可选模块路径为第一个参数）；
它也不会构建模块，且必须在 Ascend 驱动就绪后由管理员显式执行。

`stub.ko` 仅是 `devmm_*` 的占位实现，调用时会失败（`ENOSYS`）；它位于
`build/kernel/stub/stub.ko`，保留用于构建和诊断，不能替代真实 Ascend 驱动，也不是
`p2p_dev.ko` 的构建依赖。完成验证后由管理员按实际模块依赖顺序卸载模块。
