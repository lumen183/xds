# FIEMAP extent 内偏移读取错误分析与修复

## 1. 文档目的

本文记录 `tools/single_npu_stream_bench.py` 在真实 NPU/NVMe P2P 测试中出现数据校验失败的现象、证据、根因分析、代码修改范围及验证计划，供代码评审和真机回归使用。

本文对应的最小代码修复位于：

- `file_p2p/file_p2p_api.c::read_file()`；
- `file_p2p/file_p2p_api.c::read_file_batch()`。

Python benchmark、内核驱动和 UAPI 均未修改。

## 2. 故障现象

顺序扫描完成后，独立 sample 校验 pass 报错：

```text
INFO phase=verify.alloc mode=sample size=32768 io_depth=4
    buffer_bytes=131072 addr=0x12c041200000 alignment=0
INFO phase=verify.batch mode=sample batch=0 requests=4
DEBUG phase=verify.mismatch data verification failed:
    mode=sample
    batch=0
    slot=0
    request_index=11062
    request_size=32768
    file_offset=362479616
    first_index=0
    first_file_offset=362479616
    device_address=0x12c041200000
    mismatch_count=32647
    actual_window=0x10e7b994158bf419
    expected_window=0x3009f6f4dfe3bd92
    pattern=splitmix64-v1
    pattern_seed=0x58d5a17e20260715
```

关键数值换算如下：

| 字段 | 十进制 | 十六进制 |
|---|---:|---:|
| 请求大小 | 32768 | `0x8000` |
| 请求文件偏移 | 362479616 | `0x159b0000` |
| 目标 NPU 地址 | - | `0x12c041200000` |

请求大小、文件偏移和目标地址均满足 4 KiB 对齐，文件偏移也满足 32 KiB 对齐。因此，该现象不符合普通的请求地址或文件偏移未对齐问题。

## 3. 数据指纹分析

benchmark 使用 `splitmix64-v1` 生成与绝对文件偏移相关的确定性数据。对固定 seed 而言，可以根据一个完整的 64-bit pattern word 反推出其对应的 word index。

将实际读取到的前 8 字节：

```text
10 e7 b9 94 15 8b f4 19
```

按 little-endian 解释并逆向 SplitMix64 后得到：

```text
word_index       = 44040192
actual_byte_offset = word_index * 8
                   = 352321536
                   = 0x15000000
```

与请求位置比较：

```text
实际数据位置 = 0x15000000
请求数据位置 = 0x159b0000
位置差值     = 0x009b0000
```

这说明目标 NPU buffer 中不是 canary 或随机垃圾，而是测试文件中另一个确定位置的有效 pattern 数据。

sample 第一批请求位置为：

```text
slot 0: 0x159b0000
slot 1: 0x213c0000
slot 2: 0x00088000
slot 3: 0x3d250000
```

`0x15000000` 不属于同批其他请求，因此也不符合 batch 内请求结果写错 slot 的特征。

## 4. 数据路径

本次读取经过以下路径：

```text
single_npu_stream_bench.py
    file_offset = args.offset + request_index * request_size
        |
        v
file_p2p.read_file(..., file_offset, device_address, length, ...)
        |
        v
file_p2p/file_p2p_api.c
    FS_IOC_FIEMAP
    文件逻辑 offset -> 块设备物理 extent
        |
        v
p2p_dev.c
    sector = extent.fe_physical >> SECTOR_SHIFT
        |
        v
NVMe read -> NPU physical memory
```

Python runner 在提交和校验时使用同一个绝对文件偏移，并在 `drain_read()` 后执行 `torch.npu.synchronize()`，没有发现 Python 层偏移计算错误。

内核驱动直接使用用户态传入的 `fe_physical` 构造 NVMe sector。因此，FIEMAP extent 在进入内核前是否已裁剪到请求位置，直接决定最终读取位置。

## 5. 根因

### 5.1 原有代码

普通文件执行 `FS_IOC_FIEMAP` 后，原代码使用如下方式调整第一个 extent：

```c
exts->fm_extents[0].fe_physical += param->bdev_offset % 4096;
exts->fm_extents[0].fe_length -= param->bdev_offset % 4096;
```

这段代码假设只需补偿请求位置在 4 KiB 页内的偏移。

### 5.2 FIEMAP extent 语义

FIEMAP 返回的是覆盖查询范围的 extent 描述。首个 extent 的 `fe_logical` 可能早于查询的 `fm_start`，`fe_physical` 则对应该 extent 的起点。

因此，请求位置在首 extent 内的正确偏移是：

```text
extent_offset = requested_offset - extent.fe_logical
```

而不是：

```text
extent_offset = requested_offset % 4096
```

### 5.3 与本次数据的对应关系

数据指纹表明实际数据来自 `0x15000000`，而请求位置是 `0x159b0000`。这高度符合如下 FIEMAP 映射情况：

```text
first_extent.fe_logical = 0x15000000
requested_offset        = 0x159b0000
extent_offset           = 0x009b0000
```

原计算结果为：

```text
0x159b0000 % 4096 = 0
```

因此 `fe_physical` 没有前移，驱动从首 extent 起点对应的物理 sector 开始读取，最终得到 `0x15000000` 的数据。

需要注意：故障日志没有直接打印真机 FIEMAP 的 `fe_logical`。`0x15000000` 是通过实际数据指纹严格反推出的数据来源位置；其恰好是首 extent 起点属于基于代码路径的强推断，最终可通过真机增加 FIEMAP 日志确认。

## 6. 修复方案

将首 extent 的调整量改为请求位置相对 `fe_logical` 的偏移：

```c
exts->fm_extents[0].fe_physical +=
    param->bdev_offset - exts->fm_extents[0].fe_logical;
exts->fm_extents[0].fe_length -=
    param->bdev_offset - exts->fm_extents[0].fe_logical;
```

同样修改 `read_file_batch()`：

```c
exts->fm_extents[0].fe_physical +=
    param[0].bdev_offset - exts->fm_extents[0].fe_logical;
exts->fm_extents[0].fe_length -=
    param[0].bdev_offset - exts->fm_extents[0].fe_logical;
```

本次 benchmark 只调用 `read_file()`。同步修改 `read_file_batch()` 是因为两条路径包含相同的错误公式；若只修复单请求接口，batch API 仍可能在请求落入大 extent 中部时读取错误位置。

## 7. 修改范围

代码修改仅涉及一个文件：

```text
file_p2p/file_p2p_api.c | 12 ++++++++----
1 file changed, 8 insertions(+), 4 deletions(-)
```

实际语义是两处相同的偏移公式替换，新增行数主要来自代码换行。

未修改以下内容：

- `tools/single_npu_stream_bench.py`；
- pattern 生成和验证算法；
- NPU buffer 分配和地址计算；
- `drain_read()` 和同步流程；
- `p2p_dev.c` 内核模块；
- ioctl/UAPI 结构；
- FIEMAP 空洞、无 extent 或覆盖不足等通用错误处理。

分析过程中曾考虑增加 FIEMAP 完整覆盖检查等防御性逻辑，但这些属于独立的健壮性改造。为控制本次修复范围和评审风险，最终未包含这些改动。

## 8. 已完成验证

### 8.1 C 代码编译检查

```bash
gcc -std=gnu11 -Wall -Wextra -Werror -fsyntax-only \
    file_p2p/file_p2p_api.c
```

结果：通过。

### 8.2 现有自动化测试

```text
mock_file_p2p  PASS
stream_report  PASS

2/2 tests passed
```

mock 测试不执行真实 FIEMAP、NVMe 或 NPU P2P DMA，因此只能证明本次修改未破坏现有 mock/API 和报告逻辑，不能替代真机正确性验证。

## 9. 真机回归计划

### 9.1 重新构建

在原测试机器重新构建真实 `file_p2p` C 扩展：

```bash
./tools/xds.sh setup
```

### 9.2 重跑原始用例

使用产生本次错误的原始参数重新运行：

```bash
python3 tools/single_npu_stream_bench.py \
    --bdev <NVME_BLOCK_DEVICE> \
    --data-dir <NVME_MOUNT_DIR> \
    --file-size 1G \
    --size 32K \
    --io-depth 4 \
    --verify sample
```

如原始命令还包含其他 size、io-depth 或输出参数，应保持原参数完整重跑。

### 9.3 验收标准

- sample verification 通过；
- `file_offset=0x159b0000` 不再得到 `0x15000000` 对应的 pattern；
- 不再出现接近整个 request 的随机分布 mismatch；
- 原测试覆盖的其他 `size × io-depth` 组合均通过；
- 如条件允许，再执行一次 `--verify full`。

## 10. 后续诊断建议

若最小修复后仍失败，应首先在 `FS_IOC_FIEMAP` 返回后临时记录：

```text
requested_offset
fm_mapped_extents
first_extent.fe_logical
first_extent.fe_physical
first_extent.fe_length
requested_offset - first_extent.fe_logical
```

这可以直接验证真机文件系统返回的首 extent 是否以 `0x15000000` 开始。

之后再依次排查：

1. `--bdev` 是否确实对应文件所在文件系统；
2. 块设备是否为分区，以及驱动是否正确处理分区起始 sector；
3. NVMe 完成状态是否由 `drain_read()` 正确回传；
4. NPU PA 查询和跨页地址推进是否正确；
5. 多请求并发时目标 slot 是否发生覆盖。

上述项目均不是当前日志的首要解释。本次数据指纹与错误偏移公式之间已经形成直接对应关系，因此应先完成最小修复的真机回归。

