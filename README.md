# scnet-ocrdrop

面向 SCNet/Slurm 集群的文档解析投递工具。用户在本机提交 PDF，工具自动上传文件、
规划长文档分片、提交 Slurm worker、合并结果，并将 Markdown、JSON 和图片取回本地。

当前可用 backend：

| backend | 状态 | 用途 |
| --- | --- | --- |
| `mineru3` | 已验证 | MinerU 3.x pipeline |
| `mineru4` | 实验性 | MinerU 4.x Basic 等 tier |
| `paddleocr` | 已验证 | PaddleOCR 3.7 Transformers/PyTorch 纯 OCR |

项目不包含模型、MinerU/PaddleOCR 本体、集群账号或真实集群配置。

本地到集群支持两种 transport：

| transport | 认证 | 用途 |
| --- | --- | --- |
| `openapi` | SCNet 平台用户名 + AK/SK | 自动发现区域，通过 OpenAPI 上传、提交控制作业和下载 |
| `ssh` | OpenSSH profile 和私钥 | 兼容现有部署，适合环境维护和低层诊断 |

transport 和 OCR backend 是两个独立维度，例如 OpenAPI + MinerU 3，或 SSH +
PaddleOCR。

## OpenAPI 快速开始

首次使用运行：

```bash
./bin/scnet-ocrdrop setup new
```

配置面板会：

1. 询问 SCNet 平台用户名、AccessKey 和 SecretKey；
2. 验证凭据并自动发现可用 HPC 区域与 scheduler；
3. 选择默认 OCR backend；
4. 在 macOS 使用 Keychain、在 Linux 使用 Secret Service 保存 AK/SK；
5. 只把区域、scheduler 和 backend 等非敏感选择写入本机配置。

本机配置位于 `$XDG_CONFIG_HOME/scnet-ocrdrop/config.json` 或
`~/.config/scnet-ocrdrop/config.json`。目录权限为 `0700`，文件权限为 `0600`。
AK/SK 和区域 token 不会写入该 JSON。

没有系统凭据库时，通过环境变量注入：

```bash
export SCNET_OPENAPI_USER="<platform-user>"
export SCNET_OPENAPI_ACCESS_KEY="<access-key>"
export SCNET_OPENAPI_SECRET_KEY="<secret-key>"
```

不要把这些变量写入仓库中的脚本或 `.env` 文件。配置生命周期：

```bash
./bin/scnet-ocrdrop setup status
./bin/scnet-ocrdrop setup modify
./bin/scnet-ocrdrop setup reset
./bin/scnet-ocrdrop setup reset --credentials
```

完成配置且远端 deployment 已存在后：

```bash
./bin/scnet-ocrdrop doctor
./bin/scnet-ocrdrop push /path/to/document.pdf \
  --wait \
  --fetch \
  --output ./ocrdrop-results/mineru3
```

OpenAPI transport 会从平台返回的 home path 在内存中解析 deployment 的绝对路径；
保存的 `remote_root` 始终是 home-relative 路径。远端尚未部署控制器、runtime 和模型
时，仍需先按“部署”章节完成一次部署。

## 为什么使用它

- 本机一条命令提交一个 PDF、多个 PDF 或整个目录。
- 所有解析任务仍由 Slurm 调度，不在登录节点运行模型。
- 长文档按页分片，短文档保持整篇。
- 一个 worker 只加载一次模型，并连续领取多个分片。
- 多 worker 使用共享文件系统上的原子 rename 动态领取任务。
- 失败分片可以单独重试，不重复计算成功分片。
- 自动合并 Markdown、图片、content list 和 middle JSON。
- 记录输入哈希、页范围、耗时、运行时版本、worker 和错误堆栈。
- 支持中文及其他非 ASCII 文件名。

## 架构

```text
Mac / workstation
  scnet-ocrdrop push
          │  OpenAPI or SSH/SCP
          ▼
Cluster login node
  inbox → create batch → split page ranges → sbatch
                                         │
                  ┌──────────────────────┴─────────────────────┐
                  ▼                                            ▼
        Slurm accelerator array                       Slurm CPU merge job
        persistent backend instance                   afterany dependency
        claim → parse → claim → parse                 merge Markdown/JSON/images
                  │                                            │
                  └──────────────────────┬─────────────────────┘
                                         ▼
                                  batches/<id>/output
                                         │
                                  fetch │ OpenAPI or SCP
                                         ▼
                                  Local results
```

详细组件、队列状态机、backend 接口和扩展方式见
[架构文档](docs/architecture.md)。

## 仓库目录

```text
scnet-ocrdrop/
├── .github/
│   └── workflows/
│       └── tests.yml                    # Python 3.8/3.10/3.12 CI
├── bin/
│   ├── scnet-ocrdrop                    # 通用入口
│   ├── kunshan-mineru3                  # 昆山 MinerU 3 快捷入口
│   ├── kunshan-mineru4                  # 昆山 MinerU 4 快捷入口
│   └── kunshan-paddleocr                # 昆山 PaddleOCR 快捷入口
├── config.local/                        # 真实配置，Git 忽略
│   ├── mineru3.json
│   ├── mineru4.json
│   ├── paddleocr.json
│   ├── dcu-python
│   └── paddleocr-dcu-python
├── docs/
│   ├── architecture.md                  # 队列、worker、merge 架构
│   ├── backends.md                      # backend 接口
│   ├── kunshan-layout.md                # 昆山固定目录与迁移策略
│   ├── openapi.md                       # AK/SK 与 OpenAPI transport
│   ├── paddleocr.md                     # PaddleOCR 环境与实测
│   └── runtime-inventory.md             # 必须保留的执行文件
├── examples/
│   ├── config/
│   │   ├── mineru3.example.json
│   │   ├── mineru4.example.json
│   │   └── paddleocr.example.json
│   └── runtime/
│       ├── dcu-python.example
│       └── paddleocr-dcu-python.example
├── src/ocrdrop/
│   ├── __init__.py
│   ├── __main__.py
│   ├── client.py                        # 本机 CLI 与输出脱敏
│   ├── config.py                        # XDG 非敏感配置
│   ├── credentials.py                   # Keychain/Secret Service
│   ├── openapi.py                       # SCNet OpenAPI 客户端
│   ├── setup_cli.py                     # setup 生命周期
│   ├── transports.py                    # SSH/OpenAPI transport
│   ├── remote.py                        # 控制器、Slurm worker、合并器
│   └── backends/
│       ├── __init__.py                  # backend 注册表
│       ├── base.py                      # backend 契约
│       ├── mineru.py                    # MinerU 3/4 持久 worker
│       └── paddleocr.py                 # PaddleOCR 持久 worker
├── tests/
│   └── test_remote.py
├── .gitignore
├── CHANGELOG.md
├── CONTRIBUTING.md
├── LICENSE
├── README.md
├── SECURITY.md
└── pyproject.toml
```

`config.local/` 中的文件只存在于当前工作机，不进入 GitHub。仓库里可公开、可复用的
模板统一放在 `examples/`。

## 昆山固定目录

从 2026-09-27 起，昆山上所有新任务固定使用下面的目录，不再在家目录新增
`*-drop`：

```text
$HOME/softwares/projects/scnet-ocrdrop/
├── deployments/
│   ├── mineru3/
│   │   ├── app/
│   │   │   ├── mineru_drop.py
│   │   │   ├── dcu-python
│   │   │   └── ocrdrop/
│   │   ├── config.json
│   │   ├── inbox/
│   │   │   └── <upload-id>/
│   │   └── batches/
│   │       └── <batch-id>/
│   │           ├── manifest.json
│   │           ├── queue/
│   │           │   ├── pending/
│   │           │   ├── running/
│   │           │   ├── done/
│   │           │   └── failed/
│   │           ├── chunks/
│   │           ├── errors/
│   │           ├── logs/
│   │           ├── output/
│   │           └── slurm/
│   ├── mineru4/                         # 目录结构同 mineru3
│   └── paddleocr/                       # 目录结构同 mineru3
├── runtimes/
│   ├── mineru3 -> $HOME/mineru-venv-py310
│   ├── mineru4 -> $HOME/softwares/mineru4-venv-py310
│   ├── mineru4-support -> $HOME/mineru4-test
│   ├── paddleocr -> $HOME/paddleocr-venv-py310
│   ├── glibc-2.28 -> $HOME/softwares/runtime/glibc-2.28
│   └── mineru3-model-config.json -> $HOME/scripts/mineru.json
└── LAYOUT.md
```

约束：

- `deployments/` 只放控制器、队列、批次和产物，不放模型或 venv。
- `runtimes/` 使用稳定软链接，避免移动已有 venv 导致 shebang 和动态库路径失效。
- 三个 backend 永远使用不同 deployment，不共享 `config.json`、`inbox/` 或
  `batches/`。
- 历史批次不删除；旧入口保留兼容软链接，但所有新命令必须使用上面的规范路径。
- 本地结果固定放在 `ocrdrop-results/<backend>/<batch-id>/`。

详细迁移规则见 [昆山目录规范](docs/kunshan-layout.md)。

## 昆山哪些文件不能删

清理前先运行：

```bash
./bin/kunshan-mineru3 inventory
./bin/kunshan-mineru4 inventory
./bin/kunshan-paddleocr inventory
```

以下内容不能依赖 Git 恢复：

- `deployments/<backend>/config.json` 的真实私有配置；
- `inbox/` 中仍需 retry 的源 PDF；
- `batches/` 中尚未归档的状态、chunk 和结果；
- `runtimes/mineru3`、`runtimes/mineru4` 和 `runtimes/paddleocr` 指向的环境；
- 用户态 glibc；
- MinerU 模型配置及其引用的模型；
- PaddleOCR det/rec 模型 cache。

`app/mineru_drop.py` 和 `app/ocrdrop/` 误删后不需要重新编译，只要重新执行
`deploy`。runtime 或模型误删则不能靠重新克隆本仓库恢复。

完整清单、恢复方式和清理规则见
[昆山执行文件与保留策略](docs/runtime-inventory.md)。

## 前置条件

本机：

- Python 3.8+
- OpenAPI 模式：能够访问 SCNet OpenAPI；
- SSH 模式：OpenSSH 的 `ssh`、`scp`，并已配置免交互登录。

集群登录节点：

- Python 3.6+
- Slurm：`sbatch`、`squeue`
- Poppler：`pdfinfo`
- 登录节点和计算节点可访问同一共享文件系统

计算节点：

- 已安装并验证对应 OCR backend
- backend 模型已提前下载；计算任务默认离线运行

## 第一次配置

克隆后进入仓库：

```bash
git clone git@github.com:lql341/scnet-ocrdrop.git
cd scnet-ocrdrop
python3 -m pip install --upgrade pip
python3 -m pip install .
```

也可以不安装，直接使用 `./bin/scnet-ocrdrop`。Git 会保存可执行位，正常 clone
后通常不需要再次 `chmod`。

## 新机器能否直接使用

分两种情况。

### 使用 OpenAPI 日常投递、查询和取回

如果用户 home 下已经存在对应 deployment，只需要克隆并完成安全配置：

```bash
git clone git@github.com:lql341/scnet-ocrdrop.git
cd scnet-ocrdrop

./bin/scnet-ocrdrop setup new
./bin/scnet-ocrdrop doctor
./bin/scnet-ocrdrop push /path/to/document.pdf --wait --fetch
```

OpenAPI 模式不需要本机 SSH 私钥。它通过平台 API 上传 PDF，提交短控制作业，再由
远端控制器提交实际 worker 和 merge 作业。`wait` 直接读取远端 manifest 和队列目录，
不会为每次轮询创建 Slurm 作业。

### 使用 SSH 日常投递、查询和取回

可以。昆山已经部署好三套 backend，新机器只需要：

1. 克隆仓库；
2. 配置能够执行 `ssh kseshell` 的 SSH 主机别名和认证；
3. 本机有 Python 3、`ssh` 和 `scp`。

然后直接运行：

```bash
git clone git@github.com:lql341/scnet-ocrdrop.git
cd scnet-ocrdrop

ssh kseshell true
./bin/kunshan-mineru3 doctor
./bin/kunshan-mineru3 push "/path/to/document.pdf" \
  --wait \
  --fetch \
  --output ./ocrdrop-results/mineru3
```

日常使用不需要本机 `config.local/`，也不需要在新机器安装 MinerU、PaddleOCR、DTK
或模型；这些都在昆山计算节点环境中。

### 需要重新部署或修复昆山环境

仅 clone 不够，因为 `config.local/` 不进入 Git。可以从现有昆山 deployment 取回：

```bash
mkdir -p config.local

scp kseshell:softwares/projects/scnet-ocrdrop/deployments/mineru3/config.json \
  config.local/mineru3.json
scp kseshell:softwares/projects/scnet-ocrdrop/deployments/mineru4/config.json \
  config.local/mineru4.json
scp kseshell:softwares/projects/scnet-ocrdrop/deployments/paddleocr/config.json \
  config.local/paddleocr.json

scp kseshell:softwares/projects/scnet-ocrdrop/deployments/mineru3/app/dcu-python \
  config.local/dcu-python
scp kseshell:softwares/projects/scnet-ocrdrop/deployments/paddleocr/app/dcu-python \
  config.local/paddleocr-dcu-python

chmod +x config.local/dcu-python config.local/paddleocr-dcu-python
```

随后执行三个 wrapper 的 `deploy`。这仍然不叫“编译”；它只是重新上传 Python
源码、launcher 和配置。若 runtime 或模型被删除，则必须从备份恢复或按构建报告
重新安装。

复制配置模板。真实配置必须放在 `config.local/`，不要提交：

```bash
cp examples/config/mineru3.example.json config.local/mineru3.json
cp examples/runtime/dcu-python.example config.local/dcu-python
chmod +x config.local/dcu-python
```

PaddleOCR 使用独立环境和 launcher：

```bash
cp examples/config/paddleocr.example.json config.local/paddleocr.json
cp examples/runtime/paddleocr-dcu-python.example \
  config.local/paddleocr-dcu-python
chmod +x config.local/paddleocr-dcu-python
```

编辑 `config.local/mineru3.json`，至少确认：

- 远端工作根目录
- DCU/GPU 和 CPU 分区
- `gres`、CPU、内存和 walltime
- backend venv、模型配置和 Python launcher
- 集群 module 名称
- launcher 所需的 `runtime_env`

昆山 SSH 主机别名固定为：

```bash
export OCRDROP_SSH=kseshell
```

每次操作必须明确选择 backend：

```bash
# MinerU 3：当前生产默认
export OCRDROP_REMOTE_ROOT=softwares/projects/scnet-ocrdrop/deployments/mineru3

# MinerU 4：实验环境
export OCRDROP_REMOTE_ROOT=softwares/projects/scnet-ocrdrop/deployments/mineru4

# PaddleOCR：纯 OCR
export OCRDROP_REMOTE_ROOT=softwares/projects/scnet-ocrdrop/deployments/paddleocr
```

## 部署

首次部署或代码升级时执行。日常投递 PDF 不需要重复部署。

MinerU 3：

```bash
export OCRDROP_SSH=kseshell
export OCRDROP_REMOTE_ROOT=softwares/projects/scnet-ocrdrop/deployments/mineru3

./bin/scnet-ocrdrop deploy \
  --config config.local/mineru3.json \
  --launcher config.local/dcu-python

./bin/scnet-ocrdrop doctor
```

MinerU 4：

```bash
export OCRDROP_SSH=kseshell
export OCRDROP_REMOTE_ROOT=softwares/projects/scnet-ocrdrop/deployments/mineru4

./bin/scnet-ocrdrop deploy \
  --config config.local/mineru4.json \
  --launcher config.local/dcu-python

./bin/scnet-ocrdrop doctor
```

PaddleOCR：

```bash
export OCRDROP_SSH=kseshell
export OCRDROP_REMOTE_ROOT=softwares/projects/scnet-ocrdrop/deployments/paddleocr

./bin/scnet-ocrdrop deploy \
  --config config.local/paddleocr.json \
  --launcher config.local/paddleocr-dcu-python

./bin/scnet-ocrdrop doctor
```

每套 backend 的 `doctor` 都必须成功后再提交正式文档。

## 日常执行

昆山日常使用优先使用三个快捷入口，不需要手动设置环境变量：

```text
./bin/kunshan-mineru3      结构化生产解析
./bin/kunshan-mineru4      MinerU 4 实验解析
./bin/kunshan-paddleocr    纯文字 OCR
```

### MinerU 3：结构化 PDF 转 Markdown

推荐用于论文、合同、表格、公式和图片：

```bash
cd /path/to/scnet-ocrdrop

./bin/kunshan-mineru3 push "/path/to/中文长文档.pdf" \
  --wait \
  --fetch \
  --output ./ocrdrop-results/mineru3
```

### PaddleOCR：批量纯文字 OCR

推荐用于纯文本提取、扫描件和 OCR 复核：

```bash
cd /path/to/scnet-ocrdrop

./bin/kunshan-paddleocr push "/path/to/扫描件.pdf" \
  --wait \
  --fetch \
  --output ./ocrdrop-results/paddleocr
```

### MinerU 4：实验解析

```bash
cd /path/to/scnet-ocrdrop

./bin/kunshan-mineru4 push "/path/to/document.pdf" \
  --wait \
  --fetch \
  --output ./ocrdrop-results/mineru4
```

`push --wait --fetch` 会依次：

1. 上传 PDF；
2. 创建批次并提交 Slurm；
3. 定期显示 `pending/running/done/failed`；
4. 等待 merge 完成；
5. 下载整个 `output/`。

### 异步提交

先设置正确的 `OCRDROP_REMOTE_ROOT`，然后执行：

```bash
./bin/scnet-ocrdrop push report.pdf
```

输出示例：

```json
{
  "batch_id": "20260927-190425-eb078d",
  "status": "submitted",
  "task_count": 4,
  "workers": 1
}
```

`submitted` 表示 Slurm 已接受任务，不表示解析已经完成。

### 查看、等待和取回

后续命令必须和 `push` 使用同一个 backend root。例如查询 MinerU 3：

```bash
export OCRDROP_SSH=kseshell
export OCRDROP_REMOTE_ROOT=softwares/projects/scnet-ocrdrop/deployments/mineru3

# 查看指定批次
./bin/scnet-ocrdrop status --batch <batch-id>

# 阻塞等待完成；每 20 秒查询一次
./bin/scnet-ocrdrop wait --batch <batch-id>

# 完成后取回
./bin/scnet-ocrdrop fetch \
  --batch <batch-id> \
  --output ./ocrdrop-results/mineru3/<batch-id>
```

最终结果位于：

```text
ocrdrop-results/<backend>/<batch-id>/output/<document-id>/
├── document.md
├── content_list.json
├── middle.json
├── manifest.json
├── images/
└── chunks/
```

### 批量提交

多个文件：

```bash
./bin/scnet-ocrdrop push a.pdf b.pdf c.pdf
```

目录递归发现 PDF：

```bash
./bin/scnet-ocrdrop push /path/to/papers/
```

单个长文档默认使用一个持久 worker，避免多个 worker 重复加载同一套模型。多个文档
默认最多使用配置中的 `max_workers` 并行处理。

### 调整分片

```bash
./bin/scnet-ocrdrop push book.pdf \
  --workers 2 \
  --chunk-pages 64 \
  --whole-document-pages 96
```

- 页数不超过 `whole_document_pages`：整篇解析。
- 更长文档：按 `chunk_pages` 切分。
- 分片不重叠；跨边界段落和表格需要人工复核。

### 只查看计划

```bash
./bin/scnet-ocrdrop push papers/ --plan-only
```

### 失败后重试

```bash
./bin/scnet-ocrdrop retry --batch <batch-id>
```

如果原 Slurm 作业已结束，但任务因节点故障留在 `running/`：

```bash
./bin/scnet-ocrdrop retry \
  --batch <batch-id> \
  --include-running
```

必须先确认原 worker 已经结束，避免两个 worker 同时写同一个分片。

## 状态含义

| 状态 | 含义 |
| --- | --- |
| `planned` | 已生成任务，但尚未提交 |
| `submitted` | Slurm 已接受任务 |
| `running` | 至少一个分片等待或运行中 |
| `parsed` | 所有分片成功，等待 merge |
| `complete` | 所有分片和合并均成功 |
| `partial` | 至少一个分片失败，只合并成功部分 |

## MinerU 3 与 MinerU 4

不同 MinerU 主版本应使用独立 venv、模型目录和远端 root，不要原地升级生产环境：

```bash
# MinerU 3
OCRDROP_REMOTE_ROOT=softwares/projects/scnet-ocrdrop/deployments/mineru3 \
  ./bin/scnet-ocrdrop deploy --config config.local/mineru3.json

# MinerU 4
OCRDROP_REMOTE_ROOT=softwares/projects/scnet-ocrdrop/deployments/mineru4 \
  ./bin/scnet-ocrdrop deploy --config config.local/mineru4.json
```

worker 会检查 backend 与 MinerU 主版本是否匹配，并在同一进程中复用模型。

## PaddleOCR

`paddleocr` backend 使用 PaddleOCR 3.7 的 Transformers/PyTorch engine 和
PP-OCRv5 mobile det/rec 模型，不依赖 PaddlePaddle DCU wheel。它已经在 Z100/gfx906
上完成 Slurm、常驻 worker、分片、合并和 fetch 验收。

部署到独立远端 root：

```bash
export OCRDROP_REMOTE_ROOT=softwares/projects/scnet-ocrdrop/deployments/paddleocr

./bin/scnet-ocrdrop deploy \
  --config config.local/paddleocr.json \
  --launcher config.local/paddleocr-dcu-python

./bin/scnet-ocrdrop doctor
```

提交并自动取回：

```bash
./bin/scnet-ocrdrop push text-heavy.pdf \
  --wait \
  --fetch \
  --output ./ocrdrop-results/text-heavy
```

PaddleOCR backend 实现统一的 `DocumentBackend` 接口：

```python
class DocumentBackend:
    def parse(self, task, task_output):
        ...

    def shutdown(self):
        ...
```

共享层继续负责上传、分片、Slurm 调度、任务领取、重试、状态和取回；PaddleOCR
backend 负责按页渲染、文本检测/识别和标准产物输出。

它输出：

- 纯文本 Markdown；
- 每行文字、置信度和 polygon；
- 全局 PDF 页码；
- 渲染、导入、模型加载、warmup 和逐页 OCR 耗时。

它不恢复标题层级、表格、公式和图片。需要结构化 PDF 转 Markdown 时继续使用
MinerU。详细环境、配置字段、实测数据和限制见
[PaddleOCR backend 文档](docs/paddleocr.md)。

## 测试

```bash
python3 scripts/public_release_audit.py
python3 -m unittest discover -s tests -v
```

测试不需要集群或 OCR 模型。

公开仓库前还应扫描所有可达 Git 历史：

```bash
python3 scripts/public_release_audit.py --history
```

默认模式检查当前已跟踪及待提交文件，CI 每次 push/PR 都会运行。`--history` 会检查
所有本地 branch/tag 可达提交；如果它报告真实个人路径或凭据，必须在切换 Public 前
重写相应历史。

## 安全

- `config.local/`、PDF 和解析结果默认被 Git 忽略。
- 仓库不保存 SSH 私钥、AK/SK、口令、token 或模型。
- AK/SK 只进入 Keychain、Secret Service 或当前进程环境。
- 本机非敏感配置使用 `0700/0600` 权限和原子写入。
- 状态、doctor 和 inventory 默认隐藏个人绝对路径；仅本地诊断时使用
  `--show-paths`。
- 合并输出中的 manifest 和 chunk 记录不会包含源文件绝对路径、输出目录或节点名。
- 真实远端 `config.json` 上传后会设置为 `0600`。
- `fetch` 前确认本地目标目录，避免混合不同批次的同名输出。

## License

Apache-2.0。OCR backend 及模型遵循各自许可证，本仓库不重新分发它们。
SCNet OpenAPI 和凭据存储代码的来源说明见
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。
