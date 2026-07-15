# PCIe → HBM 理论带宽与拓扑查询

`tools/pcie_p2p_report.py` 用于在 XDS 目标机上查询 NVMe、PCIe bridge 和 Ascend NPU
的实际连接关系，并计算 NVMe 到每张 NPU 的 PCIe 单向理论带宽上限。

## 最简用法

不需要输入设备号、实测带宽、I/O size 或 io-depth：

```bash
python3 tools/pcie_p2p_report.py
```

命令会自动扫描本机并生成：

```text
xds-pcie-topology.json
xds-pcie-topology.html
```

将 `xds-pcie-topology.html` 拷到本地后直接用浏览器打开。HTML 不依赖网络或
JavaScript，PCIe 拓扑图以 SVG 内嵌在文件中。

如果只关心理论最值，看报告顶部的 **最佳路径 PCIe 理论最大值**，以及下方
“NVMe → NPU 路径矩阵”中的 **PCIe 硬上限** 即可。

## 自动采集的数据

工具从 sysfs 和常用系统命令收集：

- NVMe controller、namespace、型号及 PCI BDF；
- Huawei PCI vendor `0x19e5` 的加速器端点；
- 每个端点和 bridge 当前协商的 PCIe speed、width；
- PCIe 父子拓扑、NUMA node 和 IOMMU group；
- ACS Request/Completion Redirect 状态；
- `npu-smi info`、`npu-smi info -m`、`lspci`、`nvme list`、`lsblk` 和 `lscpu`
  的原始输出。

缺少 `nvme-cli`、`numactl` 或 `npu-smi` 时，脚本不会因此退出；对应原始诊断会标记为
缺失。理论链路计算的核心数据来自 `/sys/bus/pci/devices`。

## 理论带宽计算

报告展示单向带宽。PCIe 1.0/2.0 按 8b/10b 编码计算，PCIe 3.0 及以后按
128b/130b 计算：

```text
理论 GiB/s = GT/s × lane 数 × 编码效率 ÷ 8 ÷ 1024³
```

常见 PCIe 4.0 理论值：

| 链路 | 单向理论最大值 |
| --- | ---: |
| PCIe 4.0 x4 | 7.34 GiB/s |
| PCIe 4.0 x8 | 14.67 GiB/s |
| PCIe 4.0 x16 | 29.34 GiB/s |

一条 NVMe → NPU 路径的 PCIe 理论最大值，是路径中相关链路理论带宽的最小值。
例如 NPU 是 PCIe 4.0 x16、NVMe 是 PCIe 4.0 x4，单 NVMe 到该 NPU 的理论上限仍然是
7.34 GiB/s，而不是 29.34 GiB/s。

报告中的 **工程参考值** 默认是理论最大值的 90%，用于提醒 TLP/DLLP、流控和实现开销；
它不是理论硬上限。NPU/NVMe“端点链路求和”也只是完全独立链路下的宽松上界，不能代替
共享 PCIe switch、upstream 和 Root Complex 的路径分析。

## 如何看报告

重点看三个位置：

1. **最佳路径 PCIe 理论最大值**：本机扫描到的 NVMe → NPU 路径中的最高理论值；
2. **PCIe 拓扑**：确认目标 NVMe 与目标 NPU 是否处于同一 PCIe tree；
3. **NVMe → NPU 路径矩阵**：比较八张 NPU，找出同一 switch/root 下且瓶颈最高的路径。

出现以下提示时不能只相信带宽数字：

- `cross-root-or-unknown`：路径跨 Root Complex，或 sysfs 没有共同 PCI 父节点；
- `ACS Redirect`：P2P 请求可能被重定向到 Root Complex；
- `link unknown`：某个 bridge 没有公开当前链路信息；
- 自动识别的 Ascend 端点不是 8 个：需要检查 PCI class，必要时手工指定 BDF。

## 可选参数

下面这些参数都不是查询理论值所必需的。

指定输出文件名前缀：

```bash
python3 tools/pcie_p2p_report.py --output 910b4-topology
```

只分析特定 NVMe：

```bash
python3 tools/pcie_p2p_report.py --nvme nvme0
```

将 XDS 实测值与理论值放在同一报告中比较：

```bash
python3 tools/pcie_p2p_report.py --measured-gib 19.46
```

`--request-size`、`--io-depth`、`--transfer-size` 只用于解释 benchmark 一批积累了多少
在途数据及需要多少次 `drain_read()`，不会改变 PCIe 理论最大值。

## 自动识别失败

先列出 Huawei/Ascend PCI 设备：

```bash
lspci -Dnn | grep -Ei 'processing accelerators|ascend|19e5'
```

然后将八个 BDF 以逗号分隔传入：

```bash
python3 tools/pcie_p2p_report.py \
  --npu-bdf 0000:41:00.0,0000:42:00.0,0000:81:00.0,0000:82:00.0,0001:41:00.0,0001:42:00.0,0001:81:00.0,0001:82:00.0
```

这只是自动识别异常时的兜底操作，正常的 910B4 八卡机器不需要提供该参数。
