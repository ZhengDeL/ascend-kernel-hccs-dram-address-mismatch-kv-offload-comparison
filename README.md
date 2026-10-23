# AMD + Ascend 910C / 灵衢：MemCache 卸载研究与现场准备

**公开版本地址说明：** 本仓节点地址使用 RFC 5737 文档示例 `192.0.2.21`–`192.0.2.24`。上机前必须在私有现场副本中将部署脚本、采集脚本和 MemCache 配置改为本机真实地址，并保持一致。真实 IP、登录凭据和原始采集包仅保存在本地，不提交或回传到 GitHub。

更新：2026-10-23。已收到 `wk1` 的用户提供OS输出：FusionOS 23、VERSION_ID 23.3.10、`ID_LIKE=openEuler`，内核 `5.10.0-316.0.0.219.u200.fos23.x86_64`。这是openEuler生态衍生发行版，不能自动等同于某个原版openEuler补丁集；另一个节点尚需采集。

目标是在 **BIOS 不升级、不改动** 的条件下，定位 MemCache 卸载障碍，优先研究受支持的 OS / 驱动修复，并评估软件数据路径。用户确认性能损耗最多 **5%**。

当前只完成资料与本地脚本准备；没有连接服务器，没有运行模型、扫描物理页或执行任何 OS / 驱动变更。现场首错稍后由用户提供，当前根因未知。原文章的“0 GB / 地址窗口无交集”属于原作者现场，不能视为本服务器事实。

## 本次部署目标

采用 用户指定的 **latest 第5.3/5.4节官方 GLM-5.3-Flash A3 两机 PD 配置**（源码快照 `4e6eac4e`，镜像仍固定为用户的 rc2）：

| 位置 | 角色 | 官方并行 | 用途 |
| --- | --- | --- | --- |
| `192.0.2.21` | Prefill | DP2 / TP8，每机 16 逻辑 NPU | 两个 P endpoint；MemCache Meta；每 worker 20GB DDR；Proxy |
| `192.0.2.22` | Decode | DP4 / TP4，每机 16 逻辑 NPU | 四个 D endpoint；每 worker 20GB DDR；Decode不直接加载P的池条目 |

镜像：`quay.io/ascend/vllm-ascend:v0.30.0rc2-a3`。权重：`/var/ai-model/GLM-5.3-Flash-w8a8/`。日志根目录：`/var/ai-model/lzd`。不使用 23/24，不在脚本中保存登录凭据。镜像由用户手动拉取，脚本不自动拉取或安装依赖。

当前 latest A3 文档已直接使用 `AscendStoreConnector(backend=memcache)` + `MooncakeConnectorV2`，不再替换 Store 后端。Meta在21；两节点各16个worker、每worker20GB，合计640GB DDR池。官方当前配置包含1M上下文、INT8 KV、DP初始化barrier；与rc2标签的旧文档不同，已分别保存版本。真实镜像能力与640GB池可分配性仍需现场验证。

## 阅读顺序

用户指定的两套新增候选已分别整理到[独立卸载配置目录](deployment/alternatives/README.md)，每套都有配置与操作指导：

- [LMCache-Ascend MP + 本地CPU DRAM](deployment/alternatives/lmcache-ascend-mp-cpu/README.md)
- [Mooncake Ascend Direct + ASCEND_BUFFER_POOL](deployment/alternatives/mooncake-ascend-direct-buffer-pool/README.md)

1. [两机本地脚本](deployment/glm53flash-1p1d-21-22/README.md)：官方配置、固定地址/镜像/权重、日志与启动顺序。
2. [服务器第一轮采集清单](docs/01-server-evidence.md)：先提交哪些内容、每项解决什么判断。
3. [OS / 驱动研究](docs/02-os-software.md)：具体内核兼容问题与地址转换边界。
4. [保留 MemCache 的软件路线](docs/03-memcache-routes.md)：`device_sdma`、`host_shm`、Host 传输及替代项目的范围。
5. [性能与正确性验收](docs/04-performance-gates.md)：5% 门槛、冷/热缓存对照、实际业务验收。
6. [本地脚本与原配置审计](docs/05-local-input.md)：最终两机方案及不能继续沿用的旧模型参数。
7. [只读采集工具](tools/README.md)：不修改服务；原始采集结果只留本地。
8. [全链路日志源码核查](deployment/logging-source-audit.md)：OS持续采集、MemCache Local/Meta、vLLM、CANN/HCCL、MemFabric和Mooncake的真实开关与边界；操作入口见部署README日志表。

部署包当前默认启用诊断日志。每个服务从启动preflight开始留证，并跟随容器持续采集OS日志；两机各执行`bash collect_logs.sh --node 21`或`--node 22`，会将运行日志、系统证据、metrics与容器状态打包并生成SHA256。缺失/权限不足/超时/截断会显示partial；详细DEBUG/TRACE日志的现场开销仍须按5%门槛另验收。

原仓的全部文本文件已按固定提交下载并保留在 [upstream/ai_infra_summary](upstream/ai_infra_summary/README.md)。[来源清单](SOURCE_MANIFEST.json) 记录版本和文件哈希。新增研究在 `docs/`，不会改写原文来掩盖来源或历史结论。

另外已将13份相关社区源码快照下载到本地 `community-sources/`，包括MemCache、MemFabric、vLLM-Ascend rc2与main、vLLM rc2、Mooncake、LMCache及其Ascend适配、YuanRong、HIXL、HCOMM、UMDK和原研究仓。共38,596个源码文件，约577MiB（不含档案和Git历史）。公开仓保留[完整版本清单](COMMUNITY_SOURCES.json)与[可复现下载方法](tools/COMMUNITY_SOURCES.md)，原始档案、逐文件哈希与源码树保留在本地；未递归拉取子模块，链接项的处理边界见清单。

## 目前最有价值的判断

- **先分辨分配失败与真实地址无交集。** 官方 MemFabric FAQ 已记录 `alloc_contig_range()` GFP 标志校验与 HDK 兼容问题，这是具体的 OS / 驱动排查分支；是否命中本现场尚未知。
- **`host_shm` 能保留 MemCache 做本机 PCIe 卸载。** 它不能让另一台服务器直接访问 21 的 DRAM，不能作为本次跨机 DRAM 池的原样替换。
- **不能承诺一个内核参数能解决任意 AMD DRAM / HCCS 不匹配。** IOMMU / GVA / CPU PA 的链路需要实际驱动与硬件契约证据。
- **功能能通不等于满足 5%。** 比较同硬件同模型的吞吐、P99 TTFT、P99 TPOT，保留原始结果和实际传输路径证明。

## 资料与发布边界

本仓公开脚本、研究、来源版本和本地验证结果。实际内核日志、plog、ACPI、服务器身份、容器配置和服务地址明细可能含敏感信息，采集目录默认忽略，不自动上传。发布前人工审查与脱敏；凭据不得进入仓库。

目标仓原有 `LICENSE` 保留。`upstream/` 原仓本次未见 LICENSE，其内容保留原作者权利与来源，不因复制进本仓自动转为本仓许可证。
