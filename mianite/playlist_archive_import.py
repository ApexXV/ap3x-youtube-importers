#!/usr/bin/env python3
"""Serial, resumable playlist importer for Plex's YouTube Series TV library.

``check`` and ``plan`` are offline. ``run`` is deliberately gated on the
six-channel import's terminal status and one successful full-video format
probe. The pinned inventory is the authority for ordering; a changed live
playlist stops the run for review before any media is downloaded.

Run only on rackmain1. No service or timer is installed by this file.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

try:
    import fcntl
except ImportError:  # Offline planning/tests also run on Windows.
    fcntl = None


MEDIA_ROOT = Path("/home/md-media/media")
TV_ROOT = MEDIA_ROOT / "Youtube-Series"
OLD_ROOT = MEDIA_ROOT / "Youtube"
STAGE_ROOT = MEDIA_ROOT / ".youtube-series-playlist-stage"
STATE_ROOT = Path("/home/ap3x/.local/share/youtube-series-playlist-import")
CHANNEL_STATE = Path("/home/ap3x/.local/share/youtube-series-channel-import")
MUSIC_LOCK = MEDIA_ROOT / "Music/.ap3x-lofi-stage/import.lock"
MUSIC_SERVICE = "artist-archive-import.service"
LOCAL_ZONE = ZoneInfo("America/New_York")
RESERVE_BYTES = 1024 ** 4
MEDIA_EXTENSIONS = {".mkv", ".mp4", ".webm", ".mov", ".m4v"}
ID_RE = re.compile(r"(?:\[|^)([A-Za-z0-9_-]{11})(?:\]|\.)")
BOT_RE = re.compile(r"HTTP Error 429|too many requests|not a bot|captcha|verify you are human|bot verification", re.I)
UNAVAILABLE_RE = re.compile(r"video (?:is )?unavailable|private video|video is private|has been removed|not available in your country", re.I)
TRANSIENT_RE = re.compile(r"HTTP Error 5\d\d|timed out|connection reset|temporarily unavailable|requested format is not available|missing url", re.I)


class ImportErrorSafe(RuntimeError):
    pass

# ---- Wall progress (optional) ---------------------------------------------
# Live status for the Command 57 wall, written through ap3x_progress.py from
# ap3x-wall-data-collectors (installed to ~/.local/lib/ap3x). It records only a
# phase word and numbers. Every call is guarded: a missing helper, a write
# error, or AP3X_PROGRESS=off leaves the import exactly as it was.
PROGRESS_LIB = Path("/home/ap3x/.local/lib/ap3x")
PROGRESS = None


def start_progress(source: str, state_root: Path, eligible: int | None = None) -> None:
    global PROGRESS
    PROGRESS = None
    if os.environ.get("AP3X_PROGRESS", "").lower() == "off":
        return
    try:
        if str(PROGRESS_LIB) not in sys.path:
            sys.path.append(str(PROGRESS_LIB))
        from ap3x_progress import ProgressEvent
        PROGRESS = ProgressEvent(state_root, source=source, eligible=eligible)
    except Exception:
        PROGRESS = None


def progress(method: str, *args, **kwargs) -> None:
    if PROGRESS is None:
        return
    try:
        getattr(PROGRESS, method)(*args, **kwargs)
    except Exception:
        pass


def progress_stopped(error: BaseException) -> None:
    """Classify a stop for the wall (bot_or_429, unavailable, ...) without its text."""
    message = str(error)
    kind = classify_error(message)
    progress("fail", kind if kind != "unknown" else "low_space" if "free space" in message else "stopped")


# A source playlist becomes a TV show, except the explicitly reviewed splits.
# Distinct shows retain playlist positions; duplicate video IDs are hardlinked.
SHOWS = {
    "PLMBYlcH3smRwIMXxpCXNfmobs-uqwawMD": "Jacksepticeye - Subnautica Below Zero",
    "PLMBYlcH3smRzxHgETx5OhQAvC8i5uzV5m": "Jacksepticeye - Detroit Become Human",
    "PLMBYlcH3smRwM6cyq5mKf9FSHy0_h8F4K": "Jacksepticeye - Subnautica Full Release",
    "PLm29cAuVva6-9CowLx6ybMla-qIF-_BE2": "Sky Does Everything - Room Mates",
    "PLm29cAuVva6_PcRMI4D9rt8z7j3xUk9XV": "Sky Does Everything - Maze Runner",
    "PLm29cAuVva68IeB2ahqt-jtRjHgXxTh-k": "Sky Does Everything - Cops N Robbers",
    "PLMBYlcH3smRzXGvi1NxF4qIXOWh9R_qbw": "Jacksepticeye - Happy Wheels",
    "PLm29cAuVva68dtsmPQI_YzzvHSt28QY7O": "Sky Does Everything - Pirates",
    "PLm29cAuVva694tZb8rFmy-deU9Z6Ra21R": "Sky Does Everything - Do Not Laugh",
    "PLm29cAuVva6-PBXL-rg_yAAnU8uQIPCvH": "Sky Does Everything - Minecraft University",
    "PLm29cAuVva68K8KrPMejgJ3erc1NOMLnT": "Sky Does Everything - Nostalgia Craft",
    "PLAWPBnKCoHO1zNmpPMhCq2c6uOzGA5uQO": "Nobodies Live - Happy Wheels Funny Moments",
    "PLAWPBnKCoHO2pUdRLiKKyQEPTaECbHLWe": "Nobodies Live - GMOD Funny Moments",
    "PLAWPBnKCoHO0A5XsiCumCPyvYYeFJO90j": "Nobodies Live - GTA 5 Mods",
    "PLAWPBnKCoHO323RyONwvTvG3GhYYDK6fz": "Nobodies Live - GTA V Funny Moments",
    "PLAWPBnKCoHO3Z6BhJpuhPX-dw-jfRzIE8": "Nobodies Live - Who's Your Daddy Funny Moments",
}
ROOM_MATES = "PLm29cAuVva6-9CowLx6ybMla-qIF-_BE2"
MAZE_RUNNER = "PLm29cAuVva6_PcRMI4D9rt8z7j3xUk9XV"
PIRATES = "PLm29cAuVva68dtsmPQI_YzzvHSt28QY7O"
UNIVERSITY = "PLm29cAuVva6-PBXL-rg_yAAnU8uQIPCvH"
NOSTALGIA = "PLm29cAuVva68K8KrPMejgJ3erc1NOMLnT"
FRENCH_FRY_ID = "GdTvQlpWPys"  # Flat title: "1620 French Fry Challenge".
HIGH_SCHOOL_IDS = {
    "WEDcZRRK76M": 2, "kUHT-t3-vK0": 3,
    "qeLjsc-oFqs": 5, "eF9_wNYui9Y": 6,
}


def atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".tmp-{os.getpid()}")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def valid_id(value: str | None) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{11}", value or ""))


def safe_component(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", " ", value).strip(" .")
    return re.sub(r"\s+", " ", value)[:140] or "Untitled"


def xml_safe(value: object) -> str:
    return "".join(char for char in str(value)
                   if char in "\t\n\r" or 0x20 <= ord(char) <= 0xD7FF
                   or 0xE000 <= ord(char) <= 0xFFFD
                   or 0x10000 <= ord(char) <= 0x10FFFF)


def xml_write(path: Path, tag: str, fields: dict[str, object], *, uniqueid_type: str = "youtube") -> None:
    root = ET.Element(tag)
    for key, value in fields.items():
        if value is not None and value != "":
            attrs = {"type": uniqueid_type, "default": "true"} if key == "uniqueid" else {}
            ET.SubElement(root, key, attrs).text = xml_safe(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".tmp-{os.getpid()}")
    ET.ElementTree(root).write(temp, encoding="utf-8", xml_declaration=True)
    os.replace(temp, path)


def ensure_named_season(show_nfo: Path, season: int, label: str, backup_root: Path) -> None:
    """Add one Plex-visible season label, preserving the exact previous show NFO."""
    if show_nfo.is_symlink() or not show_nfo.is_file():
        raise ImportErrorSafe(f"Unsafe show NFO: {show_nfo}")
    original = show_nfo.read_bytes()
    root = ET.fromstring(original)
    if root.tag != "tvshow":
        raise ImportErrorSafe(f"Unexpected show NFO root: {show_nfo}")
    matches = [node for node in root.findall("namedseason")
               if node.get("number") == str(season)]
    if len(matches) > 1 or (matches and matches[0].text != label):
        raise ImportErrorSafe(f"Conflicting named season {season}: {show_nfo}")
    if matches:
        return
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = backup_root / f"{safe_component(show_nfo.parent.name)}-S{season:02d}-tvshow.nfo.before"
    if backup.exists():
        if backup.is_symlink() or backup.read_bytes() != original:
            raise ImportErrorSafe(f"Changed show NFO since season backup: {show_nfo}")
    else:
        with backup.open("xb") as stream:
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
    ET.SubElement(root, "namedseason", {"number": str(season)}).text = xml_safe(label)
    with tempfile.NamedTemporaryFile(mode="wb", dir=show_nfo.parent,
                                     prefix="tvshow.nfo.tmp-", delete=False) as stream:
        temp = Path(stream.name)
        try:
            os.chmod(temp, stat.S_IMODE(show_nfo.stat().st_mode))
            ET.ElementTree(root).write(stream, encoding="utf-8", xml_declaration=True)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        os.replace(temp, show_nfo)
    finally:
        temp.unlink(missing_ok=True)


def season_poster_candidate(stage: Path, video_id: str, show_dir: Path,
                            season: int) -> tuple[Path, Path] | None:
    """Use the first real JPEG thumbnail as a reversible season poster link."""
    poster = show_dir / f"Season{season:02d}.jpg"
    if poster.is_symlink():
        raise ImportErrorSafe(f"Unsafe season poster: {poster}")
    if poster.exists():
        return None  # Keep an existing or hand-curated season poster.
    for candidate in (stage / f"{video_id}.jpg", stage / f"{video_id}.jpeg"):
        if candidate.is_file() and not candidate.is_symlink():
            return candidate, poster
    return None


def load_inventory(path: Path) -> list[dict]:
    payload = read_json(path)
    sources = payload.get("playlists")
    if (not isinstance(sources, list) or len(sources) != len(SHOWS)
            or not all(isinstance(item, dict) for item in sources)
            or len({item.get("playlist_id") for item in sources}) != len(SHOWS)
            or set(SHOWS) != {item.get("playlist_id") for item in sources}):
        raise ImportErrorSafe("Inventory must contain the 16 reviewed playlist IDs exactly once")
    for source in sources:
        playlist_id = source["playlist_id"]
        ids = source.get("ordered_video_ids")
        if (source.get("canonical_url") != f"https://www.youtube.com/playlist?list={playlist_id}"
                or not isinstance(ids, list) or len(ids) != source.get("listed_count")
                or not all(valid_id(item) for item in ids)):
            raise ImportErrorSafe(f"Invalid playlist inventory: {playlist_id}")
        if safe_component(SHOWS[playlist_id]) != SHOWS[playlist_id]:
            raise ImportErrorSafe(f"Unsafe show folder: {SHOWS[playlist_id]}")
    if sum(item["listed_count"] for item in sources) != 749:
        raise ImportErrorSafe("Reviewed inventory length changed; update mapping before run")
    return sources


def placement(source: dict, position: int, video_id: str) -> dict:
    """Map one pinned playlist slot to a precise TV episode, retaining gaps."""
    playlist_id = source["playlist_id"]
    show = SHOWS[playlist_id]
    season, episode = 1, position
    if playlist_id == ROOM_MATES:
        season, episode = (1, position) if position <= 10 else (2, position - 10)
    elif playlist_id in {MAZE_RUNNER, PIRATES} and position == 9:
        episode = 10  # Episode 9 is absent from both source lists.
    elif playlist_id == UNIVERSITY and position >= 7:
        if video_id not in HIGH_SCHOOL_IDS:
            raise ImportErrorSafe("Minecraft High School split changed; review inventory")
        show = "Sky Does Everything - Minecraft High School"
        episode = HIGH_SCHOOL_IDS[video_id]
    elif playlist_id == NOSTALGIA and video_id == FRENCH_FRY_ID:
        show, season, episode = "Sky Does Everything - Extras", 0, 1
    return {"playlist_id": playlist_id, "position": position, "id": video_id,
            "show": show, "season": season, "episode": episode}


def planned_placements(sources: list[dict]) -> list[dict]:
    result = [placement(source, pos, video_id)
              for source in sources
              for pos, video_id in enumerate(source["ordered_video_ids"], 1)]
    targets = [(item["show"], item["season"], item["episode"]) for item in result]
    if len(targets) != len(set(targets)):
        raise ImportErrorSafe("Two inventory positions map to the same TV episode")
    return result


def quiet_window(now: dt.datetime | None = None) -> bool:
    current = (now or dt.datetime.now(LOCAL_ZONE)).astimezone(LOCAL_ZONE)
    minute = current.hour * 60 + current.minute
    return 3 * 60 + 30 <= minute < 5 * 60 + 30


def music_active() -> bool:
    result = subprocess.run(["systemctl", "--user", "show", MUSIC_SERVICE,
                             "-p", "ActiveState", "--value"], text=True,
                            capture_output=True, check=False, timeout=10)
    if result.returncode:
        raise ImportErrorSafe("Cannot inspect artist import service state")
    return result.stdout.strip() in {"active", "activating", "reloading", "deactivating"}


def free_bytes() -> int:
    return shutil.disk_usage(MEDIA_ROOT).free


def gate(*, wait: bool) -> None:
    while quiet_window() or music_active():
        if not wait:
            raise ImportErrorSafe("DEFERRED: artist service or protected 03:30–05:30 window")
        print("WAITING: artist service or protected 03:30–05:30 window", flush=True)
        time.sleep(60)
    if free_bytes() < RESERVE_BYTES:
        raise ImportErrorSafe(f"STOP: array free space {free_bytes()/1024**4:.2f} TiB below 1 TiB reserve")


@contextlib.contextmanager
def locked(path: Path):
    if fcntl is None:
        raise ImportErrorSafe("Run only on Linux with fcntl")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ImportErrorSafe(f"DEFERRED: importer lock busy: {path}") from error
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def channel_queue_finished() -> None:
    """A terminal six-source snapshot and a free channel lock are both required."""
    path = CHANNEL_STATE / "run-status.json"
    if not path.is_file():
        raise ImportErrorSafe("DEFERRED: six-channel queue has no terminal status")
    status = read_json(path)
    if (status.get("limited") or status.get("terminal_rescan_verified") is not True
            or status.get("unresolved") != 0
            or len(status.get("sources") or {}) != 6
            or status.get("status") not in {"complete", "complete_with_gaps", "partial"}
            or int(status.get("present", -1)) + int(status.get("unavailable", -1))
            + int(status.get("shorts_excluded", 0))
            != int(status.get("catalog_total", -2))):
        raise ImportErrorSafe("DEFERRED: six-channel queue has not finished its reviewed catalog")
    with locked(CHANNEL_STATE / "channel-import.lock"):
        pass


@contextlib.contextmanager
def music_lock_wait():
    if fcntl is None:
        raise ImportErrorSafe("Run only on Linux with fcntl")
    MUSIC_LOCK.parent.mkdir(parents=True, exist_ok=True)
    while True:
        gate(wait=True)
        with MUSIC_LOCK.open("a+") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("WAITING: shared music importer lock", flush=True)
            else:
                try:
                    yield
                finally:
                    fcntl.flock(stream, fcntl.LOCK_UN)
                return
        time.sleep(60)


def command_base(args: argparse.Namespace) -> list[str]:
    command = [args.yt_dlp, "--ignore-config", "--no-update",
               "--js-runtimes", "deno:/home/ap3x/.deno/bin/deno",
               "--sleep-requests", "1", "--retries", "0",
               "--fragment-retries", "0", "--extractor-retries", "0"]
    if args.cookies:
        command += ["--cookies", str(args.cookies)]
    return command


def classify_error(message: str) -> str:
    if BOT_RE.search(message):
        return "bot_or_429"
    if UNAVAILABLE_RE.search(message):
        return "unavailable"
    if TRANSIENT_RE.search(message):
        return "transient"
    return "unknown"


def terminate_process(process: subprocess.Popen) -> None:
    for sig, timeout in ((signal.SIGINT, 30), (signal.SIGTERM, 10), (signal.SIGKILL, 10)):
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            pass
    raise ImportErrorSafe(f"STOP: child process {process.pid} would not terminate")


def run_json_command(command: list[str], *, timeout_seconds: int) -> tuple[int, str, str]:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="extract-", dir=STATE_ROOT) as folder:
        output_path, error_path = Path(folder) / "output.json", Path(folder) / "error.log"
        with output_path.open("wb") as output, error_path.open("wb") as error:
            process = subprocess.Popen(command, stdout=output, stderr=error, start_new_session=True)
            started = time.monotonic()
            reason = "exit"
            try:
                while process.poll() is None:
                    time.sleep(5)
                    if time.monotonic() - started > timeout_seconds:
                        reason = "timeout"
                    elif quiet_window() or music_active():
                        reason = "music_window"
                    if reason != "exit":
                        terminate_process(process)
                        break
            except BaseException:
                terminate_process(process)
                raise
            code = process.wait()
        if reason == "music_window":
            raise ImportErrorSafe("DEFERRED: extraction paused for music window")
        if reason == "timeout":
            raise ImportErrorSafe("STOP: YouTube extraction timed out")
        return code, output_path.read_text(encoding="utf-8", errors="replace"), \
            error_path.read_text(encoding="utf-8", errors="replace")


def reconcile_scan(expected: list[str], observed: list[str], private: set[str]) -> tuple[list[str], list[str]]:
    """Keep pinned order while tolerating added entries and hidden private IDs.

    New entries are journaled for a later reviewed inventory revision. A
    reordered or removed titled baseline entry is unsafe for episode mapping.
    """
    @lru_cache(maxsize=None)
    def align(index: int, observed_index: int) -> tuple[int, tuple[str, ...], tuple[str, ...]] | None:
        if index == len(expected):
            remainder = tuple(observed[observed_index:])
            return len(remainder), remainder, ()
        if observed_index == len(observed):
            remainder = tuple(expected[index:])
            return (len(remainder), (), remainder) if all(item in private for item in remainder) else None
        candidates = []
        if expected[index] == observed[observed_index]:
            matched = align(index + 1, observed_index + 1)
            if matched is not None:
                candidates.append(matched)
        added = align(index, observed_index + 1)
        if added is not None:
            candidates.append((added[0] + 1, (observed[observed_index],) + added[1], added[2]))
        if expected[index] in private:
            missing = align(index + 1, observed_index)
            if missing is not None:
                candidates.append((missing[0] + 1, missing[1], (expected[index],) + missing[2]))
        if not candidates:
            return None
        return min(candidates, key=lambda candidate: (candidate[0], len(candidate[2])))

    match = align(0, 0)
    if match is None:
        raise ImportErrorSafe("Pinned playlist entry removed or reordered")
    return list(match[1]), list(match[2])


def scan_playlist(source: dict, args: argparse.Namespace) -> dict:
    gate(wait=False)
    command = command_base(args) + ["--flat-playlist", "--dump-single-json", source["canonical_url"]]
    code, output, error = run_json_command(command, timeout_seconds=1800)
    if code:
        raise ImportErrorSafe(f"STOP: playlist scan {source['playlist_id']} {classify_error(error)}: {error[-500:]}")
    response = json.loads(output)
    got = response.get("id")
    if got and got != source["playlist_id"]:
        raise ImportErrorSafe(f"STOP: playlist identity changed: {source['playlist_id']} -> {got}")
    ids = [entry.get("id") for entry in response.get("entries") or [] if isinstance(entry, dict)]
    if not all(valid_id(item) for item in ids):
        raise ImportErrorSafe(f"STOP: playlist {source['playlist_id']} scan returned invalid IDs")
    try:
        additions, hidden = reconcile_scan(source["ordered_video_ids"], ids,
                                           set(source.get("untitled_or_unavailable_ids") or []))
    except ImportErrorSafe:
        atomic_json(STATE_ROOT / "scan-drift" / f"{source['playlist_id']}.json",
                    {"expected_ids": source["ordered_video_ids"], "observed_ids": ids,
                     "checked_at": dt.datetime.now(dt.timezone.utc).isoformat()})
        raise ImportErrorSafe(f"STOP: playlist {source['playlist_id']} changed; review scan-drift before downloading")
    result = {"ids": ids, "new_unpinned_ids": additions, "hidden_private_candidates": hidden,
              "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "title": response.get("title")}
    atomic_json(STATE_ROOT / "scans" / f"{source['playlist_id']}.json", result)
    if additions:
        print(f"FOLLOW_UP {source['playlist_id']}: {len(additions)} new positions journaled", flush=True)
    return result


def probe_access(video_id: str, args: argparse.Namespace) -> None:
    gate(wait=False)
    command = command_base(args) + ["--no-playlist", "--skip-download", "--dump-single-json",
                                    f"https://www.youtube.com/watch?v={video_id}"]
    code, output, error = run_json_command(command, timeout_seconds=300)
    if code:
        kind = classify_error(error)
        atomic_json(STATE_ROOT / "probe.json", {"id": video_id, "status": kind,
                    "checked_at": dt.datetime.now(dt.timezone.utc).isoformat()})
        if kind == "unavailable":
            raise ImportErrorSafe(f"PROBE_SKIP: {video_id} unavailable")
        raise ImportErrorSafe(f"STOP: full-video format probe {kind}; no playlist media started")
    info = json.loads(output)
    if info.get("id") != video_id:
        raise ImportErrorSafe("STOP: full-video probe returned wrong ID")
    if is_short(info):
        atomic_json(STATE_ROOT / "probe.json", {"id": video_id, "status": "short"})
        atomic_json(STATE_ROOT / "shorts" / f"{video_id}.json",
                    {"id": video_id, "title": info.get("title"),
                     "checked_at": dt.datetime.now(dt.timezone.utc).isoformat()})
        raise ImportErrorSafe(f"PROBE_SKIP: {video_id} is a Short")
    if info.get("live_status") in {"is_live", "is_upcoming"}:
        atomic_json(STATE_ROOT / "probe.json", {"id": video_id, "status": "live"})
        raise ImportErrorSafe(f"PROBE_SKIP: {video_id} is live or upcoming")
    if not any(item.get("url") for item in info.get("formats") or []):
        raise ImportErrorSafe("STOP: full-video probe returned wrong ID or no playable formats")
    atomic_json(STATE_ROOT / "probe.json", {"id": video_id, "status": "ok",
                "format_count": len(info.get("formats") or []),
                "checked_at": dt.datetime.now(dt.timezone.utc).isoformat()})


def media_ids(root: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    if not root.exists():
        return found
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink() or path.suffix.lower() not in MEDIA_EXTENSIONS:
            continue
        match = ID_RE.search(path.name)
        if match:
            found.setdefault(match.group(1), path)
    return found


def staged_media(stage: Path, video_id: str) -> Path | None:
    files = [path for path in stage.glob(video_id + ".*")
             if (path.is_file() and not path.is_symlink()
                 and path.stem == video_id and path.suffix.lower() in MEDIA_EXTENSIONS)]
    if len(files) > 1:
        raise ImportErrorSafe(f"Multiple staged media files for {video_id}")
    return files[0] if files else None


def quarantine_stage_file(path: Path, reason: str) -> Path:
    """Preserve an uncertain stage file without hiding any download fragments."""
    quarantine = path.parent / ".ambiguous-media-backup"
    if quarantine.is_symlink() or (quarantine.exists() and not quarantine.is_dir()):
        raise ImportErrorSafe(f"Unsafe stage backup directory: {quarantine}")
    quarantine.mkdir(mode=0o700, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = quarantine / f"{stamp}-{os.getpid()}-{path.name}"
    if path.is_symlink() or not path.is_file():
        raise ImportErrorSafe(f"Stage file changed before backup: {path}")
    os.link(path, backup)  # Fails rather than replacing an earlier backup.
    size = path.stat().st_size
    path.unlink()
    atomic_json(backup.with_name(backup.name + ".json"), {
        "original": str(path), "backup": str(backup), "size": size,
        "reason": reason, "created_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    print(f"BACKUP {reason}: {path} -> {backup}", flush=True)
    return backup


def verify_media(path: Path, ffprobe: str) -> dict:
    if not path.is_file() or path.is_symlink() or path.stat().st_size < 1024:
        raise ImportErrorSafe(f"Missing or empty media: {path}")
    result = subprocess.run([ffprobe, "-v", "error", "-show_entries",
                             "format=duration:stream=codec_type,width,height", "-of", "json", str(path)],
                            text=True, capture_output=True, check=False, timeout=120)
    if result.returncode:
        raise ImportErrorSafe(f"ffprobe failed: {path}: {result.stderr[-300:]}")
    data = json.loads(result.stdout)
    if (not any(item.get("codec_type") == "video" for item in data.get("streams", []))
            or float(data.get("format", {}).get("duration") or 0) <= 0):
        raise ImportErrorSafe(f"No playable video or duration: {path}")
    return data


def tail(path: Path, chars: int = 12000) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        stream.seek(max(0, stream.tell() - chars))
        return stream.read().decode("utf-8", errors="replace")


def monitor_command(command: list[str], log: Path) -> tuple[int, str]:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as stream:
        process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        reason = "exit"
        try:
            while process.poll() is None:
                time.sleep(10)
                progress("ytdlp_log", tail(log, 4000))
                if free_bytes() < RESERVE_BYTES:
                    reason = "low_space"
                elif quiet_window() or music_active():
                    reason = "music_window"
                if reason != "exit":
                    terminate_process(process)
                    break
        except BaseException:
            terminate_process(process)
            raise
        return process.wait(), reason


def durable_info(info: dict) -> dict:
    """Never publish signed stream URLs or cookies into Plex sidecars."""
    keys = ("id", "title", "description", "upload_date", "release_date", "timestamp",
            "channel", "channel_id", "uploader", "uploader_id", "duration", "webpage_url",
            "original_url", "is_short", "live_status", "availability", "categories", "tags", "chapters")
    result = {key: info[key] for key in keys if info.get(key) is not None}
    result["detected_short"] = is_short(info)
    if isinstance(info.get("short_preflight"), dict):
        result["short_preflight"] = info["short_preflight"]
    return result


def is_short(info: dict) -> bool:
    if info.get("is_short") is True or info.get("detected_short") is True:
        return True
    if any("/shorts/" in str(info.get(key) or "") for key in ("webpage_url", "original_url")):
        return True
    duration = info.get("duration")
    title = str(info.get("title") or "")
    description = str(info.get("description") or "")
    tags = [str(item).casefold() for item in info.get("tags") or []]
    try:
        seconds = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        seconds = None
    dimensions = [(info.get("width"), info.get("height"))]
    dimensions.extend((item.get("width"), item.get("height"))
                      for item in info.get("formats") or [] if isinstance(item, dict))
    portrait = any(isinstance(width, (int, float)) and isinstance(height, (int, float))
                   and width > 0 and height >= width * 1.2 for width, height in dimensions)
    return bool(seconds is not None and seconds <= 180
                and (re.search(r"(?:^|\s)#shorts?\b", title + "\n" + description, re.I)
                     or "shorts" in tags or portrait))


def has_nonshort_preflight(info: dict, video_id: str) -> bool:
    proof = info.get("short_preflight")
    return (isinstance(proof, dict) and proof.get("version") == 1
            and proof.get("result") == "non_short" and proof.get("video_id") == video_id)


def stage_from_existing(stage: Path, video_id: str, original: Path) -> None:
    if not original.is_file() or original.is_symlink():
        raise ImportErrorSafe(f"Invalid reusable media: {original}")
    media = stage / (video_id + original.suffix.lower())
    if not media.exists():
        os.link(original, media)
    if media.stat().st_ino != original.stat().st_ino or media.stat().st_dev != original.stat().st_dev:
        raise ImportErrorSafe(f"Reusable media is not a hardlink: {video_id}")
    for sidecar in original.parent.glob(original.stem + ".*"):
        if sidecar == original or not sidecar.is_file() or sidecar.is_symlink():
            continue
        suffix = sidecar.name[len(original.stem):]
        if suffix == ".source.json" or suffix == ".description" or suffix == ".jpg" or \
                sidecar.suffix.lower() in {".srt", ".vtt"}:
            target = stage / (video_id + suffix)
            if not target.exists():
                os.link(sidecar, target)


def extract_metadata(video_id: str, args: argparse.Namespace) -> dict:
    """Full format probe for this ID, with no video download.

    It also gives us a chance to exclude Shorts before fetching media.
    The response's signed format URLs are never written to persistent state.
    """
    command = command_base(args) + ["--no-playlist", "--skip-download", "--dump-single-json",
                                    f"https://www.youtube.com/watch?v={video_id}"]
    for attempt, delay in enumerate((0, 60, 120, 240), 1):
        if delay:
            time.sleep(delay)
        gate(wait=False)
        code, output, error = run_json_command(command, timeout_seconds=300)
        if code == 0:
            info = json.loads(output)
            if info.get("id") != video_id:
                raise ImportErrorSafe(f"STOP: metadata ID mismatch for {video_id}")
            return info
        kind = classify_error(error)
        if kind == "unavailable":
            atomic_json(STATE_ROOT / "unavailable" / f"{video_id}.json",
                        {"id": video_id, "reason": error[-1000:],
                         "checked_at": dt.datetime.now(dt.timezone.utc).isoformat()})
            raise ImportErrorSafe(f"UNAVAILABLE: {video_id}")
        if kind != "transient" or attempt == 4:
            raise ImportErrorSafe(f"STOP: metadata extraction {video_id} {kind}: {error[-500:]}")
    raise AssertionError("unreachable")


def fetch_video(video_id: str, existing: Path | None, args: argparse.Namespace) -> tuple[Path, dict]:
    stage = STAGE_ROOT / video_id
    stage.mkdir(parents=True, exist_ok=True)
    if existing is not None:
        stage_from_existing(stage, video_id, existing)
    media = staged_media(stage, video_id)
    source_file = stage / f"{video_id}.source.json"
    info_file = stage / f"{video_id}.info.json"
    cached = read_json(source_file) if source_file.is_file() else None
    if cached is not None and has_nonshort_preflight(cached, video_id):
        info = cached
    else:
        # Legacy/channel sidecars may lack portrait dimensions and cannot
        # certify that this video is not a Short. Probe them afresh first.
        progress("phase", "probing")
        info = extract_metadata(video_id, args)
        if is_short(info):
            atomic_json(STATE_ROOT / "shorts" / f"{video_id}.json",
                        {"id": video_id, "title": info.get("title"),
                         "checked_at": dt.datetime.now(dt.timezone.utc).isoformat()})
            raise ImportErrorSafe(f"SHORT: {video_id}")
        info["short_preflight"] = {"version": 1, "result": "non_short", "video_id": video_id,
                                   "checked_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        atomic_json(source_file, durable_info(info))
    if info.get("id") != video_id:
        raise ImportErrorSafe(f"STOP: metadata ID mismatch for {video_id}")
    if is_short(info):
        atomic_json(STATE_ROOT / "shorts" / f"{video_id}.json",
                    {"id": video_id, "title": info.get("title"),
                     "checked_at": dt.datetime.now(dt.timezone.utc).isoformat()})
        raise ImportErrorSafe(f"SHORT: {video_id}")
    if info.get("live_status") in {"is_live", "is_upcoming"}:
        raise ImportErrorSafe(f"LIVE: {video_id}")
    # Existing hardlinks already supply the media. Only fetch missing sidecars.
    has_poster = any(path.is_file() and path.suffix.lower() in {".jpg", ".webp", ".jpeg"}
                     for path in stage.glob(video_id + ".*"))
    sidecars_done = stage / (video_id + ".sidecars-complete.json")
    trusted_existing = bool(existing is not None and
                            existing.with_name(existing.stem + ".source.json").is_file() and has_poster)
    if media is None and sidecars_done.is_file():
        quarantine_stage_file(sidecars_done, "completion_marker_without_media")
    if media is not None and existing is None and not sidecars_done.is_file():
        quarantine_stage_file(media, "exact_media_without_completion_marker")
        media = None
    if media is None or not (sidecars_done.is_file() or trusted_existing):
        command = command_base(args) + ["--no-playlist", "--write-info-json",
                   "--write-description", "--write-thumbnail", "--convert-thumbnails", "jpg",
                   "--write-subs", "--write-auto-subs", "--sub-langs", "en.*,en",
                   "--convert-subs", "srt", "--sleep-interval", "2", "--max-sleep-interval", "5",
                   "--limit-rate", "8M", "--concurrent-fragments", "1",
                   "--match-filter", "live_status != is_live & live_status != is_upcoming",
                   "--no-overwrites", "-o", str(stage / (video_id + ".%(ext)s"))]
        if media is not None:
            command.append("--skip-download")
        else:
            command += ["-f", "bv*+ba/b", "--merge-output-format", "mkv"]
        command.append(f"https://www.youtube.com/watch?v={video_id}")
        progress("phase", "downloading")
        for attempt, delay in enumerate((0, 60, 120, 240), 1):
            if delay:
                time.sleep(delay)
            gate(wait=False)
            log = stage / f"yt-dlp-attempt-{int(time.time())}-{attempt}.log"
            try:
                code, reason = monitor_command(command, log)
            finally:
                # yt-dlp's raw info JSON can include expiring signed URLs.
                info_file.unlink(missing_ok=True)
            if reason == "music_window":
                raise ImportErrorSafe(f"DEFERRED: {video_id} stage retained for music window")
            if reason == "low_space":
                raise ImportErrorSafe(f"STOP: free space below 1 TiB while fetching {video_id}")
            if code == 0:
                completed_media = staged_media(stage, video_id)
                if completed_media is None:
                    raise ImportErrorSafe(f"STOP: yt-dlp completed without final media for {video_id}")
                verify_media(completed_media, args.ffprobe)
                atomic_json(sidecars_done, {"id": video_id,
                            "completed_at": dt.datetime.now(dt.timezone.utc).isoformat()})
                break
            kind = classify_error(tail(log))
            if kind == "unavailable":
                atomic_json(STATE_ROOT / "unavailable" / f"{video_id}.json",
                            {"id": video_id, "reason": tail(log, 1000),
                             "checked_at": dt.datetime.now(dt.timezone.utc).isoformat()})
                raise ImportErrorSafe(f"UNAVAILABLE: {video_id}")
            if kind != "transient" or attempt == 4:
                raise ImportErrorSafe(f"STOP: {video_id} yt-dlp {kind}, exit={code}; see {log}")
    media = staged_media(stage, video_id)
    if media is None:
        raise ImportErrorSafe(f"STOP: missing media after fetch: {video_id}")
    progress("phase", "verifying")
    verify_media(media, args.ffprobe)
    return media, info


def video_date(info: dict) -> str | None:
    raw = info.get("upload_date") or info.get("release_date")
    if raw and re.fullmatch(r"\d{8}", str(raw)):
        try:
            return dt.datetime.strptime(str(raw), "%Y%m%d").date().isoformat()
        except ValueError:
            pass
    if info.get("timestamp"):
        return dt.datetime.fromtimestamp(int(info["timestamp"]), dt.timezone.utc).date().isoformat()
    return None


def season_label(item: dict) -> str:
    if item["season"] == 0:
        return "Extras"
    series = item["show"].split(" - ", 1)[-1]
    if item["playlist_id"] == ROOM_MATES:
        return f"{series} — Season {item['season']}"
    return series


def show_nfos(item: dict, source: dict) -> None:
    show_dir = TV_ROOT / item["show"]
    marker = show_dir / ".playlist-import.json"
    if show_dir.exists():
        if show_dir.is_symlink() or not show_dir.is_dir():
            raise ImportErrorSafe(f"Unexpected existing show folder: {show_dir}")
        if not marker.is_file():
            # A crash after mkdir but before writing the ownership marker can
            # leave an empty directory; it is safe to claim only that case.
            if any(show_dir.iterdir()):
                raise ImportErrorSafe(f"Unexpected existing show folder: {show_dir}")
            atomic_json(marker, {"show": item["show"], "playlist_ids": [item["playlist_id"]]})
        elif item["playlist_id"] not in read_json(marker).get("playlist_ids", []):
            raise ImportErrorSafe(f"Show folder belongs to another playlist: {show_dir}")
    else:
        show_dir.mkdir(parents=True)
        atomic_json(marker, {"show": item["show"], "playlist_ids": [item["playlist_id"]]})
    show_nfo = show_dir / "tvshow.nfo"
    if show_nfo.exists() and ET.parse(show_nfo).getroot().findtext("title") != item["show"]:
        raise ImportErrorSafe(f"Existing show NFO title mismatch: {show_nfo}")
    if not show_nfo.exists():
        show_key = re.sub(r"[^a-z0-9]+", "-", item["show"].lower()).strip("-")
        xml_write(show_dir / "tvshow.nfo", "tvshow", {
            "title": item["show"], "plot": f"Archival playlist: {source['canonical_url']}",
            "studio": source.get("owner") or "YouTube",
            "uniqueid": f"{item['playlist_id']}:{show_key}"},
            uniqueid_type="youtube-playlist-show")
    label = season_label(item)
    ensure_named_season(show_nfo, item["season"], label,
                        STATE_ROOT / "metadata-backups")
    season_dir = show_dir / f"Season {item['season']:02d}"
    season_dir.mkdir(exist_ok=True)
    if not (season_dir / "season.nfo").exists():
        xml_write(season_dir / "season.nfo", "season", {
            "title": label, "seasonnumber": item["season"]})


def safe_target(item: dict, media: Path) -> Path:
    show_dir = TV_ROOT / item["show"]
    season_dir = show_dir / f"Season {item['season']:02d}"
    stem = f"{safe_component(item['show'])} - S{item['season']:02d}E{item['episode']:03d} [{item['id']}]"
    target = season_dir / (stem + media.suffix.lower())
    if (safe_component(item["show"]) != item["show"] or
            any(path.is_symlink() for path in (TV_ROOT, show_dir, season_dir)) or
            not target.resolve(strict=False).is_relative_to(TV_ROOT.resolve())):
        raise ImportErrorSafe(f"Unsafe playlist target: {target}")
    return target


def target_files(stage: Path, media: Path, target: Path) -> list[tuple[Path, Path]]:
    video_id = media.stem
    files = []
    posters = sorted((path for path in stage.glob(video_id + ".*")
                      if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".webp"}),
                     key=lambda path: (path.suffix.lower() != ".jpg", path.name))
    if posters:
        files.append((posters[0], target.with_suffix(".jpg")))
    for path in stage.glob(video_id + ".*"):
        if not path.is_file() or path.is_symlink() or path == media:
            continue
        if path.suffix.lower() in {".srt", ".vtt"}:
            files.append((path, target.with_name(target.stem + path.name[len(video_id):])))
    for suffix in (".nfo", ".source.json", ".description"):
        path = stage / (video_id + suffix)
        if path.is_file():
            files.append((path, target.with_name(target.stem + suffix)))
    files.append((media, target))  # A scanner sees the episode last.
    return files


def record_path(item: dict) -> Path:
    return STATE_ROOT / "records" / item["playlist_id"] / f"{item['position']:04d}-{item['id']}.json"


def pending_path(item: dict) -> Path:
    return STATE_ROOT / "pending" / item["playlist_id"] / f"{item['position']:04d}-{item['id']}.json"


def verify_record_files(record: dict, *, full_hash: bool = False) -> None:
    files = record.get("files") or []
    if not files:
        raise ImportErrorSafe("Empty playlist import record")
    for entry in files:
        path = Path(entry["path"])
        if (not path.is_file() or path.is_symlink()
                or path.stat().st_size != entry["size"]
                or not path.resolve(strict=False).is_relative_to(TV_ROOT.resolve())):
            raise ImportErrorSafe(f"Recorded file is missing or changed: {path}")
        if full_hash and sha256(path) != entry["sha256"]:
            raise ImportErrorSafe(f"Recorded file checksum changed: {path}")


def promote(item: dict, source: dict, media: Path, info: dict,
            args: argparse.Namespace) -> dict:
    progress("phase", "publishing")
    stage = media.parent
    expected = safe_target(item, media)
    pending_file = pending_path(item)
    if pending_file.exists():
        pending = read_json(pending_file)
        if (pending.get("target") != str(expected) or pending.get("id") != item["id"]
                or pending.get("playlist_id") != item["playlist_id"]):
            raise ImportErrorSafe(f"Unsafe or changed pending target: {pending_file}")
    else:
        pending = {**item, "target": str(expected),
                   "file_intents": {},
                   "created_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        atomic_json(pending_file, pending)
    target = expected
    xml_write(stage / (item["id"] + ".nfo"), "episodedetails", {
        "title": info.get("title") or item["id"], "season": item["season"],
        "episode": item["episode"], "aired": video_date(info),
        "plot": info.get("description") or "", "studio": info.get("channel") or source.get("owner"),
        "uniqueid": f"{item['playlist_id']}:{item['position']}", "youtubeid": item["id"],
    }, uniqueid_type="youtube-playlist-position")
    atomic_json(stage / (item["id"] + ".source.json"), durable_info(info))
    show_nfos(item, source)
    files = []
    targets = target_files(stage, media, target)
    poster = season_poster_candidate(stage, item["id"], TV_ROOT / item["show"], item["season"])
    if poster:
        targets.insert(-1, poster)  # Media remains the final scanner-visible link.
    for original, destination in targets:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink():
            raise ImportErrorSafe(f"Target is a symlink: {destination}")
        checksum = sha256(original)
        intents = pending.setdefault("file_intents", {})
        if str(destination) not in intents:
            intents[str(destination)] = {"sha256": checksum, "size": original.stat().st_size,
                                         "preexisting": destination.exists()}
            atomic_json(pending_file, pending)  # Intent precedes hardlink.
        elif (intents[str(destination)]["sha256"] != checksum or
              intents[str(destination)]["size"] != original.stat().st_size):
            raise ImportErrorSafe(f"Changed pending file content: {destination}")
        if destination.exists():
            if destination.stat().st_size != original.stat().st_size or sha256(destination) != checksum:
                raise ImportErrorSafe(f"Target collision: {destination}")
        else:
            os.link(original, destination)
        files.append({"path": str(destination), "size": original.stat().st_size,
                      "sha256": checksum, "created": not intents[str(destination)]["preexisting"]})
    verify_media(target, args.ffprobe)
    record = {**pending, "status": "complete", "title": info.get("title"),
              "aired": video_date(info), "files": files,
              "source_media": str(media),
              "finished_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    atomic_json(record_path(item), record)
    pending_file.unlink(missing_ok=True)
    return record


def write_status(items: list[dict], *, limited: bool) -> dict:
    records, unavailable, shorts = 0, 0, 0
    for item in items:
        if record_path(item).is_file():
            records += 1
        elif (STATE_ROOT / "unavailable" / f"{item['id']}.json").is_file():
            unavailable += 1
        elif (STATE_ROOT / "shorts" / f"{item['id']}.json").is_file():
            shorts += 1
    unresolved = len(items) - records - unavailable - shorts
    new_unpinned = sum(len(read_json(path).get("new_unpinned_ids") or [])
                       for path in (STATE_ROOT / "scans").glob("*.json")) if (STATE_ROOT / "scans").exists() else 0
    status = {"status": ("complete" if not limited and unresolved == 0 and unavailable == 0 and new_unpinned == 0
                         else "complete_with_gaps" if not limited and unresolved == 0 and new_unpinned == 0
                         else "baseline_complete_new_entries" if not limited and unresolved == 0
                         else "partial"),
              "placements": len(items), "present": records, "unavailable": unavailable,
              "shorts_excluded": shorts, "new_unpinned_entries": new_unpinned,
              "unresolved": unresolved, "limited": limited,
              "unique_ids": len({item['id'] for item in items}),
              "checked_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    atomic_json(STATE_ROOT / "run-status.json", status)
    return status


def rollback_one(item: dict) -> None:
    """Remove only this importer's exact, checksum-matching linked episode files.

    Original Youtube files, other playlist hardlinks, and show-level metadata
    are left intact. The intent file makes an interrupted rollback resumable.
    """
    record_file = record_path(item)
    if not record_file.is_file():
        raise ImportErrorSafe(f"No completed record for playlist position {item['position']}")
    record = read_json(record_file)
    if any(record.get(key) != item[key] for key in ("playlist_id", "position", "id", "show", "season", "episode")):
        raise ImportErrorSafe("Rollback record does not match reviewed mapping")
    intent = STATE_ROOT / "rollback-intent" / item["playlist_id"] / record_file.name
    if not intent.exists():
        verify_record_files(record, full_hash=True)
        atomic_json(intent, {"record": record,
                             "started_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    elif read_json(intent).get("record") != record:
        raise ImportErrorSafe("Rollback intent and record mismatch")
    for entry in reversed(record["files"]):
        if not entry.get("created"):
            continue
        path = Path(entry["path"])
        if not path.resolve(strict=False).is_relative_to((TV_ROOT / item["show"]).resolve()) or path.is_symlink():
            raise ImportErrorSafe(f"Unsafe rollback path: {path}")
        if path.exists():
            if path.stat().st_size != entry["size"] or sha256(path) != entry["sha256"]:
                raise ImportErrorSafe(f"Changed rollback file: {path}")
            path.unlink()
    archived = STATE_ROOT / "rolled-back" / item["playlist_id"] / record_file.name
    atomic_json(archived, record)
    record_file.unlink()
    intent.unlink()



def run(args: argparse.Namespace, sources: list[dict]) -> int:
    if not TV_ROOT.is_dir() or not OLD_ROOT.is_dir():
        raise ImportErrorSafe("Expected Plex YouTube libraries are missing")
    channel_queue_finished()
    STAGE_ROOT.mkdir(parents=True, exist_ok=True)
    (STAGE_ROOT / ".plexignore").write_text("*\n", encoding="utf-8")
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    items = planned_placements(sources)
    with locked(STATE_ROOT / "playlist-import.lock"):
        start_progress("playlists", STATE_ROOT, eligible=len(items))
        progress("phase", "waiting")
        gate(wait=True)
        progress("phase", "scanning")
        # Source scans and a format probe happen before the first media fetch.
        for source in sources:
            while True:
                try:
                    with music_lock_wait():
                        scan_playlist(source, args)
                    break
                except ImportErrorSafe as error:
                    if not str(error).startswith("DEFERRED:"):
                        raise
                    print(error, flush=True)
                    time.sleep(60)
        private_candidates = {vid for source in sources
                              for vid in source.get("untitled_or_unavailable_ids", [])}
        for candidate in dict.fromkeys(item["id"] for item in items
                                       if item["id"] not in private_candidates):
            passed = False
            while True:
                try:
                    with music_lock_wait():
                        probe_access(candidate, args)
                    passed = True
                    break
                except ImportErrorSafe as error:
                    if str(error).startswith("PROBE_SKIP:"):
                        break
                    if str(error).startswith("DEFERRED:"):
                        print(error, flush=True)
                        time.sleep(60)
                        continue
                    raise
            if passed:
                break
        else:
            raise ImportErrorSafe("STOP: no accessible video passed format probe")
        present = media_ids(TV_ROOT)
        old = media_ids(OLD_ROOT)
        imported = 0
        by_source = {source["playlist_id"]: source for source in sources}
        for index, item in enumerate(items, 1):
            record_file = record_path(item)
            if record_file.is_file():
                verify_record_files(read_json(record_file))
                continue
            if (STATE_ROOT / "unavailable" / f"{item['id']}.json").is_file() or \
                    (STATE_ROOT / "shorts" / f"{item['id']}.json").is_file():
                continue
            progress("item", position=index, completed=sum(record_path(other).is_file() for other in items))
            while True:
                try:
                    channel_queue_finished()
                    with music_lock_wait():
                        gate(wait=False)
                        media, info = fetch_video(item["id"], present.get(item["id"]) or old.get(item["id"]), args)
                        record = promote(item, by_source[item["playlist_id"]], media, info, args)
                        present[item["id"]] = Path(record["target"])
                        imported += 1
                        write_status(items, limited=True)
                        print(f"IMPORTED {item['playlist_id']} #{item['position']} "
                              f"S{item['season']:02d}E{item['episode']:03d} {item['id']}: {record['title']}", flush=True)
                    break
                except ImportErrorSafe as error:
                    message = str(error)
                    if message.startswith("DEFERRED:"):
                        print(message, flush=True)
                        progress("phase", "waiting")
                        time.sleep(60)
                        gate(wait=True)
                        continue
                    if message.startswith(("UNAVAILABLE:", "SHORT:", "LIVE:")):
                        print(message, flush=True)
                        write_status(items, limited=True)
                        break
                    raise
            if args.limit and imported >= args.limit:
                print(f"LIMIT {args.limit} reached", flush=True)
                return 0
            time.sleep(10)
        # Playlists can gain entries during a long import. Journal additions
        # without renumbering the pinned baseline; a follow-up pass can extend
        # the reviewed inventory before reporting full completion.
        for source in sources:
            while True:
                try:
                    with music_lock_wait():
                        scan_playlist(source, args)
                    break
                except ImportErrorSafe as error:
                    if not str(error).startswith("DEFERRED:"):
                        raise
                    print(error, flush=True)
                    time.sleep(60)
        status = write_status(items, limited=False)
        progress("done")
        print(f"{status['status'].upper()}: {status['present']}/{status['placements']} placements; "
              f"{status['unavailable']} unavailable, {status['shorts_excluded']} Shorts excluded", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "plan", "run", "verify", "rollback"))
    parser.add_argument("--inventory", type=Path,
                        default=Path(__file__).with_name("requested-youtube-playlists-inventory-20261014.json"))
    parser.add_argument("--limit", type=int, help="Maximum new placements this run")
    parser.add_argument("--yt-dlp", default="/usr/bin/yt-dlp")
    parser.add_argument("--ffprobe", default="/usr/bin/ffprobe")
    parser.add_argument("--cookies", type=Path, help="Existing cookies file; never copied or logged")
    parser.add_argument("--full-hash", action="store_true", help="For verify: hash every recorded file")
    parser.add_argument("--playlist-id", help="For rollback: exact playlist ID")
    parser.add_argument("--position", type=int, help="For rollback: exact one-based source position")
    parser.add_argument("--confirm-rollback", action="store_true", help="Required for rollback")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    sources = load_inventory(args.inventory)
    items = planned_placements(sources)
    if args.action == "check":
        if sys.platform.startswith("linux"):
            if not TV_ROOT.is_dir() or not OLD_ROOT.is_dir() or not MUSIC_LOCK.parent.is_dir():
                raise ImportErrorSafe("Expected Plex media roots or shared lock directory missing")
            for binary in (args.yt_dlp, args.ffprobe, "/home/ap3x/.deno/bin/deno"):
                if not Path(binary).is_file() or not os.access(binary, os.X_OK):
                    raise ImportErrorSafe(f"Missing executable: {binary}")
            channel_queue_finished()
            gate(wait=False)
        print(f"CONFIG PASS: {len(sources)} playlists, {len(items)} placements, "
              f"{len({item['id'] for item in items})} unique videos")
        return 0
    if args.action == "plan":
        from collections import Counter
        grouped = Counter(item["show"] for item in items)
        for show, count in grouped.items():
            print(f"{show}: {count} episodes")
        print(f"TOTAL: {len(items)} positions, {len({item['id'] for item in items})} IDs; "
              "private candidates and Shorts will be recorded as gaps")
        return 0
    if args.action == "verify":
        count = 0
        for item in items:
            path = record_path(item)
            if path.is_file():
                verify_record_files(read_json(path), full_hash=args.full_hash)
                count += 1
        print(f"VERIFIED: {count}/{len(items)} placement records")
        return 0
    if args.action == "rollback":
        if not args.confirm_rollback or not args.playlist_id or not args.position:
            parser.error("rollback requires --playlist-id, --position, and --confirm-rollback")
        selected = [item for item in items
                    if item["playlist_id"] == args.playlist_id and item["position"] == args.position]
        if len(selected) != 1:
            parser.error("Unknown playlist/position mapping")
        with locked(STATE_ROOT / "playlist-import.lock"):
            with music_lock_wait():
                rollback_one(selected[0])
            write_status(items, limited=True)
        print(f"ROLLED BACK: {args.playlist_id} position {args.position}; source files preserved")
        return 0
    return run(args, sources)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ImportErrorSafe, OSError, subprocess.TimeoutExpired, ValueError) as error:
        progress_stopped(error)
        print(error, file=sys.stderr, flush=True)
        raise SystemExit(2)
