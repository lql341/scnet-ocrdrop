"""PaddleOCR Transformers/PyTorch backend.

This adapter intentionally targets the resident PP-OCRv5 det/rec path validated
on Z100. It emits a small backend-neutral JSON contract instead of pretending
that plain OCR recovers MinerU-style document structure.
"""

from __future__ import print_function

import json
import re
import statistics
import time
from pathlib import Path

from .base import DocumentBackend


def clean_text(value):
    return re.sub(r"\s+", " ", str(value)).strip()


def json_value(value):
    if hasattr(value, "tolist"):
        return value.tolist()
    if hasattr(value, "item"):
        return value.item()
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value


def record_value(record, key, default=None):
    try:
        return record[key]
    except (KeyError, TypeError):
        return default


def record_to_lines(record):
    text_values = record_value(record, "rec_texts")
    score_values = record_value(record, "rec_scores")
    texts = list(text_values) if text_values is not None else []
    scores = list(score_values) if score_values is not None else []
    polygons = []
    for key in ("rec_polys", "dt_polys", "rec_boxes", "dt_boxes"):
        values = record_value(record, key)
        if values is not None:
            polygons = list(values)
            break
    lines = []
    for index, text in enumerate(texts):
        normalized = clean_text(text)
        if not normalized:
            continue
        line = {"text": normalized}
        if index < len(scores):
            line["score"] = float(scores[index])
        if index < len(polygons):
            line["polygon"] = json_value(polygons[index])
        lines.append(line)
    return lines


class PaddleOCRBackend(DocumentBackend):
    name = "paddleocr"
    package_name = "paddleocr"
    expected_major = 3

    def __init__(self, config):
        super(PaddleOCRBackend, self).__init__(config)
        backend_started = time.perf_counter()
        import numpy as np
        import pypdfium2 as pdfium
        import torch
        from paddleocr import PaddleOCR

        self._np = np
        self._pdfium = pdfium
        self._torch = torch
        model_started = time.perf_counter()
        self.import_s = model_started - backend_started
        self._ocr = PaddleOCR(
            engine=config.get("paddleocr_engine", "transformers"),
            device=config.get("paddleocr_device", "gpu:0"),
            text_detection_model_name=config.get(
                "text_detection_model_name",
                "PP-OCRv5_mobile_det",
            ),
            text_recognition_model_name=config.get(
                "text_recognition_model_name",
                "PP-OCRv5_mobile_rec",
            ),
            use_doc_orientation_classify=bool(
                config.get("use_doc_orientation_classify", False)
            ),
            use_doc_unwarping=bool(config.get("use_doc_unwarping", False)),
            use_textline_orientation=bool(
                config.get("use_textline_orientation", False)
            ),
            engine_config={
                "trust_remote_code": bool(config.get("trust_remote_code", False))
            },
        )
        self._sync()
        self.model_load_s = time.perf_counter() - model_started
        self.backend_init_s = time.perf_counter() - backend_started
        self.warmup_s = None

    def _sync(self):
        if self._torch.cuda.is_available():
            self._torch.cuda.synchronize()

    def _render_pages(self, source_path, start_page_id, end_page_id):
        document = self._pdfium.PdfDocument(str(source_path))
        pages = []
        scale = float(self.config.get("render_scale", 2.0))
        started = time.perf_counter()
        try:
            for page_index in range(start_page_id, end_page_id + 1):
                page = document[page_index]
                bitmap = None
                try:
                    bitmap = page.render(scale=scale)
                    image = bitmap.to_numpy()
                    if image.shape[-1] == 4:
                        image = image[:, :, :3]
                    pages.append(self._np.asarray(image).copy())
                finally:
                    if bitmap is not None:
                        close_bitmap = getattr(bitmap, "close", None)
                        if close_bitmap:
                            close_bitmap()
                    close_page = getattr(page, "close", None)
                    if close_page:
                        close_page()
        finally:
            close = getattr(document, "close", None)
            if close:
                close()
        return pages, time.perf_counter() - started

    def _predict(self, page):
        self._sync()
        started = time.perf_counter()
        result = list(self._ocr.predict(page))
        self._sync()
        if not result:
            raise RuntimeError("PaddleOCR returned no result")
        return result[0], time.perf_counter() - started

    def parse(self, task, task_output):
        task_output = Path(task_output)
        pages, render_s = self._render_pages(
            task["source_path"],
            int(task["start_page_id"]),
            int(task["end_page_id"]),
        )
        if not pages:
            raise RuntimeError("PDF page range rendered no pages")

        if self.warmup_s is None:
            _, self.warmup_s = self._predict(pages[0])

        page_records = []
        content_list = []
        page_times = []
        markdown_pages = []
        all_scores = []
        all_text = []
        for local_index, page in enumerate(pages):
            source_page_id = int(task["start_page_id"]) + local_index
            record, elapsed_s = self._predict(page)
            lines = record_to_lines(record)
            page_times.append(elapsed_s)
            for line in lines:
                all_text.append(line["text"])
                if "score" in line:
                    all_scores.append(line["score"])
                content = {
                    "type": "text",
                    "text": line["text"],
                    # The shared merge layer shifts local content-list page IDs.
                    "page_idx": local_index,
                }
                if "score" in line:
                    content["score"] = line["score"]
                if "polygon" in line:
                    content["polygon"] = line["polygon"]
                content_list.append(content)

            page_records.append({
                # middle.json page IDs are already global source PDF IDs.
                "page_idx": source_page_id,
                "width": int(page.shape[1]),
                "height": int(page.shape[0]),
                "lines": lines,
                "elapsed_s": round(elapsed_s, 4),
            })
            markdown_pages.append(
                "<!-- PaddleOCR source page: %d -->\n\n%s"
                % (
                    source_page_id + 1,
                    "\n\n".join(line["text"] for line in lines),
                )
            )

        stem = task["chunk_name"]
        (task_output / (stem + ".md")).write_text(
            "\n\n---\n\n".join(markdown_pages).rstrip() + "\n",
            encoding="utf-8",
        )
        (task_output / (stem + "_middle.json")).write_text(
            json.dumps(
                {
                    "backend": self.name,
                    "pages": page_records,
                },
                ensure_ascii=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (task_output / (stem + "_content_list.json")).write_text(
            json.dumps(content_list, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
        summary = {
            "backend": self.name,
            "pages": len(pages),
            "render_s": round(render_s, 4),
            "import_s": round(self.import_s, 4),
            "model_load_s": round(self.model_load_s, 4),
            "backend_init_s": round(self.backend_init_s, 4),
            "warmup_s": round(self.warmup_s, 4),
            "ocr_total_s": round(sum(page_times), 4),
            "ocr_mean_page_s": round(statistics.mean(page_times), 4),
            "ocr_pages_per_s": (
                round(len(page_times) / sum(page_times), 4)
                if sum(page_times)
                else None
            ),
            "lines": len(all_text),
            "chars": len("\n".join(all_text)),
            "mean_score": (
                round(statistics.mean(all_scores), 4)
                if all_scores
                else None
            ),
            "start_page_id": int(task["start_page_id"]),
            "end_page_id": int(task["end_page_id"]),
        }
        (task_output / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return task_output

    def shutdown(self):
        self._ocr = None
        try:
            self._torch.cuda.empty_cache()
        except Exception:
            pass
