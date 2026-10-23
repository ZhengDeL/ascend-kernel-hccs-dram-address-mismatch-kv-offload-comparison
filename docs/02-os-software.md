# AMD + 910C + 灵衢：BIOS 固定时的 OS / 驱动研究与现场证据

核查日期：2026-10-23。本文是源码与官方文档分析，尚未连接用户服务器，也未执行内核、BIOS、驱动、交换机或服务变更。

## 1. 当前结论

**先保留 MemCache 方案，排除 OS / 驱动分配失败，再判断地址可达性。** 用户已确认 AMD CPU、910C NPU、灵衢组网、BIOS 不能升级或改动，并提供wk1的FusionOS 23 / 23.3.10、ID_LIKE=openEuler、内核5.10.0-316.0.0.219.u200.fos23.x86_64。具体发行版补丁、HDK、错误码和Host DRAM地址图仍未知；不能把ID_LIKE当成与某原版openEuler二进制相同。原始文章中另一现场的“扫描 0 GB、已确认窗口无重叠”，不能移植成此服务器的已证事实。

存在一个特别值得优先核对的软件兼容性分支：MemFabric 官方 FAQ 记录了较新内核对 `alloc_contig_range()` 的 GFP 参数校验变化与 HDK 不适配，可能导致最小 DRAM 分配也失败。这种故障与实际 DRAM 根本不在 HCCS 地址窗口内，是不同问题。反过来，NUMA、HugeTLB、`memmap=` 只能管理已有资源；没有证据证明它们可以把 AMD 已由内存控制器解码的 DRAM 物理布局搬入另一高地址窗口。

**因此既不能直接承诺“改 OS 就能原速解决”，也不能在未采集现场数据时宣告“软件完全无解”。** 需要并行保留：支持的软件组合 / 驱动修复、现有可达内存的定向分配、保留 MemCache 的其他数据路径、厂商支持的映射扩展四个分支。

## 2. 证据与版本冻结

| 来源 | 本次核对版本 | 使用边界 |
| --- | --- | --- |
| [原始调研文章](https://github.com/MarsChan8293/ai_infra_summary/blob/main/docs/ascend-kernel510-hccs-dram-address-mismatch-kv-offload-comparison.md) | 原文标注 2026-10-23 | 二手研究输入；其中的用户现场不是本任务服务器证据 |
| [MemFabric GitCode 官方源码](https://gitcode.com/Ascend/memfabric_hybrid/tree/8df6c8f3e1ca1ddfce2dc903d0780300278c1b84) | `8df6c8f3e1ca1ddfce2dc903d0780300278c1b84`，已在本地读取 | 本次主要官方实现与 FAQ；不等于用户已安装版本 |
| [Ascend GitHub 镜像](https://github.com/Ascend/memfabric_hybrid/tree/004d9317289fe99bd6bf13def0500b3fa3795ccc) | `004d9317289fe99bd6bf13def0500b3fa3795ccc` | 与 GitCode 当前树不同；仅用来核对原文章旧路径，不能混用依赖或缺陷结论 |
| Linux 上游 | `v5.10`、`v6.6`；另核对特定提交 `f6037a4a...` | 发行版可 backport；必须用服务器实际源码包复核 |

以下链接均在 2026-10-23 读取；GitCode 网页读取受限时，以已克隆官方 GitCode 仓内相同 commit 的文件为核查对象。

## 3. 高优先级：已有官方记录的 OS / HDK 兼容性分支

[MemFabric FAQ 的 HalMemCreate / A3 可用内存部分](https://gitcode.com/Ascend/memfabric_hybrid/blob/8df6c8f3e1ca1ddfce2dc903d0780300278c1b84/docs/zh/wiki/FAQ/FAQ.md) 明确区分：可达窗口容量不足、碎片化、其他进程争用，以及较新 6.6 内核的连续页分配参数兼容问题。FAQ 提到 openEuler `6.6.0-114.0.0` 合入相关变化，但此发行版版本点尚未用 openEuler 打包历史独立复核，现场应查实际源码而非仅按 `uname` 判断。

FAQ 引向 Linux 提交 [f6037a4a686523dee1967ef7620349822e019ff8](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=f6037a4a686523dee1967ef7620349822e019ff8)（作者日期 2024-12-03；[上游 GitHub 同一提交](https://github.com/torvalds/linux/commit/f6037a4a686523dee1967ef7620349822e019ff8)）。源码增加 `__alloc_contig_verify_gfp_mask()`：不支持的 GFP 位返回 `-EINVAL`。`__GFP_NORETRY`、`__GFP_ACCOUNT` 不在允许集合；FAQ 使用的 `__KA_GFP_*` 是其驱动兼容层描述，不能把两套符号名字机械替换。

**现场成立条件：** 实际内核包含此变化或等效 backport；实际 HDK 的调用参数触发校验；同一时刻的 plog / 驱动 / 内核日志能够把上层 `HalMemCreate` 失败关联至该调用。仅出现 `ret: -6` 不能证明这一根因，上层错误码可能已经转换。

**处置顺序：** 优先选择厂商已适配的 HDK 与当前内核组合；若暂时没有，再研究厂商支持的驱动小补丁，或受支持内核的受控切换。需兼容用户“BIOS 固定”的约束。未经实际调用链定位，不建议删除内核校验、全量回退上游提交、强改 GFP 标志或直接升级至任意 6.6。内核与 NPU 驱动 ABI、签名、CANN 配套和回滚启动项均须同步确认。

证据置信度：**官方已记录 + 上游变更已核实；是否命中本现场未知。** 此项是具体、可检验的 OS 修复线索。用户当前提供的是FusionOS 5.10，因此不能直接按较新6.6案例认定命中；只有实际源码存在等效backport并且错误链吻合时才适用，暂不建议升级或回退内核。

## 4. 先分清 CPU PA、DMA IOVA 与 MemFabric GVA

[Linux DMA API 指南](https://docs.kernel.org/core-api/dma-api-howto.html) 区分 CPU 虚拟地址、CPU 物理地址和设备总线地址。驱动通过 DMA API 获取设备地址，IOMMU 有时可把它转换到实际物理页。因此，一般意义上的“地址数字不同”并不足以证明设备不能访问。

但本案必须继续证明 **该条 910C/HCCS 入站访问路径是否经过 AMD IOMMU、是否使用普通 DMA API、哪个 Requester ID / PASID 发起访问、HDK 是否支持把目标映射置入该域**。不能由“AMD 支持 IOMMU”直接推导“任意 HCCS 全局地址都能映射到任意 Host DRAM”。[Linux x86 IOMMU 说明](https://docs.kernel.org/arch/x86/iommu.html) 解释了 ACPI IVRS 的设备范围信息，并提醒部分 peer-to-peer 事务不按普通 DMA 翻译；这并不是对本机 HCCS 的定论，而是必须核实真实链路的原因。

MemFabric 本身已有 GVA / HVA / DVA 映射管理。当前 GitCode 的 [`hybm_mem_segment.cpp`](https://gitcode.com/Ascend/memfabric_hybrid/blob/8df6c8f3e1ca1ddfce2dc903d0780300278c1b84/src/hybm/csrc/mm/hybm_mem_segment.cpp) 与 [`hybm_vmm_based_segment.cpp`](https://gitcode.com/Ascend/memfabric_hybrid/blob/8df6c8f3e1ca1ddfce2dc903d0780300278c1b84/src/hybm/csrc/mm/hybm_vmm_based_segment.cpp) 包含 910C 的 VMM DRAM 分配路径，经 HAL 分配 `MEM_P2P_DDR_TYPE`。这些 API 的存在不等于消除了可达窗口限制；官方 FAQ 仍要求 DRAM 物理区间与固定 HCCS 窗口求交。

所需验证是驱动与硬件的地址契约，而不是给用户态 `mmap()` 返回一个更高的指针。驱动版本识别还读取 `version.info` 中 `Innerversion=`，见 [`hybm_gva_version.cpp`](https://gitcode.com/Ascend/memfabric_hybrid/blob/8df6c8f3e1ca1ddfce2dc903d0780300278c1b84/src/hybm/csrc/driver/version/hybm_gva_version.cpp)；只报告安装包显示的 `25.5.x` 不够。

| 提议 | 能解决的情况 | 本案必须满足的额外证据 |
| --- | --- | --- |
| AMD IOMMU IOVA 重映射 | 普通 DMA 地址受限且流量经过可编程 IOMMU 的情况 | HDK 支持、实际 Requester/域可控、HCCS 路径确实经过它；当前未证实 |
| MemFabric GVA / HAL 映射 | 软件已有受支持的全局虚拟地址到可达内存映射 | 分配和远端导入均成功；不等于移动 CPU PA |
| OEM 提供 OS / 驱动侧地址转换支持 | 硬件存在可编程窗口且提供正式软件接口 | 对应服务器 SKU、寄存器所有权、地址范围、驱动/固件版本、支持声明 |
| 用户态固定 VA / `LD_PRELOAD` | 只改变虚拟地址布局 | 无法单独改变总线或内存控制器的物理解码 |

**IOMMU 参数不能盲改：** [内核参数文档](https://docs.kernel.org/admin-guide/kernel-parameters.html) 中 `iommu=pt` 是默认旁路，`iommu.passthrough=0` 才表示默认翻译；两者都不会凭空生成本案所需的 HCCS 映射。AMD GPU 的推荐参数也不能套用到 Ascend 驱动。

## 5. 扫描 0 GB 应如何解释

当前官方 [`mem_scan.py`](https://gitcode.com/Ascend/memfabric_hybrid/blob/8df6c8f3e1ca1ddfce2dc903d0780300278c1b84/src/smem/python/memfabric_hybrid/memfabric_hybrid/mem_scan.py) 已迁移到 Python 包，CLI 为 `mfcli mem scan`。它检查当时空闲页、地址交集和对齐后容量；**静态可达容量、当时空闲容量、能成功 HAL 分配的容量是三个数**。默认 1 GiB 粒度与 `-m 2` 的结果应分别保留。

先对 `lsmem` / `/proc/iomem` 的 `System RAM` 与匹配该版本的四个窗口做静态求交。若静态交集为正而空闲扫描为零，应先调查占用、碎片、NUMA 选择、预留大页和内核兼容性；若静态交集为零，应带着 CPU PA 与设备地址契约向厂商核实映射支持。仅凭扫描器输出不能合并这两个分支。

版本陷阱：GitHub 镜像旧 `script/mem_scan.py` 的配置路径曾以字符串逐字符遍历；GitCode 当前版已改为列表。旧代码打印地址还存在 PFN / 字节二次换算的静态问题；当前并行 CLI 使用另一函数，不能把兼容函数 `run_for_node()` 的旧问题归到当前 CLI 的主路径。采集时必须交付实际执行脚本、SHA256、参数及原始输出，分析不能只写文件名。

另一个需要避免的误判是“BUDDY 只有块头页，所以扫描必错”。Linux [v5.10 `fs/proc/page.c`](https://github.com/torvalds/linux/blob/v5.10/fs/proc/page.c) 与 [v6.6 同文件](https://github.com/torvalds/linux/blob/v6.6/fs/proc/page.c) 的 `stable_page_flags()` 都额外查询 `is_free_buddy_page()`，可为块内其他空闲页导出标志。[当前 pagemap 说明](https://docs.kernel.org/admin-guide/mm/pagemap.html) 也已澄清 Linux 4.6 之后的语义；老版本文档仍有仅块头的描述，应以服务器相应源码为准。扫描期间页状态也会变化，读到的不是原子快照。

## 6. NUMA / 大页 / memmap 的实际边界

| 软件手段 | 合理用途 | 不能由此保证的效果 |
| --- | --- | --- |
| NUMA 绑定、线程和内存亲和 | 从已可达 NUMA 节点分配，减少跨 socket 搬运 | 不改变 DIMM 的 CPU PA 或 HCCS 窗口 |
| 1 GiB / 2 MiB 大页，较早分配池 | 提升连续分配成功率、减少映射压力；前提是版本支持 | 不把窗口外 DRAM 搬到窗口内；错误预留还可能减少 buddy 可用页 |
| 内存整理、回收 | 解决真实碎片 / 占用分支 | 不解决静态地址无交集；会影响在线负载，不能默认执行 |
| `memmap=...` / `mem=...` | 限制、保留或修正 OS 已知内存区域 | 不能创建本来不存在的物理 DRAM，也不负责重编程 AMD 地址译码 |
| memory offline / online | 管理已存在的物理块并迁移可移动页 | 不移动硬件地址；可能因为固定页无法下线 |
| `swiotlb` / bounce buffer | 普通 DMA API 的兼容中转机制 | 不等于原 HCCS 直访，额外 CPU 拷贝；未证实适用于此 HAL 路径 |

上述边界分别参考 [HugeTLB](https://docs.kernel.org/admin-guide/mm/hugetlbpage.html)、[Memory Hotplug](https://docs.kernel.org/admin-guide/mm/memory-hotplug.html)、[SWIOTLB](https://docs.kernel.org/core-api/swiotlb.html)。`memmap=` 是给 Linux 的内存图描述，不能把高地址空洞伪报成 RAM；错误描述可能造成访问异常或数据破坏。这里不提供可直接套用的启动参数。

AMD 专项还应记录 SME / SEV 是否实际激活，而非只看 CPU 是否支持。内存加密会影响物理地址位数及 DMA 约束，见 [AMD Memory Encryption](https://docs.kernel.org/arch/x86/amd-memory-encryption.html)。[AMD Address Translation Library](https://docs.kernel.org/admin-guide/RAS/address-translation.html) 则用于把 RAS 上报的 normalized 地址翻译成系统 PA，并非通用的 DRAM 重定位工具。

## 7. 必须向服务器收集的内容

第一轮只读采集，不重启、不修改 BIOS、不清缓存、不停止现有服务。保留每条命令时间、退出码、标准输出与错误输出，权限不足也要原样记录。

| 分类 | 必需内容 | 用来决定什么 |
| --- | --- | --- |
| 服务器 / CPU | 厂商、精确 SKU、主板、BIOS 版本日期、AMD 型号/stepping、socket 数、内存总量与 DIMM 拓扑；公开包去序列号 | 是否在支持矩阵内、物理布局是否与参考机相同 |
| OS | `/etc/os-release`、`uname -a`、内核完整 RPM/DEB 名、`/proc/cmdline`、实际 config、matching kernel-source 的 `mm/page_alloc.c` 与发行版补丁清单 | 核实 GFP 校验 backport、ABI 与页配置 |
| 地址图 | root 权限的 `/proc/iomem`、`lsmem --all` 原始输出、NUMA memory block 归属/online 状态、启动时 E820 / EFI 内存图 | 与匹配版本窗口静态求交；区分红acted为0和真实地址 |
| NUMA / 页 | `numactl -H`、各节点 meminfo、`/proc/buddyinfo`、`/proc/pagetypeinfo`、全局/节点 HugeTLB 数及 THP 状态、swap 状态 | 容量、粒度、碎片、NUMA 的分离诊断 |
| AMD IOMMU | AMD-Vi 初始化/故障日志、`/sys/kernel/iommu_groups` 与 NPU PCI BDF 归属、`lspci -Dnnk/-tv`、IVRS ACPI 表（已有 acpidump 时） | 流量可能经过哪个域，是否有 IO_PAGE_FAULT |
| NPU 栈 | `npu-smi info`、HDK `version.info` 含 Innerversion、固件、CANN 版本、驱动模块版本/参数、实际容器内可见版本和宿主机版本 | HAL / VMM 分支与匹配的支持条件 |
| 灵衢 | 超节点 ID、server ID、L1/L2 型号、LCN 软件版本、链路拓扑；网管提供只读诊断 | 超节点内可达性与组网软件条件 |
| MemFabric | 实际 wheel/run 包版本、安装路径、`libmf_smem.so` 的 build commit、脚本哈希、`mfcli --help`/扫描原始输出，1 GiB / 2 MiB 分开 | 避免运行旧扫描器或加载错库 |
| 失败链 | 第一次异常的完整上下文、调用的 HAL API、返回码、同一时段 MemCache/MemFabric 日志、plog、内核日志；最小池大小/NUMA配置 | 把注册、分配、导出、导入、xcopy 各阶段拆开 |
| 运行限制 | 进程 memlock、容器 cgroup/cpuset/memory 上限、`/dev/shm` 大小、设备挂载和库路径；凭据去除 | 排除容器限制和资源限制 |

首轮不需要内存内容转储、不需要 `/dev/mem` 扫描、不需要寄存器写入。IOMMU group 存在只能证明 OS 分组，不能证明 HCCS 实际经过该域。运行 `mfcli mem scan` 会遍历页状态并产生 CPU 开销，应在可接受时段执行；生产负载下所得容量只代表该时刻。

## 8. 收集后决策与性能边界

1. **静态交集大于 0，HAL 分配失败：** 优先核实 GFP 兼容性、驱动组合、页粒度、NUMA 与资源限制。可保留现有直访架构，性能风险通常比增加整段中转更容易控制，但尚无本现场数字。
2. **静态交集大于 0，分配成功但导入 / xcopy 失败：** 核实 superPod/server ID、设备可见性、HDK / 固件 / LCN、映射和权限。先做最小数据正确性闭环。
3. **静态交集为 0：** 先向 OEM / Ascend 提交 PA 图、实际失败链和明确 SKU，询问是否有该平台受支持的 OS/驱动地址转换方案及其限制；有正式支持才做隔离 PoC。暂无公开证据可承诺通用 AMD IOMMU 参数就能解决。
4. **原生直访不可达或无受支持修复：** 保留 MemCache API / 服务架构，评估官方 Host 数据路径与 staging；具体实现与模型接入以配套调研为准。不能把换 TCP 后能通写成 HCCS 原速修复。

用户已确认性能损耗最多5%：业务吞吐不低于同条件基线的95%，P99 TTFT和P99 TPOT均不超过基线105%；这是验收要求，绝不是已测结果。若本机原生 MemCache 不能运行，应分别对比“不开卸载的业务基线”和“同硬件受支持直访参考”，不要假造本机直访基线。具体对照与尾延迟口径见[性能验收](04-performance-gates.md)。

微基准至少覆盖双向搬运、多种 KV 块尺寸、单流与并发有效 GB/s、CPU 核占用、跨 socket 流量、NPU 临时显存、首次注册与稳态传输。业务层覆盖冷/热前缀、命中率、淘汰、并发、TTFT/TPOT/P99、回退重算与数据正确性。中转增加的拷贝是否显著影响性能，取决于实际 NUMA/PCIe/内存带宽、块大小与 overlap；只有现场结果能回答“不能差太多”。

## 9. 最终验收边界

本文没有证明用户服务器存在地址窗口不重叠，没有验证任何内核修复，也没有测得性能。已得到的实质进展是：找到官方的内核 GFP 校验兼容性分支、明确当前 GitCode 与旧 GitHub 扫描器版本差异、建立可区分软件分配失败与硬件地址契约的证据清单。下一阶段应先获取上述只读材料，再决定是否需要 OS 修复或更换数据路径。
