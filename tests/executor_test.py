from pathlib import Path

from tango.common.params import Params
from tango.common.testing import TangoTestCase
from tango.local_workspace import ExecutorMetadata


class TestMetadata(TangoTestCase):
    def test_metadata(self):
        metadata = ExecutorMetadata("some_step")
        metadata.save(self.TEST_DIR)

        if (Path.cwd() / ".git").exists():
            assert metadata.git is not None
            assert metadata.git.commit is not None
            assert metadata.git.remote is not None
            assert "allenai/tango" in metadata.git.remote

        metadata2 = ExecutorMetadata.from_params(
            Params.from_file(self.TEST_DIR / "executor-metadata.json")
        )
        assert metadata == metadata2
