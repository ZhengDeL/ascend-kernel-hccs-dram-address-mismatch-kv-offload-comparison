# 两个独立的卸载候选配置

两个目录分别提供配置文件、版本依据、日志设置及操作指导。沿用P为DP2/TP8、D为DP4/TP4，每机16个逻辑NPU；模型与基础镜像固定为GLM-5.3-Flash-w8a8及v0.30.0rc2-a3。示例地址均属于RFC5737，真实值只在私有现场环境中填写。原MemCache目录保持独立，不从这两个候选入口自动启动它。

| 方案 | 配置与操作指导 | CPU缓存与数据路径 | 当前门槛 |
| --- | --- | --- | --- |
| LMCache-Ascend MP | [lmcache-ascend-mp-cpu](lmcache-ascend-mp-cpu/README.md) | P每个DP独立MP server，本地CPU DRAM预算各160GiB；D只用P→D传输 | 须核实GLM混合组几何、INT8 KV/MTP3块大小、外部HMA Connector/native依赖，再验P-D组合；块大小未填写时拒绝启动 |
| Mooncake Ascend Direct Buffer Pool | [mooncake-ascend-direct-buffer-pool](mooncake-ascend-direct-buffer-pool/README.md) | P每worker贡献20GiB普通Host DRAM，P总320GiB；D不贡献/直接读写Store；BufferPool另占NPU中转内存 | 须证明实际wheel使用Direct transport及中转路径，不能仅凭环境变量存在判成功 |

容量为候选配置，不是现场可分配容量或有效缓存容量验收。两个方案均有独立运行目录与日志说明；真实OS/驱动/库兼容、GLM完整状态往返、输出正确性、冷/热缓存性能和最多5%退化均未现场验证。不要在同一组NPU上同时运行原服务和新候选，也不要把本地CPU缓存称为跨机共享Host内存池。

所有服务操作由使用者在隔离测试窗口手动执行。配置和说明不自动安装、登录服务器、停止旧服务、修改OS/BIOS或替换镜像。
