"""Wall progress hooks: they report a live download and can never break an import.

Run from the repository root with the collectors checkout next to it:
    AP3X_PROGRESS_LIB=../ap3x-wall-data-collectors/src/youtube_archive python3 -m unittest tests.test_wall_progress
On rackmain1 the helper is found at ~/.local/lib/ap3x once the collectors installer has run.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
LIB = Path(os.environ.get("AP3X_PROGRESS_LIB", ROOT.parent / "ap3x-wall-data-collectors/src/youtube_archive")).resolve()
FAKE_YTDLP = (
    "import sys, time\n"
    "print('[youtube] abc: Downloading webpage', flush=True)\n"
    "for pct in (10.0, 55.5):\n"
    "    sys.stdout.write(f'\\r[download]  {pct}% of ~ 200.00MiB at  2.00MiB/s ETA 01:30'); sys.stdout.flush(); time.sleep(0.3)\n"
)


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str((ROOT / relative).parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


MODULES = {
    "playlists": load("wall_playlists", "playlists/playlist_archive_import.py"),
    "channels": load("wall_channels", "channels/channel_archive_import.py"),
}


def monitor(module, log: Path) -> tuple[int, str]:
    command = [sys.executable, "-c", FAKE_YTDLP]
    real_sleep = time.sleep
    with patch.object(module.time, "sleep", lambda seconds: real_sleep(0.25)), \
            patch.object(module, "free_bytes", return_value=10 ** 15), \
            patch.object(module, "quiet_window", return_value=False), \
            patch.object(module, "music_active", return_value=False):
        if module is MODULES["channels"]:
            return module.monitor_command(command, log, reserve=1, check_window=True)
        return module.monitor_command(command, log)


@unittest.skipUnless((LIB / "ap3x_progress.py").is_file(), f"ap3x_progress.py not found at {LIB}")
class WallProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)

    def start(self, module):
        with patch.object(module, "PROGRESS_LIB", LIB):
            module.start_progress("playlists", self.state, eligible=25)
        self.addCleanup(setattr, module, "PROGRESS", None)
        self.assertIsNotNone(module.PROGRESS)
        module.PROGRESS._last = -10.0

    def test_download_progress_reaches_the_event_file(self):
        for name, module in MODULES.items():
            with self.subTest(name):
                self.start(module)
                module.progress("item", position=13, completed=12)
                module.PROGRESS._last = -10.0  # the importer polls every 10 s; skip the 2 s write throttle here
                code, reason = monitor(module, self.state / "yt-dlp.log")
                self.assertEqual((code, reason), (0, "exit"))
                event = json.loads((self.state / "current.json").read_text())
                self.assertEqual((event["phase"], event["position"], event["eligible"]), ("downloading", 13, 25))
                self.assertGreater(event["bytes_downloaded"], 0)
                self.assertEqual(event["bytes_total"], 200 * 1024 ** 2)
                self.assertNotIn("youtube", json.dumps(event).replace('"source"', ""))

    def test_stop_is_classified_without_its_text(self):
        for name, module in MODULES.items():
            with self.subTest(name):
                self.start(module)
                module.progress_stopped(module.ImportErrorSafe("STOP: abc yt-dlp bot_or_429 HTTP Error 429; see /secret/log"))
                event = json.loads((self.state / "current.json").read_text())
                self.assertEqual((event["phase"], event["failure"]), ("failed", "bot_or_429"))
                self.assertNotIn("secret", json.dumps(event))
                module.progress_stopped(module.ImportErrorSafe("STOP: free space below 1 TiB while fetching x"))
                self.assertEqual(json.loads((self.state / "current.json").read_text())["failure"], "low_space")


class WallProgressNeverBreaksImportsTests(unittest.TestCase):
    def test_missing_helper_disabled_flag_and_broken_helper_are_harmless(self):
        for name, module in MODULES.items():
            with self.subTest(name):
                with patch.object(module, "PROGRESS_LIB", Path("/nonexistent")), patch.dict(sys.modules, {"ap3x_progress": None}):
                    module.start_progress("x", Path(tempfile.gettempdir()))
                self.assertIsNone(module.PROGRESS)
                module.progress("phase", "downloading")  # no-op
                with patch.dict(os.environ, {"AP3X_PROGRESS": "off"}):
                    module.start_progress("x", Path(tempfile.gettempdir()))
                self.assertIsNone(module.PROGRESS)

                class Broken:
                    def __getattr__(self, _name):
                        raise RuntimeError("boom")
                module.PROGRESS = Broken()
                try:
                    module.progress("ytdlp_log", "text")
                    module.progress_stopped(RuntimeError("STOP"))
                finally:
                    module.PROGRESS = None


if __name__ == "__main__":
    unittest.main()
