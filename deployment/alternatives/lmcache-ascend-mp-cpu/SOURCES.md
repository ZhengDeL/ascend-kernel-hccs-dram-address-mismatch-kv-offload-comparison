# 固定源码依据

本目录不包含真实现场地址。源码行号基于已经下载的固定快照，2026-10-23核查；main/latest可变化，实施时以固定SHA和镜像实装接口复核。仅有源码接口证据的结论不提升为NPU运行认证。

| 结论 | 固定源码与定位 |
| --- | --- |
| MP维护路线、依赖下限、A3 experimental、安装顺序 | [LMCache-Ascend README](https://github.com/LMCache/LMCache-Ascend/blob/73f32354ea21d9ec5de15f1f909d609aaa54ae6a/README.md#L29)，本地`README.md:29`、`:35`、`:62` |
| 混合Mamba仅lmcache_driven、align/prefix/separate-object-groups、改dtype/MTP后从实际N确定chunk | [Ascend qwen3_5.md](https://github.com/LMCache/LMCache-Ascend/blob/73f32354ea21d9ec5de15f1f909d609aaa54ae6a/docs/mp_mode/qwen3_5.md)，本地`:3`、`:7`、`:35`。这是Qwen支持证据，不是GLM验收证据 |
| hcomm匹配镜像CANN、server/engine共享失败生命周期 | [Ascend lmcache_driven.md](https://github.com/LMCache/LMCache-Ascend/blob/73f32354ea21d9ec5de15f1f909d609aaa54ae6a/docs/mp_mode/lmcache_driven.md)，本地`:96`、`:156` |
| 外部MP Connector类支持HMA；host/port与多server+DP硬拒绝；chunk逐组整除校验 | [LMCache lmcache_mp_connector.py](https://github.com/LMCache/LMCache/blob/ff9185d419e1ce55c9f4f1ad84a4f4f1f06af7be/lmcache/integration/vllm/lmcache_mp_connector.py#L556)，本地`:556`、`:662`、`:702`、`:835` |
| align模式Mamba max_num_batched_tokens至少达到resolved Mamba block | 同一[connector](https://github.com/LMCache/LMCache/blob/ff9185d419e1ce55c9f4f1ad84a4f4f1f06af7be/lmcache/integration/vllm/lmcache_mp_connector.py#L392)，`:392`–`:448` |
| 混合不做纯MLA的TP去重 | [utils.py](https://github.com/LMCache/LMCache/blob/ff9185d419e1ce55c9f4f1ad84a4f4f1f06af7be/lmcache/integration/vllm/utils.py#L336)，`:336`–`:357`；[ParallelStrategy](https://github.com/LMCache/LMCache/blob/ff9185d419e1ce55c9f4f1ad84a4f4f1f06af7be/lmcache/integration/vllm/vllm_multi_process_adapter.py#L385)，world_size=TP×PP排除DP、kv piece/writer规则`:385`–`:494` |
| scratch组不缓存 | [kv_cache_groups.py](https://github.com/LMCache/LMCache/blob/ff9185d419e1ce55c9f4f1ad84a4f4f1f06af7be/lmcache/integration/vllm/kv_cache_groups.py#L35)，`:35`–`:67`读取prefix_cacheable/cacheable |
| Mamba page或slice状态转换机制，并非普通KV假设 | [kv_cache_group_edits.py](https://github.com/LMCache/LMCache/blob/ff9185d419e1ce55c9f4f1ad84a4f4f1f06af7be/lmcache/integration/vllm/kv_cache_group_edits.py#L267)，`:267`–`:397` |
| MP服务host/port/chunk/transfer/separate-object和HTTP CLI | [multiprocess/config.py](https://github.com/LMCache/LMCache/blob/ff9185d419e1ce55c9f4f1ad84a4f4f1f06af7be/lmcache/v1/multiprocess/config.py#L375)，`:375`、`:460`、`:525`、`:747` |
| L1大小与lazy参数，GiB换算 | [distributed/config.py](https://github.com/LMCache/LMCache/blob/ff9185d419e1ce55c9f4f1ad84a4f4f1f06af7be/lmcache/v1/distributed/config.py#L854)，`:854`–`:869`、`:1171` |
| LMCache日志env默认INFO、StreamHandler | [logging.py](https://github.com/LMCache/LMCache/blob/ff9185d419e1ce55c9f4f1ad84a4f4f1f06af7be/lmcache/logging.py#L50)，`:50`–`:93` |
| NPU确有lmcache-driven IPC入口及必须存在的Ascend native ops | [NpuDeviceSpec](https://github.com/LMCache/LMCache/blob/ff9185d419e1ce55c9f4f1ad84a4f4f1f06af7be/lmcache/v1/platform/devices/npu/__init__.py#L82)；[device_ops.py](https://github.com/LMCache/LMCache/blob/ff9185d419e1ce55c9f4f1ad84a4f4f1f06af7be/lmcache/v1/platform/devices/npu/device_ops.py#L65)，`:65`–`:82` |
| rc2 builtin fallback不支持多KV组 | [vLLM rc2 lmcache_mp_connector.py](https://github.com/vllm-project/vllm/blob/fa6ff060667f/vllm/distributed/kv_transfer/kv_connector/v1/lmcache_mp_connector.py)，本地`:70`–`:84`的reformat_block_ids拒绝hybrid；模块末尾`:1224`external import失败后fallback。因此本包显式指定外部module_path |
| MultiConnector所有子connector须支持HMA，一次选择一个加载源 | [vLLM rc2 multi_connector.py](https://github.com/vllm-project/vllm/blob/fa6ff060667f/vllm/distributed/kv_transfer/kv_connector/v1/multi_connector.py#L162)，本地`:162`–`:208` |
| GLM混合层与page classes/独立组 | [Ascend rc2 cache_config.py](https://github.com/vllm-project/vllm-ascend/blob/8fde6d4e335f/vllm_ascend/models/glm5next/cache_config.py#L3)，`:3`–`:8`；[GLM KDA](https://github.com/vllm-project/vllm-ascend/blob/8fde6d4e335f/vllm_ascend/models/glm5next/kda.py#L108)，`:108`–`:133`；[Tail scratch spec](https://github.com/vllm-project/vllm-ascend/blob/8fde6d4e335f/vllm_ascend/core/kv_cache_interface.py#L304)，`:304`–`:325` |

2026-10-23再次读取官方raw固定SHA的README和qwen3_5.md，内容与本地快照一致。不同main页面/抓取缓存若显示“混合不支持”应作为版本或页面差异留证，不能覆盖固定SHA的实际内容；固定SHA文档支持Qwen仍不足以证明GLM特有布局。原image内安装包真实commit和hcomm匹配commit均待现场确认。
