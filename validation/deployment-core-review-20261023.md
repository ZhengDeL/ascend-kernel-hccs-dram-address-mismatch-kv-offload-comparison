# 两机包核心语义独立复核

日期：2026-10-23。对象：本地 `glm53flash-1p1d-21-22`；参考 `research-pinned/GLM5.3-Flash-latest-20261023.md`，官方提交 `4e6eac4e476d9be3476e99440f4cabed67b253ea`。只读审查与 Python 编译检查，无服务器连接、容器启动或 NPU 初始化。

- 拓扑：21 为 P DP2/TP8，22 为 D DP4/TP4；各使用16逻辑设备。每 worker 20GB、每节点320GB、world_size256；没有独立384GB Provider。Meta metrics8000，proxy8081；模型端口9081–9082/9900–9903。
- Meta 返回码：`sys.exit(MetaService.main())` 保留 API 失败。此前直接丢弃返回值可能把原生 `-1` 错记为 Python exit0，已修正。
- DP 子进程：任一子进程意外 exit0 也返回1，并收敛该 launcher 的子进程；不把服务意外结束当作成功。
- 现有模型与 NPU：模型启动前检查旧模型进程与 `/proc` NPU设备持有者。21 的自有 Meta 白名单验证 deployment、service、运行状态及实际镜像 ID，再通过 Docker top 纳入整个容器 PID 集。不会只允许监督器 PID，也不会只凭相同容器名称放行。
- 采集边界：启动失败时诊断异常不会覆盖原始启动错误。完整采集器非零或900秒超时会保留部分资料、记录状态并返回失败；缺少完整 manifest 时不得声称证据完整。可选完整采集器不存在时，入口只做有限快照，README已说明这一条件。
- 源码线索：记录安装包版本和 barrier 源码文本是否存在；不把文本命中当作运行时特性验收。无自动 PFN 扫描、跨版本 factory API 硬门禁或 BIOS/内核修改。

检查命令：`python -m py_compile deploy.py runtime.py launch_online_dp.py connector_config.py`，退出码0。上述结论来自代码和配置审查；FusionOS23.3.10、实际5.10内核、AMD+910C灵衢环境的启动、真实 MemCache 读写/回读、容量可达性及相对基线不超过5%的性能劣化，均需现场验证。
