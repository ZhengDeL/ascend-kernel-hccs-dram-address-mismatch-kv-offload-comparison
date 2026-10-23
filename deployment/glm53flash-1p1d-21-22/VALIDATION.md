# 本地验证记录

日期：2026-10-23。执行环境：Windows x86_64、Python 3.12、Git Bash；没有连接服务器、Docker或NPU。

- 4个Python文件：AST解析及`python -m py_compile`通过。
- 8个Shell文件：Git Bash `bash -n`通过；文件使用LF。
- 4份`deploy.py plan`（P21、D22、Meta21、proxy21）：均能离线生成并解析JSON，未调用Docker、Linux命令或网络。
- 拓扑断言：P2×8、D4×4；每侧16worker、每worker20GB/每机320GB；Meta5000/6000/8000；proxy8081；D API9900–9903。
- 直接执行`start_meta.sh --dry-run`、`start_node.sh --node 22 --dry-run`通过。包装会优先使用python3，缺失时使用python，Windows本机可预览。
- 逐项解析官方latest A3 P/D shell命令，对照本包各25个serving参数完全一致；本包仅添加显式prefix/chunked开关并换成第5.4节MultiConnector JSON。JSON参数按对象比较，不依赖空白格式。
- `memcache_dp_init_barrier=true`、非layerwise、D consumer load/put均false；worker0GB与独立384GB Provider方案均已从最终包移除。

官方文档源：`4e6eac4e476d9be3476e99440f4cabed67b253ea`，本地快照`research-pinned/GLM5.3-Flash-latest-20261023.md` SHA256：`21540693b8d00a1481218ccdd9457fdbe84ff5dc6f7a938041920a5d71ad24b2`。

未验证：固定rc2镜像的实际依赖与CLI、FusionOS/HDK/CANN兼容性、真实权重身份、NPU显存容量/1M、HCCS地址可达、HAL分配、MemCache命中、MTP/C8精度、推理性能与故障恢复。以上本地通过不代表服务器可用或业务验收。
