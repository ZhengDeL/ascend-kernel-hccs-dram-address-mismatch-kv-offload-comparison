# Mooncake Ascend Direct 中转缓冲池候选

这是独立的软件候选方案，**不替代、不修改现有 MemCache 包**。P 为 DP2/TP8：16 个 worker 各贡献 **20 GiB 普通 Host DRAM，共 320 GiB**；D 为 DP4/TP4：**贡献 0，Store load/put 均关闭**。它与原 MemCache 两节点 640 GiB 池的容量、内存类型和复用路径不同，不能把二者按相同容量直接比较。

这里组合了两层：`MooncakeConnectorV2` 承担 P→D KV/线性状态传输，`AscendStoreConnector backend=mooncake` 承担 P 的 CPU 前缀池保存、读取和复用。`ASCEND_BUFFER_POOL=4:8` 只改变底层 ADXL/HIXL 传输方式，**不是 CPU KV 容量设置，也不是 MemCache 或 OS 的修复**。它通过 NPU 中转缓冲绕开直接注册 Host 内存的路径；是否绕开本现场故障、性能是否满足 5%，均未在服务器验收。

所有地址 `192.0.2.21/22` 都是 RFC 5737 文档示例，不能直接运行。真实地址只放在现场 shell 环境或私有配置；原始日志、地址图、归档不得直接提交 GitHub。

## 依赖与执行边界

- 固定容器镜像：`quay.io/ascend/vllm-ascend:v0.30.0rc2-a3`，AMD CPU 需要实际 `amd64` 构建，现场记录 image ID/digest；不自动拉取镜像。
- 模型固定：`/var/ai-model/GLM-5.3-Flash-w8a8`；本目录保持 `1048576` 上下文、MTP 3、INT8 KV、原 P/D 批量及额外优化参数。没有偷偷降上下文或关 MTP 来获得性能结果。
- 本地审计依据为 Mooncake `dd1217b6c461`、vLLM Ascend rc2 `8fde6d4e335f` 和 main `4e6eac4e476d` 的源码快照。**源码能力不等于镜像内已安装的 wheel 能力。** 版本差异见 [SOURCES.md](SOURCES.md)。
- 镜像需要已具备匹配 CANN/HDK 的 Mooncake Python Store、TransferEngine、`mooncake_master` 和以 `USE_ASCEND_DIRECT=ON` 构建的原生库。**不存在已核实的 `MC_USE_ASCEND_DIRECT` 运行时开关。** 如缺包，先依据本仓固定源码/哈希准备隔离 wheel，并核对 CANN/ADXL/HIXL ABI；不要直接 `pip install -U` 或覆盖既有 MemCache 生产环境。本目录不安装软件。
- 仅支持手动执行。脚本不 SSH、不操作 BIOS、不改内核启动参数、不停止任何已有服务。NPU 使用同一批 16 个逻辑设备，**不能与旧模型同卡并跑**；预检遇到占用即失败，等待用户安排已授权的测试窗口，禁止清理受保护的 Meta/MemCache。
- 在单独候选容器内运行。保留宿主驱动只读挂载、`/etc/hccn.conf:ro`、16 个逻辑 NPU 及必要控制设备、模型只读挂载、共享内存和 memlock 设置。把本目录挂到 `/opt/ascend-buffer-pool:ro`；把日志根 `/var/ai-model/lzd` 以相同路径读写持久挂载。可另将该次运行的 `native-ascend` 目录挂到 `/root/ascend/log` 兜底。不要 source 旧 MemCache 的 `container-env.sh` 或 launcher。

## 按顺序准备

以下操作在相应候选容器/宿主进行；先在两边修改环境中的示例地址。`RUN_DIR` 必须是同一轮唯一目录；不要重用上一轮目录。先不要启动服务。

```bash
cd /opt/ascend-buffer-pool
export RUN_DIR=/var/ai-model/lzd/mooncake-ascend-direct-buffer-pool/run-001
export MOONCAKE_MASTER=192.0.2.21:50088   # 现场替换，不能提交真实值
export LOCAL_IP=192.0.2.21              # P；D 改为该节点实际地址
export NIC_NAME=eth0                    # 改成 LOCAL_IP 所属接口
```

先单独加载候选镜像自身的 CANN `set_env.sh`，然后 source 公共配置，顺序不能反过来：

```bash
source /usr/local/Ascend/ascend-toolkit/set_env.sh  # 镜像若使用 cann/set_env.sh 则按实际路径
source ./common-env.sh
python3 ./preflight.py                          # 仅审计配置结构
python3 ./preflight.py --host --role P            # D 节点改为 D；在任何候选服务启动前运行
python3 -c 'import importlib.metadata as m; print({p:m.version(p) for p in ("vllm","vllm-ascend","mooncake-transfer-engine")})'
python3 -c 'from mooncake.engine import TransferEngine; from mooncake.store import MooncakeDistributedStore; print(MooncakeDistributedStore.setup.__doc__)'
mooncake_master --helpshort 2>&1 | head -100
```

`preflight.py --host` 检查文件/模块版本清单、环境、NIC、16 逻辑设备、端口、NPU 文件描述符占用和 P 的可用内存。它不初始化 NPU、不测试链路，**不能证明 Direct 构建、库 ABI、C8/KDA 或卸载可用**。P 默认要求 `320+64 GiB` 可用内存，64 GiB 是本地保护余量，并非官方最低内存；实际加载进程和页锁定还需另核实。

`mooncake_master --helpshort` 至少应具有 `rpc_port`、`metrics_port`、`enable_metric_reporting`、`eviction_high_watermark_ratio` 和 `eviction_ratio`；若缺失，说明所装版本与本快照接口不同，先修正依赖，不要随意换未知开关。所有启动都应能看到 `install AscendDirectTransport`；只有 `protocol=ascend` 或 `import mooncake` 成功不足以证明编译选择正确。

## 启动 Master、D、P

在 P 的独立终端前台启动 CPU 元数据 Master：

```bash
bash ./start-master.sh
```

随后在 D 的四个独立终端分别执行（每个终端都设置同样的 `RUN_DIR`、Master、D 的 `LOCAL_IP/NIC_NAME`）：

```bash
bash ./start-worker.sh D 0
bash ./start-worker.sh D 1
bash ./start-worker.sh D 2
bash ./start-worker.sh D 3
```

最后在 P 的两个独立终端分别执行：

```bash
bash ./start-worker.sh P 0
bash ./start-worker.sh P 1
```

上面的六条命令分别在六个终端运行，不是在单终端依次等待。它们保持前台，退出码就是对应实际子进程退出码；某个 DP 退出时，人工检查其余候选 DP，不会自动终止旧服务。`start-worker.sh` 在 CANN 初始化**之前**启动日志捕获，随后加载 CANN → `common-env.sh` → 每 DP 环境/配置 → `vllm serve`。最外层 shell 参数预检或 Python 解释器缺失发生在捕获前，请保留终端/Docker 启动日志。

| 项目 | P | D |
|---|---|---|
| DP/TP，设备分组 | DP2/TP8，0–7 / 8–15 | DP4/TP4，0–3 / 4–7 / 8–11 / 12–15 |
| HTTP 端口 | 10081–10082 | 10100–10103 |
| DP RPC | 13325 | 13325 |
| PD Mooncake 基础端口 | 38680 | 38580 |
| Store lookup RPC | 39000–39001 | 39100–39103 |
| Master / metrics | 50088 / 9003，P 上 | 连接 P 的 Master |

此外 Mooncake/ADXL 还可能动态选择 TE RPC/引擎端口，以上表格不是完整防火墙白名单；以实际日志和 `ss -lnt` 为准。本目录故意用独立端口；接入官方代理时应将 prefiller 端口替换为 `10081 10082`，decoder 为 `10100 10101 10102 10103`，代理本身另选空闲端口。本目录不启动/修改旧代理。

`worker-entry.sh` 给出了完整 serving 参数；如只接入自有命令，可 source `prepare-worker.sh ROLE DP_RANK RUN_DIR`，读取 `$CANDIDATE_CONNECTOR_PATH` 作为 `--kv-transfer-config`。保留其中 `use_layerwise=false`、前缀缓存和 chunked prefill；不能加入 `--disable-hybrid-kv-cache-manager`。当前模型需要对齐的线性状态缓存，支持以 `AscendStoreConnector` 自动选择 `align` 的**实际安装代码**。不要把一个普通全注意力模型的命中结果算作本 GLM/KDA 验收。

## 中转与容量的精确含义

`common-env.sh` 真正执行 `unset ASCEND_ENABLE_USE_FABRIC_MEM ASCEND_USE_ASYNC_TRANSFER`，而不是设为 `0`：审计的 Mooncake 在这些位置按环境变量是否存在启用开关。非零中转池与 FabricMem、异步传输互斥。`HCCL_INTRA_ROCE_ENABLE`、TENT 及共享 Host 内存的继承开关也清除，避免改变本候选路线；`MOONCAKE_GLOBAL_SEGMENT_SIZE` 被清除，防止其覆盖 P/D JSON 的容量。`hccs:device` 和 `LocalCommRes version=1.2` 保持本候选的 HCCS 设备通信。

`4:8` 表示 4 个、每个 8 MiB 的中转单元；HIXL 当前实现建立两个 32 MiB NPU 池，共 **64 MiB/ADXL 实例**。由于 P→D TE、Store TE、scheduler/worker 进程可能各建实例，不能宣称每卡总共只额外占 64 MiB。合法值需两个正整数，`0:0` 特判关闭；当前解析仅校验 uint64、乘法溢出和实际分配，没有可以据此承诺的统一数值上限。双方应使用相同配置。

本候选使 `ASCEND_GLOBAL_RESOURCE_CONFIG` 非空，vLLM Store backend 因此走独立 Store TE 分支，实际传给 Store 的 `local_buffer_size=0`；JSON 也显式写 0，**没有隐含的默认 1 GiB/worker 客户端缓存**。P JSON 的 `20GB` 在 backend 中按 `20×1024³` 字节解析，是 20 GiB/worker，16 worker 共 320 GiB；D 的全局段与 local buffer 均为 0。未开启 FabricMem 时，审计的 A3 分配通常落到 `aclrtMallocHost`；该分配自身若失败，中转参数不能保证修好。

D 的 Store child 保留是为贴近官方 MultiConnector 形式，但不会在 TP4 上直接加载 TP8 池项。P 在 TP8 同布局引擎间复用 CPU 前缀，D 仍从 Mooncake P→D 收数据。这是完整的 **P 前缀 CPU Store** 路线；它不等同于任意 Decode 长会话状态的换出/恢复机制，也不等同于仅开启 PD transport staging。

## 日志、验收、采集

日志位于 `$RUN_DIR/master`、`$RUN_DIR/P/dp0..1`、`$RUN_DIR/D/dp0..3`，包含实际渲染配置、包版本、允许名单中的日志设置、`stdout.log`、独立 `first-error.log`、退出状态、`plog/`、`mooncake/` 和 `vllm_ascend/`。vLLM DEBUG、CANN DEBUG、HCCL entry log 和 Mooncake TRACE 默认打开；不启用请求/生成正文日志。stdout 单文件 20 MiB、5 个备份，首错窗口最多 128 KiB。Docker stdout 反压时仅可能丢镜像副本并计数，主日志文件继续采集；原生库的独立文件保留策略由库控制，需要检查磁盘并及时采集。

依次取得以下证据：

1. 两节点 OS/内核/HDK/CANN、CPU/NUMA、固件、库版本及哈希、镜像 digest；FusionOS 的实际补丁身份保持原样，不只写“openEuler”。
2. **Direct 路径证据**：`install AscendDirectTransport`、`Set adxl.BufferPool to:4:8`、`Ignore register host mem ... when buffer pool is enabled`；无 async/fabric 初始化，Store 分配/挂载成功。仅看到配置文件里有参数不算生效。
3. 确认 P 有预期普通 Host 段、D 段为零；记录 Master 活跃 client、池容量/占用、失效/淘汰。只有 Master 进程存活或 HTTP 200 不算 CPU 卸载成功。
4. 固定非敏感测试前缀与不同后缀，覆盖冷请求、跨 P DP 的暖命中、长上下文、INT8 KV、KDA/线性状态、MTP、流式/非流式和返回内容/finish_reason。保留请求数、错误数、实际输入/输出 token、命中 token 与证据日志。必须证明读取 CPU Store 后实际减少 Prefill 计算且内容正确。
5. 性能至少比较相同硬件/权重/1M/MTP3/INT8、请求集、并发、prefix 命中比例和日志级别；同时保留原业务基线。记录吞吐、P50/P99 TTFT、P50/P99 TPOT、CPU/NUMA 带宽、HCCS 带宽与 NPU 占用。验收为吞吐下降≤5%、P99 TTFT/TPOT 上升≤5%，不是把三项平均后≤5%。

**不能预先承诺 5%**：中转引入复制；此 Mooncake 版本在 buffer 模式把 dispatcher 强制为一个线程，即使设置 `ASCEND_THREAD_POOL_SIZE` 也不会增加该线程数。完整 DEBUG 本身也可能影响性能。只能在完成同条件实测后定论，不能靠改超时、增池或关闭 MTP 宣称通过。

两侧在宿主保留当前会话目录后分别采集：

```bash
bash /path/to/mooncake-ascend-direct-buffer-pool/collect.sh "$RUN_DIR"
```

该命令只读取系统状态，保存 OS/内核地址图、NUMA/PCI、NPU、内核和系统 journal、dmesg、socket 与磁盘快照；单命令最多 30 秒/最新 8 MiB，`command-status.tsv` 记录失败。它打包本轮已持久化的候选日志并生成 SHA256。启动时之外的宿主原生日志（如 `/var/log/npu`）若不在该目录需另行按时间范围导出；不要把容器内未挂载文件当作已采集。运行中 tar 可能提示文件变化并返回非零，请保留提示、判断是否需候选正常结束后补采集。原始包只留本地，公开前需脱敏并排除凭据、真实地址和业务内容。

## 停止与回退

在每个候选 worker 的原终端按 Ctrl-C，观察其退出状态和本候选进程组；全部候选 worker 退出后再停止候选 Master。按各次记录的候选 PID/container ID 只核对本轮进程、端口和 NPU 占用；不要 `pkill vllm` 或删除旧容器。停止不会自动重启旧 MemCache 服务；是否切回由用户安排原已确认流程。本候选不写 OS/BIOS 配置，因此没有需要恢复的 sysctl、启动项或固件改动。

完整边界见 [CAPABILITIES.md](CAPABILITIES.md)；源码位置见 [SOURCES.md](SOURCES.md)。
