#!/usr/bin/env python3
"""Import Jordan's 25 pinned Mianite S2 full streams as Mianite Season 03.

This is a separate priority queue. Run it only after the explicit 267/274
JJtCool user-skip marker passes offline validation and the global YouTube
bot hold expires. It holds both video run locks for its whole run and the
shared music lock for each network/file step.
It never changes the existing edited Mianite seasons or original video files.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import playlist_archive_import as base


PLAYLIST_ID = "PLli4oThJ08Zyoanlo77nwzuk-wHmLTHKE"
INVENTORY_DEFAULT = Path(__file__).with_name("mianite-s2-full-streams-inventory-20261014.json")
SHOW_DIR = Path("/home/md-media/media/Youtube-Series/Mianite (2014)")
SEASON_DIR = SHOW_DIR / "Season 03"
STAGE_ROOT = Path("/home/md-media/media/.mianite-s2-full-stream-stage")
STATE_ROOT = Path("/home/ap3x/.local/share/mianite-s2-full-stream-import")
CHANNEL_STATE = Path("/home/ap3x/.local/share/youtube-series-channel-import")
CHANNEL_PROJECT = Path("/home/ap3x/AP3X_YOUTUBE_CHANNELS_20261014")
SKIP_MARKER = CHANNEL_STATE / "user-scope" / "jjt-cool-skip.json"
SKIP_VALIDATOR = CHANNEL_PROJECT / "jjt_skip_handoff.py"
CHANNEL_SOURCES = CHANNEL_PROJECT / "channel_sources.json"
# Latest bot verification was 2026-10-16 04:22:45 EDT.  This is the earliest
# allowed request time; a later block must extend the hold before any run.
GLOBAL_YOUTUBE_HOLD_UNTIL = dt.datetime(2026, 10, 17, 8, 22, 45, tzinfo=dt.timezone.utc)
CHANNEL_LOCK = CHANNEL_STATE / "channel-import.lock"
PLAYLIST_LOCK = Path("/home/ap3x/.local/share/youtube-series-playlist-import/playlist-import.lock")
RUN_LOCK = STATE_ROOT / "mianite-stream-import.lock"
PROC_ROOT = Path("/proc")
IMPORTER_PROCESS_NAMES = frozenset({
    "channel_archive_import.py", "playlist_archive_import.py",
    "mianite_stream_import.py", "run_pipeline.sh",
    "catalog_registry.py", "import_releases.py",
})
OWNER_MARKER = SEASON_DIR / ".mianite-stream-import.json"
SEASON_NFO = SEASON_DIR / "season.nfo"


def configure_base() -> None:
    """Isolate reusable playlist download helpers in this process only."""
    base.STAGE_ROOT = STAGE_ROOT
    base.STATE_ROOT = STATE_ROOT


def inventory_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_inventory(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if (data.get("schema") != "ap3x-mianite-s2-full-streams-v1"
            or data.get("playlist_id") != PLAYLIST_ID
            or data.get("canonical_url") != f"https://www.youtube.com/playlist?list={PLAYLIST_ID}"
            or data.get("title") != "Mianite S2"
            or data.get("owner") != "CaptainSparklez 2"
            or data.get("target_show") != "Mianite (2014)"
            or data.get("target_season") != 3
            or data.get("target_season_title") != "Season 2 — Full Streams (Episodes 86–110)"
            or data.get("listed_count") != 25
            or data.get("known_missing_stream_episodes") != [105]):
        raise base.ImportErrorSafe("STOP: Mianite stream inventory header changed")
    entries = data.get("entries")
    if not isinstance(entries, list) or len(entries) != 25:
        raise base.ImportErrorSafe("STOP: Mianite stream inventory must have 25 entries")
    ids = []
    episode_parts = []
    for position, item in enumerate(entries, 1):
        if not isinstance(item, dict) or item.get("position") != position or not base.valid_id(item.get("id")):
            raise base.ImportErrorSafe(f"STOP: invalid Mianite playlist slot {position}")
        if (not isinstance(item.get("source_title"), str)
                or not item["source_title"].startswith("Minecraft Mianite")
                or item.get("source_part") not in (None, 1, 2)
                or not isinstance(item.get("source_episode"), int)):
            raise base.ImportErrorSafe(f"STOP: invalid stream metadata at slot {position}")
        ids.append(item["id"])
        episode_parts.append((item["source_episode"], item["source_part"]))
    expected = [(episode, None) for episode in range(86, 111) if episode != 105]
    expected[11:12] = [(97, 1), (97, 2)]
    if len(ids) != len(set(ids)) or episode_parts != expected:
        raise base.ImportErrorSafe("STOP: duplicate IDs or changed stream episode sequence")
    return data


def scan_source(inventory: dict) -> dict:
    return {
        "playlist_id": PLAYLIST_ID,
        "canonical_url": inventory["canonical_url"],
        "ordered_video_ids": [item["id"] for item in inventory["entries"]],
        "untitled_or_unavailable_ids": [],
        "owner": inventory["owner"],
    }


def verify_show_root() -> None:
    if not SHOW_DIR.is_dir() or SHOW_DIR.is_symlink():
        raise base.ImportErrorSafe("STOP: the existing Mianite show is missing or a symlink")
    show_nfo = SHOW_DIR / "tvshow.nfo"
    if not show_nfo.is_file() or show_nfo.is_symlink():
        raise base.ImportErrorSafe("STOP: existing Mianite show NFO missing")
    if ET.parse(show_nfo).getroot().findtext("title") != "Mianite":
        raise base.ImportErrorSafe("STOP: existing Mianite show NFO title changed")
    for season in (1, 2):
        folder = SHOW_DIR / f"Season {season:02d}"
        if not folder.is_dir() or folder.is_symlink() or not any(folder.glob("*.mkv")):
            raise base.ImportErrorSafe(f"STOP: edited Mianite Season {season} is missing")


def verify_season_ownership(digest: str) -> None:
    if not SEASON_DIR.exists():
        return
    if not SEASON_DIR.is_dir() or SEASON_DIR.is_symlink():
        raise base.ImportErrorSafe("STOP: Season 03 target is not a normal directory")
    if OWNER_MARKER.is_file():
        marker = base.read_json(OWNER_MARKER)
        if marker.get("playlist_id") != PLAYLIST_ID or marker.get("inventory_sha256") != digest:
            raise base.ImportErrorSafe("STOP: Season 03 belongs to another inventory")
    elif any(SEASON_DIR.iterdir()):
        raise base.ImportErrorSafe("STOP: Season 03 already contains unowned files")
    if SEASON_NFO.exists():
        if SEASON_NFO.is_symlink() or not OWNER_MARKER.is_file():
            raise base.ImportErrorSafe("STOP: Season 03 NFO is unowned")
        root = ET.parse(SEASON_NFO).getroot()
        if root.findtext("seasonnumber") != "3":
            raise base.ImportErrorSafe("STOP: Season 03 NFO number changed")


def ensure_season(inventory: dict, digest: str) -> None:
    verify_show_root()
    verify_season_ownership(digest)
    base.ensure_named_season(SHOW_DIR / "tvshow.nfo", 3,
                             inventory["target_season_title"],
                             STATE_ROOT / "metadata-backups")
    SEASON_DIR.mkdir(exist_ok=True)
    if not OWNER_MARKER.exists():
        base.atomic_json(OWNER_MARKER, {
            "schema": "ap3x-mianite-stream-season-v1",
            "playlist_id": PLAYLIST_ID,
            "inventory_sha256": digest,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        })
    if not SEASON_NFO.exists():
        base.xml_write(SEASON_NFO, "season", {
            "title": inventory["target_season_title"], "seasonnumber": 3,
            "plot": "CaptainSparklez 2 full streams of Mianite Season 2, episodes 86–110. "
                    "Source Episode 105 is absent from the playlist; Episode 97 has two parts.",
        })


def check_services_paused() -> None:
    for service in ("youtube-channel-import.service", "youtube-playlist-import.service"):
        result = subprocess.run(
            ["systemctl", "--user", "show", service, "-p", "ActiveState", "-p", "MainPID"],
            capture_output=True, text=True, timeout=10, check=False)
        if result.returncode:
            raise base.ImportErrorSafe(f"STOP: cannot inspect {service}")
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        state = fields.get("ActiveState")
        # The channel unit may remain failed after a bot block.  A stopped
        # failed unit is safe to hand off only when check_jjt_handoff() also
        # validates the user's explicit skip marker before any source request.
        allowed = {"inactive", "failed"} if service == "youtube-channel-import.service" else {"inactive"}
        if state not in allowed or fields.get("MainPID") != "0":
            raise base.ImportErrorSafe(f"DEFERRED: {service} is not fully stopped")
    result = subprocess.run(
        ["systemctl", "--user", "show", "artist-archive-import.service",
         "-p", "ActiveState", "-p", "MainPID"],
        capture_output=True, text=True, timeout=10, check=False)
    if result.returncode:
        raise base.ImportErrorSafe("STOP: cannot inspect artist import service")
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if fields.get("ActiveState") not in {"inactive", "failed"} or fields.get("MainPID") != "0":
        raise base.ImportErrorSafe("DEFERRED: artist import service is not fully stopped")
    result = subprocess.run(
        ["systemctl", "--user", "show", "artist-archive-import.timer",
         "-p", "ActiveState", "-p", "UnitFileState"],
        capture_output=True, text=True, timeout=10, check=False)
    if result.returncode:
        raise base.ImportErrorSafe("STOP: cannot inspect artist import timer")
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if fields.get("ActiveState") != "inactive" or fields.get("UnitFileState") != "disabled":
        raise base.ImportErrorSafe("DEFERRED: artist import timer must stay inactive and disabled")
    check_no_competing_importer_processes()


def check_no_competing_importer_processes() -> None:
    """Catch manual importers that systemd service state alone cannot see."""
    if not sys.platform.startswith("linux"):
        return
    if not PROC_ROOT.is_dir():
        raise base.ImportErrorSafe("STOP: cannot inspect importer processes")
    for process in PROC_ROOT.iterdir():
        if not process.name.isdecimal() or int(process.name) == os.getpid():
            continue
        try:
            arguments = (process / "cmdline").read_bytes().decode(
                "utf-8", errors="surrogateescape").split("\0")
        except FileNotFoundError:
            continue  # Process exited between listing and reading.
        except OSError as error:
            raise base.ImportErrorSafe("STOP: cannot inspect importer processes") from error
        names = {Path(argument).name for argument in arguments if argument}
        if (names.intersection(IMPORTER_PROCESS_NAMES)
                or any(name in {"yt-dlp", "yt_dlp", "youtube-dl"}
                       or name.startswith("yt-dlp-") for name in names)):
            raise base.ImportErrorSafe("DEFERRED: another importer or yt-dlp process is running")


def check_jjt_handoff() -> None:
    """Require the user's exact 267/274 JJtCool skip without claiming completion."""
    status_file = CHANNEL_STATE / "run-status.json"
    if not status_file.is_file():
        raise base.ImportErrorSafe("DEFERRED: JJtCool source status is missing")
    status = base.read_json(status_file)
    # A user-skipped tail is not a completed source boundary.  The independent
    # channel validator binds the signed-off scope to the catalog and all 267
    # surviving completion records; this importer never changes that status.
    if (status.get("stopped_after_source") != "jjt-cool"
            or status.get("status") not in {"running", "partial"}
            or status.get("source_boundary_verified") is not False
            or status.get("handoff_ready") is not False
            or status.get("full_source_scope") is not False
            or status.get("terminal_rescan_verified") is not False
            or not SKIP_MARKER.is_file()
            or SKIP_MARKER.is_symlink()
            or not SKIP_VALIDATOR.is_file()
            or SKIP_VALIDATOR.is_symlink()):
        raise base.ImportErrorSafe("DEFERRED: explicit JJtCool skip is not verified")
    result = subprocess.run(
        [sys.executable, "-I", str(SKIP_VALIDATOR), "check",
         "--state-root", str(CHANNEL_STATE), "--sources", str(CHANNEL_SOURCES),
         "--marker", str(SKIP_MARKER)],
        capture_output=True, text=True, timeout=60, check=False)
    if result.returncode:
        raise base.ImportErrorSafe("DEFERRED: explicit JJtCool skip failed offline validation")
    try:
        verdict = json.loads(result.stdout)
    except (TypeError, ValueError) as error:
        raise base.ImportErrorSafe("STOP: JJtCool skip validator returned invalid output") from error
    if (not isinstance(verdict, dict)
            or verdict.get("status") != "valid_user_skip"
            or verdict.get("source") != "jjt-cool"
            or verdict.get("catalog_eligible_count") != 274
            or verdict.get("completed_record_count") != 267
            or verdict.get("omitted_count") != 7):
        raise base.ImportErrorSafe("STOP: JJtCool skip validator returned an unexpected scope")


def check_global_youtube_hold() -> None:
    if dt.datetime.now(dt.timezone.utc) < GLOBAL_YOUTUBE_HOLD_UNTIL:
        raise base.ImportErrorSafe("DEFERRED: global YouTube bot hold remains active")


def check_cookie_mode(path: Path | None) -> None:
    if path is None:
        raise base.ImportErrorSafe("STOP: approved YouTube-only cookie path required")
    if path.is_symlink() or not path.is_file():
        raise base.ImportErrorSafe("STOP: cookie file missing or symlinked")
    metadata = path.stat()
    if stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != os.getuid():
        raise base.ImportErrorSafe("STOP: cookie file owner/mode must be current user/0600")


def strict_scan(source: dict, args: argparse.Namespace) -> dict:
    result = base.scan_playlist(source, args)
    if (result.get("ids") != source["ordered_video_ids"]
            or result.get("new_unpinned_ids")
            or result.get("hidden_private_candidates")
            or result.get("title") != "Mianite S2"):
        raise base.ImportErrorSafe("STOP: Mianite S2 playlist scan drift; review before download")
    return result


def target_path(item: dict, media: Path) -> Path:
    part = f" Part {item['source_part']}" if item["source_part"] is not None else ""
    stem = (f"Mianite - S03E{item['position']:03d} - S2 Stream "
            f"{item['source_episode']:03d}{part} [{item['id']}]")
    target = SEASON_DIR / (stem + media.suffix.lower())
    if (media.suffix.lower() not in base.MEDIA_EXTENSIONS
            or target.is_symlink()
            or not target.resolve(strict=False).is_relative_to(SEASON_DIR.resolve())):
        raise base.ImportErrorSafe(f"STOP: unsafe Mianite stream target {target}")
    return target


def verify_video_source(item: dict, info: dict) -> None:
    if info.get("id") != item["id"]:
        raise base.ImportErrorSafe("STOP: full stream video ID changed")
    observed = str(info.get("channel") or info.get("uploader") or "")
    if observed and "".join(observed.lower().split()) != "captainsparklez2":
        raise base.ImportErrorSafe(f"STOP: unexpected full stream uploader for {item['id']}")
    if base.is_short(info) or info.get("live_status") in {"is_live", "is_upcoming"}:
        raise base.ImportErrorSafe(f"STOP: unexpected Short or live stream {item['id']}")


def record_path(item: dict) -> Path:
    return STATE_ROOT / "records" / f"{item['position']:04d}-{item['id']}.json"


def pending_path(item: dict) -> Path:
    return STATE_ROOT / "pending" / f"{item['position']:04d}-{item['id']}.json"


def verify_record(item: dict, *, full_hash: bool = False) -> dict:
    record = base.read_json(record_path(item))
    for key in ("position", "id", "source_episode", "source_part"):
        if record.get(key) != item.get(key):
            raise base.ImportErrorSafe(f"STOP: changed record mapping {record_path(item)}")
    if record.get("playlist_id") != PLAYLIST_ID or record.get("season") != 3:
        raise base.ImportErrorSafe(f"STOP: changed record identity {record_path(item)}")
    if not Path(record["target"]).resolve(strict=False).is_relative_to(SEASON_DIR.resolve()):
        raise base.ImportErrorSafe("STOP: record target escaped Season 03")
    if any(not owned_record_file(Path(entry["path"])) for entry in record.get("files") or []):
        raise base.ImportErrorSafe("STOP: record file escaped Season 03 ownership")
    base.verify_record_files(record, full_hash=full_hash)
    return record


def owned_record_file(path: Path) -> bool:
    if path.is_symlink():
        return False
    if path.resolve(strict=False) == (SHOW_DIR / "Season03.jpg").resolve(strict=False):
        return True
    return path.resolve(strict=False).is_relative_to(SEASON_DIR.resolve())


def promote(item: dict, media: Path, info: dict, inventory: dict,
            digest: str, args: argparse.Namespace) -> dict:
    base.progress("phase", "publishing")
    ensure_season(inventory, digest)
    target = target_path(item, media)
    pending_file = pending_path(item)
    if pending_file.exists():
        pending = base.read_json(pending_file)
        if (pending.get("target") != str(target) or pending.get("id") != item["id"]
                or pending.get("position") != item["position"]
                or pending.get("inventory_sha256") != digest):
            raise base.ImportErrorSafe(f"STOP: changed promotion intent {pending_file}")
    else:
        if target.exists():
            raise base.ImportErrorSafe(f"STOP: unowned target collision {target}")
        pending = {**item, "playlist_id": PLAYLIST_ID, "season": 3,
                   "target": str(target), "inventory_sha256": digest,
                   "file_intents": {}, "created_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        base.atomic_json(pending_file, pending)
    label = f"S2 Full Stream Ep. {item['source_episode']}"
    if item["source_part"] is not None:
        label += f" Part {item['source_part']}/2"
    if item["source_episode"] == 110:
        label += " — Finale"
    stage = media.parent
    base.xml_write(stage / f"{item['id']}.nfo", "episodedetails", {
        "title": label, "season": 3, "episode": item["position"],
        "aired": base.video_date(info), "studio": "CaptainSparklez 2",
        "plot": (info.get("description") or "") +
                f"\n\nOriginal Mianite Season 2 stream episode {item['source_episode']}"
                + (f", part {item['source_part']}/2." if item["source_part"] else "."),
        "uniqueid": f"{PLAYLIST_ID}:{item['position']}", "youtubeid": item["id"],
    }, uniqueid_type="youtube-mianite-stream-position")
    durable = base.durable_info(info)
    durable.update({"source_playlist_id": PLAYLIST_ID,
                    "source_playlist_position": item["position"],
                    "source_playlist_title": inventory["title"],
                    "original_mianite_season": 2,
                    "original_mianite_episode": item["source_episode"],
                    "original_mianite_part": item["source_part"],
                    "pinned_source_title": item["source_title"]})
    base.atomic_json(stage / f"{item['id']}.source.json", durable)
    files = []
    targets = base.target_files(stage, media, target)
    poster = base.season_poster_candidate(stage, item["id"], SHOW_DIR, 3)
    if poster:
        targets.insert(-1, poster)
    for original, destination in targets:
        if destination.is_symlink():
            raise base.ImportErrorSafe(f"STOP: target symlink {destination}")
        checksum = base.sha256(original)
        intents = pending.setdefault("file_intents", {})
        if str(destination) not in intents:
            intents[str(destination)] = {"sha256": checksum,
                                         "size": original.stat().st_size,
                                         "preexisting": destination.exists()}
            base.atomic_json(pending_file, pending)
        elif (intents[str(destination)]["sha256"] != checksum
              or intents[str(destination)]["size"] != original.stat().st_size):
            raise base.ImportErrorSafe(f"STOP: changed staged file {original}")
        if destination.exists():
            if destination.stat().st_size != original.stat().st_size or base.sha256(destination) != checksum:
                raise base.ImportErrorSafe(f"STOP: target collision {destination}")
        else:
            os.link(original, destination)
        files.append({"path": str(destination), "size": original.stat().st_size,
                      "sha256": checksum,
                      "created": not intents[str(destination)]["preexisting"]})
    base.verify_media(target, args.ffprobe)
    record = {**pending, "status": "complete", "title": label,
              "aired": base.video_date(info), "files": files,
              "source_media": str(media),
              "finished_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    base.atomic_json(record_path(item), record)
    pending_file.unlink(missing_ok=True)
    return record


def write_status(items: list[dict], *, limited: bool, terminal_rescan_verified: bool = False) -> dict:
    present = sum(record_path(item).is_file() for item in items)
    status = {"status": "complete" if present == len(items) and terminal_rescan_verified else "partial",
              "playlist_id": PLAYLIST_ID, "season": 3, "placements": len(items),
              "present": present, "unresolved": len(items) - present,
              "limited": limited, "terminal_rescan_verified": terminal_rescan_verified,
              "known_missing_original_episode": 105,
              "checked_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    base.atomic_json(STATE_ROOT / "run-status.json", status)
    return status


def run(args: argparse.Namespace, inventory: dict, digest: str) -> int:
    check_global_youtube_hold()
    verify_show_root()
    verify_season_ownership(digest)
    check_cookie_mode(args.cookies)
    for binary in (args.yt_dlp, args.ffprobe, "/home/ap3x/.deno/bin/deno"):
        if not Path(binary).is_file() or not os.access(binary, os.X_OK):
            raise base.ImportErrorSafe(f"STOP: executable missing: {binary}")
    check_services_paused()
    check_jjt_handoff()
    source = scan_source(inventory)
    items = inventory["entries"]
    STAGE_ROOT.mkdir(parents=True, exist_ok=True)
    (STAGE_ROOT / ".plexignore").write_text("*\n", encoding="utf-8")
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    with base.locked(CHANNEL_LOCK), base.locked(PLAYLIST_LOCK), base.locked(RUN_LOCK):
        base.start_progress("mianite", STATE_ROOT, eligible=len(items))
        base.progress("phase", "scanning")
        check_services_paused()
        check_jjt_handoff()
        while True:
            try:
                check_services_paused()
                check_jjt_handoff()
                with base.music_lock_wait():
                    check_services_paused()
                    check_jjt_handoff()
                    strict_scan(source, args)
                break
            except base.ImportErrorSafe as error:
                if not str(error).startswith("DEFERRED:"):
                    raise
                print(error, flush=True)
                base.progress("phase", "waiting")
                time.sleep(60)
        while True:
            try:
                check_services_paused()
                check_jjt_handoff()
                with base.music_lock_wait():
                    check_services_paused()
                    check_jjt_handoff()
                    base.progress("phase", "probing")
                    base.probe_access(items[0]["id"], args)
                break
            except base.ImportErrorSafe as error:
                if not str(error).startswith("DEFERRED:"):
                    raise
                print(error, flush=True)
                base.progress("phase", "waiting")
                time.sleep(60)
        present = base.media_ids(base.TV_ROOT)
        old = base.media_ids(base.OLD_ROOT)
        imported = 0
        for item in items:
            path = record_path(item)
            if path.is_file():
                verify_record(item)
                continue
            base.progress("item", position=item["position"], completed=sum(record_path(other).is_file() for other in items))
            while True:
                try:
                    check_services_paused()
                    check_jjt_handoff()
                    with base.music_lock_wait():
                        check_services_paused()
                        check_jjt_handoff()
                        media, info = base.fetch_video(
                            item["id"], present.get(item["id"]) or old.get(item["id"]), args)
                        verify_video_source(item, info)
                        record = promote(item, media, info, inventory, digest, args)
                        present[item["id"]] = Path(record["target"])
                        imported += 1
                        write_status(items, limited=True)
                        print(f"IMPORTED Mianite stream S03E{item['position']:03d} "
                              f"(source S2E{item['source_episode']}) {item['id']}", flush=True)
                    break
                except base.ImportErrorSafe as error:
                    if not str(error).startswith("DEFERRED:"):
                        raise
                    print(error, flush=True)
                    base.progress("phase", "waiting")
                    time.sleep(60)
            if args.limit and imported >= args.limit:
                print(f"LIMIT {args.limit} reached; staged queue is resumable", flush=True)
                return 0
            time.sleep(10)
        while True:
            try:
                check_services_paused()
                check_jjt_handoff()
                with base.music_lock_wait():
                    check_services_paused()
                    check_jjt_handoff()
                    strict_scan(source, args)
                break
            except base.ImportErrorSafe as error:
                if not str(error).startswith("DEFERRED:"):
                    raise
                print(error, flush=True)
                base.progress("phase", "waiting")
                time.sleep(60)
        status = write_status(items, limited=False, terminal_rescan_verified=True)
        base.progress("done")
        print(f"{status['status'].upper()}: {status['present']}/25 Mianite full streams", flush=True)
    return 0


def rollback_one(item: dict, *, digest: str) -> None:
    """Remove only verified links created by this importer; keep source media."""
    record_file = record_path(item)
    intent = STATE_ROOT / "rollback-intent" / record_file.name
    rolled_back = STATE_ROOT / "rolled-back" / record_file.name

    def confirm_already_rolled_back(record: dict) -> None:
        if (record.get("inventory_sha256") != digest
                or record.get("playlist_id") != PLAYLIST_ID
                or record.get("position") != item["position"]
                or record.get("id") != item["id"]
                or not record.get("files")):
            raise base.ImportErrorSafe("STOP: rolled-back record identity changed")
        for entry in record["files"]:
            if not entry.get("created"):
                continue
            path = Path(entry["path"])
            if not owned_record_file(path):
                raise base.ImportErrorSafe("STOP: unsafe rolled-back path")
            if path.exists():
                raise base.ImportErrorSafe(f"STOP: rolled-back file reappeared: {path}")

    if intent.exists():
        intent_record = base.read_json(intent).get("record")
        if record_file.is_file():
            record = base.read_json(record_file)
            if intent_record != record:
                raise base.ImportErrorSafe("STOP: rollback intent changed")
        else:
            if not rolled_back.is_file() or base.read_json(rolled_back) != intent_record:
                raise base.ImportErrorSafe("STOP: rollback record missing without matching completion marker")
            confirm_already_rolled_back(intent_record)
            intent.unlink()
            return
    else:
        if not record_file.is_file():
            if not rolled_back.is_file():
                raise base.ImportErrorSafe("STOP: no Mianite placement record to roll back")
            confirm_already_rolled_back(base.read_json(rolled_back))
            return
        record = verify_record(item, full_hash=True)
        base.atomic_json(intent, {"record": record,
                                  "started_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    if record.get("inventory_sha256") != digest:
        raise base.ImportErrorSafe("STOP: rollback inventory hash changed")
    for entry in reversed(record["files"]):
        if not entry.get("created"):
            continue
        path = Path(entry["path"])
        if not owned_record_file(path):
            raise base.ImportErrorSafe("STOP: unsafe rollback path")
        if path.exists():
            if path.stat().st_size != entry["size"] or base.sha256(path) != entry["sha256"]:
                raise base.ImportErrorSafe(f"STOP: changed rollback file {path}")
            path.unlink()
    base.atomic_json(rolled_back, record)
    record_file.unlink()
    intent.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "plan", "run", "verify", "rollback"))
    parser.add_argument("--inventory", type=Path, default=INVENTORY_DEFAULT)
    parser.add_argument("--cookies", type=Path, help="Existing YouTube-only cookie file; values never printed")
    parser.add_argument("--yt-dlp", default="/usr/bin/yt-dlp")
    parser.add_argument("--ffprobe", default="/usr/bin/ffprobe")
    parser.add_argument("--limit", type=int, help="Maximum new placements in this run")
    parser.add_argument("--full-hash", action="store_true")
    parser.add_argument("--position", type=int)
    parser.add_argument("--confirm-rollback", action="store_true")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    configure_base()
    inventory = load_inventory(args.inventory)
    digest = inventory_digest(args.inventory)
    if args.action == "plan":
        for item in inventory["entries"]:
            part = f" part {item['source_part']}" if item["source_part"] else ""
            print(f"S03E{item['position']:03d} <- S2E{item['source_episode']}{part}: {item['id']}")
        print("25 pinned full streams; source S2E105 is absent; S2E97 has two parts")
        return 0
    if args.action == "check":
        if sys.platform.startswith("linux"):
            check_global_youtube_hold()
            verify_show_root()
            verify_season_ownership(digest)
            check_cookie_mode(args.cookies)
            check_services_paused()
            check_jjt_handoff()
            for binary in (args.yt_dlp, args.ffprobe, "/home/ap3x/.deno/bin/deno"):
                if not Path(binary).is_file() or not os.access(binary, os.X_OK):
                    raise base.ImportErrorSafe(f"STOP: executable missing: {binary}")
            base.gate(wait=False)
            with base.locked(CHANNEL_LOCK), base.locked(PLAYLIST_LOCK), base.locked(RUN_LOCK):
                pass
        print("CONFIG PASS: 25 pinned streams -> Mianite Season 03; no media changed")
        return 0
    if args.action == "verify":
        count = 0
        for item in inventory["entries"]:
            if record_path(item).is_file():
                verify_record(item, full_hash=args.full_hash)
                count += 1
        print(f"VERIFIED: {count}/25 Mianite stream placement records")
        return 0
    if args.action == "rollback":
        if not args.confirm_rollback or args.position not in range(1, 26):
            parser.error("rollback requires --position 1..25 and --confirm-rollback")
        check_services_paused()
        with base.locked(CHANNEL_LOCK), base.locked(PLAYLIST_LOCK), base.locked(RUN_LOCK):
            with base.music_lock_wait():
                item = inventory["entries"][args.position - 1]
                rollback_one(item, digest=digest)
                write_status(inventory["entries"], limited=True)
        print(f"ROLLED BACK: Season 03 position {args.position}; source media preserved")
        return 0
    return run(args, inventory, digest)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (base.ImportErrorSafe, OSError, subprocess.TimeoutExpired, ValueError, ET.ParseError) as error:
        base.progress_stopped(error)
        print(error, file=sys.stderr, flush=True)
        raise SystemExit(2)
