# 日志开关源码核查（2026-10-23）

范围：当前下载的社区源码；固定 `vllm-rc2-fa6ff060667f`、`vllm-ascend-rc2-8fde6d4e335f`、`memcache-98fb4c303cae`、`memfabric-8df6c8f3e1ca`、`mooncake-dd1217b6c461`，及官方 CANN 9.1 日志说明。下述是源码接口核查，不表示已连接 FusionOS 主机、核实容器安装版本或成功产生现场日志。镜像中实际 wheel/so 的版本和能力必须采集后复核。

## 直接影响本地脚本的结论

1. **MemCache LocalService 不能靠 `ock.mmc.log_path` 改日志目录。** 该配置属于 MetaConfig。LocalService 默认通过 C++ `std::cout` 输出；必须捕获每个 DP 进程的原生 stdout/stderr，Python `logging` 文件 handler 单独使用不足以覆盖 C++ MemCache/MemFabric 日志。
2. Meta 的日志路径是配置目录下的 `logs/mmc-meta.log` 和 `logs/mmc-meta-audit.log`，不能把配置目录直接当日志文件位置。
3. MemFabric 日志不应猜 `SMEM_LOG_LEVEL`、`BM_LOG_LEVEL`、`MF_LOG_PATH` 等环境变量。真实 `MF_LOG_LEVEL=0` 支持 Python adapter；MemCache 初始化另会显式设置 SMEM/BM 日志级别，所以 `ock.mmc.log_level=debug` 才能覆盖此调用链。
4. CANN 使用 `ASCEND_SLOG_PRINT_TO_STDOUT=0` 才按文件方式保存独立 plog；值为 1 会改为 Host 日志打屏，官方明确说明此时不再写 log 文件。保持 DP 原生输出捕获可以记录回退 stdout 的日志，但不要声称设置为 1 会同时保留独立 plog。
5. vLLM-Ascend 自带 Python 文件日志有独立 `additional-config.ascend_log_path`；CANN 的 `ASCEND_PROCESS_LOG_PATH` 不控制这个路径。默认路径仍需采集，或用该真实配置显式迁移。
6. Mooncake `MC_LOG_LEVEL` 支持 `TRACE/INFO/WARNING/ERROR`，**没有 `DEBUG` 解析**。诊断用 `TRACE` 或 `INFO`；TRACE 会启用额外 trace 条件。`MC_LOG_DIR` 必须预先创建且可写，否则回退 stderr。

## MemCache

| 项目 | 真实开关、默认值及约束 | 证据（路径相对 community-sources/memcache-98fb4c303cae） |
| --- | --- | --- |
| 配置日志等级 | `ock.mmc.log_level=info`；配置接受小写 `debug/info/warn/error`；diagnostic 设 `debug` | `src/memcache/csrc/config/mmc_config_const.h:50`；`src/memcache/csrc/config/mmc_configuration.h:48` |
| 日志级别数值 | `0 DEBUG, 1 INFO, 2 WARN, 3 ERROR, 4 FATAL, 5 TRACE`；不是所有数值都可以填入配置文本枚举 | `src/memcache/csrc/common/mmc_logger.h:42` |
| Meta 输出目录 | `ock.mmc.log_path=/var/log/memcache_hybrid` | `src/memcache/csrc/config/mmc_config_const.h:51`；仅 `MetaConfig::LoadDefault` 注册，`mmc_configuration.h:220` |
| Meta 最终文件 | `${log_path}/logs/mmc-meta.log`、`${log_path}/logs/mmc-meta-audit.log` | `src/memcache/csrc/meta_service/mmc_meta_service_process.cpp:468` |
| Meta 轮转 | `ock.mmc.log_rotation_file_size=20`，单位 MiB；范围 1–500；`ock.mmc.log_rotation_file_count=50`，范围 1–50 | `src/memcache/csrc/config/mmc_config_const.h:52`、`:146`；`mmc_configuration.h:357` 的 `* MB_NUM` |
| Meta 输出目标 | `ock.mmc.log_output_target=file`，支持 `screen/file/both`，诊断可设 `both`；日志及审计用相同目标 | `src/memcache/csrc/config/mmc_config_const.h:54`；`mmc_configuration.h:50`；`mmc_meta_service_process.cpp:488`、`:496` |
| Meta 刷盘 | 每秒 flush，ERROR 立即 flush | `src/memcache/csrc/log/spdlogger.cpp:130` |
| LocalService 配置 | `ClientConfig::LoadDefault()` 仅注册等级，不注册路径、目标和轮转；`GetLocalServiceConfig()` 仅写 logLevel | `src/memcache/csrc/config/mmc_configuration.h:390`、`:506` |
| LocalService 输出 | `mmc.cpp` 仅在非空外部函数时设 callback；默认全局配置零初始化；默认日志走 `std::cout`，含 PID/TID | `src/memcache/csrc/mmc.cpp:38`、`:262`；`src/memcache/csrc/common/mmc_logger.h:98` |
| MemFabric/BM 级别接入 | InitBm 主动 `SmemSetLogLevel(initConfig.logLevel)`，非空时还设置外部 logger；level 从 LocalService options 传递 | `src/memcache/csrc/local_service/mmc_bm_proxy.cpp:36`；`mmc_local_service_default.cpp:297` |
| Meta MemFabric 输出 | Meta 同时向 MFLoggerApi 设置等级和 SPDLOG_LogMessage callback，MemFabric 日志可能进入 Meta 文件 | `src/memcache/csrc/meta_service/mmc_meta_service_process.cpp:483` |
| MemFabric 性能追踪 | MemCache InitBm 将 ptracer dumpFilePath 设置 `/var/log/memfabric_hybrid`；实际是否产生数据还依赖构建 `ENABLE_PTRACER` 和 runtime 初始化 | `src/memcache/csrc/local_service/mmc_bm_proxy.cpp:60`；参见下一节 |

对 LocalService：为每个 DP 记录配置快照和完整 native stdout/stderr，通过持久日志目录下每 DP 独立文件及外部有界轮转保存。不要把不受此配置控制的 native 日志只写入“Python日志目录已配置”的说明。

## MemFabric / SMEM / BM

| 项目 | 真实值及调用边界 | 证据（相对 community-sources/memfabric-8df6c8f3e1ca） |
| --- | --- | --- |
| 环境变量 | 新名 `MF_LOG_LEVEL`，旧名 `ASCEND_MF_LOG_LEVEL`；新名优先，使用旧名会产生 deprecated 警告 | `src/util/csrc/mf_env_define.h:24`、`:65` |
| 等级 | `0 DEBUG,1 INFO,2 WARN,3 ERROR,4 FATAL,5 TRACE`；默认 `WARN`；此排序中 TRACE 不是最小阈值，勿照搬一般日志库理解 | `src/util/csrc/mf_out_logger.h:47`、`:236` |
| env 真正使用处 | transfer Python binding 初始化和 `pytransfer_create_config_store` 检查单字符数字并调用 `smem_set_log_level`；不等于所有 C++ 调用都会主动读取 env | `src/smem/csrc/python_wrapper/mk_transfer_adapter/csrc/transfer_util.cpp:23`；同目录 `pytransfer.cpp:59` |
| 外部设置 | `smem_set_log_level` 设置 MF OutLogger，并传播到 `hybm_set_log_level` | `src/smem/csrc/smem.cpp:142` |
| 输出目标 | 有外部 logger 使用 callback；否则 `std::cout`，含时间、PID、TID；没有在此源码看到通用 MF 文件输出路径 env | `src/util/csrc/mf_out_logger.h:175` |
| ptracer | `ENABLE_PTRACER` 决定 enabled；默认文件名 `ptracer_<pid>.dat`；MemCache 给其目录 `/var/log/memfabric_hybrid` | `src/util/csrc/ptracer/ptracer.cpp:35`；`src/util/csrc/ptracer/tracers/ptracer_default.cpp:205` |

推荐诊断环境保留 `MF_LOG_LEVEL=0`（如兼容旧版需要，可同时显式设置旧名），同时把 local 配置改为 `debug`，并持久挂载 `/var/log/memfabric_hybrid`。ptracer 有文件则收集；无文件需要在 manifest 标记“未产生/不支持/初始化失败”，不能声称采集成功。

## vLLM 与 vLLM-Ascend

| 项目 | 设置和默认 | 证据 |
| --- | --- | --- |
| vLLM 全局日志 | `VLLM_CONFIGURE_LOGGING=1` 默认；`VLLM_LOGGING_LEVEL=INFO` 默认，诊断设 `DEBUG` | `community-sources/vllm-rc2-fa6ff060667f/vllm/envs.py:828` |
| 自定义配置 | `VLLM_LOGGING_CONFIG_PATH` 指 JSON dictConfig；它与 `VLLM_CONFIGURE_LOGGING=0` 同时出现会抛错 | `community-sources/vllm-rc2-fa6ff060667f/vllm/logger.py:175` |
| 默认 logging | `vllm` 命名空间 StreamHandler，propagate=False | `community-sources/vllm-rc2-fa6ff060667f/vllm/logger.py:41` |
| HTTP access | 默认 `disable_uvicorn_access_log=False`；uvicorn 等级默认 info，可选 critical/error/warning/info/debug/trace；保持访问日志启用 | `community-sources/vllm-rc2-fa6ff060667f/vllm/entrypoints/launchers/cli_args.py:277`；`api_server/entry.py:147` |
| 请求日志 | 默认关闭，需 `--enable-log-requests`；INFO记录request ID/参数，DEBUG再打印prompt/token IDs | `community-sources/vllm-rc2-fa6ff060667f/vllm/engine/arg_utils.py:3000` |
| 请求长度 | `--max-log-len` 默认不限；诊断可限制，例如256，仍保留请求ID/参数日志 | `community-sources/vllm-rc2-fa6ff060667f/vllm/entrypoints/launchers/cli_args.py:129` |
| 输出日志 | `--enable-log-outputs` 默认False，需要enable-log-requests；INFO打印输出及finish reason，DEBUG再打印token IDs | `community-sources/vllm-rc2-fa6ff060667f/vllm/entrypoints/launchers/cli_args.py:150` |
| 统计日志 | `disable_log_stats=False` 默认；保持统计日志打开 | `community-sources/vllm-rc2-fa6ff060667f/vllm/engine/arg_utils.py:566` |
| Ascend 控制台 | 独立 `vllm_ascend` handler，等级取 `VLLM_LOGGING_LEVEL`，propagate=False | `community-sources/vllm-ascend-rc2-8fde6d4e335f/vllm_ascend/logger.py:179` |
| Ascend 文件日志 | 默认 `~/ascend/log/vllm_ascend`；真实 additional config 属性 `ascend_log_path` 控制目录，分别有 vllm 与 vllm_ascend handler；文件名含timestamp/PID | `community-sources/vllm-ascend-rc2-8fde6d4e335f/vllm_ascend/ascend_config.py:601`；`logger.py:137`、`:157` |
| Ascend 文件轮转 | 每文件20MiB；每次追加序号，无固定保留数量或总容量限制 | `community-sources/vllm-ascend-rc2-8fde6d4e335f/vllm_ascend/logger.py:25`、`:97` |
| Ascend 调用点 | 平台初始化和worker初始化调用文件日志配置 | `community-sources/vllm-ascend-rc2-8fde6d4e335f/vllm_ascend/platform.py:481`；`worker/worker.py:154` |

stdout/stderr 文件捕获需从进程启动最早阶段开始，覆盖 import、原生库、容器初始化和异常堆栈。自定义 JSON handler 不能使某些引擎worker只因进程ID冲突写同一个轮转文件；宜用PID独立文件或单一外部stream汇聚。镜像实际CLI需用 `vllm serve --help` 复核输出日志选项，不能因为下载的源码支持就断言镜像已有全部选项。

## Mooncake

| 项目 | 真实实现 | 证据（相对 community-sources/mooncake-dd1217b6c461） |
| --- | --- | --- |
| 等级 | `MC_LOG_LEVEL=TRACE/INFO/WARNING/ERROR`；默认INFO；TRACE设置trace=true且底层glog INFO；没有DEBUG解析 | `mooncake-transfer-engine/include/config.h:93`；`mooncake-transfer-engine/src/config.cpp:428` |
| 路径 | `MC_LOG_DIR` 指向已有可写目录；成功后 `FLAGS_log_dir=...`、`FLAGS_logtostderr=0`；无效时告警并回退stderr；磁盘满时stop_logging_if_full_disk=true | `mooncake-transfer-engine/src/config.cpp:504` |
| TRACE代价 | config.trace被通信实现按条件用于更多日志，属于诊断运行边界 | `mooncake-transfer-engine/src/config.cpp:431` |

即使当前MemCache用device_sdma，仍采集Mooncake配置、版本和stderr/文件，因为官方P-D方案里P2P路径可能独立使用Mooncake。是否被加载需凭运行日志和已装包确认；“未使用”与“日志丢失”需区分。

## CANN、HCCL、Host与Device

官方网页于2026-10-23核查，来源为CANN 9.1维护参考。实际镜像CANN版本待现场采集，旧版与当前源码差异不能默认为零。

| 项目 | 设置与边界 | 官方来源 |
| --- | --- | --- |
| 应用调试等级 | `ASCEND_GLOBAL_LOG_LEVEL=0` DEBUG；1 INFO、2 WARNING、3 ERROR默认、4 NULL；官方说明DEBUG可能影响性能 | [ASCEND_GLOBAL_LOG_LEVEL](https://www.hiascend.com/doc_center/source/zh/CANNCommunityEdition/910/maintenref/envvar/envref_07_0122.html) |
| Host日志落盘 | `ASCEND_SLOG_PRINT_TO_STDOUT=0`保存文件；1只打屏、不再保存Host log文件 | [ASCEND_SLOG_PRINT_TO_STDOUT](https://www.hiascend.com/doc_center/source/zh/CANNCommunityEdition/910/maintenref/envvar/envref_07_0121.html) |
| 路径 | `ASCEND_PROCESS_LOG_PATH`优先于`ASCEND_WORK_PATH`优先于`$HOME/ascend/log`，需有写权限 | [ASCEND_PROCESS_LOG_PATH](https://www.hiascend.com/doc_center/source/zh/CANNCommunityEdition/910/maintenref/envvar/envref_07_0120.html) |
| HCCL调用入口 | `HCCL_ENTRY_LOG_ENABLE=1`，默认0；每次通信算子调用生成runtime日志；默认`$HOME/ascend/log/run/plog/plog-PID_*.log`；其适用约束为单集合通信算子调用场景 | [HCCL_ENTRY_LOG_ENABLE](https://www.hiascend.com/doc_center/source/en/CANNCommunityEdition/900/API/hcclug/hcclenvref_07_0030.html) |

HCCL通过CANN slog/plog采集，不猜 `HCCL_DEBUG=INFO` 或 `HCCL_LOG_LEVEL`。本地hcomm快照确认HCCL_ENTRY_LOG_ENABLE在910路径有解析，默认0，合法0或1：`community-sources/hcomm-fa196b811738/src/legacy/ascend910/platform/common/externalinput.cc:1709`。`HCCL_DEBUG`同名的C++宏不构成环境变量支持证据。Device系统类日志、驱动daemon和OS日志独立于应用env；仍需采集宿主机`/var/log/npu`、journal/kernel/dmesg、驱动/固件及日志daemon状态。设置应用DEBUG不等于已修改Device全局日志等级，也不等于日志daemon正常。

## OS采集与性能5%边界

- OS应同时保存启动前快照、连续事件和复现后快照：`journalctl -k`、`journalctl -f`、`dmesg`、boot ID、`/proc/cmdline`、`/proc/iomem`、NUMA/内存/PCIe/IOMMU/ACPI/SRAT/SLIT和FusionOS准确包版本。记录所有命令退出码、权限/缺工具/日志为空；不能用空文件算成功。
- `dmesg -n`只改变console打印阈值，不能开启编译时未开启的内核DEBUG；不应为“全日志”默认打开整内核dynamic_debug、function tracing或逐页IOMMU trace，这些存在显著开销且需要具体调用点。
- 全DEBUG短复现与5%性能验收分开留证。对比必须固定模型、输入输出长度/并发、硬件拓扑、镜像、池大小、缓存命中状态与日志档位；除MemCache候选修复外其余条件一致。记录日志写速率、磁盘容量/IO负载和采集进程CPU开销；不自动关闭业务日志/OS服务来制造性能结果。
- 源码审查不能承诺日志开销在5%内。要证明卸载/OS修复候选退化不超过5%，需在实际NPU环境测吞吐、P99 TTFT、P99 TPOT；“功能HTTP 200”不代表KV卸载正确，更不能替代性能验收。
