# Backend 扩展

## 接口

backend 继承 `DocumentBackend`：

```python
class MyBackend(DocumentBackend):
    name = "my-backend"
    package_name = "my-package"
    expected_major = 1

    def __init__(self, config):
        super(MyBackend, self).__init__(config)
        # 只执行一次：加载模型、创建 predictor。

    def parse(self, task, task_output):
        # 解析 task["start_page_id"] 到 task["end_page_id"]。
        # 将 Markdown、JSON、图片写入 task_output。
        return task_output

    def shutdown(self):
        # 可选：释放子进程池和临时资源。
        pass
```

然后在 `src/ocrdrop/backends/__init__.py` 的 `BACKENDS` 中注册。

## Task 输入

常用字段：

| 字段 | 含义 |
| --- | --- |
| `source_path` | 共享文件系统上的源 PDF |
| `source_sha256` | 输入哈希 |
| `start_page_id` | 0-based 起始页，包含 |
| `end_page_id` | 0-based 结束页，包含 |
| `chunk_name` | 当前分片的安全输出名 |
| `language` | 语言提示 |

backend 不负责领取任务或更新队列状态。

## 产物要求

最低要求：

- 一个非空 Markdown 文件；
- 一个能够表示页面和内容块的 JSON 文件；
- 图片引用使用相对路径。

若 backend 的原生格式不同，应在 backend 内转换为共享合并层可以识别的格式，或为
合并层增加显式格式处理器。不要让控制器依赖 backend 的模型对象。

## PaddleOCR 实现

`paddleocr` backend 已实现并完成 Z100 实机验证：

- worker 启动时创建一次 PaddleOCR pipeline；
- 使用 `pypdfium2` 只渲染任务指定的页范围；
- 第一个任务执行一次 warmup；
- 后续任务复用模型；
- 输出 Markdown、`middle.json`、`content_list.json` 和 `summary.json`；
- `middle.json` 使用源 PDF 全局页码；
- `content_list.json` 使用 chunk 局部页码，由 merge 层增加偏移。

当前实现固定验证路线为：

```text
PaddleOCR 3.7
engine=transformers
device=gpu:0
PP-OCRv5_mobile_det
PP-OCRv5_mobile_rec
```

它是纯 OCR backend，不把文本行伪装成表格、公式或版面结构。PP-Structure 和
PaddleOCR-VL 应分别作为新的 backend 接入，不与当前 pipeline 共用含义不清的配置。
完整说明见 [PaddleOCR backend](paddleocr.md)。
