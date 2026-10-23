# AMD CPU + Ascend 910C + 灵衢：只读现场信息采集

目的：在 BIOS 无法升级或改动的约束下，先区分物理地址布局、内核与驱动、NUMA/锁页/cgroup 限额、容器版本及 MemCache 运行条件。采集结果用于选择软件修复和小规模验证路线；采集器不会修复系统，也不能证明 HCCS、MemCache 或性能已经合格。

本工具需要 Linux、Python 3.8+，只用标准库。没有安装依赖、联网、上传、重启、服务启动、sysctl/GRUB/BIOS 修改、NPU 初始化、PFN 扫描、pagemap 读取、devmem 访问或业务请求。`npu-smi info` 查询现有设备；容器查询通过现有 Docker 容器内的 Python 读取 metadata 和精选信息，不导入 torch、torch_npu 或 vLLM。命令可能产生正常审计日志；这不是零系统开销采集。

## 在哪些服务器执行

在每台参与测试的计算服务器各采一份；独立部署的 LocalService、Meta 节点也各采一份。不要只采一台后认为所有节点一致。建议在故障发生后、现场状态尚未变化时采集，记录故障时间与时区。普通用户可以执行，但 `iomem` 地址、内核日志、DMI、其他用户进程可能被屏蔽；已有管理员权限时用 root 采集可补齐证据。本工具不会自行提权。

将本目录三个 Python 文件放到服务器的一个工作目录。输出目录必须不存在，避免混合两次结果：

```bash
python3 collect_server.py --output /var/tmp/evidence-compute-a-20261023
```

如果已知故障进程 PID、已有业务容器或 LocalService 容器，按实际名称填写。参数可重复；不要照抄示例 PID 或容器名：

```bash
python3 collect_server.py \
  --output /var/tmp/evidence-compute-a-with-process-20261023 \
  --pid 12345 \
  --container existing-model-container \
  --container existing-localservice-container
```

仅采文件、不执行 `npu-smi`、Docker、DMI、日志查询等外部命令时使用：

```bash
python3 collect_server.py --output /var/tmp/evidence-files-only-20261023 --no-commands
```

默认每条外部命令最多 15 秒，每个文件和每条命令的 stdout/stderr 各最多保留 2 MiB。命令输出由双线程持续读取并截断保留，避免双管道阻塞和无界内存增长；命令超时终止采集器新建的命令进程组。容器内 metadata 子进程另有 Linux SIGALRM 自限时，避免只终止 Docker CLI 后遗留容器查询进程。命令缺失、无权限、非零退出、超时与截断都会保留记录。某些工具缺失属采集缺口，不代表服务器故障。现场工具非常慢时可指定 `--timeout 30`，允许范围 0.1–120 秒。多个命令串行执行，因此总耗时可以超过单条超时。

## 采集范围

| 类别 | 实际采集内容 | 判断边界 |
| --- | --- | --- |
| CPU/OS/NUMA | uname、lscpu、numactl、numastat、OS release、NUMA node meminfo/cpulist/distance/numastat、node memory block 归属链接 | NUMA 空闲量和物理地址可达性是不同信息 |
| 物理布局与分配状态 | iomem、meminfo、buddyinfo、pagetypeinfo、zoneinfo、hugepages、THP、内存压力、swap、精选 VM sysctl | 不读取 pagemap、不扫描 PFN；buddy/free 不能代替硬件容量 |
| 启动内存参数 | 仅提取 IOMMU、NUMA、hugepages、mem/memmap、crashkernel 等白名单参数 | 不收集完整 `/proc/cmdline`，避免其中的凭据 |
| NPU/PCI/IOMMU | npu-smi info、lspci 树与详细信息、PCI NUMA/link/resource/driver、IOMMU group 链接 | 没有端到端 HCCS 验证；PCIe 带宽不能当作灵衢带宽 |
| 驱动与包版本 | lsmod、精选 modinfo、sysfs module version/srcversion、Ascend 版本文件、Python distribution metadata | metadata 不导入业务包；与进程真实加载的库仍需对照 maps 和已部署文件哈希 |
| OS 日志与固件标识 | 当前 dmesg、当前启动 kernel journal 最后 2000 条、DMI BIOS/system/memory/processor | 只读取 BIOS 标识，不修改 BIOS；日志可能因权限或轮转不完整 |
| 限额/cgroup | 当前采集进程限制、根 cgroup、指定进程及容器 PID 对应 cgroup 的内存/cpuset 状态 | cgroup v1 默认 memory 路径与 v2 标准挂载支持；非标准挂载要人工补齐 |
| `--pid` 指定进程 | maps、numa_maps、status、limits、cgroup | 不收集进程 cmdline 或 environ 全集；maps 可能泄露模型/库路径 |
| `--container` 指定已有容器 | 镜像 ID/名称、状态/PID、设备映射、挂载、memory/shm/ulimits、精选 Python metadata 和 Ascend 版本文件、环境变量白名单 | 不收集完整 inspect、Config.Env、启动 Cmd/Args、标签或任意环境变量；不执行原启动脚本 |

环境变量仅使用脚本 `ENV_ALLOW` 的明确白名单，如 `FULL_MEMCACHE_STORAGE_ENABLED`、`ASCEND_BUFFER_POOL*` 中列出的具体名称、Ascend 路径/可见设备、精选 HCCL 参数和 allocator 参数。不会按前缀收取任意新变量，不收集 token、密码或私钥变量。该清单可能漏掉实际镜像新增的关键参数，需由管理员补充经脱敏的实际部署配置。

另外读取 MemFabric 的 `MF_HYBM_RDMA_SWAP_SPACE_SIZE`、`HCOM_MAX_SLICE_SIZE`、`HCOM_RECV_DATA_SIZE`、`MEMFABRIC_HYBRID_EXTEND_LIB_PATH`、`MF_HOST_SHM_USE_MEMFD` 以及 `MMC_META_CONFIG_PATH`/`MMC_LOCAL_CONFIG_PATH` 的路径值，不自动读取这些配置文件内容。`ASCEND_BUFFER_POOL` 是 MooncakeTE 路线参数，不能据此认定 MemCache 的 device_sdma 故障已修复；采集也不改变任何参数。Python 包清单包含 `memfabric-hybrid` 与 NPU wheel 的 `mooncake-transfer-engine-npu`。

## 物理地址窗口分析

默认不假设某个驱动分支的固定 `mem_scan` 范围。`iomem_analysis.json` 会报告可见 System RAM 的物理覆盖容量；未提供窗口时，窗口交集为未知。必须先找到现场驱动/内核实际采用的窗口和精确版本，再将 `windows.example.json` 复制到现场文件并填写，例如：

```json
{
  "source_reference": "实际源文件路径与行号或文档链接",
  "source_commit": "实际提交SHA或厂商构建标识",
  "windows": [
    {"name": "actual-window-0", "start": "0x100000000", "end_inclusive": "0x1ffffffff"}
  ]
}
```

**上例地址只是格式示例，不是 910C 或灵衢有效地址范围。** 地址是字节地址，结束地址为包含端点；支持整数或 `0x` 十六进制字符串。多个窗口重叠时，会分别列出每个交集，再计算去重的窗口并集交集：

```bash
python3 collect_server.py \
  --output /var/tmp/evidence-with-exact-windows-20261023 \
  --windows-json actual-version-windows.json
```

已有 iomem 文本可离线重算，无需重新访问服务器：

```bash
python3 analyze_iomem.py \
  --iomem /var/tmp/evidence-compute-a-20261023/raw/files/proc/iomem \
  --windows-json actual-version-windows.json \
  --output /var/tmp/iomem-analysis-exact-version.json
```

解析器合并重叠/嵌套的 System RAM，Kernel code 等子资源不重复计数。若地址全部被屏蔽为 `00000000-00000000`，容量输出 `null` 并标记 `addresses_hidden`；解析不完整时也不输出确定容量。**交集仅表示 iomem 描述的地址容量，不能证明当前可分配、可锁页、可注册或可经 HCCS 访问。Free=0 不能推出 Capacity=0。** 窗口归属的 NUMA node 仍标记 unknown，须与已采 node memory block 和具体驱动逻辑核对。

## 还需管理员补充的资料

工具无法替代实际故障证据。请本地保留原件，提交研究仓库前制作脱敏副本：

1. 发生错误的完整日志片段：首次失败前后时间、调用栈、错误码、物理地址、NPU/host 标识、对应请求与 LocalService/model 角色。说明是启动/注册/首次卸载/回读/跨节点传输阶段。
2. 当前角色拓扑：每台服务器 CPU/NPU/NUMA 配置、计算/LocalService/Meta 归属、实际通信接口，注明 IP 可替换为稳定代号。不要把同主机或同 NUMA 成功当作跨节点成功。
3. 现场驱动/固件/CANN/内核/容器镜像对应的完整版本或 digest、已加载关键 `.ko`/`.so` 的 SHA256、对应源码/补丁及构建配置。版本文件与 metadata 是线索，不能单独证明当前执行二进制的身份。
4. MemCache/MemFabric/vLLM connector 实际配置和部署启动参数的脱敏副本：DRAM pool 大小、backend、HCCS/其他 transport、NUMA 策略、容器 memlock/shm/内存限制、实际生效的 FULL_MEMCACHE/ASCEND_BUFFER 参数。请人工去除 API key、token、密码、私钥与客户数据。
5. 由灵衢网络管理员导出：交换机型号、软件/固件版本、L1/L2/LCN 拓扑、节点/端口映射、链路速率/状态、错误/丢包/重传/拥塞计数以及取样时间。该工具不自动 SSH 交换机。
6. 业务基准：固定模型/权重与上下文、输入输出长度、并发、命中率和数据集；无卸载与现有方案的吞吐、TTFT、TPOT/P95/P99、CPU/NUMA/NPU 利用率。正式目标为相对双方确认的基线性能劣化不超过 **5%**；现场数据尚未取得，不能承诺软件修复后的结果。

如果 Python 不在容器 PATH、容器不是 Docker 管理、没有容器权限或模块/内核源码不开放，采集器会保留缺口；由管理员提供上述脱敏资料即可。不要为了运行采集器安装依赖或重启现有业务。

## 结果、安全与校验

输出根目录权限为 `0700`，每个输出文件为 `0600`。`collection_status.json` 记录每个源文件的读取状态和每条外部命令的 argv、返回码、stderr 路径、超时、截断等；`manifest.sha256.json` 记录除自身以外的每个输出文件 SHA256。采集完整性与业务验收分开判断：命令失败不假装成功，采集结束也不表示问题已解决。

原始输出可能含服务器序列号、IP、物理地址、客户文件路径与日志内容；只能在受控的本地目录保存。**不要对采集目录执行 `git add`，不要把原始包上传 GitHub。** 本仓只提交采集工具、方法、脱敏样例和经核对的结论。自动脱敏无法保证日志和路径中没有机密；由管理员审阅后再制作可共享证据。工具默认不联网，也不提供上传参数。

## 本地验证边界

```bash
python3 -m unittest -v test_collector
```

本地测试覆盖隐藏 iomem、重叠/嵌套资源、窗口交集并集、未知 NUMA、不完整/截断输入、非法窗口、命令缺失/失败/超时/双流输出截断、容器 metadata 不导入 NPU 运行库且不采任意环境变量、Linux 文件树模拟采集及逐项 SHA256 校验。Windows Python 3.12.7 已运行 15 项通过，另 1 项 Linux SIGALRM 自限时测试因平台跳过；这不是 Linux 实机、910C、灵衢、MemCache 功能或 5% 性能目标的验收。真实 Linux `/proc`/`sysfs` 权限和现场命令行为须在服务器采集后核对状态文件。
