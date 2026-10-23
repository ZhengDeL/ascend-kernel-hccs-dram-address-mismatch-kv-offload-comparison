# 最终本地配置与官方来源

2026-10-23最终要求：直接采用用户链接的官方 **latest 第5.3/5.4节 A3两机PD + MemCache**；21=P、22=D，23/24不用，只修改本地，不连接服务器。之前四机/旧rc2文档拓扑已撤出交付。

## 参数合同

| 项目 | 最终值与来源 |
| --- | --- |
| 镜像 | 用户固定 `quay.io/ascend/vllm-ascend:v0.30.0rc2-a3`，手动拉取 |
| 模型 | 用户固定 `/var/ai-model/GLM-5.3-Flash-w8a8/` |
| 日志 | 用户固定根目录 `/var/ai-model/lzd`，按组件/时间/DP rank保存 |
| 21 Prefill | 官方DP2/TP8；两个API `9081-9082` |
| 22 Decode | 官方DP4/TP4；四个API `9900-9903` |
| Pool | 官方MemCache `device_sdma`，Meta在21；每worker20GB，两节点各320GB，共640GB |
| 初始化 | 官方各AscendStore子Connector `memcache_dp_init_barrier=true`，所有DP rank一致 |
| 模型能力 | 官方1M上下文、`kv-cache-dtype=int8`、MTP3、V1 model runner；按对应P/D角色配置图模式 |
| HCCS | 官方 `ASCEND_GLOBAL_RESOURCE_CONFIG` 的 `hccs:device` 与 `ASCEND_LOCAL_COMM_RES` 版本1.2 |
| 验收 | 用户5%性能损耗上限，未实测 |

“MemCache放21”在此按用户最新“直接抄官方第5.4节”执行为 **Meta放21，数据池由两机worker共同贡献**。不额外启动384GB standalone Provider。若以后必须把全部DDR都集中到21，需要另做配置与性能验证，不能声称与本节完全相同。

## 精确来源与版本差异

- 用户指定的 [latest 中文第5.4节](https://docs.vllm.ai/projects/ascend/zh-cn/latest/tutorials/models/GLM5.3-Flash.html#54-prefill-decode-disaggregation-with-kv-cache-pool)。
- 对应本次固定的 [源码快照4e6eac4e](https://github.com/vllm-project/vllm-ascend/blob/4e6eac4e476d9be3476e99440f4cabed67b253ea/docs/source/tutorials/models/GLM5.3-Flash.md)：以A3第5.3与5.4共同构成完整命令。
- [rc2标签旧文档8fde6d4e](https://github.com/vllm-project/vllm-ascend/blob/8fde6d4e335fe300e2b949aab0e31659cce0811d/docs/source/tutorials/models/GLM5.3-Flash.md)：当时示例为不同Decode拓扑/缓存后端/上下文参数；仅用于对照镜像版本，不能覆盖用户最新指定来源。

最新版文档不是固定镜像内源码的证明。首次启动必须记录实际镜像ID/架构、vLLM/vLLM-Ascend/MemFabric/MemCache版本和配置；缺参数或能力时保留错误，不能悄悄升级镜像或替换用户镜像。

## 保留的原始输入

工作区`set-a-1-2-7-8-9-39`是另一环境的六机GLM-5.3脚本。它包含旧模型固定PP层划分、原镜像专用CANN/ATB初始化、不同IP/并行/`MooncakeConnectorV1`和优化字段，已保留本地原件。最终GLM-5.3-Flash包按指定官方源码重建，不沿用旧层划分或C8开关；新版官方INT8 KV由`--kv-cache-dtype int8`设置，不由权重目录名字决定。

## 验证边界

本地语法检查、参数展开、参数捕获、采集器fixture测试和官方差异核对不能替代真实NPU运行。当前未知FusionOS实际HDK/固件/LCN、可达DDR容量与原始首错；采用官方`device_sdma`配置本身尚未修复地址或HAL分配故障。
