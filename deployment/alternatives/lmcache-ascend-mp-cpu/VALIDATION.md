# 本地验证（2026-10-23）

环境：Windows x86_64、Python 3.12、Git Bash。未运行Docker/LMCache/vLLM/NPU，未连接服务器。

`python validate_local.py`完成10项检查：5份connector JSON结构；CAPABILITIES保留未测与未知几何；仅RFC5737/loopback/wildcard地址；2个shell脚本与文档bash片段语法及LF；真实空几何提前退出2；复制到隔离stub目录后，以**合成**attention/Mamba/chunk=8/8/16、L1=1GiB、MemAvailable=2GiB和替代memlock输出验证真实CLI参数传递，fake lmcache退出7时pipefail仍返回7。

合成数值仅在`validation-artifacts/stub-case`中使用，不是GLM部署默认值。该目录及stub stdout不进入Git；[report.json](validation-artifacts/report.json)和[空几何门禁输出](validation-artifacts/unresolved-gate.txt)作为离线证据。Git Bash不具备Linux MemAvailable/ulimit语义，因此stub覆盖这些接口；没有声称容量检查已通过真实Linux。

未验证：依赖构建/导入、镜像与hcomm匹配、GLM各缓存组resolved geometry、实际CLI启动、Host pinned内存与NPU IPC/DMA、KDA/conv状态正确性、CPU缓存store/retrieve、MultiConnector异步语义与P-D重排、1M/MTP3/INT8 KV业务正确性及退化≤5%。
