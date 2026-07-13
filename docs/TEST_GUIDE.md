# XDS Ascend 快速测试与性能测试设计

本文定义 XDS 在 Ascend 真机上的快速验证和性能测试脚手架。目标是把目前分散的
构建、环境检查、模块加载、设备内存分配、P2P 读、数据校验和结果统计串成一套
可重复的命令。

本文描述已实现脚手架的行为和使用约定，并作为后续改动的验收标准。

## 1. 要解决的问题

XDS 的真实路径不是一个普通的 Python 函数调用，原因在于它同时跨越了几个层次：

```text
Python file_p2p
    │ ioctl
/dev/p2p_device
    │
p2p_dev.ko
    │ NVMe tracepoint + P2P SGL
NVMe 控制器 / 块设备
    │
Ascend devmm
    │
torch_npu 分配的 NPU 内存
```

因此仅执行 Python import、编译 `.ko` 或打开 `/dev/p2p_device`，都不能证明真实
P2P 读链路可用。快速测试必须包含一次真实读写和设备端数据校验；性能测试必须
把数据校验从计时区间中排除。

## 2. 命令入口

计划提供统一入口：

```bash
./tools/xds.sh setup
./tools/xds.sh smoke [options]
./tools/xds.sh bench [options]
./tools/xds.sh cleanup
```

命令职责如下：

| 命令 | 作用 | 是否需要 root |
| --- | --- | --- |
| `setup` | 检查环境、构建产物、按需加载 `p2p_dev.ko` | 仅 `insmod` 使用 sudo |
| `smoke` | 生成测试文件，分配 NPU 内存，执行一次真实读并完整校验 | Python 进程通常不需要 root；设备权限需可访问 |
| `bench` | 预热、多轮提交、统计吞吐和延迟，结束后校验 | 同 `smoke` |
| `cleanup` | 删除脚本创建的临时文件，显式卸载模块 | `rmmod` 使用 sudo |

脚本不强制执行 `rmmod -f`。测试中有打开的 fd 或未完成 I/O 时，`cleanup` 应拒绝
强制卸载并提示仍在使用模块的进程。

## 3. 为什么需要构建和加载模块

### 3.1 构建模块做什么

`./build.sh -X on -P` 会：

1. 用匹配当前运行内核的 headers 编译 `p2p_dev.ko`；
2. 编译 `stub.ko`（仅用于构建/诊断，不提供真实 Ascend 能力）；
3. 编译真实的 `file_p2p` Python C 扩展；
4. 将产物放在 `build/`，不会自动安装到系统。

构建阶段需要 kernel headers；如果内核启用了 module versions，还需要真实 Ascend
`devmm` 提供者导出的 `Module.symvers`：

```bash
KDIR=/lib/modules/$(uname -r)/build \
ASCEND_MODULE_SYMVERS=/path/to/Module.symvers \
./build.sh -X on -P
```

没有 `Module.symvers` 时，构建允许 `devmm_*` 符号以 warning 形式未解析。这只表示
模块可以在目标机继续完成符号解析，不表示当前机器已经具备 Ascend 运行能力。

### 3.2 加载模块做什么

`insmod p2p_dev.ko` 的初始化过程会：

- 创建字符设备 `/dev/p2p_device`；
- 注册 `nvme_setup_cmd` tracepoint；
- 为符合条件的 NVMe 命令设置 `NVME_CMD_SGL_METABUF`，使其能使用 P2P SGL。

没有加载模块时，Python 的 `new_p2p_fd()` 无法打开 `/dev/p2p_device`。即使绕过设备
节点，NVMe 命令也没有被改造成目标设备内存的 P2P 形式，不能作为真实链路验证。

加载一次后可以复用。`smoke` 和 `bench` 不应每轮重复加载/卸载，否则会把模块管理
开销混进测试，且会增加 tracepoint 和设备状态切换的风险。

### 3.3 卸载模块做什么

`rmmod p2p_dev` 会注销 tracepoint hook、删除字符设备并释放模块资源。卸载不是每个
测试用例都必须执行，但在以下场景应显式执行：

- 测试结束；
- 更换或重新构建模块版本；
- 重载 Ascend/NVMe 驱动前；
- 需要确认系统回到测试前状态。

## 4. `setup` 的行为

`setup` 应按以下顺序执行，并在缺项时立即失败：

1. 检查 Linux、bash、CMake、Python 版本；
2. 自动发现并 source CANN 环境，例如：
   `/usr/local/Ascend/ascend-toolkit/set_env.sh`；
3. 检查 `torch` 和 `torch_npu` 可导入，确认 NPU 设备编号有效；
4. 检查 kernel build 目录，默认使用 `/lib/modules/$(uname -r)/build`；
5. 检查 `/proc/kallsyms` 是否存在 `__tracepoint_nvme_setup_cmd`；
6. 检查真实 `devmm_*` 符号是否由 Ascend 驱动提供；
7. 执行 `./build.sh -X on -P`，或在产物存在且配置未变化时复用增量产物；
8. 读取 tracepoint 地址；
9. 如果模块尚未加载，用 sudo 执行 `insmod`；
10. 验证 `/dev/p2p_device` 可打开。

环境变量覆盖：

```bash
CANN_ENV=/path/to/set_env.sh
KDIR=/path/to/kernel/build
ASCEND_MODULE_SYMVERS=/path/to/Module.symvers
PYTHON=python3
BUILD_JOBS=8
```

脚本不应把 Ascend 环境缺失自动降级为 mock。mock 必须通过显式的
`./build.sh -X off -t run` 使用。

## 5. 测试文件生成

`smoke` 和 `bench` 可以自动生成测试文件，但必须要求用户指定目标文件系统目录：

```bash
./tools/xds.sh smoke \
  --bdev /dev/nvme0n1 \
  --data-dir /mnt/nvme \
  --size 1G
```

这样做是因为当前目录可能位于系统盘、tmpfs 或网络文件系统；XDS 需要通过 FIEMAP
获取文件的物理位置，再将其转换为 NVMe 读取范围。

生成流程：

1. 在 `--data-dir` 下创建带唯一名称的临时文件；
2. 写入确定性测试模式，而不是只调用 `fallocate`；
3. `fsync` 后关闭写 fd；
4. 检查文件大小至少为 `--size`；
5. 检查文件不是稀疏文件，能够执行 FIEMAP；
6. 检查文件所在文件系统与 `--bdev` 一致；
7. 测试结束后删除临时文件，即使收到 SIGINT 也要尝试清理。

默认测试模式应能根据文件 offset 计算期望字节，例如：

```text
expected[i] = (file_offset + i) & 0xff
```

这样校验失败时可以报告首个错误的文件偏移、设备地址和实际/期望值。

用户也可以传入已有文件：

```bash
./tools/xds.sh smoke \
  --file /mnt/nvme/input.bin \
  --bdev /dev/nvme0n1 \
  --size 8M
```

`--file` 和 `--generate` 不能同时使用；未指定 `--file` 时，脚本在 `--data-dir`
自动生成并负责删除文件。

## 6. NPU 内存分配

脚手架使用 `torch_npu` 作为外部分配器，不在 C 扩展中绑定某个 CANN allocator：

```python
import torch
import torch_npu  # 新版可能由设备后端自动加载

device = torch.device(f"npu:{devid}")
buf = torch.empty(size, dtype=torch.uint8, device=device)
addr = buf.data_ptr()
```

必须保持 `buf` 变量存活到 `drain_read()` 和校验结束。调用流程：

```python
fd = file_p2p.new_p2p_fd()
file_p2p.read_file(fd, file_name, bdev, offset, addr, size, devid, vfid)
file_p2p.drain_read(fd)
torch.npu.synchronize()
result = buf.cpu()
file_p2p.close_p2p_fd(fd)
```

`torch_npu`、PyTorch 和 CANN 必须使用兼容版本；运行前应加载对应 CANN 的环境脚本。
版本配套和环境初始化以 [Ascend/pytorch 官方说明](https://github.com/Ascend/pytorch)
为准。

## 7. `smoke` 流程

`smoke` 的目标是回答：“这台 Ascend 机器上，真实 XDS P2P 读是否能完成且数据正确？”

完整流程：

```text
检查模块已加载
    ↓
生成或检查文件
    ↓
分配 uint8 NPU tensor
    ↓
调用 read_file（提交）
    ↓
调用 drain_read（等待完成）
    ↓
torch.npu.synchronize()
    ↓
拷回 CPU 并完整校验
    ↓
输出 PASS/FAIL 和错误定位
```

建议命令：

```bash
./tools/xds.sh smoke \
  --bdev /dev/nvme0n1 \
  --data-dir /mnt/nvme \
  --size 8M \
  --devid 0 \
  --vfid 0
```

失败时必须区分：环境检查失败、模块加载失败、文件/FIEMAP 失败、NPU 分配失败、
ioctl 失败、drain 失败和数据校验失败。

## 8. `bench` 流程

### 8.1 API 模式

支持两种模式：

```text
single  每个请求调用 read_file
batch   调用 read_file_batch，一次提交连续分块
```

未指定 `--api` 时，建议依次运行 single 和 batch，并分别输出结果。batch 仅用于
连续源文件范围；当前用户态实现按第一个请求的 `bdev_offset` 和
`size * param_num` 获取 FIEMAP，不能把任意跳跃 offset 当作通用批读。

### 8.2 in-flight 请求

驱动的 `read_file()` 只提交请求，`drain_read()` 才等待并回收，因此 bench 支持：

```bash
--inflight 1      # 默认，单请求基线
--inflight 8      # 常用吞吐测试
--inflight 32     # 峰值探索
```

一次迭代的提交方式是：

```text
连续提交 inflight 个 read_file
    或提交一个 batch
一次 drain_read
```

每个 in-flight 请求必须使用不重叠的 NPU 地址区域，避免完成顺序造成数据覆盖。

### 8.3 计时和校验

建议参数：

```bash
./tools/xds.sh bench \
  --bdev /dev/nvme0n1 \
  --data-dir /mnt/nvme \
  --size 1G \
  --api single \
  --inflight 8 \
  --warmup 5 \
  --iterations 100 \
  --json result.json
```

计时规则：

1. 分配 NPU buffer；
2. 执行 warmup，不计入结果；
3. 正式迭代只计 `read_file` 提交到 `drain_read` 完成的时间；
4. 计时区间内不做 NPU→CPU 拷贝；
5. 所有正式迭代完成后，默认执行一次完整校验；
6. 校验失败则整体结果为 FAIL；
7. `--no-verify` 仅用于探索纯性能上限，不应作为默认模式。

输出指标：

```text
API
请求大小
inflight / batch size
预热次数 / 正式迭代次数
有效数据量
端到端总耗时
平均带宽 GB/s
P50 / P95 / P99 延迟
校验结果
失败次数
```

其中端到端计时包括 XDS 的提交和 drain 开销，但不包括最终校验拷贝；脚本应明确
标注单位和计时边界，避免把结果误解为纯 NVMe 原始带宽。

## 9. 输出约定

stdout 只输出核心结果，例如：

```text
PASS api=single size=1GiB inflight=8 iterations=100 bandwidth=6.42GiB/s p50=1.21ms p95=1.38ms verify=ok
```

错误输出到 stderr，并包含可操作的下一步。详细结果通过显式参数保存：

```bash
--json result.json
```

JSON 至少包含：

```json
{
  "status": "PASS",
  "api": "single",
  "bdev": "/dev/nvme0n1",
  "file": "/mnt/nvme/xds-test.bin",
  "devid": 0,
  "vfid": 0,
  "size": 1073741824,
  "inflight": 8,
  "warmup": 5,
  "iterations": 100,
  "bytes": 107374182400,
  "elapsed_ns": 0,
  "bandwidth_bytes_per_sec": 0,
  "latency_ns": {"p50": 0, "p95": 0, "p99": 0},
  "verify": {"enabled": true, "status": "ok"}
}
```

## 10. 参数约定

公共参数：

```text
--bdev PATH              目标块设备，例如 /dev/nvme0n1
--data-dir PATH          自动生成测试文件的目录
--file PATH              使用已有文件，不自动生成
--size SIZE              读大小，支持 K/M/G 等后缀
--offset BYTES           文件起始偏移，默认 0
--devid N                Ascend device id，默认 0
--vfid N                 虚拟设备 id，默认 0
```

bench 参数：

```text
--api single|batch       API 模式；不指定时两种都测
--batch-size N           batch 中连续分块数量
--inflight N             未完成请求数，默认 1
--warmup N               预热次数，默认 5
--iterations N           正式迭代次数，默认 20 或由实现设定
--verify / --no-verify   默认校验；显式关闭校验
--json PATH              保存详细 JSON
```

## 11. 故障排查

### 找不到 kernel build 目录

```text
Kernel build directory is unavailable
```

安装与运行内核匹配的 headers，或设置：

```bash
KDIR=/path/to/kernel/build ./tools/xds.sh setup
```

### 找不到 `nvme_setup_cmd` tracepoint

检查：

```bash
awk '/__tracepoint_nvme_setup_cmd/ {print $1}' /proc/kallsyms
```

目标内核或 NVMe 驱动没有该 tracepoint 时，当前模块不能直接运行，需要做内核适配。

### `devmm_*` undefined 或 `Unknown symbol`

这表示真实 Ascend devmm 提供者没有加载、版本不匹配，或构建时没有提供正确的
`Module.symvers`。`stub.ko` 不能替代真实驱动。

### `/dev/p2p_device` 不存在

查看模块状态和内核日志：

```bash
lsmod | grep p2p_dev
dmesg | tail -n 80
```

确认 `setup` 使用了包含 tracepoint 地址的 `insmod` 参数。

### FIEMAP 失败或文件和块设备不一致

确认文件位于本地 NVMe 文件系统，并将 `--data-dir` 换成目标 NVMe 的挂载目录。不要
用系统盘、tmpfs、NFS 或无法提供物理 extents 的文件系统做真实测试。

### 数据校验失败

保留 JSON 结果和内核日志，优先检查：

1. NPU tensor 在整个 I/O 生命周期内是否存活；
2. 是否调用 `drain_read()` 和 `torch.npu.synchronize()`；
3. `devid` 与 tensor 所在设备是否一致；
4. 文件 offset/size 是否越过文件末尾；
5. batch 请求是否为连续范围；
6. NVMe、Ascend 驱动和内核版本是否匹配。

## 12. 实现验收标准

脚手架完成后应满足：

1. `setup` 能给出清晰的环境缺项，并能复用已加载模块；
2. `smoke` 能自动生成文件、完成真实 P2P 读并报告校验结果；
3. `bench` 支持 single/batch、warmup、iterations 和 inflight；
4. bench 计时不包括最终 NPU→CPU 校验拷贝；
5. bench 默认校验，`--no-verify` 必须显式指定；
6. stdout 输出简洁核心指标，`--json` 保存详细结果；
7. `cleanup` 不强制卸载，能够处理模块仍被占用的情况；
8. 没有 Ascend 的机器仍可通过既有 `./build.sh -X off -t run` 运行 mock 测试；
9. 文档中的命令与实际脚本参数保持一致。
