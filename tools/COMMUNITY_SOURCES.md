# 社区源码下载与复现

本地源码位于工作区根的 `community-sources/`，不放入部署服务器，也不把源码大树混入本仓交付。13份快照覆盖 MemCache、MemFabric、vLLM-Ascend rc2和本轮main、vLLM rc2、Mooncake、LMCache-Ascend、LMCache、YuanRong DataSystem、HIXL、HCOMM、UMDK和原始调研仓。

本轮实际结果见 [COMMUNITY_SOURCES.json](../COMMUNITY_SOURCES.json)：13/13成功，38,596个常规文件、605,092,097字节。GitHub压缩包另保存于本地 `archives/`；GitCode组件保留git checkout。`manifests/*.sha256` 为逐文件SHA256、字节数和相对路径；本地 `download-report.json` 有完整下载报告，`verification-report.json` 有独立复核结果。源码保持原项目许可，下载不等于适配或实机验收。

复现本轮固定提交（在工作站执行）：

```bash
python tools/download_community_sources.py --output community-sources --workers 4 --lock-file COMMUNITY_SOURCES.json
```

仅下载部分组件可加 `--only memcache memfabric hixl hcomm`。不传 `--lock-file` 时，脚本中已经固定的研究提交保持固定；未固定的项目先解析所列分支当前commit并记录，结果可能随社区更新。LMCache核心默认分支为 `dev`。已有不完整目录会保留并报错，不自动删除；先人工检查后移走，再重试。

不会递归拉取子模块。本轮记录了15个未拉取子模块；各项目`.gitmodules`及清单保留其来源。另有12个归档链接项仅记录、未在Windows物化；原始归档保留这些链接元数据。所有常规源文件已解包；这些限制明确记录，不能把目录称为已完成全部构建依赖安装。

解包拒绝绝对路径、`..`、Windows drive/反斜线越界和不支持的特殊对象；符号/硬链接只记清单。工具不安装软件、不执行下载项目的脚本、不启动服务、不连接部署服务器，也不修改BIOS/OS。它会使用官方站点网络下载和git读取。
