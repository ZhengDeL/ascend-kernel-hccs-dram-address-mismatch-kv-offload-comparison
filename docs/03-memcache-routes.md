# AMD CPU + 910C + 灵衢：保留 MemCache 的软件实现路线

核查日期：2026-10-23。本文是源码和文档分析，未连接或修改现场服务器，未进行 AMD + 910C 实机验收。BIOS 不可升级、不可调整是本次固定约束；更换内核也必须先证明对应缺陷，不能当作默认动作。

**最终目标**：按用户指定latest官方5.3/5.4节，两台A3服务器各16个逻辑NPU：21 Prefill DP2×TP8，22 Decode DP4×TP4。Meta在21，每个worker贡献20GB DDR，每机320GB、总640GB。用户镜像固定为`quay.io/ascend/vllm-ascend:v0.30.0rc2-a3`，权重`/var/ai-model/GLM-5.3-Flash-w8a8`，日志根`/var/ai-model/lzd`。性能损耗上限5%。wk1已提供FusionOS23.3.10与内核`5.10.0-316.0.0.219.u200.fos23.x86_64`，但HDK/物理地址图/首错仍缺。

当前默认脚本按用户要求保留官方`device_sdma`，不悄悄换协议。`host_shm`为后续隔离PoC候选：21的P实例可共享本机池，但它不能让22访问21的共享内存；Decode关闭Store读写也不表示跳过初始化。采用官方配置尚未解决未知地址/分配故障。

## 1. 先纠正路线优先级

用户的目标是 **MemCache 卸载可用，且性能退化可控**。先采集现场数据，优先排查可保留原传输路径的 OS / HDK 缺陷；确认需要更换数据路径后，再按下表进行隔离 PoC。原参考文章主要推荐 LMCache / Mooncake / YuanRong，但这些方案不能直接等同于“已实现 MemCache”。本轮新增的关键证据是：**MemCache 官方已有 A3 `host_shm` 单机 L2 池化备选方案，明确面向无法升级 HCCS 底软的现场**。P0 是后续候选验证顺序，不代表本地部署包已经切换协议，或已经满足5%性能要求。

| 优先级 | 路线 | 保留 MemCache | 保留什么能力 | 关键限制 |
|---|---|---|---|---|
| P0 | MemCache `host_shm`：NPU HBM ↔ 普通本机 Host DRAM | 是 | 同机多实例共享缓存、HBM 卸载、prefix reuse；PD 仍可使用现有独立传输 Connector | 每台机器独立 DRAM 池；不能把它称为跨机统一 Host DRAM 池；AMD PCIe / NUMA 性能待测 |
| P1，需 RDMA NIC | MemCache `host_rdma` | 是 | 跨机 DRAM 池和现有 Store API | 需要真实 Host RDMA 网卡、互通链路、HCOM；现有只有灵衢并不能推出具备此条件 |
| P2，功能基线 | MemCache `host_tcp` | 是 | 跨机 Store API、普通 TCP 网络传输 | HCOM、CPU/内存拷贝和网络带宽可能成为瓶颈；管理网不宜直接承诺生产性能 |
| 条件分支 | 修复已证实的 OS / HDK 分配缺陷，继续 `device_sdma` | 是 | 有条件保留 HCCS 直访 | 仅当真实 DRAM 与硬件窗口有交集且故障来自分配/驱动时成立；不能靠 OS 虚拟地址重映射创造不存在的物理窗口 |
| 研发分支 | MemCache + 自研 HCCS staging transport | 理论可保留 | 可能保留 Store 上层 API + HCCS 跨机传输 | 本轮固定版本源码没有发现可直接打开的 HIXL Buffer Pool 后端；需新增 transport / 调度 / 生命周期实现，不是改一个环境变量 |
| 备选 | Mooncake Ascend Direct Buffer Pool / YuanRong HIXL Buffer Pool | 否，若用其替换 Store | 官方已有中转缓冲思路 | 属于后端迁移，需要用户后续接受功能变化，不能算 MemCache 目标完成 |

**当前不能声称已经确认本用户机器的 `mem_scan=0GB` 或地址完全无交集。** 这些是原文章作者记录的现场输入；本用户机器仍需独立采集内存布局与原始错误。若确认无交集，P0 / P1 改数据路径；若有交集，则先检查分配碎片、NUMA、HDK 和具体内核缺陷。

本地既有旧材料的旁证：工作区 `set-a-1-2-7-8-9-39` 是 `device_sdma`、独立 LocalService 贡献 384GB、模型进程贡献 0GB、`world_size=1024`、`MultiConnector` 同时配置 Mooncake 与 AscendStore/MemCache。旧镜像标签为 `wings-vllm-ascend:glm5.x-a3-202609262230`。这些仅是本地旧脚本事实，不能覆盖本轮已经确认的新目标镜像与拓扑，也不代表线上配置；报告不发布其中真实 IP 或全量原脚本。

## 2. 官方 `host_shm` 是真实实现，不是推测

固定来源：MemCache `98fb4c303caeeb294bdc2db84667a87e54833acf`，MemFabric `8df6c8f3e1ca1ddfce2dc903d0780300278c1b84`。

1. MemCache 官方 [A3 PD 分离备选方案](https://gitcode.com/Ascend/memcache/blob/98fb4c303caeeb294bdc2db84667a87e54833acf/docs/zh/getting_started/tutorials/models/DSV4-Flash%2BA3-PD%E5%88%86%E7%A6%BB%EF%BC%88%E5%A4%87%E9%80%89%E6%96%B9%E6%A1%88%EF%BC%89.md) 使用 `protocol=host_shm`、每机独立 MetaService 和内存池、vLLM `backend=memcache`。PD 链路仍由 `MooncakeHybridConnector` 承担。该方案的单机池化与整个模型跨机 PD 是两个维度。
2. MemCache [协议映射源码](https://gitcode.com/Ascend/memcache/blob/98fb4c303caeeb294bdc2db84667a87e54833acf/src/memcache/csrc/common/mmc_smem_bm_helper.h) 将 `host_shm` 映射为 `SMEMB_DATA_OP_HOST_SHM`，也存在 `host_tcp` / `host_rdma` 的真实映射。
3. MemFabric [host_shm 数据面](https://gitcode.com/Ascend/memfabric_hybrid/blob/8df6c8f3e1ca1ddfce2dc903d0780300278c1b84/src/hybm/csrc/data_operation/host/hybm_data_op_host_shm.cpp) 的 `CopyDeviceToHost` / `CopyHostToDevice` 使用 `DlHybridApi::Memcpy` 和 `ACL_MEMCPY_DEVICE_TO_HOST` / `ACL_MEMCPY_HOST_TO_DEVICE`；批量路径先尝试 `AclrtMemcpyBatch`，失败再逐块拷贝。这是本机常规 D2H/H2D 路径，不是远端 HCCS 直访任意 Host DRAM。
4. [实体检查](https://gitcode.com/Ascend/memfabric_hybrid/blob/8df6c8f3e1ca1ddfce2dc903d0780300278c1b84/src/hybm/csrc/entity/hybm_entity_default.cpp) 对 HOST_SHM 禁止 HBM 池和与 SDMA/RDMA/TCP 等混用，并跳过 transport 扩展库及 transport manager 初始化。`host_shm` 数据面明确不支持其自身异步 copy；这不等于 vLLM 完全不能使用后台线程，但不能凭名称承诺拷贝计算重叠。
5. MemCache [配置说明](https://gitcode.com/Ascend/memcache/blob/98fb4c303caeeb294bdc2db84667a87e54833acf/docs/zh/memcache_config.md) 声明 host_shm：DRAM > 0、HBM = 0、无 HCOM；host 协议 DRAM 对齐 2MB。

**最小 PoC 原则**：在隔离环境中先建立每节点小容量 host_shm 池、`use_layerwise=false`，验证本机 NPU 数据写回取回，再接现有 AscendStoreConnector。现有 384GB 独立池 + 模型 0GB 贡献的模式不能机械照搬：需核对安装版本对“零贡献客户端 + host_shm”的参数检查、跨进程共享内存导入和设备可见性；官方案例采用每个 vLLM 进程贡献 DRAM。先按实际参与进程数测小池，之后再验证独立大池。

**功能变化须明确**：相对跨机共池，P0只提供同机池；重复前缀需被调度到已有缓存的节点才能获益。最终目标中两个Prefill实例都在21，因此P端同机复用具备位置条件；Decode直接跨机读21的池仍不支持。即使 PD 数据仍走 HCCS，也不能称外部 KV Pool 本身继续跨机 HCCS 直访。

### 2.1 对独立 Provider 384GB / 模型进程 0GB 的精确判断

当前官方文档写“host_shm requires DRAM > 0”，但本轮固定源码的实际检查更细，不能直接解释为“每个 client 必须分配 DRAM”：

- MemCache `mmc_configuration.h:547` 的 `ValidateLocalServiceConfig` 没有 host_shm 专门的 localDRAM>0 检查；它要求 `storage.enabled=true` 的进程 localDRAM>0，并检查 `max_dram_size >= localDRAMSize`。
- MemFabric `smem_bm.cpp:268` 的 `SmemBmCreateOptionCheck` 要求 **maxDramSize/maxHbmSize 不能同时为0**；允许 localDRAMSize=0。HOST_SHM 检查禁止 localHBM!=0 和混合协议。
- `smem_bm_entry.cpp:139` 明确把 localDRAM/localHBM 都为0的 rank 定义为 Proxy；`hybm_host_shm_segment.cpp:441` 对本地 size=0 成功跳过 MapLocalShm，导入时跳过对端 size=0的项。`AllocLocalMemory(0)` 不因0本身拒绝。

所以在这些固定 master 源码中，**同机 Provider>0 + model Proxy=0 结构有代码支撑**；不必为所有模型 worker 重复分配384GB，也不能把元数据用 `init_bm=False` 客户端混同于可读写数据的0GB Proxy。

准确迁移前置是：固定实际镜像中 MemCache/MemFabric 对应版本与源码，验证上述零贡献实现；Provider 配正 DRAM、HBM=0，所有参与者配置一致的协议/Meta/config store/world/max_dram（max_dram>0且覆盖Provider容量），模型进程localDRAM=0且SSD关闭；同机共享文件挂载/UID/IPC权限可导入；先两进程小池做NPU put/get。旧版本可能仍有“HBM/DRAM不能同时0”的参数限制或头文件旧注释，现场wheel未查前不能给无条件保证。

**跨节点限制不会被0GB Proxy绕过**：21的host_shm Provider只能供21上可导入其共享内存的进程访问；22的Proxy不能通过Meta TCP把共享内存变成远端访问。最终两机拓扑的P workers均在21，是host_shm候选成立的重要条件。

## 3. 镜像与版本不能从当前上游推断

官方上述 A3 案例使用 `vllm-ascend:v0.23.0rc1`，文中推荐 MemCache/MemFabric `1.1.5`，并给出一处 A3 lazy initialization 的兼容修补。当前克隆 MemCache `VERSION=1.5.0`，当前 vLLM-Ascend main 也有更多接口和修复。**这是不同版本证据，不能把新源码的全部能力直接归给旧案例或最终选定的rc2镜像。** 不要仅为了照抄案例把现有整套软件降到 1.1.5。

现场应提供：

- `docker image inspect` 的 ID / RepoDigest、镜像实际架构；容器中 Python / torch / torch-npu / vLLM / vLLM-Ascend / MemCache / MemFabric 版本、安装路径、wheel 哈希、源码 commit 和改动。
- 实际 `MMC_META_CONFIG_PATH` / `MMC_LOCAL_CONFIG_PATH`、三种进程（Meta / standalone LocalService / vLLM）的脱敏配置、启动参数和每类进程数。
- `memcache_hybrid` / `memfabric_hybrid` 的 `.so` 依赖列表，是否包含 `libhcom.so`，是否可实际接受 `host_shm`、是否存在批量读写所需接口。
- vLLM MemcacheBackend 实际代码是否使用当前 `DistributedObjectStore` / `batch_*_layers`；是否有本地 patch、重复 init、device ID 映射问题。仅 `pip show` 成功不代表该组合可运行。

当前 vLLM-Ascend [MemcacheBackend 源码](https://github.com/vllm-project/vllm-ascend/blob/4e6eac4e476d9be3476e99440f4cabed67b253ea/vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/backend/memcache_backend.py) 注册 NPU 缓冲并通过 `COPY_L2G` / `COPY_G2L` 调用 MemCache；不是自动执行一层 Python CPU staging。上层无需切换到另一个 Store 名称，但真正数据路径取决于 MemFabric 协议和实际安装版本。

官方 [vLLM-Ascend Store 文档](https://github.com/vllm-project/vllm-ascend/blob/4e6eac4e476d9be3476e99440f4cabed67b253ea/docs/source/user_guide/feature_guide/kv_cache_pool_ascend_store.md) 还说明：layerwise 需 MemCache >=1.3.0，且支持 Prefill 端；不能把它作为旧版本所有场景默认优化。不同 KV 物理布局升级时还可能要求新的 cache namespace，避免旧缓存命中但数据格式不兼容。

## 4. `host_rdma` / `host_tcp` 能否真正承载 NPU KV

这两种协议在 MemCache 与 MemFabric 中均有实现。MemFabric [HostComposeDataOp](https://gitcode.com/Ascend/memfabric_hybrid/blob/8df6c8f3e1ca1ddfce2dc903d0780300278c1b84/src/hybm/csrc/data_operation/host/hybm_compose_data_op.cpp) 对 HOST_RDMA / HOST_TCP / HOST_URMA 共用 HostDataOpRDMA。

[host_rdma 数据面](https://gitcode.com/Ascend/memfabric_hybrid/blob/8df6c8f3e1ca1ddfce2dc903d0780300278c1b84/src/hybm/csrc/data_operation/host/hybm_data_op_host_rdma.cpp) 实现 NPU 到远端 DRAM 的路径：`BatchWriteLD2RH` 先申请本机 Host swap 区、`BatchCopyLD2LH`，然后 `WriteRemoteAsync`；反向也有相应读取路径。所以并非只支持 CPU 到 CPU，但要付出本地 D2H/H2D 和网络传输代价。

依赖和限制：

- Host TCP / RDMA 依赖 HCOM。Host RDMA 必须有可用 RDMA NIC、verbs / GID、路由和对端，灵衢并不自动提供 Host NIC 的 RDMA 能力。
- `MF_HYBM_RDMA_SWAP_SPACE_SIZE` 单位为 MB，当前源码 NPU/GPU 默认 1024，Host TCP 共用此实现；必须按实际版本验证，不能把此变量当作 HCCS 中转开关。
- swap 池需覆盖传输块和并发；过小会失败，过大则每进程重复占内存。固定数据块、缓冲复用、批量聚合和 NUMA 定位比盲目增加线程更值得先验证。
- `host_rdma` 规避的是本次 HCCS Host 物理窗口前提，仍需满足 NIC DMA / IOMMU / 驱动本身要求。
- TCP 若实际经过低速管理网，则不应列为“性能不能差太多”的默认生产方案；先取链路速率和端到端有效带宽。

## 5. HIXL / ADXL Buffer Pool 能否直接加给 MemCache

**本轮固定版本未找到 MemCache / MemFabric 可直接选择的 HIXL Buffer Pool 配置。** `ASCEND_BUFFER_POOL` 来自 Mooncake Ascend Direct 的解析路径，不会因为 MemCache 也运行在同一个进程里就自动给 MemFabric `device_sdma` 增加 staging。

Mooncake 固定 SHA `dd1217b6c461491e7c73ff29b4ffad215e0a9ac0` 的 [TransferExecutorBase](https://github.com/kvcache-ai/Mooncake/blob/dd1217b6c461491e7c73ff29b4ffad215e0a9ac0/mooncake-transfer-engine/src/transport/ascend_transport/ascend_direct_transport/transfer_executor_base.cpp) 确认：解析 `ASCEND_BUFFER_POOL`，传递 `adxl.BufferPool`，缓冲池与异步模式互斥；[官方说明](https://github.com/kvcache-ai/Mooncake/blob/dd1217b6c461491e7c73ff29b4ffad215e0a9ac0/docs/source/design/transfer-engine/transport/ascend_direct_transport.md) 给出 `4:8` 示例。

因此有两种不同工作量：

1. 切换 AscendStore 后端到 Mooncake / YuanRong，采用其现有 Buffer Pool：是可研究的备选迁移；不满足“继续用 MemCache”这个严格目标。
2. 保留 MemCache Meta / Store API，自研 MemFabric transport，或在 MemCache 与其他 Transfer Engine 之间增加桥接：需处理 GVA ↔ 远端 DRAM对象映射、端点控制、分块、双缓冲、并发、NPU stream 同步、缓冲归还、故障 drain、重连和 CRC。还必须证明 NPU 缓冲路径不依赖原失配的 Host 物理窗口。没有完成这些工作前不能宣传只需环境变量即可低损耗 HCCS staging。

MemFabric [外部 Host DRAM 注册入口](https://gitcode.com/Ascend/memfabric_hybrid/blob/8df6c8f3e1ca1ddfce2dc903d0780300278c1b84/src/hybm/csrc/mm/hybm_asymmetric_mem_segment.cpp) 的 `CheckHostRegisterAllowed` 明确拒绝纯 SDMA 对外部 DRAM 的注册；不能简单先 `malloc` 再注册，就推断 `device_sdma` 可以访问任意 AMD DRAM。

## 6. 性能不能靠“走了灵衢”担保

官方 A3 host_shm 案例是 Atlas 900 A3 SuperPoD 四机 64 卡；脚本含 aarch64 运行库路径，并未建立 AMD x86 的同条件认证。它的可复核表格同时有收益和回退：

| 官方案例，输入/输出/并发/前缀复用 | 无池 TTFT | host_shm MemCache TTFT | 无池 TPOT | host_shm TPOT | 含义 |
|---|---:|---:|---:|---:|---|
| 64K / 1024 / 80 / 50% | 15627.7 ms | 9988.6 ms | 25.8 ms | 26.6 ms | TTFT 降约 36.1%，TPOT 增约 3.1% |
| 128K / 1024 / 80 / 30% | 57755.3 ms | 63597.4 ms | 24.0 ms | 23.7 ms | TTFT 增约 10.1%，低复用时会退化 |
| 128K / 1024 / 100 / 50% | 75377.0 ms | 52333.3 ms | 23.8 ms | 25.2 ms | TTFT 降约 30.6%，TPOT 增约 5.9% |

这些是官方案例数据，**不是本轮实测，也不是 `host_shm` 对 `device_sdma` 的相对性能**。原表未把上述TTFT/TPOT标为P99，不能用它判断本任务的P99门槛。没有健康的同硬件 device_sdma 实测基线时，只能量化相对“不开池”和已有可用方案的差值；不能声称比不可运行的 HCCS 直访只慢多少。

性能验收要求（用户已确认，尚未实测）：

- 同条件吞吐必须 ≥基线的 **95%**；P99 TTFT / P99 TPOT（及适用的P99 ITL）必须 ≤基线的 **105%**。冷缓存0%复用与真实业务复用率分别评估，不能只挑高复用收益掩盖退化。
- “不开池”用于证明卸载相对重算的端到端代价/收益；若要求相对健康HCCS直访只损失5%，必须提供该同条件健康基线或同SKU对照机器。没有该基线则该项明确标“无法评估”，不能把较弱基线替换进去宣称达标。
- 所有门槛必须固定模型 hash、量化、KV dtype/layout、上下文、并发、prefix 复用分布、路由、P/D/TP/DP 拓扑及持续时间；不能把不同模型或请求长度拼成比较结果。

可用于容量规划的下界：每请求实际 KV 读取量 `S_read`、写入量 `S_write` 以 Connector descriptor / 层实测合计，先估算同步传输预算 `T ≈ S_read/B_H2D + S_write/B_D2H + API/排队开销`。Host RDMA/TCP 还需网络段。存在流水和重叠时稳态受最慢环节限制，不能用两段带宽相加；全卡并发需测 CPU 内存带宽与 PCIe 交换拓扑的聚合瓶颈。

优先优化顺序：NPU 对应 CPU NUMA 的内存与线程绑定 → 静态大页 / 锁页和共享内存可用性 → 合并小块、固定 Buffer、调 batch → 线程和并发 → 在真实版本支持时评估 layerwise。**大页只能改善分配与传输，不会创造 HCCS 可达的物理内存。** 不直接执行生产 drop_caches、compact_memory、大页重配或停进程；先采集和隔离测量。

## 7. 服务器必须提供的信息

| 组别 | 必需内容 | 用途 |
|---|---|---|
| 真实故障 | 首个错误时间/命令/返回码，MemCache/MemFabric/HDK/vLLM 日志上下文，发生在 alloc/register/import/copy/首次读/重复命中哪一步 | 避免把地址、碎片、版本、Device ID 和数据格式缺陷混在一起 |
| AMD 平台 | OEM/整机 SKU、CPU 型号/Socket/NUMA、DRAM 总量及每 NUMA 容量、BIOS 当前版本（仅查）、PCIe 树/每 NPU link width+speed+NUMA | 识别本机卸载可能的 NUMA/PCIe 瓶颈 |
| 地址与分配 | 完整 `lsmem -a -b`、`/proc/iomem`、`/proc/zoneinfo`、`/proc/buddyinfo`、`/proc/pagetypeinfo`、每节点 meminfo；固定版本 mem_scan / mfcli 扫描原始输出 | 求实际 DRAM 与可达窗口交集，区分无交集/占用/碎片；普通内存权限导致全零地址时需注明 |
| 版本配套 | OS/distro、完整 kernel release/package/source patch、内核 cmdline / taint / secure boot / IOMMU；HDK/固件/CANN/灵衢L1/CC版本 | 确定是否存在可修 OS/HDK 缺陷，不直接升级 6.6 |
| 容器与 wheel | 镜像 digest、容器 mounts/device/IPC/PID/ulimit/cgroup 限额、/dev/shm/hugetlbfs、各包版本与源码、libhcom依赖 | 确认 host_shm 的实现与跨进程导入条件实际具备 |
| 网络拓扑 | 服务器数/每机 NPU逻辑ID→物理die映射/灵衢域；P/D/Store流量；Host NIC 型号/速率/verbs/GID | 区分 PD HCCS 与 Store HCCS；判断 host_rdma是否有真实硬件基础 |
| 模型业务 | 模型/权重hash、KV布局、TP/DP/EP/DCP/PP、max model len、block size；典型输入输出/并发/命中率/缓存工作集与 SLA | 决定容量、传输块、能否命中，以及“性能差不多”的验收定义 |

`host_shm` 特别检查：同一节点的进程是否在可互相导入共享内存的 IPC / mount / PID / UID 条件下；不要因为使用 Docker 就擅自新增 `--pid=host`。当前 MemFabric `MF_HOST_SHM_USE_MEMFD=1` 是另一个有版本条件的实现分支，需要共享 PID 命名空间和 UID 条件，不能默认开启；源码版本和日志先确认。

扫描API也必须匹配版本：本轮固定MemFabric的`show()`在SoC未知时仍使用地址窗口，但`stat()`仅在识别为910C时使用窗口；SoC识别失败会走普通内存统计。因此自动容量门禁必须要求SoC明确匹配，并记录API/脚本版本；不能将未知SoC下的`stat()`结果当HCCS可达容量。任何扫描都只反映当时状态，实际HAL分配和CRC仍是后续门槛。

## 8. 验收矩阵与后续实施入口

| 阶段 | 负载 | 通过标准 / 应保留证据 |
|---|---|---|
| A，只读定位 | 每节点平台/软件/地址/错误原文 | 明确根因类别；没有把原文作者结论移植为本现场结论 |
| B，本机 copy | 每 NPU 和全卡并发；8KiB / 1MiB / 8MiB / 64MiB 双向 D2H/H2D，各 NUMA | CRC/字节一致；有效GB/s、P50/P95/P99、CPU/DRAM/PCIe利用率；不产生新硬件错误 |
| C，MemCache小池 | host_shm 1个/多进程、多对象、批量 put/get、淘汰/容量 | 真实 NPU→DRAM→NPU 内容一致，日志明确 HOST_SHM，内存不泄漏；仅进程Up或exists命中不足以通过 |
| D，完整模型 | 无池/host_shm同条件；0/30/50/90%真实复用；单机和既有PD | 输出语义/finish_reason/流式一致，缓存命中与实际load字节对应，TTFT/TPOT/吞吐/P99符合预算 |
| E，容量与稳定 | 逐步扩大至目标池、缓存满、长时间请求、隔离故障恢复 | 无静默数据损坏和不可回收Buffer；失败语义明确；模型混合KV状态单独验证 |
| F，跨机需求 | 如必须跨机共享，再测MemCache host_rdma；或单独PoC HCCS staging研发 | NIC/路由真实；双端CRC；无静默TCP降级；吞吐/CPU/尾延迟与业务预算对齐 |

本轮建议的最小下一步：先收集 A；如确认旧 `device_sdma` 不能直访且 BIOS固定，优先做隔离的 B+C `host_shm`，同时保留现有 P/D 传输路径。只有跨机缓存共享确为硬需求时，才把 host_rdma 或新增 HCCS staging 作为下一条主线。

## 9. 检索范围和结论边界

- 已联合检索 vLLM / vLLM-Ascend / SGLang / sgl-kernel-npu 的 MemCache / host_* / HIXL 线索；SGLang 有公开 [MemCache L3 PR #26043](https://github.com/sgl-project/sglang/pull/26043/files) 作为协议接入线索，但本报告未把 PR 当作当前镜像已合入、可生产运行或性能保证。
- 本轮 MemCache / MemFabric 均从官方 GitCode 仓库克隆并固定 SHA；工作区 `research-src-*` 是研究快照，不建议将整个第三方源码无差别上传到交付主仓。
- AMD + 910C + 实际灵衢域、最终选定的rc2镜像、GLM模型对应 KV布局、host_shm大容量独立池及SLA尚未实测；结论是“优先可验证的软件方案”，不是“已修复/已验收”。

## 10. 最终以用户指定latest官方A3 MemCache为准

用户最终指定 [latest中文第5.4节](https://docs.vllm.ai/projects/ascend/zh-cn/latest/tutorials/models/GLM5.3-Flash.html#54-prefill-decode-disaggregation-with-kv-cache-pool)。本次固定对应源码[4e6eac4e](https://github.com/vllm-project/vllm-ascend/blob/4e6eac4e476d9be3476e99440f4cabed67b253ea/docs/source/tutorials/models/GLM5.3-Flash.md)。这版A3已直接使用MemCache，D为DP4TP4，P为DP2TP8；每worker20GB、各节点16worker，共640GB，Meta在P节点，不另启standalone384GB Provider。

rc2标签旧文档8fde6d4e的A3示例使用Mooncake Store和另一D拓扑；它作为镜像版本对照保留，不应覆盖本次指定的latest配置。rc2源码MemCache backend已包含`memcache_dp_init_barrier`机制，但实际镜像版本和所有模型能力仍须读取镜像及运行日志验证。

### 混合状态合同

MultiConnector由MooncakeConnectorV2执行P→D，AscendStore/MemCache在P查询、加载、保存缓存前缀。Prefix caching开启、chunked prefill按模型默认保持开启、hybrid KV manager启用、`use_layerwise=false`、最终`mamba_cache_mode=align`。每DP组的全部worker在AscendStore子Connector中显式设置`memcache_dp_init_barrier=true`，初始化完成后、图捕获前同步。

D子Store保持`consumer_is_to_load=false`与`consumer_is_to_put=false`；D仍向DDR池贡献内存，但不直接把TP8的混合状态池条目读入TP4。关闭读写不能推出MemCache不初始化。需要验证SFA KV、indexer pool、tail、KDA和conv等状态整体正确；注册普通K/V成功或exists命中不够。

### host_shm软件候选的改动边界

若第一轮确认device_sdma问题无法在支持的OS/HDK组合修复，先做单节点D2H/H2D和MemCache Provider/Proxy小池PoC。后续若只让P启用Store、D保留P→D connector，可能规避D跨机host_shm初始化，但这已经偏离当前官方双端MultiConnector配置，必须独立验证，不能偷偷替换当前脚本。

本轮仍未连接服务器，没有内核修复、真实KV复用或5%性能通过结果。研究输出用于选择后续可证伪的试验，不是已修复报告。
