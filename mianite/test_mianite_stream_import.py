"""Offline integrity checks for the priority Mianite full-stream queue."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

import mianite_stream_import as stream
import playlist_archive_import as base


class InventoryTests(unittest.TestCase):
    def test_pinned_inventory_preserves_split_and_gap(self):
        inventory = stream.load_inventory(stream.INVENTORY_DEFAULT)
        entries = inventory["entries"]
        self.assertEqual(len(entries), 25)
        self.assertEqual((entries[0]["source_episode"], entries[-1]["source_episode"]), (86, 110))
        self.assertEqual([(item["source_episode"], item["source_part"])
                          for item in entries[11:13]], [(97, 1), (97, 2)])
        self.assertNotIn(105, {item["source_episode"] for item in entries})
        self.assertEqual(entries[20]["source_episode"], 106)

    def test_inventory_rejects_changed_sequence_or_duplicate_id(self):
        inventory = stream.load_inventory(stream.INVENTORY_DEFAULT)
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            path = Path(folder) / "inventory.json"
            inventory["entries"][20]["source_episode"] = 105
            path.write_text(json.dumps(inventory), encoding="utf-8")
            with self.assertRaisesRegex(base.ImportErrorSafe, "changed stream episode sequence"):
                stream.load_inventory(path)
            inventory["entries"][20]["source_episode"] = 106
            inventory["entries"][1]["id"] = inventory["entries"][0]["id"]
            path.write_text(json.dumps(inventory), encoding="utf-8")
            with self.assertRaisesRegex(base.ImportErrorSafe, "duplicate IDs"):
                stream.load_inventory(path)

    def test_strict_scan_stops_on_addition_or_reorder(self):
        inventory = stream.load_inventory(stream.INVENTORY_DEFAULT)
        source = stream.scan_source(inventory)
        args = argparse.Namespace()
        expected = source["ordered_video_ids"]
        for observed in (expected + ["abcdefghijk"], list(reversed(expected))):
            with self.subTest(observed_count=len(observed)):
                with mock.patch.object(base, "scan_playlist", return_value={
                    "ids": observed, "new_unpinned_ids": observed[len(expected):],
                    "hidden_private_candidates": [], "title": "Mianite S2"}):
                    with self.assertRaisesRegex(base.ImportErrorSafe, "scan drift"):
                        stream.strict_scan(source, args)
        with mock.patch.object(base, "scan_playlist", return_value={
                "ids": expected, "new_unpinned_ids": [],
                "hidden_private_candidates": [], "title": "Mianite S2"}):
            self.assertEqual(stream.strict_scan(source, args)["ids"], expected)

    def test_video_source_rejects_wrong_uploader_and_short(self):
        item = stream.load_inventory(stream.INVENTORY_DEFAULT)["entries"][0]
        stream.verify_video_source(item, {"id": item["id"], "channel": "CaptainSparklez 2",
                                          "duration": 7000})
        with self.assertRaisesRegex(base.ImportErrorSafe, "unexpected full stream uploader"):
            stream.verify_video_source(item, {"id": item["id"], "channel": "Unknown Creator"})
        with self.assertRaisesRegex(base.ImportErrorSafe, "unexpected Short"):
            stream.verify_video_source(item, {"id": item["id"], "is_short": True})

    def test_active_channel_service_blocks_priority_run(self):
        response = mock.Mock(returncode=0, stdout="ActiveState=activating\nMainPID=1234\n")
        with mock.patch.object(stream.subprocess, "run", return_value=response):
            with self.assertRaisesRegex(base.ImportErrorSafe, "not fully stopped"):
                stream.check_services_paused()

    def test_only_validated_jjt_user_skip_allows_handoff(self):
        """The 267/274 JJtCool user skip (not a completed boundary) is the handoff, after offline validation."""
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            state = Path(folder)
            marker, validator = state / "jjt-cool-skip.json", state / "jjt_skip_handoff.py"
            marker.write_text("{}", encoding="utf-8")
            validator.write_text("", encoding="utf-8")
            payload = {"stopped_after_source": "jjt-cool", "status": "partial",
                       "source_boundary_verified": False, "handoff_ready": False,
                       "full_source_scope": False, "terminal_rescan_verified": False}
            verdict = {"status": "valid_user_skip", "source": "jjt-cool", "catalog_eligible_count": 274,
                       "completed_record_count": 267, "omitted_count": 7}

            def validated(result):
                return mock.Mock(returncode=0, stdout=json.dumps(result))

            with mock.patch.object(stream, "CHANNEL_STATE", state), \
                    mock.patch.object(stream, "SKIP_MARKER", marker), \
                    mock.patch.object(stream, "SKIP_VALIDATOR", validator):
                (state / "run-status.json").write_text(json.dumps(payload), encoding="utf-8")
                with mock.patch.object(stream.subprocess, "run", return_value=validated(verdict)):
                    stream.check_jjt_handoff()
                with mock.patch.object(stream.subprocess, "run", return_value=validated({**verdict, "completed_record_count": 266})):
                    with self.assertRaisesRegex(base.ImportErrorSafe, "unexpected scope"):
                        stream.check_jjt_handoff()
                with mock.patch.object(stream.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="")):
                    with self.assertRaisesRegex(base.ImportErrorSafe, "failed offline validation"):
                        stream.check_jjt_handoff()
                # A run that claims a completed boundary is not the owner's skip.
                (state / "run-status.json").write_text(json.dumps({**payload, "handoff_ready": True}), encoding="utf-8")
                with self.assertRaisesRegex(base.ImportErrorSafe, "skip is not verified"):
                    stream.check_jjt_handoff()
                marker.unlink()
                (state / "run-status.json").write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(base.ImportErrorSafe, "skip is not verified"):
                    stream.check_jjt_handoff()


class PlacementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.show = root / "Youtube-Series" / "Mianite (2014)"
        self.show.mkdir(parents=True)
        base.xml_write(self.show / "tvshow.nfo", "tvshow", {"title": "Mianite"})
        for number in (1, 2):
            season = self.show / f"Season {number:02d}"
            season.mkdir()
            (season / f"Mianite - S{number:02d}E001.mkv").write_bytes(b"original" * 200)
        self.season = self.show / "Season 03"
        self.stage = root / "stage"
        self.stage.mkdir()
        self.state = root / "state"
        self.inventory = stream.load_inventory(stream.INVENTORY_DEFAULT)
        self.digest = stream.inventory_digest(stream.INVENTORY_DEFAULT)
        for module, name, value in (
                (stream, "SHOW_DIR", self.show), (stream, "SEASON_DIR", self.season),
                (stream, "OWNER_MARKER", self.season / ".mianite-stream-import.json"),
                (stream, "SEASON_NFO", self.season / "season.nfo"),
                (stream, "STATE_ROOT", self.state), (stream, "STAGE_ROOT", self.stage),
                (base, "TV_ROOT", root / "Youtube-Series")):
            patcher = mock.patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_distinct_plex_positions_and_source_labels(self):
        first, one, two, finale = (self.inventory["entries"][index] for index in (0, 11, 12, 24))
        media = self.stage / "example.mkv"
        self.assertIn("S03E001", stream.target_path(first, media).name)
        self.assertIn("S03E012", stream.target_path(one, media).name)
        self.assertIn("S2 Stream 097 Part 1", stream.target_path(one, media).name)
        self.assertIn("S03E013", stream.target_path(two, media).name)
        self.assertIn("S2 Stream 097 Part 2", stream.target_path(two, media).name)
        self.assertIn("S03E025", stream.target_path(finale, media).name)

    def test_unowned_season_is_rejected_without_changes(self):
        self.season.mkdir()
        original = self.season / "unrelated.mkv"
        original.write_bytes(b"keep me")
        with self.assertRaisesRegex(base.ImportErrorSafe, "unowned files"):
            stream.ensure_season(self.inventory, self.digest)
        self.assertEqual(original.read_bytes(), b"keep me")
        self.assertFalse(stream.OWNER_MARKER.exists())

    def test_named_season_conflict_preserves_existing_show(self):
        show_nfo = self.show / "tvshow.nfo"
        root = ET.parse(show_nfo).getroot()
        ET.SubElement(root, "namedseason", {"number": "3"}).text = "A different season"
        ET.ElementTree(root).write(show_nfo, encoding="utf-8", xml_declaration=True)
        before = show_nfo.read_bytes()
        with self.assertRaisesRegex(base.ImportErrorSafe, "Conflicting named season"):
            stream.ensure_season(self.inventory, self.digest)
        self.assertEqual(show_nfo.read_bytes(), before)
        self.assertFalse(self.season.exists())

    def test_promote_verify_and_rollback_preserve_edited_media(self):
        item = self.inventory["entries"][0]
        stage = self.stage / item["id"]
        stage.mkdir()
        media = stage / f"{item['id']}.mkv"
        media.write_bytes(b"synthetic archived video" * 100)
        (stage / f"{item['id']}.jpg").write_bytes(b"JPEG stream thumbnail")
        info = {"id": item["id"], "title": item["source_title"],
                "channel": "CaptainSparklez 2", "duration": 7000,
                "upload_date": "20150501", "description": "Full live stream"}
        # The Windows workspace sandbox disallows hardlinks. Exercise the
        # promotion journal with a file copy; rackmain1 uses real os.link.
        with mock.patch.object(base, "verify_media", return_value={"format": {"duration": "7000"}}), \
                mock.patch.object(stream.os, "link", side_effect=shutil.copyfile):
            record = stream.promote(item, media, info, self.inventory, self.digest,
                                    argparse.Namespace(ffprobe="ffprobe"))
        target = Path(record["target"])
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_bytes(), media.read_bytes())
        show_nfo = ET.parse(self.show / "tvshow.nfo").getroot()
        self.assertEqual([(node.get("number"), node.text)
                          for node in show_nfo.findall("namedseason")],
                         [("3", self.inventory["target_season_title"])])
        season_poster = self.show / "Season03.jpg"
        self.assertEqual(season_poster.read_bytes(), b"JPEG stream thumbnail")
        self.assertTrue(any(entry["path"] == str(season_poster)
                            for entry in record["files"]))
        self.assertEqual(len(list((self.state / "metadata-backups").glob("*.before"))), 1)
        self.assertEqual(len(list(self.show.glob("Season 01/*.mkv"))), 1)
        self.assertEqual(len(list(self.show.glob("Season 02/*.mkv"))), 1)
        stream.verify_record(item, full_hash=True)
        source = json.loads(target.with_name(target.stem + ".source.json").read_text())
        self.assertEqual((source["original_mianite_season"], source["original_mianite_episode"]), (2, 86))
        stream.rollback_one(item, digest=self.digest)
        self.assertFalse(target.exists())
        self.assertFalse(season_poster.exists())
        self.assertTrue(media.exists())
        self.assertEqual(len(list(self.show.glob("Season 01/*.mkv"))), 1)
        self.assertEqual(len(list(self.show.glob("Season 02/*.mkv"))), 1)

    def test_rollback_resume_after_record_unlink_preserves_source(self):
        item = self.inventory["entries"][0]
        stage = self.stage / item["id"]
        stage.mkdir()
        media = stage / f"{item['id']}.mkv"
        media.write_bytes(b"synthetic full stream" * 100)
        info = {"id": item["id"], "title": item["source_title"],
                "duration": 7000, "upload_date": "20150501"}
        with mock.patch.object(base, "verify_media", return_value={"format": {"duration": "7000"}}), \
                mock.patch.object(stream.os, "link", side_effect=shutil.copyfile):
            record = stream.promote(item, media, info, self.inventory, self.digest,
                                    argparse.Namespace(ffprobe="ffprobe"))
        record_file = stream.record_path(item)
        intent = self.state / "rollback-intent" / record_file.name
        completed = self.state / "rolled-back" / record_file.name
        base.atomic_json(intent, {"record": record})
        base.atomic_json(completed, record)
        for entry in record["files"]:
            if entry["created"]:
                Path(entry["path"]).unlink()
        record_file.unlink()

        target = Path(record["target"])
        target.write_bytes(b"unexpected replacement")
        with self.assertRaisesRegex(base.ImportErrorSafe, "rolled-back file reappeared"):
            stream.rollback_one(item, digest=self.digest)
        self.assertEqual(target.read_bytes(), b"unexpected replacement")
        target.unlink()

        stream.rollback_one(item, digest=self.digest)
        self.assertFalse(intent.exists())
        stream.rollback_one(item, digest=self.digest)
        self.assertTrue(completed.is_file())
        self.assertTrue(media.is_file())
        self.assertEqual(len(list(self.show.glob("Season 01/*.mkv"))), 1)
        self.assertEqual(len(list(self.show.glob("Season 02/*.mkv"))), 1)


if __name__ == "__main__":
    unittest.main()
