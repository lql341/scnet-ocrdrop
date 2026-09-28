# PaddleOCR backend

## 定位

`paddleocr` 是高吞吐纯 OCR backend，适合：

- 批量提取文本；
- 扫描件或图片型 PDF 的文字识别；
- 保留文本框 polygon 和置信度；
- 对 MinerU 结果进行局部 OCR 复核。

它不恢复标题层级、表格、公式、图片引用或完整 Markdown 结构。上述需求应使用
`mineru3` 或 `mineru4`。

## 已验证环境

2026-09-27 在单张 Z100/gfx906 上验证：

```text
Python 3.10
PaddleOCR 3.7.0
PaddleX 3.7.2
Transformers 5.17.0
Torch 2.7.1 + DTK 26.04
PP-OCRv5_mobile_det
PP-OCRv5_mobile_rec
用户态 glibc 2.28
```

使用 PaddleOCR 的 `transformers` engine，不依赖 PaddlePaddle DCU wheel。模型必须
提前放入本地 PaddleX cache，计算节点设置离线变量。

本实现依据 PaddleOCR 源码树中提交 `1ef8a43` 的复现材料整理。原始证据仍由
PaddleOCR 仓库维护，避免在两个仓库复制后产生漂移：

| 原始文件 | 用途 | SHA256 前缀 |
| --- | --- | --- |
| `reports/paddleocr-z100/paddleocr-z100-transformers-report.html` | 主报告 | `44a8322b` |
| `reports/paddleocr-z100/README.md` | 安装与复现 | `031f3616` |
| `reports/paddleocr-z100/paddleocr_z100_benchmark.py` | 基准脚本 | `48009467` |
| `reports/paddleocr-z100/paddleocr_z100_launcher.sh` | loader 模板 | `1672a2b1` |
| `reports/paddleocr-z100/paddleocr_z100.slurm` | Slurm 模板 | `d564ba46` |

scnet-ocrdrop 吸收其中已经实测的运行时、模型和常驻 worker 结论；原始 benchmark
继续负责独立性能复现，生产 backend 负责批量队列与标准产物。

## 运行结构

```text
Slurm worker starts
  ├── import Torch/PaddleOCR
  ├── load det/rec models once
  ├── claim first chunk
  │     ├── render selected PDF pages
  │     ├── one-time warmup
  │     └── OCR pages
  ├── claim next chunk
  │     └── OCR pages with resident models
  └── shutdown
```

不要给同一个 worker 并发调用同一个 PaddleOCR pipeline。需要并发时增加 Slurm
array worker，每张卡一个进程。

## 配置

从模板开始：

```bash
cp examples/config/paddleocr.example.json config.local/paddleocr.json
cp examples/runtime/paddleocr-dcu-python.example \
  config.local/paddleocr-dcu-python
chmod +x config.local/paddleocr-dcu-python
```

关键字段：

| 字段 | 含义 |
| --- | --- |
| `backend` | 固定为 `paddleocr` |
| `activate_venv` | 独立 loader 环境通常设为 `false` |
| `paddleocr_engine` | 已验证值 `transformers` |
| `paddleocr_device` | 已验证值 `gpu:0` |
| `text_detection_model_name` | `PP-OCRv5_mobile_det` |
| `text_recognition_model_name` | `PP-OCRv5_mobile_rec` |
| `paddle_cache_home` | 已离线准备的模型 cache |
| `trust_remote_code` | 是否允许模型 cache 中的自定义 Python 代码 |
| `render_scale` | PDF 渲染倍率，已验证值 `2.0` |
| `chunk_pages` | 长文档每个任务的页数 |
| `whole_document_pages` | 小文档保持整篇的上限 |

不要省略模型名称。PaddleOCR 默认模型版本变化可能导致计算节点尝试下载未准备的模型。
`trust_remote_code` 的程序默认值为 `false`。只有在已经审计并固定本地模型 cache 时
才显式设为 `true`；公开下载或来源不明的模型不得开启。

## 部署

PaddleOCR 与 MinerU 使用不同远端 root：

```bash
export OCRDROP_SSH=my-cluster
export OCRDROP_REMOTE_ROOT=softwares/projects/scnet-ocrdrop/deployments/paddleocr

./bin/scnet-ocrdrop deploy \
  --config config.local/paddleocr.json \
  --launcher config.local/paddleocr-dcu-python

./bin/scnet-ocrdrop doctor
```

`doctor` 应显示：

```json
{
  "backend": "paddleocr",
  "backend_version": "3.7.0",
  "backend_compatible": true
}
```

## 使用

```bash
./bin/scnet-ocrdrop push document.pdf \
  --wait \
  --fetch \
  --output ./ocrdrop-results/document
```

批量输入：

```bash
./bin/scnet-ocrdrop push /path/to/pdf-directory/
```

单个长文档默认使用一个 worker，让所有分片复用一次模型加载。大量独立文档可以提高
`max_workers`，让多个 Slurm array task 并行领取任务。

## 输出

合并目录与其他 backend 一致：

```text
output/<document-id>/
├── document.md
├── content_list.json
├── middle.json
├── manifest.json
└── chunks/
```

`content_list.json` 的文本项示例：

```json
{
  "type": "text",
  "text": "recognized line",
  "page_idx": 0,
  "score": 0.97,
  "polygon": [[10, 20], [200, 20], [200, 48], [10, 48]]
}
```

chunk 目录还包含 `summary.json`：

```json
{
  "backend": "paddleocr",
  "pages": 1,
  "import_s": 83.4,
  "model_load_s": 70.3,
  "backend_init_s": 153.7,
  "warmup_s": 14.6,
  "ocr_total_s": 1.27,
  "lines": 52,
  "chars": 4310
}
```

导入、模型加载和 warmup 是 worker 级一次性成本；后续 chunk 继续记录同一个 worker
的初始化指标，但 `task_elapsed_s` 只包含该 chunk 实际承担的渲染、warmup（仅首块）
和 OCR。

## 实测

3 页 PDF 被强制拆为 3 个单页任务，由 1 个 Slurm worker 连续处理：

| 阶段 | 实测 |
| --- | ---: |
| Python/Torch/PaddleOCR 导入 | 83.43 s |
| det/rec 模型加载 | 70.29 s |
| backend 初始化合计 | 153.72 s |
| 首次 warmup | 14.64 s |
| 第 1 页任务（含 warmup） | 15.68 s |
| 第 2 页暖态任务 | 1.65 s |
| 第 3 页暖态任务 | 1.32 s |
| worker Slurm 总耗时 | 3 min 6 s |
| worker MaxRSS | 约 1.9 GiB |

验收结果：

- 3/3 chunk 成功，0 失败；
- merge 完成；
- `middle.json` 全局页码连续为 0、1、2；
- `content_list.json` 包含 196 条文本，页码覆盖 0、1、2；
- Markdown 非空；
- fetch 后本地复核通过。

23 页基准测试的稳定态均值为约 2.04 秒/页；不同页面版面和文本密度会明显影响单页
耗时。

## 限制

- gfx906 没有匹配的 HIPBLASLt Tensile kernel，日志会出现回退警告；
- Flash Attention 不可用，Transformers 使用 Torch math attention；
- 当前没有人工标注 ground truth，置信度不能替代准确率评估；
- 当前 backend 不提供表格、公式、图片或标题层级；
- PP-OCRv6、PP-Structure 和 PaddleOCR-VL 尚未纳入该 backend；
- Torch、Transformers、DTK、模型或 launcher 变化后必须重新验收。
