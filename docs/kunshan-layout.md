# 昆山目录规范

## 固定根目录

scnet-ocrdrop 在昆山的唯一长期根目录是：

```text
$HOME/softwares/projects/scnet-ocrdrop/
```

不要再创建 `~/mineru-drop-*`、`~/ocrdrop-*` 或其他平级临时目录。

## 完整目录

```text
$HOME/softwares/projects/scnet-ocrdrop/
├── deployments/
│   ├── mineru3/
│   │   ├── app/
│   │   │   ├── mineru_drop.py
│   │   │   ├── dcu-python
│   │   │   └── ocrdrop/
│   │   │       ├── __init__.py
│   │   │       └── backends/
│   │   │           ├── __init__.py
│   │   │           ├── base.py
│   │   │           ├── mineru.py
│   │   │           └── paddleocr.py
│   │   ├── config.json
│   │   ├── inbox/
│   │   └── batches/
│   ├── mineru4/
│   │   ├── app/
│   │   ├── config.json
│   │   ├── inbox/
│   │   └── batches/
│   └── paddleocr/
│       ├── app/
│       ├── config.json
│       ├── inbox/
│       └── batches/
├── runtimes/
│   ├── mineru3 -> $HOME/mineru-venv-py310
│   ├── mineru4 -> $HOME/softwares/mineru4-venv-py310
│   ├── mineru4-support -> $HOME/mineru4-test
│   ├── paddleocr -> $HOME/paddleocr-venv-py310
│   ├── glibc-2.28 -> $HOME/softwares/runtime/glibc-2.28
│   └── mineru3-model-config.json -> $HOME/scripts/mineru.json
└── LAYOUT.md
```

## Deployment 与 runtime 的边界

`deployments/<backend>/` 是可重建状态：

- `app/`：由本地 `deploy` 上传；
- `config.json`：由 `config.local/<backend>.json` 上传；
- `inbox/`：原始投递文件；
- `batches/`：任务、日志、chunk 和合并结果。

`runtimes/` 是稳定接口：

- 不复制大体积 venv；
- 不直接移动已有 venv；
- 配置只引用 `runtimes/` 下的稳定路径；
- 底层环境确需迁移时，只更新软链接并重新执行 `doctor`。

系统级 GCC 和 DTK 继续使用 `/public/software/` 下的平台路径，不在项目目录重复安装。

## 历史目录

迁移前存在：

```text
$HOME/mineru-drop
$HOME/mineru4-drop
$HOME/scnet-ocrdrop-paddleocr
```

迁移后它们是指向规范 deployment 的兼容软链接。历史 manifest、source path 和旧脚本
仍可访问，但新命令不得再把这些旧名称写入配置。

## 不可随意变更

以下路径视为稳定接口：

```text
softwares/projects/scnet-ocrdrop/deployments/mineru3
softwares/projects/scnet-ocrdrop/deployments/mineru4
softwares/projects/scnet-ocrdrop/deployments/paddleocr
softwares/projects/scnet-ocrdrop/runtimes/
```

如确需变更：

1. 确认没有活动 Slurm 作业；
2. 保留旧路径软链接；
3. 更新本地 `config.local/`；
4. 重新部署三个 backend；
5. 分别执行 `doctor`；
6. 用短 PDF 完成 push、merge、fetch 验收；
7. 再允许正式任务进入新路径。

## 日常维护

查看三个 deployment 占用：

```bash
ssh kseshell \
  'du -sh softwares/projects/scnet-ocrdrop/deployments/*'
```

查看最近批次时，必须指定 backend：

```bash
OCRDROP_SSH=kseshell \
OCRDROP_REMOTE_ROOT=softwares/projects/scnet-ocrdrop/deployments/mineru3 \
  ./bin/scnet-ocrdrop status
```

不要手工移动 `running/` 中的任务。只有确认原 Slurm worker 已结束后，才能执行：

```bash
./bin/scnet-ocrdrop retry --batch <batch-id> --include-running
```
