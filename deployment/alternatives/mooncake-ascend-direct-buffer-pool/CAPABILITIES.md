# 能力与验收边界

本目录是本地源码审计后生成的实验配置。没有连接服务器、运行 NPU、卸载 KV 或测量吞吐；没有证明 FusionOS 内核、AMD CPU 与 910C/灵渠的兼容问题已修好。没有升级 BIOS、OS、驱动或覆盖现有 MemCache 文件。

| 项目 | 已确认内容 | 未确认内容 |
|---|---|---|
| Ascend Direct 选择 | Mooncake `USE_ASCEND_DIRECT` 编译宏与 `protocol=ascend` 安装分支真实存在 | 固定镜像所装 wheel 是否使用这个分支、CANN/ADXL ABI 是否一致 |
| 中转方式 | `ASCEND_BUFFER_POOL=4:8` 传给 ADXL；Host 注册在中转模式被跳过 | 本现场普通 Host 分配、Host↔NPU 复制及灵渠传输是否正确 |
| 互斥 | 必须 unset async/FabricMem；非零 pool 与二者不兼容 | 旧依赖版本是否有其他行为差异，需运行日志核对 |
| CPU KV 池 | rc2 中 Mooncake Store + AscendStoreConnector 已有 P 前缀复用实现；P 配置共 320 GiB，D 0 | 实际成功挂载容量、跨 P DP 命中和释放行为 |
| PD 与 Store 组合 | 官方 rc2 GLM 文档给出 MultiConnector：MooncakeConnectorV2 + Mooncake Store；D Store 不读写 | 本目录固定的 DP2/TP8 → DP4/TP4 与 1M/INT8/MTP3 组合仍待验收 |
| Hybrid/KDA | rc2 Mooncake PD 代码有 Mamba/KDA 组和 TP 拆合处理；GLM Store 文档要求 `align`、HMA 开启、`use_layerwise=false` | INT8 KV、KDA、MTP 的实际内容正确性和长请求稳定性；不能仅凭类名或短请求认定通过 |
| 日志 | 生成配置、版本/允许名单、stdout/首错、plog、Mooncake、vLLM Ascend 路径独立 | 原生库是否向预期目录输出、宿主日志权限和完整保留窗口 |
| 性能 | 有中转复制开销，Mooncake buffer 模式 dispatcher 强制 1 线程 | 吞吐下降≤5%、P99 TTFT/TPOT 上升≤5%；没有给出未实测的性能承诺 |
| OS/BIOS | 本目录不做系统改动 | 不代表内核地址图缺陷或 BIOS 问题被修复 |

`snapshot` 有不同含义，不能混用：

- GLM 的对齐 recurrent/KDA 状态是 KV 正确性的一部分；前缀命中必须具备匹配的可恢复线性状态，不能用只有注意力 KV 的命中代替。
- Mooncake Master 的恢复快照是元数据 HA 功能，不会自动把本候选的 CPU DRAM 数据变成持久存储。dd1217 快照中旧 Master 生成的 snapshot/restore 路径已移除，使用 `enable_oplog_snapshot` 的 standby batch OpLog 路径，要求额外的 HA/etcd、OpLog、对象存储等配置。本目录**未开启**，不要照旧文章添加 `enable_snapshot`、`enable_restore` 假定可用。
- `local_buffer_size=0` 是 Store 客户端本地中转缓冲配置，不会关闭上述 NPU BufferPool，也不表示 CPU 全局段为 0。全局 CPU 池容量由 `global_segment_size` 控制。

本候选与现有 MemCache 配置的比较必须同时报告：池总容量从 640 GiB 改成 320 GiB、D 不贡献/不读写池、普通 Host 内存替代 FabricMem、整套日志模式和参数版本。只测 PD 传输成功、HTTP 健康、NPU 已初始化或缓存写入成功，都不足以宣称完整 KV 卸载和 5% 性能验收通过。
