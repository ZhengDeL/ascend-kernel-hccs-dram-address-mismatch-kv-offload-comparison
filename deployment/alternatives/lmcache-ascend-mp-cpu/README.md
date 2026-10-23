# LMCache-Ascend MP + P本机CPU DRAM（独立候选方案）

本目录提供独立配置和手动操作指导：P为DP2/TP8，D为DP4/TP4；模型`/var/ai-model/GLM-5.3-Flash-w8a8`，基础镜像`quay.io/ascend/vllm-ascend:v0.30.0rc2-a3`，1M上下文、INT8 KV、MTP3均保留。公开示例仅使用RFC5737地址`192.0.2.21`/`192.0.2.22`；实际IP请只填写到现场私有副本，不提交GitHub。

**状态：配置候选，尚未上服务器。GLM-5.3-Flash的实际混合缓存几何、依赖和P-D组合没有验收；未知块大小使cache启动脚本当前明确拒绝启动。** 这不是已经可用的GLM部署认证，也不承诺性能退化在5%以内。[CAPABILITIES.json](CAPABILITIES.json)列出所有门槛。

## 方案边界和文件

P每个DP独立运行一个MP server，server和所属vLLM workers放在同一容器/IPC环境内，通过NPU IPC和本机D2H/H2D复制保存到CPU DRAM。D仅接收Mooncake P→D传输，不连接P的LMCache端口；没有跨节点共享CPU池。该方案替换P的外部缓存层，**P-D传输仍依赖原来的Mooncake/灵衢路径**。本机CPU卸载也依赖正常的CANN/NPU IPC、页锁定及DMA能力，不能声称绕过所有DRAM地址或IOMMU故障。

| 文件 | 作用 |
| --- | --- |
| `config/cache-p-dp0.env`、`cache-p-dp1.env` | 两个P本地MP服务的端口、DRAM上限及待填写块几何 |
| `config/p-dpN-lmcache-only.json` | 第一阶段P本地存取验证，显式使用外部MP Connector |
| `config/p-dpN-multi-candidate.json` | 第二阶段P的Mooncake+LMCache MultiConnector候选 |
| `config/d-mooncake.json` | D各rank使用的纯Mooncake consumer配置 |
| `start_cache.sh` | 手动前台启动一个P MP服务；未知几何即拒绝，业务失败通过pipefail保留 |
| `diagnostic-env.sh` | LMCache/vLLM/CANN/HCCL/Mooncake诊断日志变量 |
| [MODEL-COMMANDS.md](MODEL-COMMANDS.md) | 原官方P/D serving参数的手动启动模板 |
| [SOURCES.md](SOURCES.md) | 固定提交的源码行号和官方链接 |

## 版本和安装前置

固定源码组合为LMCache `ff9185d419e1ce55c9f4f1ad84a4f4f1f06af7be`、LMCache-Ascend `73f32354ea21d9ec5de15f1f909d609aaa54ae6a`。不跟随main/dev滚动安装。Ascend README要求Python>=3.10、CANN>=8.5、Driver>=25.5、PyTorch>=2.8、vLLM/vLLM-Ascend>=0.23；`pyproject.toml`进一步限定Python<3.13。A3在README中标为experimental，这不等于本用户AMD+910C/FusionOS已经验证。

原rc2镜像内容未知，不能认定已经包含这两份新源码或其匹配native ops。安装只在本方案独立容器/构建副本中手动执行，先保留镜像ID/digest及`pip freeze`、CANN/torch/torch_npu/vLLM版本；保持基础镜像、模型和serving参数。不要在现有MemCache容器内覆盖`lmcache`包，也不要把旧in-process LMCache 0.4.5和MP dev同时安装到一个Python环境。

准备源码的手动命令如下（不是本包自动执行步骤）：

```bash
git clone https://github.com/LMCache/LMCache.git LMCache
git -C LMCache checkout ff9185d419e1ce55c9f4f1ad84a4f4f1f06af7be
git clone https://github.com/LMCache/LMCache-Ascend.git LMCache-Ascend
git -C LMCache-Ascend checkout 73f32354ea21d9ec5de15f1f909d609aaa54ae6a
git -C LMCache-Ascend submodule update --init --recursive
git -C LMCache rev-parse HEAD
git -C LMCache-Ascend rev-parse HEAD
```

`LMCache-Ascend/third_party/hcomm`必须与**镜像实际CANN版本**匹配，按上游安装指导选择匹配tag并记录其真实commit，然后再编译；CANN版本未收集前，本包不给出一个猜测hcomm tag。必须具备编译工具链、CANN headers/libs、torch_npu和对应SoC信息；不能把Windows语法检查当native构建成功。

依赖匹配检查完成后，在独立容器内手动安装（`--no-deps`用于避免安装过程无审查升级torch/vLLM，缺少的依赖按固定源码requirements单独准备）：

```bash
python3 -m pip install -v --no-build-isolation --no-deps -e ./LMCache
python3 -m pip install -v --no-build-isolation --no-deps -e ./LMCache-Ascend
python3 -m pip check
python3 -m pip show lmcache lmcache-ascend vllm vllm-ascend torch torch-npu
lmcache server --help
python3 - <<'PY'
import inspect
import lmcache_ascend
import lmcache_ascend.ops
from lmcache.integration.vllm.lmcache_mp_connector import LMCacheMPConnector
from vllm.distributed.kv_transfer.kv_connector.v1.base import SupportsHMA
print(inspect.getfile(LMCacheMPConnector))
print('SupportsHMA:', issubclass(LMCacheMPConnector, SupportsHMA))
assert issubclass(LMCacheMPConnector, SupportsHMA)
PY
```

导入失败、缺native ops、HCCL/CANN不匹配、CLI缺参数即保留安装日志并停止该候选，不自动换版本、关闭hybrid manager或改回普通MLA路径。vLLM rc2内置旧MP Connector不支持hybrid；所有P配置都显式写`kv_connector_module_path=lmcache.integration.vllm.lmcache_mp_connector`，使外部包导入失败直接暴露，避免悄悄fallback旧实现。

## GLM混合缓存几何门槛

GLM-5.3-Flash不是普通MLA。固定vLLM-Ascend源码明确包含main MLA、compressed indexer、每请求tail ring，以及KDA/conv/Mamba recurrent state；部分组共享block ID，部分组独立，tail是不可prefix-cache的scratch。LMCache固定dev源码具备HMA、多组、Mamba page/slice和scratch过滤机制，但**没有找到这个GLM-5.3-Flash + INT8 KV + MTP3 + 两种TP组合的正式验收配方**。

LMCache-Ascend固定SHA的混合模型文档只给Qwen模型实例。它要求`lmcache_driven`、`--mamba-cache-mode align`、prefix caching和`--separate-object-groups`；`engine_driven`不能用于此混合家族。Qwen表中的块大小既不能套GLM，也不能在改KV dtype或启用MTP后照抄。取实际启动日志中的`Setting attention block size to ...`以及resolved Mamba block/group spans作为证据，chunk必须能被每个cacheable组的有效token跨度整除，即是这些跨度的公倍数。取LCM或协商后的块大小仍需由注册日志验证，不假设128或4096。

1. 先按原官方固定模型参数完成一次隔离基线启动并保存日志、KVCacheConfig、各组spec/shape/dtype/block-size/page-size、scratch标记、NPU IPC/native ops信息。这个步骤不启用新的LMCache候选，也不要求修改现有现场服务。
2. 将**实际**`ATTENTION_BLOCK_SIZE`、`MAMBA_BLOCK_SIZE`和兼容所有组的`LMCACHE_CHUNK_SIZE`分别填写两个`config/cache-p-dpN.env`。两个P DP使用相同模型参数，仍需核对几何一致。
3. P保持`max-num-batched-tokens=4096`；如果实际Mamba block>4096，当前候选被阻断，不能为启动成功悄悄改变官方batch参数。D保持192，但D不装LMCache，不把LMCache的Mamba下限强套到D。
4. 启动MP后的connector注册仍会检查所有组的chunk对齐；脚本只能检查已填写的attention/Mamba块大小，不能代替实际多组注册和正确性验证。

## 端口、TP/DP与内存计量

| P本机服务 | ZMQ（仅loopback） | HTTP（仅loopback） | 所属模型进程 | DRAM上限候选 |
| --- | --- | --- | --- | --- |
| P DP0 MP | 127.0.0.1:5555 | 127.0.0.1:8150 | DP0的TP0–7（逻辑NPU0–7） | 160GiB |
| P DP1 MP | 127.0.0.1:5556 | 127.0.0.1:8151 | DP1的TP0–7（逻辑NPU8–15） | 160GiB |

这两个MP服务各有一个L1存储预算，**不是每个TP worker再各分配160GiB**。单server的`--l1-size-gb`按源码乘`1<<30`，上限合计320GiB，不包含模型CPU内存、页锁定、临时staging、native线程和系统余量。lazy L1默认开启，初始分配默认20GiB；不是启动即证明160GiB全部被分配或可用。实际memory footprint要用server进程RSS/PSS、Host MemAvailable、pinned页和缓存bytes共同核对。

160GiB只是对照原P侧320GiB预算的候选分配，不能直接套到任何服务器。启动前用`free -h`、`numactl --hardware`或sysfs节点信息、`ulimit -l`和现有进程RSS/锁页确认：两个server最终上限加模型冷加载/运行、staging、系统服务均有余量，并检查NUMA到NPU的本机路径。完成容量规划后才在两份env填`LMCACHE_CAPACITY_REVIEWED=yes`。脚本另检查当前MemAvailable至少覆盖本server的L1上限；这个最低门槛不能替代两个lazy池增长后的总容量规划，也不能保证运行中不会OOM。本包不修改memlock、NUMA策略或OS配置。

混合模型不享受pure-MLA的TP去重：LMCache的`mla_only()`排除`is_hybrid`，每个TP rank都需要正确保存其KV/state piece。缓存容量应覆盖8个pieces及实际重复/分片字节，不能套“TP8仅存TP0一份”的普通MLA估算。独立DP服务保证worker-registration namespace隔离，但DP0和DP1**不会互相命中**，同前缀可能在两池重复占用；第一阶段请直连各DP API测试，别让负载均衡掩盖冷/热差异。

当前core明确拒绝同一个connector配置`server_urls`含多个server且`data_parallel_size>1`；不要把两个P端口写进同一`lmcache.mp.server_urls`。每DP一个single-server配置是本包的隔离设计，其实际运行支持仍待检验。TP workers共用所属server端口，无需每个TP单独开server。P和D的TP8/TP4缓存piece结构不同，D不能直接读取P本地池，更不能跨节点连loopback池。

## 手动启动与分阶段验证

同一P候选容器中挂载本目录，且两个MP进程与两个P DP都能看到同一文件、NPU设备和IPC资源。没有必要把LMCache ZMQ/HTTP暴露给D。以下路径只作新方案示例，不写入旧MemCache目录：

```bash
cd /var/ai-model/lzd/lmcache-ascend-mp-cpu
# 按镜像实际路径先source其CANN set_env.sh，再在两个独立终端分别执行：
bash start_cache.sh 0
bash start_cache.sh 1
```

如果N尚未填写，脚本退出2且不会调用`lmcache`。填写后使用真实CLI `lmcache server --chunk-size ... --l1-size-gb ... --separate-object-groups --supported-transfer-mode lmcache_driven --eviction-policy LRU`，均为固定源码存在的参数。服务健康接口和实际readiness路径以已装CLI/HTTP日志为准，不把端口监听当KV正确。

第一阶段只启动P，使用`p-dpN-lmcache-only.json`，按[MODEL-COMMANDS.md](MODEL-COMMANDS.md)保留P的DP2/TP8、1M、INT8 KV、MTP3。两次相同长prompt必须直连同一DP API；保存模型输出/finish_reason、server store/retrieve日志、缓存bytes、external hit统计。由于本地prefix cache必须保持开启，重复请求本身不足以证明LMCache命中；在**同一已初始化KV布局内**制造可控NPU缓存淘汰后再重复，观察真实retrieve。不要通过仅重启模型来声称已有IPC注册和CPU缓存跨重启可用：上游要求server与engine作为一组管理其失败生命周期。

还需验证KDA/conv state、compressed indexer、scratch排除、跨chunk边界/MTP拒绝分支下冷/热输出正确性；量化或混合缓存不能只凭文字相近验收，保存确定性输入、score/token差异和无外部缓存基线。功能未通过前，不进入下一阶段。

第二阶段使用P的`p-dpN-multi-candidate.json`和D的`d-mooncake.json`，按原D先起、P后起、最后proxy的顺序。P的LMCache只提供本地前缀缓存，Mooncake负责实际P→D KV/state交付。固定源码显示MultiConnector要求所有子connector支持HMA，且一次请求只选择一个加载源；外部LMCache类有SupportsHMA标记。这仅证明接口具备组合条件，**不证明这个GLM mixed cache+Mooncake组合已经正确**。若HMA检查、异步完成/失败传播、load选择、KDA状态交付或TP8→TP4重排异常，则本组合标记blocked并留证，不通过关闭hybrid manager或回退普通MLA冒充成功。

停止/重启MP server前先停止该候选的请求并按实际服务依赖管理engine，不能在in-flight请求中只重启cache server。脚本不自动stop/restart/delete任何服务。

## 日志、采集与性能

`diagnostic-env.sh`开启`LMCACHE_LOG_LEVEL=DEBUG`、vLLM DEBUG、CANN DEBUG文件日志、HCCL入口日志和Mooncake TRACE。LMCache Python logger默认StreamHandler，server脚本用`set -Eeuo pipefail`和`2>&1 | tee`保留输出与失败状态；每次运行有`effective-cache.env`、`lmcache-server.log`、`exit-status.txt`及`plog/`。这是前台短复现入口，**tee文件本身未做有界轮转**，应监控磁盘并在完成复现后停止该候选、保存日志；不能把它写成具备旧方案的完整host observer。

模型日志路径模板在MODEL-COMMANDS中；每DP独立`plog/`、`mooncake/`、`vllm_ascend/`及stdout。OS证据需另行只读采集当前boot journal/kernel/dmesg、Host/NPU系统日志、FusionOS内核与驱动/CANN版本、CPU NUMA与内存、PCIe/IOMMU/ACPI地址图；本目录不自动改OS、sysctl、BIOS或全局日志daemon。现场原始日志保留本地，公开前脱敏真实IP、主机名、序列号、凭据、请求内容。

验收分两层：先证明本机CPU store/retrieve及P-D的真实状态正确，再在固定同一日志档位、数据集、请求数、token长度、并发和命中率下对比无卸载基线/候选，记录吞吐、P99 TTFT/TPOT、有效D2H/H2D带宽、CPU/内存/磁盘日志开销。吞吐下降和P99升幅分别不得超过5%；诊断日志自身也可能超过此预算，本地语法或stub通过不构成5%承诺。
