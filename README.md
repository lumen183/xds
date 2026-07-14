# XDS：NVMe 到 Ascend NPU 的 P2P 文件读取

XDS 尝试把 NVMe 上普通文件的物理块直接读到 Ascend NPU 内存，避免经过用户态 CPU
缓冲区的常规 `read() -> memcpy/to(device)` 数据路径。它由 Linux 内核模块、Python C
扩展和测试脚本组成。

> 这是面向特定 Linux 内核、NVMe 驱动和 Ascend 驱动组合的实验性集成项目，不是通用
> 文件 I/O 驱动。编译成功、模块可加载或 Python 可导入，都不等于 P2P 读已正确完成。
> 真机结果必须同时通过 I/O 完成检查和数据校验。

## 先说结论：当前 bench 能说明什么

当前 `tools/xds.sh bench` 对 **single API** 可用于：

- 验证一段已分配、已写入的本地文件是否能被读入 NPU；
- 比较相同机器、相同文件系统、相同参数下的端到端趋势；
- 探索请求大小和并发数的较优组合。

它目前**不能单独证明**“NVMe/NPU 的理论峰值”，也不应把一次结果作为严谨性能结论：

| 项目 | 当前行为 | 对结果的影响 |
| --- | --- | --- |
| 计时范围 | 从 `read_file` 提交到 `drain_read` 返回 | 包含 Python/C 扩展调用、打开文件和块设备、FIEMAP、ioctl、内核请求创建与完成等待；不只是盘的读取时间。 |
| 不计入计时 | 文件生成、`fsync`、NPU 分配、预热、最终 NPU→CPU 拷贝和校验 | 结果是“热态 P2P 请求端到端吞吐”，不是应用全流程吞吐，也不是纯盘带宽。 |
| 文件映射检查 | FIEMAP 预检查已按 256 段分页遍历 | 碎片文件超过 256 个 extent 不再被误判为映射结束；碎片仍会降低真实性能。 |
| 每次运行的测试文件 | `--data-dir` 模式会新建并删除文件 | 不同运行可能得到不同物理布局；正式横向比较应使用同一个、已验证的 `--file`。 |
| I/O 完成错误 | 驱动当前会记录完成错误，但 `drain_read()` 未把错误回传给用户态 | **必须修复后才可把 PASS 当作严格传输成功。** 目前默认字节校验能发现多数错误，但不是完成状态的替代品。 |
| batch API | 内核 batch 处理在最后一个请求后仍访问下一个地址/长度元素 | **当前不应将 batch 结果用于正确性或性能结论，需先修复越界访问。** |

因此，当前可信的使用方式是：先用 `smoke` 和带默认校验的 `single` bench 验证链路；
把 `--no-verify` 的结果只视为调参的候选值；完成下文“必须修复项”后，再报告峰值。

## 目录

- [基本概念](#基本概念)
- [数据路径与工作原理](#数据路径与工作原理)
- [环境与构建](#环境与构建)
- [真机快速开始](#真机快速开始)
- [如何正确测试](#如何正确测试)
- [如何找峰值吞吐](#如何找峰值吞吐)
- [磁盘、文件系统与 FIEMAP](#磁盘文件系统与-fiemap)
- [多 NPU、多磁盘、RAID 与卷管理](#多-npu多磁盘raid-与卷管理)
- [当前必须修复项](#当前必须修复项)
- [故障排查](#故障排查)
- [Python API 与 mock](#python-api-与-mock)

## 基本概念

### NVMe、块设备、文件系统和文件

`/dev/nvme0n1` 是一个 NVMe **块设备**，它只认识从 0 开始的扇区号。ext4、XFS 等
**文件系统**在它上面管理目录、文件名和空闲空间。一个普通文件在逻辑上连续，但它在
盘上可能被分为许多不连续的物理区间，称为 **extent（区段）**。

XDS 不能把文件偏移直接当成 NVMe 扇区号。它必须通过 FIEMAP 取得下列映射：

```text
文件逻辑偏移 0..8 MiB  ── FIEMAP ──>  块设备物理偏移 A..A+8 MiB
```

若文件是稀疏文件、有洞、含尚未写入的预分配 extent，或文件系统不提供稳定的物理映射，
该路径不成立。网络文件系统、tmpfs、overlay 文件系统和错误的块设备都不适合作为真机
性能数据源。

### P2P、in-flight 与 batch

- **P2P（peer-to-peer）**：NVMe 控制器以 NPU 内存的物理地址为目标直接 DMA；目标是
  避免数据先落到 CPU 普通内存再复制到 NPU。
- **in-flight**：已提交但尚未完成的请求数。增大它通常可提高队列利用率，直到 NVMe、
  PCIe、NPU 内存或软件开销饱和；它不是越大越好。
- **batch**：一次 ioctl 提交多个连续请求的 API。它与 `single --inflight N` 不是同一
  测试；前者测批处理实现，后者测多个独立提交的并行度。
- **warmup**：不计入结果的预热轮次，用于避开首次分配、频率爬升、缓存和队列初始化的
  影响。

### 吞吐、延迟和“峰值”

吞吐是 `成功传输的字节数 / 计时耗时`。本项目输出 GiB/s，其中
`1 GiB = 1024^3 bytes`。单次延迟是一次迭代从提交到 `drain_read()` 完成的时间；在
有多个 in-flight 请求时，它是一个批次延迟，不是单条 NVMe 命令延迟。

“峰值吞吐”至少应明确它是哪一种：

1. **XDS 端到端热态峰值**：本项目目前可测的指标，包含软件提交开销；
2. **设备路径峰值**：只计驱动/NVMe 完成，需内核 tracepoint 或性能计数器；
3. **NVMe 原始顺序读峰值**：用标准工具测到的存储设备能力，不等于 P2P；
4. **应用全流程吞吐**：还包括文件准备、CPU/NPU 同步、后处理等。

报告数字时必须同时写出这四类中的哪一类、请求大小、并发、文件系统、设备型号、是否
校验、迭代数和统计方式。

## 数据路径与工作原理

```text
普通文件（ext4/XFS 等）
        │ FIEMAP：文件偏移 -> 块设备物理 extent
        ▼
file_p2p Python C 扩展
        │ ioctl(IOCTL_READ_FILE / IOCTL_READ_FILE_BATCH)
        ▼
/dev/p2p_device ── p2p_dev.ko
        │ 取得 Ascend NPU 内存物理页，构造 NVMe read + P2P SGL
        ▼
NVMe 控制器 ── PCIe DMA ──> Ascend NPU tensor
        │
        ▼
drain_read() 等待完成；torch.npu.synchronize()；可选完整字节校验
```

当前内核代码把一个 NVMe 命令限制为 128 KiB（`HW_LIMIT_SIZE`），大请求会被拆成多条
NVMe 命令。这是 XDS 的实现限制，不是磁盘容量上限。

## 环境与构建

需要 Linux、Bash、CMake 3.16+、C 编译器、GNU Make、Python 3.8+。真实路径还需要：

- 与运行内核精确匹配的 kernel headers/build 目录；
- 可用的 Ascend 驱动、CANN、PyTorch/`torch_npu` 组合；
- 一个 NVMe 块设备及其本地文件系统；
- 有权限加载模块的管理员账户。

以 openEuler/RHEL 系为例：

```bash
sudo dnf install gcc make cmake bash python3 python3-devel \
  kernel-devel-$(uname -r) kernel-headers-$(uname -r)
ls -l /lib/modules/$(uname -r)/build/Makefile
```

构建入口：

```bash
./build.sh [-X on|off] [-P] [-t build|run] [-i on|off]
```

| 参数 | 含义 |
| --- | --- |
| `-X on` | 构建真实 `p2p_dev.ko`（默认）。 |
| `-X off` | 构建 Python mock，不需要 Ascend、NVMe 或 kernel headers。 |
| `-P` | 构建 Python C 扩展。 |
| `-t run` | 构建并运行测试；真实模式只做扩展 API smoke，不访问真机设备。 |
| `-i on` | 增量构建。 |

例如：

```bash
# 没有真机时，验证 API 语义
./build.sh -X off -t run

# 目标机编译真实模块和扩展
KDIR=/lib/modules/$(uname -r)/build ./build.sh -X on -P
```

若内核启用 module versions，额外提供 Ascend 驱动的符号文件：

```bash
ASCEND_MODULE_SYMVERS=/path/to/Module.symvers ./build.sh -X on -P
```

`stub.ko` 仅用于构建/诊断；它的 `devmm_*` 占位实现会失败，不能作为真实 Ascend 驱动。

## 真机快速开始

先确认文件系统与目标块设备一致：

```bash
findmnt -no SOURCE,FSTYPE --target /mnt/nvme
lsblk -o NAME,TYPE,SIZE,MOUNTPOINTS /dev/nvme0n1
```

当前实现只应在文件系统直接位于**单个未分区 NVMe namespace** 的场景使用。例如文件系统
直接创建在 `/dev/nvme0n1` 上，则 `/mnt/nvme` 的 SOURCE 和 `--bdev` 都应为
`/dev/nvme0n1`。分区、LVM、RAID、device-mapper 和加密卷的限制见
[多 NPU、多磁盘、RAID 与卷管理](#多-npu多磁盘raid-与卷管理)。然后：

```bash
./tools/xds.sh setup

# 第一条真机验证：小、单请求、完整校验
./tools/xds.sh smoke \
  --bdev /dev/nvme0n1 --data-dir /mnt/nvme --size 8M \
  --devid 0 --vfid 0

# 正确性优先的 single 测试
./tools/xds.sh bench \
  --bdev /dev/nvme0n1 --data-dir /mnt/nvme --size 256M \
  --api single --inflight 1 --warmup 20 --iterations 100 \
  --verbose --json single-baseline.json

./tools/xds.sh cleanup
```

不要在带 `--verbose` 的命令上采集最终峰值；它会额外输出阶段日志。`--verbose` 的用途
是定位文件、FIEMAP、NPU 分配和 I/O 阶段的问题。

## 如何正确测试

### 第一步：确认数据源

对自动生成的文件，脚本会写入确定性模式、`fsync`、检查分配字节数、检查 FIEMAP，并
确认文件系统来源与 `--bdev` 一致。FIEMAP 日志中的 `coverage=complete` 表示整个请求
范围已映射；`total_mapped_extents` 是累计的 extent 数。

若使用 `--file`，文件必须满足：

1. 位于目标 NVMe 的本地文件系统；
2. 大小不少于 `offset + size * 请求数`；
3. 完整写入、非稀疏、无未写入 extent；
4. 内容为脚本期望模式：文件偏移 `p` 的字节值为 `p & 0xff`。

普通 `fallocate` 往往产生“未写入 extent”，不满足第 3 条，也不会生成第 4 条的数据。

### 第二步：验证，再测速

依次做：

1. `smoke --size 8M`：确认一条真实读和字节校验；
2. `bench --api single --inflight 1`：获得单请求基线；
3. 每次只改变一个变量（`--size` 或 `--inflight`）；
4. 每组先保持默认校验；
5. 用 `--no-verify` 做候选峰值搜索后，以相同参数再运行一次默认校验。

默认校验位于正式计时之后，所以它不会降低报告的 P2P 计时吞吐，但会保证最终一次写入的
数据与模式一致。

### 第三步：保存足够的上下文

每个结果至少保存 JSON、命令行、内核版本、CANN/torch 版本、设备型号、挂载方式及
`findmnt` 输出。遇到失败时再保存：

```bash
dmesg -T | tail -n 200
findmnt -no SOURCE,FSTYPE,OPTIONS --target /mnt/nvme
cat /sys/block/nvme0n1/queue/{logical_block_size,physical_block_size,max_hw_sectors_kb,max_sectors_kb,nr_requests}
```

## 如何找峰值吞吐

以下是得到“XDS 端到端热态峰值”的最小严谨流程；在修复完成错误回传和 batch 越界问题前，
只使用 `single`。

1. 让系统空闲：停止同盘 I/O、记录 CPU governor/温度/PCIe 拓扑，避免 NVMe 热降速。
2. 使用同一个已验证测试文件，或至少在同一空闲文件系统上重复生成；不要把不同盘、不同
   挂载选项或不同碎片状态的数字放在一起比较。
3. 固定 `warmup=50`、`iterations=300`，扫请求大小：64M、256M、1G。
4. 对每个大小扫 `inflight`：1、2、4、8、16、32；分配的 NPU 内存为
   `size * inflight`，不可超过可用显存。
5. 每个组合至少跑三次，报告中位数，并保留 P50/P95/P99；不要只报最佳的一次。
6. 选出候选组合后去掉 `--verbose`，可用 `--no-verify` 做多轮吞吐采样；最后重新开启
   校验跑一次，并检查内核日志无 I/O 错误。

示例（候选峰值，不是最终可信报告）：

```bash
for size in 64M 256M 1G; do
  for qd in 1 2 4 8 16 32; do
    ./tools/xds.sh bench --bdev /dev/nvme0n1 --data-dir /mnt/nvme \
      --size "$size" --api single --inflight "$qd" \
      --warmup 50 --iterations 300 --no-verify \
      --json "result-${size}-q${qd}.json"
  done
done
```

这个循环会反复创建测试文件，适合初步寻优，不适合严格横向复现。正式报告使用同一个
`--file`，并在报告中写明文件 extent 数。

### 单卡不同读粒度测试

`--size` 是每个 P2P `read_file()` 请求的粒度，因此可直接测试 `4K`、`8K`、`32K`、
`64K`、`128K` 等。当前 C 用户库对小于 4 KiB 的 FIEMAP 容量计算有缺陷，驱动又以
512 B 扇区发命令；在该缺陷修复前，只使用 **4 KiB 的整数倍**。测试单卡、单请求粒度
时必须显式指定 `--api single --inflight 1`：不指定 `--api` 会额外运行当前未可用的
batch 路径。

先用默认校验确认每个粒度：

```bash
for size in 4K 8K 16K 32K 64K 128K; do
  ./tools/xds.sh bench --bdev /dev/nvme0n1 --data-dir /mnt/nvme \
    --devid 0 --size "$size" --api single --inflight 1 \
    --warmup 100 --iterations 1000 --json "verify-${size}.json"
done
```

随后为每个粒度传输近似相同的总字节数，重复三轮并取中位数。例如目标约 6.25 GiB：

| 请求粒度 | `--iterations`（`inflight=1`） |
| --- | ---: |
| 32K | 200000 |
| 64K | 100000 |
| 128K | 50000 |
| 256K | 25000 |

`128K` 是当前驱动定义的单条 NVMe 命令最大大小；`256K` 及以上请求会拆成多条命令。
实际拆分还可能受 NPU 物理页边界影响，所以“`--size=128K`”应称为 **P2P 请求粒度**，
不应无条件称为“一条 NVMe 命令”。

当前 runner 在每次迭代都重复读取同一段文件范围。它很适合比较小请求的 XDS 软件路径
开销，但 NVMe 控制器或盘内缓存可能使结果偏高，不能代表大数据集的冷读/流式吞吐。若要
验证后者，runner 应增加 `--working-set-size` 与顺序/随机 offset 模式：每次迭代从一个
远大于设备缓存的已验证文件中选择新范围，并在 JSON 记录工作集大小、stride、随机种子和
是否绕过缓存。该模式应在完成上述驱动正确性修复后实现。

### 单 NPU 顺序大文件扫描

`tools/single_npu_stream_bench.py` 用于单张 NPU 的顺序大文件扫描。它只使用稳定的
`read_file()`/`drain_read()` 路径：创建一个临时文件后，按每个 `size × io-depth` 组合从
头到尾读完整个文件。组合之间复用同一个文件；不会为每组重新生成文件。

先准备 CANN 环境和真实 P2P 设备：

```bash
source /usr/local/Ascend/ascend-toolkit/latest/set_env.sh
./tools/xds.sh setup
python3 tools/single_npu_stream_bench.py \
  --bdev /dev/nvme0n1 --data-dir /mnt/nvme \
  --file-size 3G --size 32K,64K,128K --io-depth 4,8,16,32 \
  --json stream-results.json
```

`--file-size` 默认 `3G`，并指定一次扫描读取的总字节数；`--size` 是每个 P2P 请求的
粒度，`--io-depth` 是每次 `drain_read()` 前连续提交的请求数。两项都支持一个值或逗号
分隔的多个值。默认扫描：

```text
--size     32K,64K,128K,256K,512K,1M
--io-depth 4,8,16,32,64,128
```

默认会在每组扫描之后额外验证文件的首、中、尾三个样本；验证不计入带宽计时。使用
`--no-verify` 可关闭它以只测性能。临时文件在脚本退出时删除；因此 `--data-dir` 必须位于
`--bdev` 对应的本地文件系统，并有至少 `--file-size` 的可用空间。

## 磁盘、文件系统与 FIEMAP

### 查看设备能力

```bash
dev=/dev/nvme0n1
name=${dev##*/}

# 容量（字节）与型号
blockdev --getsize64 "$dev"
lsblk -d -o NAME,SIZE,MODEL,SERIAL "$dev"

# Linux 块层对请求的限制，不等于设备宣传的顺序读带宽
cat /sys/block/"$name"/queue/{logical_block_size,physical_block_size,max_hw_sectors_kb,max_sectors_kb,nr_requests,read_ahead_kb}

# 安装 nvme-cli 后查看控制器和 namespace 能力
nvme id-ctrl -H "$dev"
nvme id-ns -H "$dev"
```

没有一个单独的“磁盘最大值”。容量、接口带宽、NVMe 队列深度、Linux 块层最大请求、
文件系统碎片度、PCIe 拓扑和 NPU P2P 能力共同决定最终吞吐。

### 查看 extent 与碎片

```bash
filefrag -v /mnt/nvme/input.bin
```

extent 少且连续通常有利于顺序读；extent 多会增加 FIEMAP、用户态传递和驱动逐段提交的
开销。FIEMAP 分页修复解决的是“检查被 256 段截断”的正确性问题，不能消除碎片本身。

## 多 NPU、多磁盘、RAID 与卷管理

下面的矩阵描述的是**当前代码实际覆盖范围**，不是硬件是否理论上可能支持。

| 场景 | 当前状态 | 原因与正确用法 |
| --- | --- | --- |
| 一张 NPU + 一个 NVMe namespace | 条件支持 | 唯一应进行真机验证的基线。文件系统应直接位于该 namespace，文件在测试期间不得改变。 |
| 多张 NPU 读取同一个文件 | 未由脚本覆盖；可作为后续验证项 | 每次命令仅分配一个 `npu:<devid>` tensor，只接受一个 `--devid`。驱动会把 `devid`/`vfid` 传给 Ascend `devmm`，且每次打开 `/dev/p2p_device` 有独立等待队列；这为“多个进程、各自一张卡、读同一只读文件”提供了基础，但没有并发编排、拓扑检查、聚合吞吐统计或真机回归测试。不要据此宣称已支持多卡。 |
| 多张 NPU 读取不同文件 | 未覆盖 | 与上一项相同；还需要验证各卡 NPU 内存、NVMe 队列和 PCIe 根端口之间不存在不可预期争用。 |
| 多块独立 NVMe | 单命令不支持 | CLI 只有一个 `--bdev`、一个文件和一个目标 tensor。可分别运行单盘测试，但没有多盘聚合、负载分配或跨盘一致性测试。 |
| NVMe 多 namespace | 不支持 | 用户态把 `nsid` 硬编码为 `1`。若目标是 namespace 2 或更高，命令可能读错 namespace；必须从打开的块设备解析实际 NSID 并传入驱动。 |
| GPT/MBR 分区，如 `/dev/nvme0n1p1` | 不支持 | 驱动直接以 `fe_physical >> 9` 构造 NVMe `slba`，没有加上分区起始扇区。脚本允许“分区的父盘”这一检查不足以保证正确，可能读到错误位置。 |
| Linux 软件 RAID（`/dev/md*`，RAID0/1/5/6/10） | 不支持 | RAID 是虚拟块设备；它需要把逻辑地址拆成成员盘和条带地址。当前驱动直接构造 NVMe 命令，不能让 md 层完成映射。RAID0 尤其需要按条带向多个控制器发请求。 |
| LVM、device-mapper、多路径（`/dev/mapper/*`、dm-*） | 不支持 | 当前只做了一层 `lsblk PKNAME` 检查，没有将逻辑块地址解析至真正物理 NVMe namespace；直接绕过映射层会读错地址或绕过策略。 |
| dm-crypt/LUKS | 不支持且不应绕过 | 文件系统看到的是解密后的逻辑数据，底层 NVMe 是密文；直接 NVMe P2P 读不会自动解密。 |
| 硬件 RAID | 取决于它向 Linux 暴露的设备 | 若控制器向 Linux 暴露为一个受当前 NVMe 驱动和 `nvme_setup_cmd` tracepoint 管理的单一 namespace，理论上可按单盘验证；普通 RAID HBA/SCSI 设备不在本驱动覆盖范围内。 |
| btrfs、ZFS 等 CoW/多设备文件系统 | 未验证，不支持声明 | 文件逻辑地址、校验、压缩、镜像和多设备映射不能由当前“FIEMAP + 单 bdev”假设完整表达。 |

### 多卡读同一文件应怎样设计

这是合理的生产场景：例如多张 NPU 都读取同一份模型或训练数据。它与“一个卡把同一份数据
复制给其他卡”不同；每张卡都要有自己的目标 NPU buffer，每张卡各自向 NVMe 发读请求。
文件只读时，多个读取者本身不会互相破坏数据，瓶颈通常是 NVMe 队列、PCIe 上游链路或
CPU/驱动提交开销。

要正式支持它，应新增一个多卡 runner，而不是在 shell 中随意并发启动现有脚本：

1. 接受 `--devid-list 0,1,...`，为每张卡分配独立 tensor、独立 `/dev/p2p_device` fd；
2. 在同一屏障后开始，分别记录每卡字节数、完成状态、P50/P95，另计算聚合 GiB/s；
3. 对每张卡完整校验或抽样校验，并确保每张卡的 `devid/vfid` 与 tensor 对应；
4. 用 `lspci -tv`、`npu-smi info`（若环境提供）记录 PCIe/NPU 拓扑，分别测试“同根端口”
   与“不同根端口”；
5. 先验证一张卡，再验证两张卡，最后逐张增加；吞吐不再增长或延迟急升的位置就是有效并发上限。

### 若要支持 RAID/LVM，驱动需要怎样改

不能只放宽 `check_bdev()`。正确方案需要在内核块层解析映射，或明确只接收最终的物理 NVMe
namespace，并确保地址转换正确：

1. 从块设备取得真实 NVMe controller 和实际 NSID，取消硬编码 `nsid=1`；
2. 对分区加上 `bdev` 的起始扇区，或拒绝分区设备；
3. 对 dm/LVM/md 使用块层映射 API 将每段逻辑 sector 拆为 `(底层 bdev, sector, length)`；
4. RAID0/5/6/10 可能把一个 extent 分散到多个成员盘，必须按成员控制器分别建立 NVMe
   请求和完成追踪，不能向 md 虚拟队列塞 NVMe 专有命令；
5. dm-crypt、压缩、校验和 CoW 文件系统不能绕过其转换层；需要相应的设备端解密/文件系统
   集成，否则明确拒绝。

在这些改动和逐层真机测试完成前，最安全的策略是：脚本显式拒绝分区、md、dm、LVM、加密卷、
非 NVMe 块设备和 `nsid != 1` 的设备，而不是给出可能错误的“通过”。

## 当前必须修复项

在把此项目作为稳定 benchmark 或产品路径前，至少完成并测试以下修改：

1. **回传 drain 中的首个 I/O 错误。** `p2p_drain_read()` 目前统计 `err_cnt`，但最后
   返回 `0`。应保存第一个 `issue_err` 或完成状态转换得到的 errno，并在 drain 后返回
   负 errno；同时补充“NVMe 完成失败必须让 bench FAIL”的测试。
2. **修复 batch 的末尾越界。** `do_read_ios_batch()` 在处理最后一个请求后递增 `idx`
   并读取 `addr_off[idx]`/`align_size[idx]`。在递增后先判断 `idx == desc.count`，到达
   末尾即停止更新下一请求状态。
3. **在 C 用户库校验 FIEMAP 覆盖。** `read_file()` 和 `read_file_batch()` 当前只拒绝
   `total_size > requested_size`，也应拒绝 `total_size < requested_size`，否则不应提交不
   完整的 extent 列表。
4. **处理小于 4 KiB 的请求。** C 用户库以 `size >> 12` 计算 `fm_extent_count`，小于
   4 KiB 时可能得到 0；改为向上取整，并显式拒绝不满足块/扇区对齐的请求或实现尾部
   字节处理。
5. **为性能报告增加元数据。** JSON 应记录 extent 总数、文件系统类型、块层队列限制、
   内核/CANN 版本、是否开启校验，以及计时边界版本。
6. **收紧设备拓扑检查。** 只允许经验证的单一 NVMe namespace；拒绝分区、md、dm/LVM、
   dm-crypt 和非 NSID 1 的设备，直到完成上述地址/namespace 映射支持。
7. **新增多卡测试器。** 多进程或多设备并发必须有屏障、每卡校验、聚合指标和 PCIe 拓扑
   记录；不要把多个独立命令的输出简单相加。

修复后应加入 mock 单元测试和真机回归测试：成功、NVMe 完成失败、FIEMAP 不完整、256+
extent、batch 最后一项、非 4 KiB 请求、不同 in-flight。

## 故障排查

| 现象 | 首先检查 |
| --- | --- |
| `FIEMAP extents do not cover...` | 文件是否有洞/未写入段；`filefrag -v`；确认 `coverage=complete`。 |
| `mapped_extents=256` | 这只是单页数量；查看之后是否继续有下一页及最终 `coverage=complete`。 |
| 文件与块设备不一致 | `findmnt -no SOURCE,FSTYPE --target /mnt/nvme`，不要把分区、LVM、DM 映射误传成不对应的盘。 |
| `/dev/p2p_device` 不存在 | `lsmod | grep p2p_dev`、`dmesg -T | tail -n 200`，并重新执行 `setup`。 |
| `devmm_*` 未定义 | Ascend 驱动未加载、版本不匹配，或构建时缺少正确 `Module.symvers`。 |
| 结果波动很大 | 同盘有其他 I/O、温度降频、CPU/PCIe 省电状态、文件布局变化、样本过少。 |
| 数据校验失败 | 保留 JSON 和内核日志；检查 offset/size、NPU tensor 生命周期、`drain_read()`、`torch.npu.synchronize()`、驱动版本与 batch 连续性。 |

## Python API 与 mock

真实扩展的核心接口：

```python
fd = file_p2p.new_p2p_fd()
ret = file_p2p.read_file(
    fd, file_name, bdev_name, file_offset, npu_address, size, devid, vfid
)
ret = file_p2p.drain_read(fd)
file_p2p.close_p2p_fd(fd)
```

`read_file()` 只提交，`drain_read()` 才等待。调用者必须让目标 NPU tensor 保持存活到
drain、NPU 同步和校验结束。

无 Ascend 环境可使用 mock 验证 API 语义：

```bash
./build.sh -X off -t run
```

mock 不会验证真正的 NVMe、PCIe、FIEMAP 或 Ascend P2P DMA；它不能替代真机结果。
从构建产物目录手工导入时：

```bash
PYTHONPATH="$PWD/build/python" python3 your_program.py
```

更详细的命令参数与实现约定见 [docs/TEST_GUIDE.md](docs/TEST_GUIDE.md)，历史改动见
[docs/MODIFICATION.md](docs/MODIFICATION.md)。
