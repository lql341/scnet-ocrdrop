"""Persistent MinerU 3.x and 4.x worker adapters."""

from __future__ import print_function

from pathlib import Path

from .base import DocumentBackend


class MinerU3Backend(DocumentBackend):
    name = "mineru3"
    package_name = "mineru"
    expected_major = 3

    def __init__(self, config):
        super(MinerU3Backend, self).__init__(config)
        from mineru.cli.common import do_parse, read_fn

        self._do_parse = do_parse
        self._read_fn = read_fn
        self._cached_path = None
        self._cached_bytes = None

    def parse(self, task, task_output):
        if self._cached_path != task["source_path"]:
            self._cached_bytes = self._read_fn(task["source_path"])
            self._cached_path = task["source_path"]

        self._do_parse(
            output_dir=str(task_output),
            pdf_file_names=[task["chunk_name"]],
            pdf_bytes_list=[self._cached_bytes],
            p_lang_list=[task["language"]],
            backend="pipeline",
            parse_method=self.config["parse_method"],
            formula_enable=bool(self.config["formula_enable"]),
            table_enable=bool(self.config["table_enable"]),
            start_page_id=int(task["start_page_id"]),
            end_page_id=int(task["end_page_id"]),
            f_dump_orig_pdf=True,
        )
        expected = task_output / task["chunk_name"] / "auto"
        if expected.is_dir():
            return expected
        matches = list(task_output.glob("*/auto"))
        if len(matches) == 1:
            return matches[0]
        raise RuntimeError("cannot locate MinerU output under %s" % task_output)

    def shutdown(self):
        try:
            from mineru.utils.pdf_image_tools import shutdown_pdf_render_executor

            shutdown_pdf_render_executor()
        except Exception:
            pass


class MinerU4Backend(DocumentBackend):
    name = "mineru4"
    package_name = "mineru"
    expected_major = 4

    def __init__(self, config):
        super(MinerU4Backend, self).__init__(config)
        if config.get("disable_formula_sdpa"):
            from mineru.model.mfr.pp_formulanet.predict_formula import FormulaRecognizer

            original_formula_init = FormulaRecognizer.__init__

            def optimized_formula_init(instance, *args, **kwargs):
                original_formula_init(instance, *args, **kwargs)
                instance.net.head.set_fast_attention(False)

            FormulaRecognizer.__init__ = optimized_formula_init

        from mineru.parser import MinerUParser

        self._parser = MinerUParser(
            tier=config.get("tier", "basic"),
            parse_mode=config.get("ocr_mode", "txt"),
            image_analysis=True,
        )

    def parse(self, task, task_output):
        page_range = "%d-%d" % (
            int(task["start_page_id"]) + 1,
            int(task["end_page_id"]) + 1,
        )
        result = self._parser.parse(task["source_path"], page_range=page_range)
        markdown_path = task_output / (task["chunk_name"] + ".md")
        middle_path = task_output / (task["chunk_name"] + "_middle.json")
        markdown_path.write_text(result.markdown(), encoding="utf-8")
        middle_path.write_text(result.to_json(), encoding="utf-8")
        return Path(task_output)
