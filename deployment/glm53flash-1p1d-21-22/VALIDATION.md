# 本地验证记录

日期：2026-10-23。执行环境：Windows x86_64、Python 3.12、Git Bash；没有连接服务器、Docker或NPU。

- 13个Python文件（含采集器及测试）：AST解析及`python -m py_compile`通过。
- 8个Shell文件：Git Bash `bash -n`通过；文件使用LF。
- 4份`deploy.py plan`（P21、D22、Meta21、proxy21）：均能离线生成并解析JSON，未调用Docker、Linux命令或网络。
- 拓扑断言：P2×8、D4×4；每侧16worker、每worker20GB/每机320GB；Meta5000/6000/8000；proxy8081；D API9900–9903。
- 6个Shell入口dry-run（Meta、P、D、proxy、两机collect）通过，collect预览不执行采集。包装会优先使用python3，缺失时使用python，Windows本机可预览。
- 逐项解析官方latest A3 P/D shell命令，对照本包各25个serving参数完全一致；本包仅添加显式prefix/chunked开关并换成第5.4节MultiConnector JSON。JSON参数按对象比较，不依赖空白格式。
- `memcache_dp_init_barrier=true`、非layerwise、D consumer load/put均false；worker0GB与独立384GB Provider方案均已从最终包移除。

## 2026-10-23 日志覆盖补全回归

本轮实测共75项：**69通过，6项Linux专属检查在Windows明确跳过**。

| 验证集 | 通过 | 跳过 | 实际覆盖 |
| --- | --- | --- | --- |
| Host observer | 18 | 0 | 本机子进程双流/超时/稀疏输出、轮转首段、CID/标签、PID身份锁、容器退出生命周期fixture |
| 日志打包 | 18 | 0 | 文件和总量截断、首错优先、symlink隔离、错误部署拒绝、退出/删除容器留证、异常JSON/partial、实际归档与逐文件SHA256 |
| runtime/DP日志 | 8 | 5 | 实际原生FD与traceback捕获、超长/二进制双流、短写、写盘失败保留业务码、Docker镜像背压、首错保留；Linux继承/信号/进程组尚未实测 |
| 部署集成 | 10 | 0 | 外部调用mock：旧容器保护、preflight留证、observer失败隔离、启动顺序、原生日志挂载、collect默认all与partial返回2 |
| 原系统采集器 | 15 | 1 | 地址窗口/脱敏地址与fixture、输出限额、超时、凭据排除；Linux容器内SIGALRM尚未实测 |

另外通过实际Bash shell表达式展开和vLLM stub检查两侧`--additional-config`、每DP `ascend_log_path`、TP/DP/1M/INT8与MultiConnector参数；stub不加载模型或NPU，Windows Path分隔符归一后比较。日志JSON通过标准库`logging.config.dictConfig`加载；LocalConfig仅保留支持的debug开关，Meta为debug+both+20MiB/5份备份。

启动顺序现在为runtime先捕获，再执行CANN环境包装及真实服务。解释器/工具本身无法加载或runtime捕获器自身失败时，仍以Docker原始日志兜底，不能声称Python已捕获其自身尚未运行时的错误。全部系统或业务调用均未在服务器执行。

复现命令（在本目录）：

```bash
python3 -W error::ResourceWarning -m unittest discover -s . -p 'test_*.py' -v
```

独立系统采集器测试位于仓库`tools/test_collector.py`。本次结构化结果和测试输出见仓库`validation/logging-20261023/`；输出仅为本地测试数据，未含服务器原始日志。

官方文档源：`4e6eac4e476d9be3476e99440f4cabed67b253ea`，本地快照`research-pinned/GLM5.3-Flash-latest-20261023.md` SHA256：`21540693b8d00a1481218ccdd9457fdbe84ff5dc6f7a938041920a5d71ad24b2`。

未验证：固定rc2镜像的实际依赖与CLI、FusionOS/HDK/CANN兼容性、真实权重身份、NPU显存容量/1M、HCCS地址可达、HAL分配、MemCache命中、MTP/C8精度、推理性能与故障恢复。以上本地通过不代表服务器可用或业务验收。
