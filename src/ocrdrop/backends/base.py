"""Backend contract shared by worker-side document parsers."""

from __future__ import print_function


class DocumentBackend(object):
    """A persistent parser instance owned by one Slurm worker."""

    name = None
    package_name = None
    expected_major = None

    def __init__(self, config):
        self.config = config

    def parse(self, task, task_output):
        """Parse one task and return the directory containing its artifacts."""
        raise NotImplementedError

    def shutdown(self):
        """Release process pools or other backend resources."""

    @classmethod
    def validate_version(cls, version):
        return bool(
            cls.expected_major
            and version
            and version.startswith(str(cls.expected_major) + ".")
        )
