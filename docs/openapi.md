# SCNet OpenAPI transport

## 定位

OpenAPI transport 让本机无需 SSH 私钥即可完成：

- 认证并发现用户有权访问的 HPC 区域；
- 发现区域用户、home path 和 scheduler；
- 创建目录并上传 PDF；
- 提交短生命周期的控制作业；
- 查询 manifest 和文件队列状态；
- 递归下载合并结果。

OCR 模型仍由 Slurm worker 执行。OpenAPI 只替代本机到集群的控制与文件传输通道，
不会让 OCR 模型在本机或平台 API 服务中运行。

## 凭据

首次配置：

```bash
scnet-ocrdrop setup new
```

需要 SCNet 平台用户名、AccessKey 和 SecretKey。SecretKey 输入不回显。

凭据查找优先级：

1. `SCNET_OPENAPI_USER`、`SCNET_OPENAPI_ACCESS_KEY`、
   `SCNET_OPENAPI_SECRET_KEY`；
2. macOS Keychain；
3. Linux Secret Service。

Keychain/Secret Service 的 service 名称与 `scnet-hpc` 兼容，因此两个工具可以复用
同一组平台凭据。区域 token 每次调用时重新获取，只保存在当前进程内。

普通配置文件只保存：

- transport 和默认 OCR backend；
- 本机启用的区域 ID；
- 默认区域 ID 与显示名称；
- 每个启用区域的 scheduler 列表和选择；
- home-relative deployment root；
- 凭据 provider 名称。

它不会保存 AK、SK、token、平台用户名、区域用户名或 home path。

## 多区域选择

交互式 setup 先多选本机启用区域，再从已启用区域中选择一个默认区域：

```text
选择在本机启用的 HPC 区域：
  [x] 华东一区【昆山】
  [x] 西北二区【甘肃】
  [ ] 华东二区【合肥】

默认区域：华东一区【昆山】
```

支持 ↑/↓ 移动、Space 勾选、`a` 全选、`n` 清空和 Enter 保存。非交互终端使用
编号列表，例如 `1,3,5-8`。

`setup modify` 会读取已有的 `enabled_region_ids`、`default_region_id` 和各区域
`scheduler_id` 作为默认值。也可以显式执行：

```bash
scnet-ocrdrop setup modify \
  --enable-region 11250,11257 \
  --default-region 11250 \
  --region-scheduler 11250=<scheduler-id>
```

运行命令时 region 选择优先级为：

1. `--region`；
2. `SCNET_OPENAPI_REGION_ID`；
3. setup 保存的默认区域。

指定区域后，scheduler 选择优先级为：

1. `--scheduler-id`；
2. `SCNET_OPENAPI_SCHEDULER_ID`；
3. 该区域保存的 `scheduler_id`。

未启用区域不会被静默使用。每次上传、部署或提交仍必须解析为一个明确区域，不会把
同一次操作广播到多个区域。

## 远端路径

公开和本机持久配置使用相对路径：

```text
softwares/projects/scnet-ocrdrop/deployments/mineru3
```

OpenAPI 返回的用户 home path 只在内存中使用。客户端会拒绝把 deployment root
解析到所选 home 之外。

远端 deployment 的私有 `config.json` 可以包含 runtime、模型和系统软件绝对路径，
但它：

- 不进入 Git；
- 上传后设置为 `0600`；
- 不由 `setup status` 输出；
- 不进入 fetch 后的公开结果 manifest。

## 控制作业

OpenAPI 不提供任意登录节点命令执行接口。需要运行远端控制器时，客户端提交一个短
CPU 控制作业：

```text
OpenAPI submit
  └── python3 app/mineru_drop.py submit ...
        ├── plan chunks
        ├── sbatch worker array
        └── sbatch merge job
```

控制作业结束后，客户端读取其 stdout 并解析控制器 JSON。长时间 `wait` 不会重复
提交控制作业；它通过文件 API 读取 `manifest.json` 和四个 queue 目录来计算状态。

因此目标集群必须允许调度作业调用 `sbatch` 提交后续 worker 和 merge 作业。

## 部署

如果所选用户 home 下还没有 deployment，需要准备私有配置并执行：

```bash
scnet-ocrdrop --transport openapi --ocr-backend mineru3 deploy \
  --config config.local/mineru3.json \
  --launcher config.local/dcu-python
```

OpenAPI deploy 会上传：

- 控制器和 backend Python 包；
- runtime launcher；
- 私有 `config.json`。

然后提交一个短 CPU 作业设置 `config.json` 和 launcher 权限。模型、venv、用户态
glibc 和其他 runtime 仍需预先存在，不能由本仓库恢复。

## 限制

- OpenAPI live 行为依赖目标区域是否同时开放 HPC 和 efile 服务。
- 一个区域有多个 scheduler 时，必须在 setup 中为该区域明确选择。
- setup 会发现所有授权区域；普通操作只刷新当前目标区域，不扫描其他启用区域。
- 控制作业默认超时 10 分钟，可用 `--control-timeout` 调整。
- deploy 和 submit 是有副作用的操作；网络超时后不要盲目重试，应先查询作业和文件。
- 环境变量适合 CI 或一次性注入，不应写入 shell trace、公开日志或仓库文件。
