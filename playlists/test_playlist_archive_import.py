"""Offline safety tests for the pinned YouTube playlist importer."""

from __future__ import annotations

import contextlib
import copy
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import playlist_archive_import as importer


INVENTORY = Path(__file__).with_name("requested-youtube-playlists-inventory-20261014.json")


class PlaylistImportTests(unittest.TestCase):
    def test_named_seasons_are_visible_and_conflicts_preserve_show_nfo(self):
        sources = importer.load_inventory(INVENTORY)
        room = next(source for source in sources if source["playlist_id"] == importer.ROOM_MATES)
        first, second = (importer.placement(room, position, room["ordered_video_ids"][position - 1])
                         for position in (1, 11))
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            root = Path(folder)
            with patch.object(importer, "TV_ROOT", root / "tv"), \
                 patch.object(importer, "STATE_ROOT", root / "state"):
                importer.show_nfos(first, room)
                importer.show_nfos(second, room)
                show_nfo = root / "tv" / first["show"] / "tvshow.nfo"
                self.assertEqual({node.get("number"): node.text for node in
                                  ET.parse(show_nfo).getroot().findall("namedseason")},
                                 {"1": "Room Mates — Season 1", "2": "Room Mates — Season 2"})
                self.assertEqual(ET.parse(show_nfo.parent / "Season 02" / "season.nfo")
                                 .getroot().findtext("title"), "Room Mates — Season 2")
                before = show_nfo.read_bytes()
                importer.show_nfos(second, room)
                self.assertEqual(show_nfo.read_bytes(), before)
                with self.assertRaisesRegex(importer.ImportErrorSafe, "Conflicting named season"):
                    importer.ensure_named_season(show_nfo, 2, "Different series",
                                                 root / "state" / "metadata-backups")
                self.assertEqual(show_nfo.read_bytes(), before)
                self.assertEqual(len(list((root / "state" / "metadata-backups").glob("*.before"))), 2)

    def test_pinned_inventory_and_precise_episode_mapping(self):
        sources = importer.load_inventory(INVENTORY)
        items = importer.planned_placements(sources)
        self.assertEqual((len(sources), len(items), len({item["id"] for item in items})),
                         (16, 749, 736))
        by_playlist = {}
        for item in items:
            by_playlist.setdefault(item["playlist_id"], []).append(item)
        room = by_playlist[importer.ROOM_MATES]
        self.assertEqual((room[9]["season"], room[9]["episode"]), (1, 10))
        self.assertEqual((room[10]["season"], room[10]["episode"]), (2, 1))
        self.assertEqual(by_playlist[importer.MAZE_RUNNER][-1]["episode"], 10)
        self.assertEqual(by_playlist[importer.PIRATES][-1]["episode"], 10)
        university = by_playlist[importer.UNIVERSITY]
        self.assertEqual([item["episode"] for item in university[6:]], [2, 3, 5, 6])
        self.assertTrue(all("High School" in item["show"] for item in university[6:]))
        nostalgia = by_playlist[importer.NOSTALGIA]
        self.assertEqual(nostalgia[-1]["id"], importer.FRENCH_FRY_ID)
        self.assertEqual((nostalgia[-1]["show"], nostalgia[-1]["season"]),
                         ("Sky Does Everything - Extras", 0))
        cops = by_playlist["PLm29cAuVva68IeB2ahqt-jtRjHgXxTh-k"]
        self.assertEqual(len(cops), 100)
        self.assertEqual(len({item["id"] for item in cops}), 97)
        self.assertEqual(len({(item["show"], item["season"], item["episode"]) for item in cops}), 100)
        self.assertNotEqual(importer.record_path(cops[0]), importer.record_path(cops[1]))
        payload = json.loads(INVENTORY.read_text(encoding="utf-8"))
        payload["playlists"].append(copy.deepcopy(payload["playlists"][0]))
        payload["playlists"][-1]["ordered_video_ids"] = []
        payload["playlists"][-1]["listed_count"] = 0
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            bad_inventory = Path(folder) / "bad.json"
            bad_inventory.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(importer.ImportErrorSafe, "exactly once"):
                importer.load_inventory(bad_inventory)

    def test_scan_reconciliation_retains_pinned_slots(self):
        expected = ["aB_cD-12345", "bB_cD-12345", "cB_cD-12345"]
        self.assertEqual(importer.reconcile_scan(expected,
                         ["aB_cD-12345", "new_id-123", "bB_cD-12345", "cB_cD-12345"], set()),
                         (["new_id-123"], []))
        self.assertEqual(importer.reconcile_scan(expected,
                         ["aB_cD-12345", "cB_cD-12345"], {"bB_cD-12345"}),
                         ([], ["bB_cD-12345"]))
        self.assertEqual(importer.reconcile_scan(expected,
                         ["aB_cD-12345", "new_id-1234", "bB_cD-12345", "cB_cD-12345"],
                         {"bB_cD-12345"}), (["new_id-1234"], []))
        with self.assertRaises(importer.ImportErrorSafe):
            importer.reconcile_scan(expected,
                                     ["aB_cD-12345", "cB_cD-12345", "bB_cD-12345"], set())
        with self.assertRaises(importer.ImportErrorSafe):
            importer.reconcile_scan(expected,
                                     ["aB_cD-12345", "cB_cD-12345"], set())

    def test_live_scan_journals_new_entries_without_renumbering_baseline(self):
        source = {"playlist_id": "PLm29cAuVva6-9CowLx6ybMla-qIF-_BE2",
                  "canonical_url": "https://www.youtube.com/playlist?list=PLm29cAuVva6-9CowLx6ybMla-qIF-_BE2",
                  "ordered_video_ids": ["aB_cD-12345", "bB_cD-12345"],
                  "untitled_or_unavailable_ids": []}
        observed = {"id": source["playlist_id"], "title": "Room Mates",
                    "entries": [{"id": "aB_cD-12345"}, {"id": "new_id-1234"},
                                {"id": "bB_cD-12345"}]}
        args = SimpleNamespace(yt_dlp="yt-dlp", cookies=None)
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            with patch.object(importer, "STATE_ROOT", Path(folder)), patch.object(importer, "gate"), \
                 patch.object(importer, "run_json_command", return_value=(0, json.dumps(observed), "")):
                scan = importer.scan_playlist(source, args)
                self.assertEqual(scan["new_unpinned_ids"], ["new_id-1234"])
                self.assertEqual(source["ordered_video_ids"], ["aB_cD-12345", "bB_cD-12345"])
                self.assertEqual(json.loads((Path(folder) / "scans" /
                    f"{source['playlist_id']}.json").read_text())["ids"],
                    ["aB_cD-12345", "new_id-1234", "bB_cD-12345"])

    def test_short_excluded_before_any_video_download(self):
        video_id = "aB_cD-12345"
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            root = Path(folder)
            info = {"id": video_id, "title": "Clip #shorts", "duration": 40,
                    "formats": [{"url": "https://signed.example"}]}
            with patch.object(importer, "STAGE_ROOT", root / "stage"), \
                 patch.object(importer, "STATE_ROOT", root / "state"), \
                 patch.object(importer, "extract_metadata", return_value=info), \
                 patch.object(importer, "monitor_command", side_effect=AssertionError("video download attempted")):
                with self.assertRaisesRegex(importer.ImportErrorSafe, "SHORT:"):
                    importer.fetch_video(video_id, None, SimpleNamespace(ffprobe="ffprobe"))
            self.assertTrue((root / "state" / "shorts" / f"{video_id}.json").is_file())
            self.assertEqual(list((root / "stage" / video_id).glob("*.mkv")), [])
        vertical = {"id": video_id, "title": "Vertical clip", "duration": 120,
                    "formats": [{"width": 1080, "height": 1920, "url": "https://signed.example"}]}
        self.assertTrue(importer.is_short(vertical))
        self.assertTrue(importer.durable_info(vertical)["detected_short"])
        self.assertFalse(importer.is_short({"id": video_id, "title": "Brief landscape episode",
                                            "duration": 120, "width": 1920, "height": 1080}))

    def test_reused_sparse_sidecar_gets_fresh_short_preflight(self):
        video_id = "aB_cD-12345"
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            root = Path(folder)
            original = root / (video_id + ".mkv")
            original.write_bytes(b"existing media that must remain untouched")
            original.with_name(video_id + ".source.json").write_text(
                json.dumps({"id": video_id, "title": "An old clip", "duration": 120}),
                encoding="utf-8")
            fresh = {"id": video_id, "title": "An old clip", "duration": 120,
                     "formats": [{"width": 1080, "height": 1920,
                                  "url": "https://signed.example"}]}

            def stage_existing(stage, _video_id, media):
                shutil.copy2(media, stage / media.name)
                sidecar = media.with_name(media.stem + ".source.json")
                shutil.copy2(sidecar, stage / sidecar.name)

            with patch.object(importer, "STAGE_ROOT", root / "stage"), \
                 patch.object(importer, "STATE_ROOT", root / "state"), \
                 patch.object(importer, "stage_from_existing", side_effect=stage_existing), \
                 patch.object(importer, "extract_metadata", return_value=fresh) as preflight, \
                 patch.object(importer, "monitor_command",
                              side_effect=AssertionError("media fetch attempted")):
                with self.assertRaisesRegex(importer.ImportErrorSafe, "SHORT:"):
                    importer.fetch_video(video_id, original, SimpleNamespace(ffprobe="ffprobe"))
            preflight.assert_called_once()
            self.assertTrue(original.is_file())
            self.assertTrue((root / "state" / "shorts" / f"{video_id}.json").is_file())
            self.assertEqual(list((root / "state").glob("records/**/*.json")), [])

    def test_interrupted_fragment_is_not_treated_as_finished_media(self):
        video_id = "aB_cD-12345"
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            root = Path(folder)
            stage = root / "stage" / video_id
            stage.mkdir(parents=True)
            fragment = stage / f"{video_id}.f137.mp4"
            fragment.write_bytes(b"incomplete fragment" * 100)
            description = stage / f"{video_id}.description"
            description.write_text("Existing full description", encoding="utf-8")
            poster = stage / f"{video_id}.jpg"
            poster.write_bytes(b"existing poster")
            finished = stage / f"{video_id}.mkv"
            self.assertIsNone(importer.staged_media(stage, video_id))

            def finish_download(command, _log):
                self.assertNotIn("--skip-download", command)
                self.assertEqual(command[command.index("-f") + 1], "bv*+ba/b")
                finished.write_bytes(b"finished media" * 100)
                return 0, "exit"

            info = {"id": video_id, "title": "Long video", "duration": 3600,
                    "width": 1920, "height": 1080}
            args = SimpleNamespace(yt_dlp="yt-dlp", cookies=None, ffprobe="ffprobe")
            with patch.object(importer, "STAGE_ROOT", root / "stage"), \
                 patch.object(importer, "STATE_ROOT", root / "state"), \
                 patch.object(importer, "extract_metadata", return_value=info), \
                 patch.object(importer, "gate"), \
                 patch.object(importer, "monitor_command", side_effect=finish_download), \
                 patch.object(importer, "verify_media", return_value={"format": {"duration": "3600"}}):
                media, returned_info = importer.fetch_video(video_id, None, args)
            self.assertEqual(media, finished)
            self.assertEqual(returned_info["id"], video_id)
            self.assertEqual(importer.staged_media(stage, video_id), finished)
            self.assertTrue(fragment.is_file())
            self.assertEqual(description.read_text(encoding="utf-8"), "Existing full description")
            self.assertEqual(poster.read_bytes(), b"existing poster")

    def test_crash_during_merge_backs_up_exact_media_and_resumes_fragments(self):
        video_id = "aB_cD-12345"
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            root = Path(folder)
            stage = root / "stage" / video_id
            stage.mkdir(parents=True)
            ambiguous = stage / f"{video_id}.mkv"
            ambiguous_bytes = b"partially merged media" * 100
            ambiguous.write_bytes(ambiguous_bytes)
            fragment = stage / f"{video_id}.f137.mp4"
            fragment.write_bytes(b"resumable video fragment" * 100)
            description = stage / f"{video_id}.description"
            description.write_text("Preserved description", encoding="utf-8")
            self.assertEqual(importer.staged_media(stage, video_id), ambiguous)

            def finish_download(command, _log):
                self.assertNotIn("--skip-download", command)
                self.assertIn("--merge-output-format", command)
                self.assertFalse(ambiguous.exists())
                self.assertTrue(fragment.is_file())
                ambiguous.write_bytes(b"fully merged media" * 100)
                return 0, "exit"

            info = {"id": video_id, "title": "Long stream", "duration": 7200,
                    "width": 1920, "height": 1080}
            args = SimpleNamespace(yt_dlp="yt-dlp", cookies=None, ffprobe="ffprobe")
            with patch.object(importer, "STAGE_ROOT", root / "stage"), \
                 patch.object(importer, "STATE_ROOT", root / "state"), \
                 patch.object(importer, "extract_metadata", return_value=info), \
                 patch.object(importer, "gate"), \
                 patch.object(importer.os, "link", side_effect=shutil.copyfile), \
                 patch.object(importer, "monitor_command", side_effect=finish_download), \
                 patch.object(importer, "verify_media", return_value={"format": {"duration": "7200"}}):
                media, _ = importer.fetch_video(video_id, None, args)
            backups = [path for path in (stage / ".ambiguous-media-backup").iterdir()
                       if path.is_file() and not path.name.endswith(".json")]
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), ambiguous_bytes)
            self.assertEqual(media, ambiguous)
            self.assertEqual(media.read_bytes(), b"fully merged media" * 100)
            self.assertTrue(fragment.is_file())
            self.assertEqual(description.read_text(encoding="utf-8"), "Preserved description")
            self.assertTrue((stage / f"{video_id}.sidecars-complete.json").is_file())

    def test_probe_skips_non_downloadable_but_stops_bot(self):
        video_id = "aB_cD-12345"
        args = SimpleNamespace(yt_dlp="yt-dlp", cookies=None)
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            with patch.object(importer, "STATE_ROOT", Path(folder)), patch.object(importer, "gate"), \
                 patch.object(importer, "run_json_command", return_value=(1, "", "Private video")):
                with self.assertRaisesRegex(importer.ImportErrorSafe, "PROBE_SKIP:"):
                    importer.probe_access(video_id, args)
            with patch.object(importer, "STATE_ROOT", Path(folder)), patch.object(importer, "gate"), \
                 patch.object(importer, "run_json_command", return_value=(1, "", "HTTP Error 429")):
                with self.assertRaisesRegex(importer.ImportErrorSafe, "bot_or_429"):
                    importer.probe_access(video_id, args)

    def test_extractor_internal_retries_disabled_for_bot_stop(self):
        command = importer.command_base(SimpleNamespace(yt_dlp="yt-dlp", cookies=None))
        for flag in ("--retries", "--fragment-retries", "--extractor-retries"):
            self.assertEqual(command[command.index(flag) + 1], "0")

    def test_channel_terminal_status_counts_excluded_shorts(self):
        @contextlib.contextmanager
        def fake_lock(_path):
            yield
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            state = Path(folder)
            (state / "run-status.json").write_text(json.dumps({
                "limited": False, "unresolved": 0, "sources": {str(i): 1 for i in range(6)},
                "status": "partial", "terminal_rescan_verified": True,
                "present": 8, "unavailable": 1,
                "shorts_excluded": 1, "catalog_total": 10}))
            with patch.object(importer, "CHANNEL_STATE", state), patch.object(importer, "locked", fake_lock):
                importer.channel_queue_finished()
                data = json.loads((state / "run-status.json").read_text())
                data["terminal_rescan_verified"] = False
                (state / "run-status.json").write_text(json.dumps(data))
                with self.assertRaisesRegex(importer.ImportErrorSafe, "has not finished"):
                    importer.channel_queue_finished()
                data["terminal_rescan_verified"] = True
                data["unresolved"] = 1
                (state / "run-status.json").write_text(json.dumps(data))
                with self.assertRaisesRegex(importer.ImportErrorSafe, "has not finished"):
                    importer.channel_queue_finished()

    def test_duplicate_id_positions_promote_to_two_records_and_rollback_one(self):
        sources = importer.load_inventory(INVENTORY)
        source = next(item for item in sources if item["playlist_id"] ==
                      "PLm29cAuVva68IeB2ahqt-jtRjHgXxTh-k")
        items = [item for item in importer.planned_placements([source])
                 if item["id"] == "OOSnGZBvCU0"]
        self.assertEqual(len(items), 2)
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            root = Path(folder)
            tv, state, stage = root / "tv", root / "state", root / "stage"
            tv.mkdir(); stage.mkdir()
            media = stage / "OOSnGZBvCU0.mkv"
            media.write_bytes(b"video" * 500)
            (stage / "OOSnGZBvCU0.jpg").write_bytes(b"JPEG thumbnail")
            info = {"id": "OOSnGZBvCU0", "title": "A repeated playlist entry",
                    "description": "Plot", "upload_date": "20240102", "channel": "Sky Does Everything"}
            # The Windows sandbox forbids hardlinks. On Linux the same test
            # exercises real inode sharing across both playlist positions.
            link_scope = (patch.object(importer.os, "link", side_effect=lambda src, dst: shutil.copy2(src, dst))
                          if os.name == "nt" else contextlib.nullcontext())
            with patch.object(importer, "TV_ROOT", tv), patch.object(importer, "STATE_ROOT", state), \
                 patch.object(importer, "verify_media", return_value={"format": {"duration": "12"}}), \
                 link_scope:
                first = importer.promote(items[0], source, media, info, SimpleNamespace(ffprobe="ffprobe"))
                second = importer.promote(items[1], source, media, info, SimpleNamespace(ffprobe="ffprobe"))
                self.assertNotEqual(first["target"], second["target"])
                if os.name != "nt":
                    self.assertEqual(Path(first["target"]).stat().st_ino, Path(second["target"]).stat().st_ino)
                    self.assertEqual(Path(first["target"]).stat().st_dev, Path(second["target"]).stat().st_dev)
                self.assertTrue(importer.record_path(items[0]).is_file())
                self.assertTrue(importer.record_path(items[1]).is_file())
                season_poster = tv / items[0]["show"] / "Season01.jpg"
                self.assertEqual(season_poster.read_bytes(), b"JPEG thumbnail")
                self.assertEqual(sum(entry["path"] == str(season_poster)
                                     for entry in first["files"]), 1)
                self.assertFalse(any(entry["path"] == str(season_poster)
                                     for entry in second["files"]))
                first_nfo = ET.parse(Path(first["target"]).with_suffix(".nfo"))
                second_nfo = ET.parse(Path(second["target"]).with_suffix(".nfo"))
                self.assertEqual(first_nfo.findtext("episode"),
                                 str(items[0]["episode"]))
                self.assertEqual(second_nfo.findtext("episode"),
                                 str(items[1]["episode"]))
                self.assertNotEqual(first_nfo.findtext("uniqueid"), second_nfo.findtext("uniqueid"))
                self.assertEqual(first_nfo.findtext("youtubeid"), second_nfo.findtext("youtubeid"))
                importer.rollback_one(items[0])
                self.assertFalse(Path(first["target"]).exists())
                self.assertFalse(season_poster.exists())
                self.assertTrue(Path(second["target"]).exists())
                self.assertTrue(media.exists())
                self.assertTrue(importer.record_path(items[1]).exists())


if __name__ == "__main__":
    unittest.main()
