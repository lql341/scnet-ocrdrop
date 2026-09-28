# 昆山执行文件与保留策略

## 结论

scnet-ocrdrop 自身是 Python 源码，正常部署不需要编译。`deploy` 只是把控制器、
backend 包、launcher 和私有配置复制到昆山。

Git 仓库只能恢复代码和公开模板，不能恢复已经安装好的 DCU runtime、模型 cache、
私有配置、上传的 PDF 或历史批次。

## 每个 deployment 必须保留

```text
deployments/<backend>/
├── app/
│   ├── mineru_drop.py              # 远端入口
│   ├── dcu-python                  # deploy 上传的 launcher 副本
│   └── ocrdrop/
│       ├── __init__.py
│       └── backends/
│           ├── __init__.py
│           ├── base.py
│           ├── mineru.py
│           └── paddleocr.py
├── config.json                     # 当前 deployment 的真实配置
├── inbox/                          # retry 仍需读取的源 PDF
└── batches/                        # 状态、日志、chunk 与最终输出
```

恢复方式：

| 内容 | 能否由 Git 恢复 | 恢复方式 |
| --- | --- | --- |
| `app/mineru_drop.py` | 可以 | 重新执行 `deploy` |
| `app/ocrdrop/` | 可以 | 重新执行 `deploy` |
| `app/dcu-python` | 模板可以 | 使用正确的私有 launcher 重新 `deploy` |
| `config.json` | 不完整 | 从 `config.local/` 或远端备份恢复 |
| `inbox/` | 不可以 | 从原始 PDF 重新上传 |
| `batches/` | 不可以 | 从备份恢复，或重新解析 |

不要只保留 `output/` 后就随意删除整个 batch。`manifest.json`、`queue/done/` 和
`chunks/` 是复现耗时、页范围和错误信息的依据。

## Runtime 必须保留

```text
runtimes/
├── mineru3
├── mineru4
├── mineru4-support
├── paddleocr
├── glibc-2.28
└── mineru3-model-config.json
```

这些路径是稳定软链接。它们指向：

- MinerU 3 Python 3.10/DCU 环境；
- MinerU 4 Python 3.10/DCU 环境；
- MinerU 4 launcher 和 YAML 配置；
- PaddleOCR 3.7、PaddleX、Transformers 和依赖；
- 用户态 glibc 2.28；
- MinerU 3 模型配置。

PaddleOCR runtime 下还必须保留：

```text
paddleocr/cache/official_models/
├── PP-OCRv5_mobile_det_safetensors/
└── PP-OCRv5_mobile_rec_safetensors/
```

MinerU 模型实际目录由各自的 JSON/YAML 配置引用。删除模型后，重新克隆
scnet-ocrdrop 无法恢复权重。

## 检查命令

每次清理前分别执行：

```bash
./bin/kunshan-mineru3 inventory
./bin/kunshan-mineru4 inventory
./bin/kunshan-paddleocr inventory
```

输出中的 `all_present` 必须为 `true`。条目策略：

- `redeploy-from-git`：误删后可重新部署；
- `redeploy-from-private-config`：需要本机 `config.local/`；
- `preserve-runtime`：不能靠 Git 恢复；
- `retain-with-batch`：batch 需要重试时必须保留；
- `retain-until-exported`：至少保留到结果取回和归档完成。

再执行：

```bash
./bin/kunshan-mineru3 doctor
./bin/kunshan-mineru4 doctor
./bin/kunshan-paddleocr doctor
```

`inventory` 检查文件闭包，`doctor` 检查版本、launcher、Slurm 和 PDF 工具。两者用途
不同，清理后都应该执行。

## 误删后的处理

### 只删除了 deployment 代码

无需重新编译：

```bash
./bin/kunshan-mineru3 deploy \
  --config config.local/mineru3.json \
  --launcher config.local/dcu-python
```

其他 backend 使用对应的 wrapper、配置和 launcher。

### 删除了 runtime 或模型

不能通过重新克隆仓库修复。需要：

1. 从备份恢复 runtime 或模型；
2. 或按照对应构建报告重新安装环境和下载模型；
3. 重建 `runtimes/` 软链接；
4. 执行 `inventory` 和 `doctor`；
5. 提交短 PDF 做 Slurm、merge、fetch 验收。

### 删除了 batch 或 inbox

- 有原始 PDF：重新 `push`，会产生新的 batch；
- 没有原始 PDF：Git 仓库无法恢复；
- 只删除 inbox：已完成结果还能读取，但失败任务无法 retry。

## 清理规则

允许清理：

- 本机 `__pycache__/`、`.pytest_cache/`；
- 已明确为 smoke test 且有更完整替代记录的测试批次；
- 没有 manifest 的失败初始化目录；
- 重复的 `--plan-only` 批次；
- 已确认不再需要的本地 fetch 副本。

默认保留：

- 所有正式 `complete` 批次；
- 每个 backend 至少一个端到端验收批次；
- 对应 inbox；
- 三套 runtime 和模型；
- 旧路径兼容软链接。

任何远端删除前都先确认没有相关活动 Slurm 作业。

## 2026-09-27 清理记录

以下可再生或未完成内容已经永久删除：

```text
mineru3/batches/20260927-134526-300b5a   # 重复的 plan-only
mineru3/batches/20260927-183333-27294a   # 创建 manifest 前失败
mineru3/inbox/20260927-134518-7044c9
paddleocr/batches/20260927-200811-c2ae5f # 已被 3 页验收替代
paddleocr/inbox/20260927-200801-3ebd68
paddleocr/test-input/
```

正式 complete 批次和 PaddleOCR 3 页常驻 worker 验收批次仍在活动目录。

本机 smoke-test 下载结果和旧的 `tools/mineru_drop/config.*.json` 副本也已永久删除。
上述内容不再保留恢复副本。
