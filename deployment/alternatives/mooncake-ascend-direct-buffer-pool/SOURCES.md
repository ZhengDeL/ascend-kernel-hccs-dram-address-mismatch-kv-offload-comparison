# 源码审计依据

本地源码均来自 `../community-sources/` 的固定快照。下面的行号对应该快照，不能当作任意 latest 或已安装 wheel 的行号。代码中公开 upstream 自带的地址与用户现场地址无关；本候选文件只使用 RFC 5737 文档地址。现场实际配置和原始日志只留本机。

| 结论 | 本地源码路径与行号 |
|---|---|
| Direct 是编译选择，`protocol=ascend` 分别可落到 Direct 或其他编译后端；全仓未找到 `MC_USE_ASCEND_DIRECT` | `mooncake-dd1217b6c461/mooncake-transfer-engine/src/multi_transport.cpp:616-629`；`docs/source/zh_archive/ascend_direct_transport.md:7` |
| Mooncake 明确给出 Host 直传不通时使用 `ASCEND_BUFFER_POOL=4:8` 的候选方法 | `mooncake-dd1217b6c461/docs/source/zh_archive/ascend_direct_transport.md:39-56,77-81` |
| async 按 env 存在启用；非 0:0 pool 与 async 互斥；参数透传 ADXL | `mooncake-dd1217b6c461/mooncake-transfer-engine/src/transport/ascend_transport/ascend_direct_transport/transfer_executor_base.cpp:182-214,281-297` |
| 非零 pool 的 Host 注册被跳过，这给出候选绕行机制，不是 MemCache 补丁 | 同上 `transfer_executor_base.cpp:528-537` |
| BufferPool 的格式、各端一致、FabricMem 互斥，默认 4:8；HIXL 支持 A3 | `hixl-787542ebec27/docs/zh/api/cpp/HIXL-interface.md:111-155` |
| HIXL 底层单位为 1024²、两个池；正整数、溢出与实际分配检查，无统一文档数值上限 | `hixl-787542ebec27/src/llm_datadist/adxl/adxl_inner_engine.cc:25-30,216-247,253-299` |
| 中转模式强制单调度线程，覆盖线程池 env 的效果 | `mooncake-dd1217b6c461/mooncake-transfer-engine/src/transport/ascend_transport/ascend_direct_transport/slice_dispatcher.cpp:34-55` |
| Store C++ 对 `ASCEND_ENABLE_USE_FABRIC_MEM` 按存在启用；Python backend 按 ==1 判断，=0 会产生分歧 | `mooncake-dd1217b6c461/mooncake-store/src/client_service.cpp:863-868`；`vllm-ascend-rc2-8fde6d4e335f/vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/backend/mooncake_backend.py:202-208` |
| 非空 GLOBAL_RESOURCE_CONFIG 且未开启 FabricMem 时使用独立 Store TE；该分支 setup 的 local_buffer_size=0 | rc2 同上 `mooncake_backend.py:202-208,262-289` |
| P2P GlobalTE 也是 ascend，同进程的中转 env 会影响 P2P 与 Store；这里独立 TE 并不隔离环境变量 | `vllm-ascend-rc2-8fde6d4e335f/vllm_ascend/distributed/kv_transfer/utils/mooncake_transfer_engine.py:10-29`；`kv_p2p/mooncake/base_worker.py:114-119`（共同前缀均为 `vllm_ascend/distributed/kv_transfer/`） |
| Store JSON 参数、Master env 优先、容量 GB 按 GiB 解析 | rc2 `.../kv_pool/ascend_store/backend/mooncake_backend.py:517-572,608-624` |
| global segment 0 不挂池；local buffer 0 不注册额外客户端缓冲 | `mooncake-dd1217b6c461/mooncake-store/src/real_client.cpp:1193-1241` |
| 普通 Ascend Host 分配与 Fabric 分配分支不同，默认普通路径为 aclrtMallocHost | `mooncake-dd1217b6c461/mooncake-transfer-engine/src/transport/ascend_transport/ascend_allocator.cpp:384-420`；`mooncake-store/src/real_client.cpp:1389-1395` |
| rc2 GLM 文档已有 A3 Mooncake Store：P 池复用、D 不读/不写/不贡献，且 HMA 开启、align、非 layerwise | `vllm-ascend-rc2-8fde6d4e335f/docs/source/tutorials/models/GLM5.3-Flash.md:1005-1021,1032-1105,1110-1175` |
| latest/main A3 官方路线改为 MemCache；当前 1M/INT8/MTP3 参数来自 main A3 recipe；不能误说 rc2 文档与 main 完全相同 | `vllm-ascend-main-4e6eac4e476d/docs/source/tutorials/models/GLM5.3-Flash.md:1314-1326,1420-1504`，A3 serving 位于 `:705-930` |
| GLM hybrid 含非 prefix-cacheable 组，要求 whole-segment/non-layerwise；不能照搬普通注意力 layerwise recipe | main 上述模型文档 `:1617-1625`；rc2 `.../kv_pool/ascend_store/pool_scheduler.py:626-642` |
| PD Mamba/KDA 的 P TP 必须可被 D TP 整除；P8→D4 符合该静态约束，仍需真实验证 | rc2 `.../kv_p2p/mooncake_connector.py:1212-1213,2989-2995,3600-3620` |
| Store 不能因 PD 支持 TP 异构就宣称支持跨 TP 的 hybrid 池读取；该实现对相关 mismatch 场景有拒绝分支 | rc2 `.../kv_pool/ascend_store/pool_worker.py:324-351`；`backend/mooncake_layerwise.py:196-198` |
| Master CLI 精确名称 | `mooncake-dd1217b6c461/mooncake-store/src/master.cpp:131-143,164-170,203-204,387-395`；glog 标准参数另须以所装 binary `--helpshort` 核对 |
| 旧 Master snapshot/restore 已移除；新 standby snapshot 的前置条件 | `mooncake-dd1217b6c461/docs/source/deployment/mooncake-store-deployment-guide.md:179-180,359-372` |

官方仓库入口（按固定提交引用，浏览时不要自动切换 latest）：

- [Mooncake dd1217b6c461](https://github.com/kvcache-ai/Mooncake/tree/dd1217b6c461)
- [vLLM Ascend rc2 8fde6d4e335f](https://github.com/vllm-project/vllm-ascend/tree/8fde6d4e335f)
- [vLLM Ascend main 4e6eac4e476d](https://github.com/vllm-project/vllm-ascend/tree/4e6eac4e476d)
- [HIXL 固定快照接口说明](https://gitcode.com/cann/hixl/blob/787542ebec27/docs/zh/api/cpp/HIXL-interface.md)

本目录不复制任何现场地址/凭据，不将日志里的参数“存在”当作运行生效证明。最后仍以安装库版本、Direct 初始化/Host 注册跳过/Store 分配日志、真实内容和性能对照验收。
