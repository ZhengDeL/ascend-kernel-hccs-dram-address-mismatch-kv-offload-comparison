# GLM-5.3-Flash：两台 A3 的官方 1P1D + MemCache

本目录按用户最后指定的 [latest 中文文档第5.3/5.4节](https://docs.vllm.ai/projects/ascend/zh-cn/latest/tutorials/models/GLM5.3-Flash.html#54-prefill-decode-disaggregation-with-kv-cache-pool) 准备，网页对应源码冻结于 `4e6eac4e476d9be3476e99440f4cabed67b253ea`，核查日期2026-10-23。仅完成本地准备，未登录服务器或启动服务。

| 服务器 | 模型并行 | API | DDR池与其他服务 |
| --- | --- | --- | --- |
| 192.0.2.21 | P：DP2/TP8/PP1 | 9081、9082 | 16 worker×20GB=320GB；Meta5000、ConfigStore6000、metrics8000；proxy8081 |
| 192.0.2.22 | D：DP4/TP4/PP1 | 9900–9903 | 16 worker×20GB=320GB |

每台16个逻辑NPU，共32个worker、640GB DDR池。**MemCache Meta在21；DDR池按最新官方配置分布在两台worker中，LocalService由worker初始化。** 无独立Provider，23/24不参与，旧 `set-a-1-2-7-8-9-39` 原件不动。

权重固定 `/var/ai-model/GLM-5.3-Flash-w8a8`；镜像保留用户要求的 `quay.io/ascend/vllm-ascend:v0.30.0rc2-a3`。部署目录建议 `/var/ai-model/lzd/glm53flash-1p1d-21-22`；日志始终在其 `runs/<时间>-<服务>-<节点>/` 下，不写旧 `/mnt/models`。

## 文档与镜像的版本边界

用户链接的latest与rc2 tag模型文档已有实质差异。最终只采用latest A3的DP2TP8/DP4TP4、1M、动态C8、MTP3与每worker20GB池配置。固定rc2镜像保持不变，**不能因此保证其实际二进制支持latest全部行为**。运行时记录实际vLLM、vLLM-Ascend、MemCache、MemFabric、Mooncake版本与barrier源码线索，最终由真实CLI、模型加载、图捕获、HAL和业务验证。缺包/参数不兼容即失败留证，不自动安装或改镜像。

上游rc2源码已含`memcache_dp_init_barrier`从Connector到Worker到Backend的支持，字段存在本身仍不等于容器内完整功能验收。latest官方将本页PD方案定位为示例参考，不是生产认证；客户生产推荐应参考其MindIE Motor方案。本包继续完成用户要求的本地官方示例准备。

最新参数：两侧V1 runner，`--max-model-len 1048576`、`--kv-cache-dtype int8`、MTP3。P maxseq64/batch4096，MTP草稿eager=true；D maxseq32/batch192、FULL_DECODE_ONLY、MTP草稿eager=false。两侧FlashComm1、glm47 reasoning、HCCS资源配置按原文。P保留`--prefix-cache-retention-interval None`并按池化节添加prefix caching；chunked保持启用，hybrid KV manager不禁用。P最新脚本没有主模型`--enforce-eager`，未沿用旧tag设置。

两侧均为`MultiConnector`：`MooncakeConnectorV2`负责P→D，`AscendStoreConnector(backend=memcache)`负责池化。子Store设置`memcache_dp_init_barrier=true`、`use_layerwise=false`；D另设置`consumer_is_to_load=false`、`consumer_is_to_put=false`。按原文保留P engine_id=0、D=1；lookup IPC标识随DP rank唯一。没有旧GLM-5.3的42,36层分配、旧index缓存或W8A8C8权重路径。当前C8是官方在W8A8权重上使用动态INT8 KV缓存，不要求换一套C8权重。

MemCache配置与原文一致：world_size256、protocoldevice_sdma、dram.size20GB、max.dram.size1024GB、storage.enabledfalse。`MMC_META_CONFIG_PATH`与`MMC_LOCAL_CONFIG_PATH`为精确变量名；环境包装自动找到已安装MemCache lib和Python LIBDIR加入库路径。Meta使用Python API，包装额外保留`MetaService.main()`的实际返回码。

## 已知OS与启动门槛

用户口述一台`wk1`为FusionOS23，VERSION_ID23.3.10、ID_LIKEopenEuler、内核`5.10.0-316.0.0.219.u200.fos23.x86_64`、CPUx86_64。尚未把此输出对应到21或22，也未证明两台完全一致。此信息不构成HDK/CANN/灵衢兼容认证；不升级BIOS或OS。

真实启动前检查Linux本机IP/NIC、16设备、amd64镜像本地存在、驱动及hccn.conf路径、端口、真实模型metadata/量化标识/shard完整性。模型启动需root只读核对旧vLLM/通信测试进程与其他进程持有的NPU设备描述符；发现占用即拒绝，不停止已有服务。同名容器一律拒绝复用或删除。

每台模型启动前`MemAvailable`至少384GiB，是本包为320GiB池增加64GiB余量的本地保护候选，不属于官方要求，也不足以证明模型冷加载内存充足；仍需现场容量规划。此检查不证明DRAM位于HCCS可达窗口，实际HAL初始化才是关键门槛。启动流程不自动全量扫描PFN，可在维护时段按实际版本手动执行`mfcli mem scan`与`mfcli mem scan -m 2`并留证。

镜像缺失会明确报“未拉取”；不会自动pull/pip install/SSH/stop/restart/delete/reboot，不含凭据，不转储全量环境。Docker主进程监督实际服务，保留失败退出码与退出容器，没有`sleep infinity`。

## 本机预览

这一步不需要Docker、Linux或NPU，不访问网络：

```bash
python3 deploy.py plan --node 21 --service model
python3 deploy.py plan --node 22 --service model
bash start_meta.sh --dry-run
bash start_node.sh --node 21 --dry-run
bash start_node.sh --node 22 --dry-run
bash start_proxy.sh --dry-run
```

将同一份目录放到两台服务器；NIC按对应IP自动查找，也可通过`--nic bond0`限制。`docker-run.sh --service ... --node ...`与`start_*.sh`是同一直接Docker入口，选一个使用，勿重复执行。

## 本机启动步骤

1. 在21启动Meta，确认5000/6000与metrics8000正常：

   ```bash
   cd /var/ai-model/lzd/glm53flash-1p1d-21-22
   bash start_meta.sh
   docker logs -f glm53flash-21-22-meta-21
   ```

2. 在22启动D，等待4个引擎及其每worker20GB LocalService完成初始化：

   ```bash
   cd /var/ai-model/lzd/glm53flash-1p1d-21-22
   bash start_node.sh --node 22
   ```

3. 在21启动P，等待2个引擎及其每worker20GB LocalService完成初始化：

   ```bash
   bash start_node.sh --node 21
   ```

4. 所有6个API的`/health`返回200后，在21启动镜像自带官方proxy；脚本会逐端点预检：

   ```bash
   bash start_proxy.sh
   curl --noproxy '*' http://192.0.2.21:8081/healthcheck
   ```

外层成功只表示容器被创建并在早期检查时运行，不能当作推理/卸载成功。看每次运行的`plan.json`、`versions.json`、`stdout.log`、`dpN/stdout.log`、`dpN/plog`、`exit-status.json`。Meta/模型失败后容器保留，处理已有失败容器属于运维单独操作，本脚本不自动清理。

固定端口：21Meta5000/6000/8000、proxy8081；两台各自DP RPC12325；P握手36680–36697、D36580–36599；P API9081/9082，D API9900–9903。Mooncake传输引擎还有动态RPC和HCCL通信，固定端口预检不等于完整防火墙证明；按真实灵衢/HCCS组网核实。

## 验证与采集

```bash
curl --noproxy '*' http://192.0.2.21:8081/v1/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"glm","prompt":"The future of AI is","max_tokens":50}'
bash collect_logs.sh --node 21 --service model
bash collect_logs.sh --node 22 --service model
bash collect_logs.sh --node 21 --service meta
```

需验证实际生成内容、finish_reason、SSE，另一个P rank的MemCache命中/put/get、淘汰、P→D完整性与故障恢复。重复请求可能仅命中本地prefix，不能单独证明池命中。正式性能需实际请求数、token长度、并发、命中率、TTFT/TPOT/P99、有效GB/s与CPU消耗；本地编译/plan不覆盖这些。GLM-5.3-Flash不应设置`enable_thinking:false`或`thinking:false`。

`collect_logs.sh`只读、有界采集；存在`tools/collect_server.py`时调用完整采集器。启动前与失败后保留有限设备/内存快照，采集错误不会吞模型退出码。模型metadata SHA256与分片数量会记录，但未计算全部权重哈希，不能声称完整权重身份认证。原始服务器日志需留本地并脱敏审查，不直接纳入Git。
