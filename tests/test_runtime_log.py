from __future__ import annotations

import logging
import shutil
import tempfile
import unittest
from pathlib import Path

from wxbot import runtime_log
from wxbot.runtime_log import log_event, setup_runtime_log


class RuntimeLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._logger = logging.getLogger(runtime_log._LOGGER_NAME)
        self._previous_handlers = list(self._logger.handlers)
        self._directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._directory, ignore_errors=True)
        self.addCleanup(self._restore_handlers)

    def _restore_handlers(self) -> None:
        for handler in self._logger.handlers[:]:
            if handler not in self._previous_handlers:
                self._logger.removeHandler(handler)
                handler.close()
        runtime_log._configured_paths.clear()

    def test_setup_writes_desensitized_event_to_log_file(self) -> None:
        data_dir = Path(self._directory)
        setup_runtime_log(data_dir)
        log_event("poll_error", "error=NetworkError failures=1 retry_in=2")
        log_file = data_dir / "logs" / "wxbot.log"
        content = log_file.read_text(encoding="utf-8")
        self.assertIn("poll_error error=NetworkError failures=1 retry_in=2", content)
        self.assertEqual(len(content.rstrip().splitlines()), 1)

    def test_repeated_setup_does_not_duplicate_handlers(self) -> None:
        data_dir = Path(self._directory)
        setup_runtime_log(data_dir)
        setup_runtime_log(data_dir)
        log_event("startup")
        content = (data_dir / "logs" / "wxbot.log").read_text(encoding="utf-8")
        self.assertEqual(len(content.rstrip().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
