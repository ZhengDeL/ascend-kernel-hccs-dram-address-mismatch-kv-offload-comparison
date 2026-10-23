# GLM-5.3-Flash：两台 A3 的官方 1P1D + MemCache

**公开脚本使用示例地址。** `192.0.2.21`–`192.0.2.24` 是 RFC 5737 文档地址，不能原样用于现场部署。上机前只在私有副本中修改 `deploy.py`、`launch_online_dp.py`、`diagnostics.py`、`config/mmc-meta.conf`、`config/mmc-local.conf` 等对应地址，并保持一致；真实地址、凭据及原始采集包不得提交或回传 GitHub。

本目录按用户最后指定的 [latest 中文文档第5.3/5.4节](https://docs.vllm.ai/projects/ascend/zh-cn/latest/tutorials/models/GLM5.3-Flash.html#54-prefill-decode-disaggregation-with-kv-cache-pool) 准备，网页对应源码冻结于 `4e6eac4e476d9be3476e99440f4cabed67b253ea`，核查日期2026-10-23。仅完成本地准备，未登录服务器或启动服务。

| 服务器 | 模型并行 | API | DDR池与其他服务 |
| --- | --- | --- | --- |
| 192.0.2.21 | P：DP2/TP8/PP1 | 9081、9082 | 16 worker×20GB=320GB；Meta5000、ConfigStore6000、metrics8000；proxy8081 |
| 192.0.2.22 | D：DP4/TP4/PP1 | 9900–9903 | 16 worker×20GB=320GB |

每台16个逻辑NPU，共32个worker、640GB DDR池。**MemCache Meta在21；DDR池按最新官方配置分布在两台worker中，LocalService由worker初始化。** 无独立Provider，23/24不参与，旧 `set-a-1-2-7-8-9-39` 原件不动。

权重固定 `/var/ai-model/GLM-5.3-Flash-w8a8`；镜像保留用户要求的 `quay.io/ascend/vllm-ascend:v0.30.0rc2-a3`。部署目录建议 `/var/ai-model/lzd/glm53flash-1p1d-21-22`；日志始终在其 `runs/<时间>-<服务>-<节点>/` 下，不写旧 `/mnt/models`。

## MemCache 在哪里、怎么启动

**本包已配置 MemCache。** MetaService在21单独启动；LocalService随21/22的每个vLLM worker初始化，当前官方方案不另起独立LocalService容器。因此没有单独的`start_memcache.sh`，启动Meta后再启动D、P即会初始化各worker的MemCache。

| MemCache内容 | 本地文件 | 实际作用 |
| --- | --- | --- |
| Meta启动入口 | [start_meta.sh](start_meta.sh) | 在21创建并启动`glm53flash-21-22-meta-21`容器 |
| Meta服务配置 | [config/mmc-meta.conf](config/mmc-meta.conf) | Meta `192.0.2.21:5000`、ConfigStore `:6000`、metrics `:8000` |
| worker内的LocalService配置 | [config/mmc-local.conf](config/mmc-local.conf) | 两机相同：`device_sdma`、每worker `20GB`、`world_size=256` |
| vLLM接入MemCache | [connector_config.py](connector_config.py) | `AscendStoreConnector`的`backend=memcache`，与P→D的`MooncakeConnectorV2`组成`MultiConnector` |
| 配置与库路径加载 | [deploy.py](deploy.py)、[container-env.sh](container-env.sh) | 挂载本目录，设置`MMC_META_CONFIG_PATH`/`MMC_LOCAL_CONFIG_PATH`并加入MemCache库路径 |

启动顺序如下；以下命令供服务器上执行，当前没有远端执行记录：

```bash
# 21：先启动MemCache Meta
cd /var/ai-model/lzd/glm53flash-1p1d-21-22
bash start_meta.sh
docker logs --tail 100 glm53flash-21-22-meta-21

# 22：启动D，16个worker各初始化20GB MemCache LocalService
cd /var/ai-model/lzd/glm53flash-1p1d-21-22
bash start_node.sh --node 22

# 21：D就绪后启动P，16个worker各初始化20GB MemCache LocalService
cd /var/ai-model/lzd/glm53flash-1p1d-21-22
bash start_node.sh --node 21

# 21：P/D全部就绪后启动代理
bash start_proxy.sh
```

`start_meta.sh`最终在容器里执行的MemCache原生入口是：

```python
import sys
from memcache_hybrid import MetaService
sys.exit(MetaService.main())
```

**这里“Meta在21”不代表“全部DDR只在21”。** 当前按用户指定的官方第5.4节，每机16个worker各贡献20GB，两机各320GB、合计640GB。D端仍初始化LocalService并贡献DDR，但其Store直接加载/保存开关为false；P→D传输由Mooncake连接器负责。

包使用所指定镜像里的`memcache_hybrid`和`memfabric_hybrid`，启动时检查实际包；没有源码编译安装步骤，也不会缺包后自动安装或改协议。镜像内容及Host DRAM可达性仍需现场验证。已经下载的社区源码位于本工作区的`community-sources/memcache-98fb4c303cae/`与`community-sources/memfabric-8df6c8f3e1ca/`，不在此部署目录内。

Meta与LocalService的日志出口不同：Meta文件位于每次Meta运行目录的`memcache/logs/`；LocalService及其MemFabric/BM原生日志跟随模型worker的`dpN/stdout.log`。LocalService不支持使用`ock.mmc.log_path`指定文件目录，不能把空的`memcache/`目录当作worker日志已采集的证据。完整开启项、实际路径和采集方法见下方“日志开启与持续采集”。

## 文档与镜像的版本边界

用户链接的latest与rc2 tag模型文档已有实质差异。最终只采用latest A3的DP2TP8/DP4TP4、1M、动态C8、MTP3与每worker20GB池配置。固定rc2镜像保持不变，**不能因此保证其实际二进制支持latest全部行为**。运行时记录实际vLLM、vLLM-Ascend、MemCache、MemFabric、Mooncake版本与barrier源码线索，最终由真实CLI、模型加载、图捕获、HAL和业务验证。缺包/参数不兼容即失败留证，不自动安装或改镜像。

上游rc2源码已含`memcache_dp_init_barrier`从Connector到Worker到Backend的支持，字段存在本身仍不等于容器内完整功能验收。latest官方将本页PD方案定位为示例参考，不是生产认证；客户生产推荐应参考其MindIE Motor方案。本包继续完成用户要求的本地官方示例准备。

最新参数：两侧V1 runner，`--max-model-len 1048576`、`--kv-cache-dtype int8`、MTP3。P maxseq64/batch4096，MTP草稿eager=true；D maxseq32/batch192、FULL_DECODE_ONLY、MTP草稿eager=false。两侧FlashComm1、glm47 reasoning、HCCS资源配置按原文。P保留`--prefix-cache-retention-interval None`并按池化节添加prefix caching；chunked保持启用，hybrid KV manager不禁用。P最新脚本没有主模型`--enforce-eager`，未沿用旧tag设置。

两侧均为`MultiConnector`：`MooncakeConnectorV2`负责P→D，`AscendStoreConnector(backend=memcache)`负责池化。子Store设置`memcache_dp_init_barrier=true`、`use_layerwise=false`；D另设置`consumer_is_to_load=false`、`consumer_is_to_put=false`。按原文保留P engine_id=0、D=1；lookup IPC标识随DP rank唯一。没有旧GLM-5.3的42,36层分配、旧index缓存或W8A8C8权重路径。当前C8是官方在W8A8权重上使用动态INT8 KV缓存，不要求换一套C8权重。

MemCache配置与原文一致：world_size256、protocoldevice_sdma、dram.size20GB、max.dram.size1024GB、storage.enabledfalse。`MMC_META_CONFIG_PATH`与`MMC_LOCAL_CONFIG_PATH`为精确变量名；环境包装自动找到已安装MemCache lib和Python LIBDIR加入库路径。Meta使用Python API，包装额外保留`MetaService.main()`的实际返回码。

## 已知OS与启动门槛

用户口述一台`wk1`为FusionOS23，VERSION_ID23.3.10、ID_LIKEopenEuler、内核`5.10.0-316.0.0.219.u200.fos23.x86_64`、CPUx86_64。尚未把此输出对应到21或22，也未证明两台完全一致。此信息不构成HDK/CANN/灵衢兼容认证；不升级BIOS或OS。

真实启动前检查Linux本机IP/NIC、16设备、amd64镜像本地存在、驱动及hccn.conf路径、端口、真实模型metadata/量化标识/shard完整性。模型启动需root只读核对旧vLLM/通信测试进程与其他进程持有的NPU设备描述符；发现占用即拒绝，不停止已有服务。同名容器一律拒绝复用或删除。

每台模型启动前`MemAvailable`至少384GiB，是本包为320GiB池增加64GiB余量的本地保护候选，不属于官方要求，也不足以证明模型冷加载内存充足；仍需现场容量规划。此检查不证明DRAM位于HCCS可达窗口，实际HAL初始化才是关键门槛。启动流程不自动全量扫描PFN，可在维护时段按实际版本手动执行`mfcli mem scan`与`mfcli mem scan -m 2`并留证。

镜像缺失会明确报“未拉取”；不会自动pull/pip install/SSH/stop/restart/delete/reboot，不含凭据，不转储全量环境。Docker主进程监督实际服务，保留失败退出码与退出容器，没有`sleep infinity`。

## 本机预览

这一步不需要Docker、Linux或NPU，不访问网络：

```bash
python3 deploy.py plan --node 21 --service model
python3 deploy.py plan --node 22 --service model
bash start_meta.sh --dry-run
bash start_node.sh --node 21 --dry-run
bash start_node.sh --node 22 --dry-run
bash start_proxy.sh --dry-run
```

将同一份目录放到两台服务器；NIC按对应IP自动查找，也可通过`--nic bond0`限制。`docker-run.sh --service ... --node ...`与`start_*.sh`是同一直接Docker入口，选一个使用，勿重复执行。

## 本机启动步骤

1. 在21启动Meta，确认5000/6000与metrics8000正常：

   ```bash
   cd /var/ai-model/lzd/glm53flash-1p1d-21-22
   bash start_meta.sh
   docker logs -f glm53flash-21-22-meta-21
   ```

2. 在22启动D，等待4个引擎及其每worker20GB LocalService完成初始化：

   ```bash
   cd /var/ai-model/lzd/glm53flash-1p1d-21-22
   bash start_node.sh --node 22
   ```

3. 在21启动P，等待2个引擎及其每worker20GB LocalService完成初始化：

   ```bash
   bash start_node.sh --node 21
   ```

4. 所有6个API的`/health`返回200后，在21启动镜像自带官方proxy；脚本会逐端点预检：

   ```bash
   bash start_proxy.sh
   curl --noproxy '*' http://192.0.2.21:8081/healthcheck
   ```

外层成功只表示容器被创建并在早期检查时运行，不能当作推理/卸载成功。看每次运行的`plan.json`、`versions.json`、`logging-settings.json`、`stdout.log`、`dpN/stdout.log`、`dpN/plog`、`os/observer.json`、`exit-status.json`。Meta/模型失败后容器保留，处理已有失败容器属于运维单独操作，本脚本不自动清理。

固定端口：21Meta5000/6000/8000、proxy8081；两台各自DP RPC12325；P握手36680–36697、D36580–36599；P API9081/9082，D API9900–9903。Mooncake传输引擎还有动态RPC和HCCL通信，固定端口预检不等于完整防火墙证明；按真实灵衢/HCCS组网核实。

## 验证与采集

### 日志开启与持续采集

启动包使用诊断日志档位。日志总根固定为`/var/ai-model/lzd/glm53flash-1p1d-21-22`；每个Meta、model、proxy启动各有独立的`runs/<时间>-<服务>-<节点>/`目录，挂载到容器内`/run/glm53flash`。以下表格中的`run/`代表对应宿主机运行目录，`dpN/`只存在于model运行目录；P为dp0–dp1，D为dp0–dp3。

| 层级 | 已设置的日志项 | 持久文件与采集边界 |
| --- | --- | --- |
| OS系统日志 | 每服务启动宿主机只读observer；保存当前boot系统journal快照并持续follow | `run/os/journal-system-boot.log`、`journal-system-tail.log`、`journal-system.stdout.log`及其轮转/首段文件 |
| OS内核日志 | 当前boot内核journal快照+tail+follow；另存启动时dmesg；journal不可用或follow中断时回退dmesg follow并记partial | `run/os/journal-kernel-boot.log`、`journal-kernel-tail.log`、`journal-kernel.stdout.log`、`dmesg-boot.log`；回退为`dmesg-follow.stdout.log` |
| 现有OS文本日志 | 对现存`/var/log/messages`、`/var/log/kern.log`、`/var/log/syslog`分别保存tail并持续`tail -F` | `run/os/os-file-*.log`与`os-file-*-follow.stdout.log`；不存在的文件不会伪造为已覆盖 |
| Docker事件 | 按本次完整容器CID持续记录create/start/die等事件 | `run/os/docker-events.stdout.log`；不会修改Docker或全局日志配置 |
| 内存/NUMA/进程 | 初始/退出快照，每30秒采集meminfo/vmstat/buddyinfo/PSI、各NUMA节点meminfo/numastat及本容器进程资源；初始/退出增加numa_maps/smaps_rollup等 | `run/os/host-snapshots.jsonl`；进程数量/字节设上限，缺失/权限/截断可查metadata |
| NPU状态 | 初始及每60秒`npu-smi info` | `run/os/npu-smi.log`；不把一次健康快照当卸载验收 |
| 容器主进程 | 最早preflight开始捕获Python和原生stdout/stderr，保留异常堆栈及业务退出码 | `run/stdout.log`、`run/first-error.log`、`run/exit-status.json`；容器侧版本与开关为`versions.json`、`logging-settings.json` |
| 每DP模型及原生库 | 每DP独立捕获stdout/stderr、版本、日志开关、LocalService配置及退出码 | `run/dpN/stdout.log`、`first-error.log`、`versions.json`、`logging-settings.json`、`config/mmc-local.conf`、`exit-status.json` |
| MemCache Meta | `ock.mmc.log_level=debug`、`log_output_target=both`；文件轮转20MiB、5份备份 | `run/memcache/logs/mmc-meta.log`和`mmc-meta-audit.log`及轮转文件；同时进入`run/stdout.log` |
| MemCache LocalService | 每DP复制有效local配置，`ock.mmc.log_level=debug`；没有可用的LocalConfig `log_path` | C++日志进入`run/dpN/stdout.log`；空的`dpN/memcache/`目录不代表存在独立MemCache文件日志 |
| MemFabric/SMEM/BM | `MF_LOG_LEVEL=0`、兼容旧名`ASCEND_MF_LOG_LEVEL=0`；MemCache还将debug等级传给BM | 默认原生日志同样进入`run/dpN/stdout.log`；Meta通过其logger callback输出；不猜`MF_LOG_PATH`/`SMEM_LOG_LEVEL` |
| CANN/HCCL应用日志 | `ASCEND_GLOBAL_LOG_LEVEL=0`、`ASCEND_SLOG_PRINT_TO_STDOUT=0`、`HCCL_ENTRY_LOG_ENABLE=1` | 主进程`run/plog/`、每DP`run/dpN/plog/`；其中plog/run/device等实际子目录由CANN版本创建。设置stdout=0是保留独立文件，stdout=1会改为打屏 |
| Mooncake传输 | `MC_LOG_LEVEL=TRACE`；主进程和每DP启动前均预建可写`MC_LOG_DIR` | `run/mooncake/`与`run/dpN/mooncake/`；只有实际加载/初始化后才可能产生文件；没有`MC_LOG_LEVEL=DEBUG`这一有效解析值 |
| vLLM/vLLM-Ascend | `VLLM_CONFIGURE_LOGGING=1`、`VLLM_LOGGING_LEVEL=DEBUG`，专用JSON日志配置；统计周期10秒；uvicorn HTTP access保留默认INFO | 控制台进入`stdout.log`；模型additional config设置`ascend_log_path`到每DP的`run/dpN/vllm_ascend/`，原生Python文件按20MiB分卷；默认不另开request prompt/token/output内容日志 |
| 库默认路径兜底 | 每服务额外持久挂载`/root/ascend/log`与`/var/log/memfabric_hybrid` | 分别为`run/native-ascend/`和`run/native-memfabric/`。只有实际编译启用并初始化成功的ptracer才会产生`ptracer_<pid>.dat`，目录无文件时不能称追踪日志已产生 |
| 宿主机NPU系统日志 | 手动完整采集时读取现有`/var/log/npu`及相关状态 | 不修改Device/宿主机全局日志daemon或级别；应用DEBUG不能证明系统daemon已正常产生日志 |

observer只跟踪带本部署标签的完整容器CID，其生命周期跟随对应容器。observer自身子命令失败、日志不可读或缺工具不会停止、重启模型；`run/os/observer.json`的`status`、`partial`及`observer-events.jsonl`保存覆盖缺口。只有`observer-registered.json`表示进程注册，`observer-ready.json`才表示完成初始化检查；最终状态也需检查`partial`，不能只看observer进程存在。

runtime和DP的stdout流每文件20MiB、5份备份；识别首个error/fatal/exception/failed等匹配后，额外保留最多128KiB的`first-error.log`上下文。该文件是匹配线索，未匹配到不代表服务无故障。Docker控制台镜像输出使用固定长度队列；拥塞时可丢弃镜像副本并在`exit-status.json`的`docker_mirror_bytes_dropped`计数，取证应优先读取主`stdout.log`及首错文件，不能仅依赖`docker logs`。Host observer每流10MiB、5份备份，另保留最初2MiB的`.first`首段。CANN、Mooncake、vLLM-Ascend原生文件仍由其库决定写入/轮转；vLLM-Ascend的20MiB分卷并无固定总保留份数，因此仍需观察磁盘余量并及时采集。

开关与路径依据[当前社区源码核查](../logging-source-audit.md)。CANN官方明确[DEBUG可能影响业务性能](https://www.hiascend.com/doc_center/source/zh/CANNCommunityEdition/910/maintenref/envvar/envref_07_0122.html)。短时全日志复现与正式性能验收需分开留证；正式对比固定日志档位、工作负载、拓扑和镜像，同时记录采集进程CPU、磁盘IO与日志写入量。**这里只完成本地开启与采集准备，尚未证明现场所有层均产生日志，也未完成吞吐/P99 TTFT/P99 TPOT退化不超过5%的NPU验收。**

本次Windows离线runtime回归共13项，8项通过、5项依赖Linux进程/信号语义而跳过；跳过项不能计为Linux或NPU验证通过。其余本地检查及未覆盖范围见[VALIDATION.md](VALIDATION.md)。

### 手动打包与回传

同一份脚本分别在21和22本机执行。采集默认`--service all`：21覆盖meta、model、proxy；22覆盖model，也可以明确限制服务。

```bash
curl --noproxy '*' http://192.0.2.21:8081/v1/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"glm","prompt":"The future of AI is","max_tokens":50}'

# 21本机：采集Meta、P模型及proxy
bash collect_logs.sh --node 21
# 22本机：采集D模型
bash collect_logs.sh --node 22
# 单项采集示例
bash collect_logs.sh --node 21 --service meta
```

需验证实际生成内容、finish_reason、SSE，另一个P rank的MemCache命中/put/get、淘汰、P→D完整性与故障恢复。重复请求可能仅命中本地prefix，不能单独证明池命中。正式性能需实际请求数、token长度、并发、命中率、TTFT/TPOT/P99、有效GB/s与CPU消耗；本地编译/plan不覆盖这些。GLM-5.3-Flash不应设置`enable_thinking:false`或`thinking:false`。

`collect_logs.sh`只读、有界采集，组合运行目录日志、容器日志/状态与`tools/collect_server.py`系统证据，输出到`manual-evidence/<本次采集>/`，并生成旁侧`<本次采集>.tar.gz`及`<本次采集>.tar.gz.sha256`。先检查其中`collection-report.json`、`manifest.sha256.json`和`host-profile/`内完整采集器manifest：缺失、无权限、超时、截断或采集失败均明确记为partial/相应失败状态，不能仅凭tar.gz存在认为证据完整。运行目录与宿主机NPU原生日志复制默认共享总量2GiB、单文件64MiB上限，各目录内优先首错/首段及status、plan等关键metadata；其他系统命令、metrics和host-profile各自独立有界，限制及实际保留情况写入报告。上限可能截断证据，应尽快在错误首次出现后打包；采集包不会删除原始运行日志。

启动前与失败后保留有限设备/内存快照，采集错误不会吞模型退出码。模型metadata SHA256与分片数量会记录，但未计算全部权重哈希，不能声称完整权重身份认证。原始服务器日志及采集包需回传本机并脱敏审查，不直接纳入Git。
