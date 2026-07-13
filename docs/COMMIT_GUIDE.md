# XDS 两次提交说明

本文用于将当前工作区拆分为两个独立提交。提交操作由维护者手工执行；本次工作不会自动执行 git add、git commit 或推送。

## 提交边界

| 提交 | 内容 | 目的 |
| --- | --- | --- |
| Commit 1 | C 内核/用户态源码和 Python C 扩展源码修正 | 修复源码兼容性、日志和 API 文档问题，不引入构建框架或测试后端。 |
| Commit 2 | CMake、build.sh、Makefile、mock、测试、文档和加载辅助脚本 | 引入统一构建流程、无 Ascend 测试能力和开发文档。 |

建议不要把 build/ 构建产物加入任何提交。当前工作区已有的未追踪 .gitignore 不在本拆分范围内，除非维护者明确决定单独加入。

---

## Commit 1：源码修正

### 建议提交标题

    fix: improve kernel and Python source compatibility

### 文件范围

    p2p_dev.c
    file_p2p/file_p2p_api.c
    file_p2p/py_file_p2p_api.c

### 每处改动及理由

#### p2p_dev.c

1. 增加 linux/version.h。

   需要根据目标内核版本选择 blk_execute_rq_nowait 的函数签名。

2. 为 blk_execute_rq_nowait 增加条件编译：Linux 5.9 及更新版本使用 disk、req、at_head、done，更早版本保留 queue、disk、req、at_head、done。

   当前 Linux 5.15 headers 下旧调用会因参数类型错误而失败；条件编译修复当前构建，同时保留旧内核路径。

3. 修正 pr_warn 和 pr_info 的格式化类型。

   unsigned int、u64 和 unsigned long 混用会触发内核编译警告；内核构建通常启用 -Werror，警告会升级为错误。

4. 删除 p2p_drain_read 中未使用的 time 和 size 局部变量。

   消除无效变量和编译警告，避免误导后续维护者。

5. 对 g_size / g_time 增加零值保护。

   统计日志不能因为时间统计值为零而触发除零；该改动只影响统计输出，不改变 I/O 提交逻辑。

#### file_p2p/file_p2p_api.c

1. 删除 noooo 调试输出及其 fflush。

   该输出不是稳定错误信息，会污染正常调用方的标准输出；边界截断逻辑保留。

2. 修正 total_size 和 param->size 的错误日志格式。

   原格式符与实际整数类型不匹配，在 64 位环境下会产生编译警告并可能输出错误信息。

#### file_p2p/py_file_p2p_api.c

1. 增加 stdlib.h。

   文件使用 malloc 和 free，应显式包含其声明，避免隐式声明和编译器兼容性问题。

2. 修正 read_file_batch 的扩展模块文档字符串。

   原文错误地描述成八个参数；实际 C 解析器接收的是：
   
       read_file_batch(dev_fd, file_name, bdev_name, requests)

   requests 的每一项为 (bdev_offset, addr, size)。这次只修正文档，不改变 Python C 扩展的参数解析行为。

### Commit 1 验证与提交命令

    git add p2p_dev.c \
            file_p2p/file_p2p_api.c \
            file_p2p/py_file_p2p_api.c
    git diff --cached --check
    git diff --cached --stat
    git commit -m "fix: improve kernel and Python source compatibility"

该提交不依赖新构建框架；完整构建验证属于 Commit 2。

---

## Commit 2：构建框架、mock、测试和文档

### 建议提交标题

    build: add CMake workflow and Ascend-free mock tests

### 文件范围

    Makefile
    CMakeLists.txt
    build.sh
    go.sh
    file_p2p/mock_file_p2p.py
    tests/mock/test_file_p2p.py
    tests/real/test_import.py
    README.md
    docs/MODIFICATION.md
    docs/COMMIT_GUIDE.md

### 每处改动及理由

#### Makefile

增加 KERNELRELEASE 条件，区分 Kbuild 内部阶段和外部调用阶段；保留 obj-m 声明，并提供 modules/clean 目标。这样既保留 Linux 外部模块兼容入口，也允许 CMake 为两个模块生成隔离的 Kbuild 目录；日常入口统一为 build.sh。

#### CMakeLists.txt

1. 增加 XDS_ASCEND、XDS_BUILD_PYTHON 和 XDS_BUILD_TESTS 选项，拆分真实后端、Python 组件和测试组件。
2. 将 p2p_dev.c 和 stub.c 分别复制到 build/kernel/ 下独立目录编译。若两个模块在同一次 Kbuild 中生成，p2p_dev.ko 会被自动标记为依赖 stub.ko；真实 Ascend 路径应该由真实驱动提供 devmm_*，不能绑定返回 ENOSYS 的 stub。
3. 支持 XDS_ASCEND_MODULE_SYMVERS；未提供时使用 KBUILD_MODPOST_WARN，让开发机可以完成编译并明确显示未解析的 devmm_* warning。
4. 使用 FindPython3 构建真实 file_p2p C 扩展，输出到 build/python/，不执行全局安装。
5. 使用 CTest 注册 mock 测试和真实 Python smoke test。

#### build.sh

1. 集中解析 -X、-P、-t 和 -i，避免用户进入不同目录执行不同构建命令。
2. 默认使用 -X on 和 -i off，确保默认真实构建和完整、可复现的重建。
3. -X off 自动构建 mock，跳过 kernel headers 和内核模块。
4. -t run 自动补齐测试依赖后执行 CTest。
5. 不执行 insmod、rmmod，避免编译和测试隐式修改系统内核状态。

#### file_p2p/mock_file_p2p.py

实现与真实扩展一致的主要 API，使用普通文件和 bdev_offset 模拟数据源，以 addr 保存模拟设备内存，并通过 drain_read 体现排队/完成顺序；get_buffer(addr) 提供硬件环境不存在的可观察验证点。

#### tests/mock/test_file_p2p.py

覆盖单次读取、批量读取、drain 前后数据可见性、无效 handle、文件不存在、读取越界和关闭后的生命周期行为。这些测试不依赖 Ascend、NVMe 或 root。

#### tests/real/test_import.py

只验证真实 Python C 扩展导入及公开函数存在，不打开 /dev/p2p_device，避免普通测试隐式触发真实硬件 I/O。

#### go.sh

默认模块路径改为 build/kernel/p2p_dev/p2p_dev.ko，增加路径和 tracepoint 检查，保留为人工加载辅助脚本，不参与构建。这样 CMake 不再把模块产物写入源码根目录，同时保留已有的手工加载习惯。

#### README.md

补充构建参数、mock 示例、测试语义、产物位置、Module.symvers 配置和真机限制，让新开发者可以从干净工作区直接完成 mock 流程，并明确真实模块的环境边界。

#### docs/MODIFICATION.md 与本文档

MODIFICATION.md 完整记录功能和代码改造；COMMIT_GUIDE.md 说明提交边界和逐处修改理由。两者分开便于使用文档和代码审查文档分别维护。

### Commit 2 验证与提交命令

    bash -n build.sh go.sh
    ./build.sh -X off -t run
    ./build.sh -P -t run
    ./build.sh -X off -i on -t build
    git add Makefile \
            CMakeLists.txt \
            build.sh \
            go.sh \
            file_p2p/mock_file_p2p.py \
            tests/mock/test_file_p2p.py \
            tests/real/test_import.py \
            README.md \
            docs/MODIFICATION.md \
            docs/COMMIT_GUIDE.md
    git diff --cached --check
    git diff --cached --stat
    git commit -m "build: add CMake workflow and Ascend-free mock tests"

预期结果：mock 测试通过；真实 Python C 扩展导入/API smoke test 通过；有匹配 kernel headers 时两个 .ko 可以生成；没有 Ascend Module.symvers 时的 devmm_* warning 属于预期现象；不会自动加载或卸载模块。

## 当前工作区文件归属

    Commit 1（源码）
    ├── p2p_dev.c
    ├── file_p2p/file_p2p_api.c
    └── file_p2p/py_file_p2p_api.c

    Commit 2（构建/测试/文档）
    ├── Makefile
    ├── CMakeLists.txt
    ├── build.sh
    ├── go.sh
    ├── file_p2p/mock_file_p2p.py
    ├── tests/mock/test_file_p2p.py
    ├── tests/real/test_import.py
    ├── README.md
    ├── docs/MODIFICATION.md
    └── docs/COMMIT_GUIDE.md

不应提交：

    build/
    *.ko
    *.o
    *.mod.c
    Module.symvers
    modules.order
