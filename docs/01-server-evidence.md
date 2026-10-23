# 服务器第一轮需要收集什么

日期：2026-10-23。已知 `wk1`：FusionOS 23 / 23.3.10、`ID_LIKE=openEuler`，内核 `5.10.0-316.0.0.219.u200.fos23.x86_64`；由用户提供终端输出，尚未实时读取或确认对应IP及另一节点。第一轮只读取，不改变 BIOS、内核参数、大页、IOMMU、服务或缓存。计算节点先收集 **21、22 两台**；21 的 Meta 与两台模型容器分别采集；LocalService由各vLLM worker内部初始化。最终首错由用户稍后提供。

## 先交付这八项

| 优先级 | 资料 | 判别目的 |
| --- | --- | --- |
| 1 | 第一次错误前后完整日志、发生时间和时区、发生阶段、API/ret、节点/设备/rank | 区分 Meta Join、Host 分配、注册/导入、P→D、首次 Pool Get/Put；不要只截最后一个报错 |
| 2 | `/etc/os-release`、`uname -a`、`/proc/cmdline`、matching kernel RPM/DEB 与 config | 核对实际内核及发行版 backport，不能假定仍为 Linux 5.10 |
| 3 | root 下 `/proc/iomem`、`lsmem --all`、NUMA 拓扑/各节点 meminfo、buddyinfo、pagetypeinfo、HugeTLB/THP | 分离静态地址交集、当前空闲、连续分配能力；全零地址属于权限隐藏，不能判为无内存 |
| 4 | AMD 精确型号、服务器 SKU、BIOS 当前版本、PCI BDF/拓扑、IOMMU 初始化/故障、SME/SEV 激活状态 | 识别物理布局和地址翻译限制；只记录 BIOS，不变更 |
| 5 | `npu-smi info`、HDK/固件、driver `version.info`（含 `Innerversion`）、CANN、容器 image ID/RepoDigest | 确认实际 HAL / GVA 分支与软件矩阵，标签不等于唯一镜像身份 |
| 6 | MemCache / MemFabric / Mooncake / vLLM / vLLM-Ascend / torch-npu 的实际包版本和位置、库 build commit | 判断官方源码方案是否存在于当前 rc2 镜像，防止 host_shm 仅换配置就上线 |
| 7 | LocalService、模型客户端与 Meta 的实际配置；进程 limits / cgroup / cpuset / numa_maps；16逻辑NPU可见性 | 最终官方配置每worker20GB，两机各16个worker、每机320GB；避免误保留旧384GB每worker配置；排除容器限制 |
| 8 | 灵衢 L1/L2 型号、LCN/CC版本、SuperPod/server ID、接线图、链路错误和重试计数 | 证明真实 HCCS 路径和可达范围；网管导出，不由采集脚本自动登录交换机 |

内核兼容性分支命中时，另提供**对应运行内核版本**的源码包、厂商补丁清单与 `mm/page_alloc.c`，检查 `__alloc_contig_verify_gfp_mask()` 及调用方传入 flags。没有匹配源码时保留“未知”，不根据版本号推断提交存在。

## 推荐采集顺序

1. 两机均在 Host 上运行 [采集器](../tools/README.md)，用相同时间窗口与节点标签，记录失败/权限不足项。
2. 指定各自模型容器再采一次容器元数据；21 的 Meta/Provider 容器也采。容器还不存在时先交付 Host 材料，不为采集而启动业务。
3. 第一条错误出现后，保留模型 stdout/stderr、MemCache/MemFabric、Ascend plog 与 kernel 同一窗口，保留第一次失败和内部重试。禁止为追日志先清日志、重启 Meta 或清缓存。
4. 确认实际安装版本后，再选择相应 `mfcli mem scan` / 旧 `mem_scan.py`。先 `--help` 和脚本 SHA256，再安排空闲时段扫描；1GiB / 2MiB 粒度结果分开。第一轮自动采集器不会遍历整个 kpageflags。
5. 结合匹配版本窗口做静态求交，再做隔离小容量 HAL 分配与 CRC 数据传输。此处仅列下一阶段计划，尚未执行。

## 扫描结果应如何报告

至少分别报告 `static_ram_overlap_bytes`、扫描当时可用容量、HAL 实际分配成功容量。前者大于零不代表可分配，空闲为零不证明静态无交集。实际物理地址隐藏、范围来源不匹配、NUMA 归属未知时，应标记不确定。

[analyze_iomem.py](../tools/analyze_iomem.py) 支持独立读取导出的 iomem 和版本化 windows JSON；不会自动把原文固定窗口当成所有 AMD 平台的硬件契约。参考窗口必须附源码提交和对应脚本。

## 网络和权限边界

不需要把 root 密码、SSH 私钥、完整环境变量、业务请求正文、模型权重、内存内容或 `/dev/mem` 数据发到公开仓库。原始包留在本地，只上传经人工核对的脱敏证据摘要。权限不够时保留错误输出；容器中拿到全零 iomem 时回 Host 核实。

采集器不代替网管检查、NPU 主动压测或业务验收。采集成功表示材料生成，不表示卸载问题已修复。
