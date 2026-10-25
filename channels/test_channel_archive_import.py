import datetime as dt
import contextlib
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import channel_archive_import as importer


VIDEO_ID = "aB_cD-12345"


class ChannelImportTests(unittest.TestCase):
    def test_sources_and_classification(self):
        sources = importer.load_sources(Path(__file__).with_name("channel_sources.json"))
        self.assertEqual(len(sources), 6)
        self.assertEqual(sources[2]["show"], "Willjum Videos")
        self.assertEqual(importer.classify_error("HTTP Error 429: Too Many Requests"), "bot_or_429")
        self.assertEqual(importer.classify_error("Sign in to confirm you're not a bot"), "bot_or_429")
        self.assertEqual(importer.classify_error("HTTP Error 503"), "transient")
        self.assertEqual(importer.classify_error("This video is unavailable"), "unavailable")

    def test_music_quiet_window(self):
        zone = importer.LOCAL_ZONE
        self.assertTrue(importer.quiet_window(dt.datetime(2026, 10, 14, 4, 0, tzinfo=zone)))
        self.assertFalse(importer.quiet_window(dt.datetime(2026, 10, 14, 5, 30, tzinfo=zone)))

    def test_video_date_and_existing_ids(self):
        self.assertEqual(importer.video_date({"upload_date": "20241108"}), "2024-11-08")
        self.assertEqual(importer.video_date({"upload_date": "bad", "timestamp": 0}), None)
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            root = Path(temp)
            (root / f"Video [{VIDEO_ID}].mkv").write_bytes(b"media")
            (root / "unrelated.mp4").write_bytes(b"media")
            self.assertEqual(list(importer.media_ids(root)), [VIDEO_ID])

    def test_flat_scan_and_full_video_bot_probe(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            source = {"slug": "jjt-cool", "show": "JJtCool Videos",
                      "url": "https://www.youtube.com/@JJtCool/videos",
                      "channel_id": "UCL-Hy0nGqqFKH6qIZR25uyA"}
            args = SimpleNamespace(yt_dlp="yt-dlp", cookies=None)
            flat = {"id": "UULF-Hy0nGqqFKH6qIZR25uyA",
                    "channel_id": source["channel_id"],
                    "entries": [{"id": VIDEO_ID}, {"id": "bB_cD-12345"}]}
            with patch.object(importer, "STATE_ROOT", Path(temp)), patch.object(importer, "gate"), \
                 patch.object(importer, "run_json_command", side_effect=[
                     (0, json.dumps(flat), ""),
                     (1, "", "This channel does not have a shorts tab")]) as mocked:
                ids = importer.scan_channel(source, args)
                self.assertEqual(ids, ["bB_cD-12345", VIDEO_ID])
                self.assertNotIn("--no-playlist", mocked.call_args.args[0])
                self.assertEqual(mocked.call_count, 2)
                self.assertTrue(any(value.endswith("/shorts") for value in mocked.call_args.args[0]))
            with patch.object(importer, "STATE_ROOT", Path(temp)), patch.object(importer, "gate"), \
                 patch.object(importer, "run_json_command", return_value=(1, "", "Sign in to confirm you're not a bot")) as mocked:
                with self.assertRaisesRegex(importer.ImportErrorSafe, "bot_or_429"):
                    importer.probe_channel(source, VIDEO_ID, args)
                self.assertEqual(json.loads((Path(temp) / "probes" / "jjt-cool.json").read_text())["status"],
                                 "bot_or_429")
                command = mocked.call_args.args[0]
                self.assertEqual(command[command.index("--extractor-retries") + 1], "0")

    def test_shorts_tab_excludes_overlap_and_fails_closed(self):
        source = {"slug": "jjt-cool", "show": "JJtCool Videos",
                  "url": "https://www.youtube.com/@JJtCool/videos",
                  "channel_id": "UCL-Hy0nGqqFKH6qIZR25uyA"}
        args = SimpleNamespace(yt_dlp="yt-dlp", cookies=None)
        flat = {"channel_id": source["channel_id"],
                "entries": [{"id": VIDEO_ID}, {"id": "bB_cD-12345"}]}
        shorts = {"channel_id": source["channel_id"], "entries": [{"id": VIDEO_ID}]}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            state = Path(temp)
            with patch.object(importer, "STATE_ROOT", state), patch.object(importer, "gate"), \
                 patch.object(importer, "run_json_command", side_effect=[
                     (0, json.dumps(flat), ""), (0, json.dumps(shorts), "")]):
                self.assertEqual(importer.scan_channel(source, args), ["bB_cD-12345"])
            catalog = json.loads((state / "catalog" / "jjt-cool.json").read_text())
            self.assertEqual(catalog["videos_tab_count"], 2)
            self.assertEqual(catalog["shorts_excluded_from_videos"], 1)
            self.assertEqual(json.loads((state / "skipped-shorts" / f"{VIDEO_ID}.json").read_text())["reason"],
                             "listed_in_channel_shorts_tab")
            with patch.object(importer, "STATE_ROOT", state):
                status = importer.write_run_status({"jjt-cool": ["bB_cD-12345"]},
                                                   {"bB_cD-12345": Path("other.mkv")},
                                                   limited=False, terminal_rescan_verified=True)
            self.assertEqual((status["catalog_total"], status["present"], status["shorts_excluded"],
                              status["unresolved"], status["status"]), (2, 1, 1, 0, "complete"))
            with patch.object(importer, "STATE_ROOT", state), patch.object(importer, "gate"), \
                 patch.object(importer, "run_json_command", side_effect=[
                     (0, json.dumps(flat), ""), (1, "", "HTTP Error 429")]):
                with self.assertRaisesRegex(importer.ImportErrorSafe, "Shorts exclusion scan failed"):
                    importer.scan_channel(source, args)

    def test_metadata_short_preflight_skips_before_fetch(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            root = Path(temp)
            tv, old, stage, state = (root / name for name in ("tv", "old", "stage", "state"))
            tv.mkdir(); old.mkdir(); state.mkdir()
            source = {"slug": "jjt-cool", "show": "JJtCool Videos",
                      "url": "https://www.youtube.com/@JJtCool/videos",
                      "channel_id": "UCL-Hy0nGqqFKH6qIZR25uyA"}
            (state / "catalog").mkdir()
            (state / "catalog" / "jjt-cool.json").write_text(json.dumps({"videos_tab_ids": [VIDEO_ID],
                                                                    "videos_tab_count": 1,
                                                                    "shorts_excluded_from_videos": 0}))
            @contextlib.contextmanager
            def fake_lock(*_args):
                yield
            metadata = {"id": VIDEO_ID, "channel_id": source["channel_id"],
                        "title": "A brief clip #Shorts", "duration": 45,
                        "formats": [{"url": "https://signed.example/video"}]}
            self.assertEqual(importer.shorts_evidence(metadata), "short_duration_with_shorts_tag")
            self.assertIsNone(importer.shorts_evidence({"title": "Brief regular clip", "duration": 45}))
            with patch.object(importer, "TV_ROOT", tv), patch.object(importer, "OLD_ROOT", old), \
                 patch.object(importer, "STAGE_ROOT", stage), patch.object(importer, "STATE_ROOT", state), \
                 patch.object(importer, "locked", fake_lock), patch.object(importer, "music_lock_wait", fake_lock), \
                 patch.object(importer, "gate"), patch.object(importer, "existing_inventory", return_value=({}, {})), \
                 patch.object(importer, "scan_channel", return_value=[VIDEO_ID]), \
                 patch.object(importer, "probe_channel"), \
                 patch.object(importer, "run_json_command", return_value=(0, json.dumps(metadata), "")), \
                 patch.object(importer, "fetch_video") as fetch, patch.object(importer.time, "sleep"):
                self.assertEqual(importer.run(SimpleNamespace(limit=None, yt_dlp="yt-dlp", cookies=None), [source]), 0)
                fetch.assert_not_called()
            status = json.loads((state / "run-status.json").read_text())
            self.assertEqual((status["catalog_total"], status["shorts_excluded"], status["unresolved"]),
                             (1, 1, 0))

    def test_run_status_not_complete_with_unavailable(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            state = Path(temp)
            (state / "unavailable").mkdir()
            (state / "unavailable" / f"{VIDEO_ID}.json").write_text("{}")
            with patch.object(importer, "STATE_ROOT", state):
                status = importer.write_run_status({"s": [VIDEO_ID, "bB_cD-12345"]},
                                                   {"bB_cD-12345": Path("dummy.mkv")}, limited=False)
                self.assertEqual(status["status"], "partial")
                self.assertEqual(status["unavailable"], 1)

    def test_atomic_promotion_nfo_and_recovery_metadata(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            root = Path(temp)
            tv = root / "Youtube-Series"
            stage_root = root / "stage"
            state = root / "state"
            source = {"slug": "jjt-cool", "show": "JJtCool Videos", "url": "https://www.youtube.com/@JJtCool/videos"}
            stage = stage_root / source["slug"] / VIDEO_ID
            stage.mkdir(parents=True)
            media = stage / f"{VIDEO_ID}.mkv"
            media.write_bytes(b"video" * 500)
            (stage / f"{VIDEO_ID}.info.json").write_text(json.dumps({"id": VIDEO_ID, "title": "A title"}))
            (stage / f"{VIDEO_ID}.description").write_text("Full original description")
            (stage / f"{VIDEO_ID}.en.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n")
            (stage / f"{VIDEO_ID}.webp").write_bytes(b"webp")
            (stage / f"{VIDEO_ID}.jpg").write_bytes(b"jpg")
            info = {"id": VIDEO_ID, "title": "A title", "upload_date": "20241108",
                    "description": "Full original description", "channel": "JJtCool",
                    "formats": [{"url": "https://signed.example/token"}]}
            # The Windows sandbox denies hardlink creation; copying here tests
            # promotion ordering and content without changing Linux behavior.
            with patch.object(importer, "TV_ROOT", tv), patch.object(importer, "STATE_ROOT", state), \
                 patch.object(importer, "verify_media", return_value={"format": {"duration": "12"}}), \
                 patch.object(importer.os, "link", side_effect=lambda src, dst: shutil.copy2(src, dst)):
                record = importer.promote(source, VIDEO_ID, media, info, SimpleNamespace(ffprobe="ffprobe"))
                target = Path(record["target"])
                self.assertTrue(target.is_file())
                self.assertFalse(stage.exists())
                self.assertEqual(record["season"], 2024)
                self.assertEqual(record["episode"], 1)
                self.assertTrue(target.with_suffix(".jpg").is_file())
                self.assertTrue(target.with_name(target.stem + ".en.srt").is_file())
                source_info = json.loads(target.with_name(target.stem + ".source.json").read_text())
                self.assertEqual(source_info["id"], VIDEO_ID)
                self.assertNotIn("formats", source_info)
                self.assertEqual(ET.parse(target.with_suffix(".nfo")).findtext("title"), "A title")
                self.assertEqual(ET.parse(target.with_suffix(".nfo")).find("uniqueid").attrib["type"], "youtube")
                self.assertEqual(importer.next_episode(tv / source["show"], 2024), 2)
                self.assertFalse((state / "pending" / f"{VIDEO_ID}.json").exists())
                self.assertTrue((state / "records" / f"{VIDEO_ID}.json").exists())

    def test_pending_target_cannot_escape_show(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            root = Path(temp)
            tv = root / "Youtube-Series"
            tv.mkdir()
            state = root / "state"
            (state / "pending").mkdir(parents=True)
            source = {"slug": "jjt-cool", "show": "JJtCool Videos", "url": "https://www.youtube.com/@JJtCool/videos"}
            media = root / (VIDEO_ID + ".mkv")
            media.write_bytes(b"video" * 500)
            info = {"id": VIDEO_ID, "title": "A title", "upload_date": "20241108"}
            pending = {"source": "jjt-cool", "id": VIDEO_ID, "target": str(tv / ".." / "outside.mkv"),
                       "season": 2024, "episode": 1, "aired": "2024-11-08"}
            (state / "pending" / f"{VIDEO_ID}.json").write_text(json.dumps(pending))
            with patch.object(importer, "TV_ROOT", tv), patch.object(importer, "STATE_ROOT", state):
                with self.assertRaisesRegex(importer.ImportErrorSafe, "Unsafe target"):
                    importer.promote(source, VIDEO_ID, media, info, SimpleNamespace(ffprobe="ffprobe"))

    def test_music_window_deferral_resumes_same_video(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            root = Path(temp)
            tv, old, stage, state = (root / name for name in ("tv", "old", "stage", "state"))
            tv.mkdir(); old.mkdir(); state.mkdir()
            source = {"slug": "jjt-cool", "show": "JJtCool Videos", "url": "https://www.youtube.com/@JJtCool/videos"}
            (state / "catalog").mkdir()
            (state / "catalog" / "jjt-cool.json").write_text(json.dumps({"ids_oldest_first": [VIDEO_ID]}))
            @contextlib.contextmanager
            def fake_lock(*_args):
                yield
            imported = {"target": str(tv / f"Video [{VIDEO_ID}].mkv"), "season": 2024,
                        "episode": 1, "title": "A title"}
            with patch.object(importer, "TV_ROOT", tv), patch.object(importer, "OLD_ROOT", old), \
                 patch.object(importer, "STAGE_ROOT", stage), patch.object(importer, "STATE_ROOT", state), \
                 patch.object(importer, "locked", fake_lock), patch.object(importer, "music_lock_wait", fake_lock), \
                 patch.object(importer, "gate"), patch.object(importer, "existing_inventory", return_value=({}, {})), \
                 patch.object(importer, "scan_channel", return_value=[VIDEO_ID]), \
                 patch.object(importer, "probe_channel"), patch.object(importer, "preflight_video"), \
                 patch.object(importer, "fetch_video",
                     side_effect=[importer.ImportErrorSafe("DEFERRED: music window"), (Path("dummy"), {"id": VIDEO_ID})]) as fetch, \
                 patch.object(importer, "promote", return_value=imported), patch.object(importer.time, "sleep"):
                self.assertEqual(importer.run(SimpleNamespace(limit=None), [source]), 0)
                self.assertEqual(fetch.call_count, 2)

    def test_final_rescan_processes_upload_added_during_run(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            root = Path(temp)
            tv, old, stage, state = (root / name for name in ("tv", "old", "stage", "state"))
            tv.mkdir(); old.mkdir(); state.mkdir()
            source = {"slug": "jjt-cool", "show": "JJtCool Videos",
                      "url": "https://www.youtube.com/@JJtCool/videos"}
            later_id = "bB_cD-12345"
            process_scans = iter(([VIDEO_ID], [VIDEO_ID, later_id]))
            final_scans = iter(([VIDEO_ID, later_id], [VIDEO_ID, later_id]))

            def scan_from(sequence):
                ids = list(next(sequence))
                importer.atomic_json(state / "catalog" / "jjt-cool.json",
                                     {"videos_tab_ids": ids, "videos_tab_count": len(ids),
                                      "shorts_excluded_from_videos": 0})
                return ids

            def fake_process(_source, ids, _args, present_new, _present_old, imported):
                for video_id in ids:
                    if video_id not in present_new:
                        present_new[video_id] = tv / f"Video [{video_id}].mkv"
                        imported += 1
                return imported, False

            @contextlib.contextmanager
            def fake_lock(*_args):
                yield

            with patch.object(importer, "TV_ROOT", tv), patch.object(importer, "OLD_ROOT", old), \
                 patch.object(importer, "STAGE_ROOT", stage), patch.object(importer, "STATE_ROOT", state), \
                 patch.object(importer, "locked", fake_lock), patch.object(importer, "gate"), \
                 patch.object(importer, "existing_inventory", return_value=({}, {})), \
                 patch.object(importer, "scan_source_for_run", side_effect=lambda *_: scan_from(process_scans)), \
                 patch.object(importer, "scan_source_for_rescan", side_effect=lambda *_: scan_from(final_scans)), \
                 patch.object(importer, "process_source", side_effect=fake_process):
                self.assertEqual(importer.run(SimpleNamespace(limit=None), [source]), 0)
            status = json.loads((state / "run-status.json").read_text())
            self.assertEqual((status["status"], status["scan_pass"], status["terminal_rescan_verified"],
                              status["catalog_total"], status["present"]),
                             ("complete", 2, True, 2, 2))
            first = json.loads((state / "runs" / status["run_id"] / "rescan-pass-1.json").read_text())
            self.assertEqual(first["additions"]["jjt-cool"]["new_videos_tab_ids"], [later_id])

    def test_repeated_rescan_additions_leave_resumable_partial_status(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            root = Path(temp)
            tv, old, stage, state = (root / name for name in ("tv", "old", "stage", "state"))
            tv.mkdir(); old.mkdir(); state.mkdir()
            source = {"slug": "jjt-cool", "show": "JJtCool Videos",
                      "url": "https://www.youtube.com/@JJtCool/videos"}
            ids = [VIDEO_ID, "bB_cD-12345", "cC_dE-12345"]
            process_scans = iter((ids[:1], ids[:2]))
            final_scans = iter((ids[:2], ids[:3]))

            def scan_from(sequence):
                result = list(next(sequence))
                importer.atomic_json(state / "catalog" / "jjt-cool.json",
                                     {"videos_tab_ids": result, "videos_tab_count": len(result),
                                      "shorts_excluded_from_videos": 0})
                return result

            def fake_process(_source, current, _args, present_new, _present_old, imported):
                for video_id in current:
                    if video_id not in present_new:
                        present_new[video_id] = tv / f"Video [{video_id}].mkv"
                        imported += 1
                return imported, False

            @contextlib.contextmanager
            def fake_lock(*_args):
                yield

            with patch.object(importer, "TV_ROOT", tv), patch.object(importer, "OLD_ROOT", old), \
                 patch.object(importer, "STAGE_ROOT", stage), patch.object(importer, "STATE_ROOT", state), \
                 patch.object(importer, "MAX_SCAN_PASSES", 2), patch.object(importer, "locked", fake_lock), \
                 patch.object(importer, "gate"), patch.object(importer, "existing_inventory", return_value=({}, {})), \
                 patch.object(importer, "scan_source_for_run", side_effect=lambda *_: scan_from(process_scans)), \
                 patch.object(importer, "scan_source_for_rescan", side_effect=lambda *_: scan_from(final_scans)), \
                 patch.object(importer, "process_source", side_effect=fake_process):
                self.assertEqual(importer.run(SimpleNamespace(limit=None), [source]), 0)
            status = json.loads((state / "run-status.json").read_text())
            self.assertEqual((status["status"], status["scan_pass"], status["terminal_rescan_verified"],
                              status["catalog_total"], status["present"], status["unresolved"]),
                             ("partial", 2, False, 3, 2, 1))
            self.assertEqual(status["rescan_additions"]["jjt-cool"]["new_eligible_ids"], [ids[2]])

    def test_rescan_short_conflict_stops_without_terminal_status(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            root = Path(temp)
            tv, old, stage, state = (root / name for name in ("tv", "old", "stage", "state"))
            tv.mkdir(); old.mkdir(); state.mkdir()
            source = {"slug": "jjt-cool", "show": "JJtCool Videos",
                      "url": "https://www.youtube.com/@JJtCool/videos"}

            def process_scan(*_args):
                importer.atomic_json(state / "catalog" / "jjt-cool.json",
                                     {"videos_tab_ids": [VIDEO_ID], "videos_tab_count": 1})
                return [VIDEO_ID]

            def final_scan(*_args):
                importer.atomic_json(state / "catalog" / "jjt-cool.json",
                                     {"videos_tab_ids": [VIDEO_ID], "videos_tab_count": 1,
                                      "shorts_excluded_from_videos": 1})
                importer.record_short_skip(source, VIDEO_ID, "listed_in_channel_shorts_tab")
                return []

            def fake_process(_source, _ids, _args, present_new, _present_old, imported):
                present_new[VIDEO_ID] = tv / f"Video [{VIDEO_ID}].mkv"
                return imported + 1, False

            @contextlib.contextmanager
            def fake_lock(*_args):
                yield

            with patch.object(importer, "TV_ROOT", tv), patch.object(importer, "OLD_ROOT", old), \
                 patch.object(importer, "STAGE_ROOT", stage), patch.object(importer, "STATE_ROOT", state), \
                 patch.object(importer, "locked", fake_lock), patch.object(importer, "gate"), \
                 patch.object(importer, "existing_inventory", return_value=({}, {})), \
                 patch.object(importer, "scan_source_for_run", side_effect=process_scan), \
                 patch.object(importer, "scan_source_for_rescan", side_effect=final_scan), \
                 patch.object(importer, "process_source", side_effect=fake_process):
                with self.assertRaisesRegex(importer.ImportErrorSafe, "Shorts already present in TV"):
                    importer.run(SimpleNamespace(limit=None), [source])
            status = json.loads((state / "run-status.json").read_text())
            self.assertEqual((status["status"], status["terminal_rescan_verified"],
                              status["issues"]["jjt-cool"]), ("partial", False, [VIDEO_ID]))
            issue = json.loads((state / "issues" / f"shorts-present-{VIDEO_ID}.json").read_text())
            self.assertEqual(issue["type"], "shorts_already_in_tv")
            self.assertEqual(issue["id"], VIDEO_ID)

    def test_single_source_scope_cannot_mark_global_complete(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            with patch.object(importer, "STATE_ROOT", Path(temp)):
                status = importer.write_run_status({"jjt-cool": [VIDEO_ID]},
                                                   {VIDEO_ID: Path("existing.mkv")}, limited=False,
                                                   terminal_rescan_verified=True, full_source_scope=False)
            self.assertEqual(status["status"], "partial")
            self.assertFalse(status["full_source_scope"])


if __name__ == "__main__":
    unittest.main()
