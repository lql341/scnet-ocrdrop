"""Document parsing backend registry."""

from .mineru import MinerU3Backend, MinerU4Backend
from .paddleocr import PaddleOCRBackend


BACKENDS = {
    "mineru3": MinerU3Backend,
    "mineru4": MinerU4Backend,
    "paddleocr": PaddleOCRBackend,
}


def get_backend_class(config):
    name = config.get("backend", config.get("adapter", "mineru3"))
    backend_class = BACKENDS.get(name)
    if backend_class is None:
        supported = ", ".join(sorted(BACKENDS))
        raise RuntimeError(
            "unsupported backend %r; available backends: %s" % (name, supported)
        )
    return backend_class


def create_backend(config):
    return get_backend_class(config)(config)


def supported_backends():
    return sorted(BACKENDS)
