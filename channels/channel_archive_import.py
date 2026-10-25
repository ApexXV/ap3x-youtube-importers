#!/usr/bin/env python3
"""Resumable, serial YouTube channel importer for the Plex YouTube Series TV library.

Run on rackmain1. ``check`` and ``plan`` never download media. ``run`` is the
only command that writes to the Plex tree. Keep the stage on the media volume:
promotion uses hard links, so a complete episode appears only after its NFO,
thumbnail and subtitles are in place. A completed record is written last.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

try:
    import fcntl
except ImportError:  # Allows pure planning tests on Windows.
    fcntl = None


MEDIA_ROOT = Path("/home/md-media/media")
TV_ROOT = MEDIA_ROOT / "Youtube-Series"
OLD_ROOT = MEDIA_ROOT / "Youtube"
STAGE_ROOT = MEDIA_ROOT / ".youtube-series-channel-stage"
STATE_ROOT = Path("/home/ap3x/.local/share/youtube-series-channel-import")
MUSIC_LOCK = MEDIA_ROOT / "Music/.ap3x-lofi-stage/import.lock"
MUSIC_SERVICE = "artist-archive-import.service"
RESERVE_BYTES = 1024 ** 4  # Keep 1 TiB free on the mirrored array.
MAX_SCAN_PASSES = 3  # Leave a resumable partial status if uploads keep changing.
DEFAULT_REQUEST_SLEEP = 3
DEFAULT_INTER_VIDEO_DELAY = 120
MEDIA_EXTENSIONS = {".mkv", ".mp4", ".webm", ".mov", ".m4v"}
ID_RE = re.compile(r"(?:\[|^)([A-Za-z0-9_-]{11})(?:\]|\.)")
BOT_RE = re.compile(r"HTTP Error 429|too many requests|not a bot|captcha|verify you are human|bot verification", re.I)
UNAVAILABLE_RE = re.compile(r"video (?:is )?unavailable|private video|has been removed|not available in your country", re.I)
TRANSIENT_RE = re.compile(r"HTTP Error 5\d\d|timed out|connection reset|RemoteDisconnected|remote end closed connection without response|temporarily unavailable|requested format is not available|missing url", re.I)
NO_SHORTS_TAB_RE = re.compile(r"does not have a shorts tab", re.I)
LOCAL_ZONE = ZoneInfo("America/New_York")


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


def valid_video_id(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{11}", value or ""))


def safe_component(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", " ", value).strip(" .")
    return re.sub(r"\s+", " ", value)[:140] or "Untitled"


def xml_safe(value: object) -> str:
    return "".join(char for char in str(value)
                   if char in "\t\n\r" or 0x20 <= ord(char) <= 0xD7FF
                   or 0xE000 <= ord(char) <= 0xFFFD
                   or 0x10000 <= ord(char) <= 0x10FFFF)


def load_sources(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ImportErrorSafe("Source file must be a nonempty JSON list")
    slugs, shows = set(), set()
    for source in raw:
        for key in ("slug", "url", "show"):
            if not isinstance(source.get(key), str) or not source[key].strip():
                raise ImportErrorSafe(f"Missing source {key}: {source}")
        if not re.fullmatch(r"[a-z0-9_-]+", source["slug"]):
            raise ImportErrorSafe(f"Unsafe slug: {source['slug']}")
        if not source["url"].startswith("https://www.youtube.com/"):
            raise ImportErrorSafe(f"Unexpected source URL: {source['url']}")
        if not source["url"].endswith("/videos"):
            raise ImportErrorSafe(f"Channel source must use its /videos tab: {source['url']}")
        if safe_component(source["show"]) != source["show"]:
            raise ImportErrorSafe(f"Unsafe show folder: {source['show']}")
        if source["slug"] in slugs or source["show"] in shows:
            raise ImportErrorSafe("Source slugs and show folders must be unique")
        expected = source.get("channel_id")
        if expected and not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", expected):
            raise ImportErrorSafe(f"Invalid channel ID for {source['slug']}")
        if "reuse_existing_show" in source and not isinstance(source["reuse_existing_show"], bool):
            raise ImportErrorSafe(f"reuse_existing_show must be boolean for {source['slug']}")
        slugs.add(source["slug"])
        shows.add(source["show"])
    return raw


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


def existing_inventory() -> tuple[dict[str, Path], dict[str, Path]]:
    # New Plex episodes take precedence; old-library files can be reused by
    # hardlink without changing their original path or watch record.
    new = media_ids(TV_ROOT)
    old = media_ids(OLD_ROOT)
    return new, old


def command_base(args: argparse.Namespace) -> list[str]:
    cmd = [args.yt_dlp, "--ignore-config", "--js-runtimes", "deno:/home/ap3x/.deno/bin/deno",
           "--sleep-requests", str(getattr(args, "sleep_requests", DEFAULT_REQUEST_SLEEP)),
           "--retries", "0", "--fragment-retries", "0", "--extractor-retries", "0"]
    if args.cookies:
        cmd.extend(["--cookies", str(args.cookies)])
    return cmd


def tail(path: Path, chars: int = 16000) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        stream.seek(max(0, stream.tell() - chars))
        return stream.read().decode("utf-8", errors="replace")


def classify_error(message: str) -> str:
    if BOT_RE.search(message):
        return "bot_or_429"
    if UNAVAILABLE_RE.search(message):
        return "unavailable"
    if TRANSIENT_RE.search(message):
        return "transient"
    return "unknown"


def quiet_window(now: dt.datetime | None = None) -> bool:
    current = (now or dt.datetime.now(LOCAL_ZONE)).astimezone(LOCAL_ZONE)
    minutes = current.hour * 60 + current.minute
    return 3 * 60 + 30 <= minutes < 5 * 60 + 30


def music_active() -> bool:
    result = subprocess.run(["systemctl", "--user", "show", MUSIC_SERVICE,
                             "-p", "ActiveState", "--value"], text=True, capture_output=True,
                            check=False, timeout=10)
    # Unknown unit state is a safety stop, not permission to compete.
    if result.returncode:
        raise ImportErrorSafe("Cannot inspect artist import service state")
    return result.stdout.strip() in {"active", "activating", "reloading", "deactivating"}


def free_bytes() -> int:
    return shutil.disk_usage(MEDIA_ROOT).free


def gate(*, wait: bool, interval: int = 60) -> None:
    while quiet_window() or music_active():
        if not wait:
            raise ImportErrorSafe("DEFERRED: music service or protected 03:30–05:30 window")
        print("WAITING: music service or protected 03:30–05:30 window", flush=True)
        time.sleep(interval)
    if free_bytes() < RESERVE_BYTES:
        raise ImportErrorSafe(f"STOP: array free space {free_bytes()/1024**4:.2f} TiB is below 1 TiB reserve")


@contextlib.contextmanager
def locked(path: Path):
    if fcntl is None:
        raise ImportErrorSafe("The importer must run on Linux with fcntl")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ImportErrorSafe(f"DEFERRED: importer lock busy: {path}") from error
        yield


@contextlib.contextmanager
def music_lock_wait():
    """Wait for the shared music lock, then own it for just one video."""
    if fcntl is None:
        raise ImportErrorSafe("The importer must run on Linux with fcntl")
    MUSIC_LOCK.parent.mkdir(parents=True, exist_ok=True)
    while True:
        gate(wait=True)
        with MUSIC_LOCK.open("a+") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("WAITING: shared music importer lock", flush=True)
            else:
                yield
                return
        time.sleep(60)


def scan_channel(source: dict, args: argparse.Namespace) -> list[str]:
    # Cross-check /shorts even though the requested catalog is /videos only:
    # YouTube may also surface a Short in /videos on a later scan.
    gate(wait=False)
    cmd = command_base(args) + ["--flat-playlist", "--dump-single-json", source["url"]]
    code, output, error = run_json_command(cmd, timeout_seconds=1800)
    if code:
        kind = classify_error(error)
        raise ImportErrorSafe(f"Channel scan failed ({source['slug']}, {kind}): {error[-500:]}")
    response = json.loads(output)
    # The top-level id can be a playlist ID, not a UC channel ID.
    got_channel = response.get("channel_id")
    if source.get("channel_id") and got_channel and got_channel != source["channel_id"]:
        raise ImportErrorSafe(f"Channel identity changed for {source['slug']}: {got_channel}")
    ids = []
    for entry in response.get("entries") or []:
        video_id = entry.get("id") if isinstance(entry, dict) else None
        if valid_video_id(video_id) and video_id not in ids:
            ids.append(video_id)
    if not ids:
        raise ImportErrorSafe(f"No valid uploads found for {source['slug']}")
    videos_tab_ids = list(ids)
    shorts_url = source["url"][:-len("/videos")] + "/shorts"
    shorts_cmd = command_base(args) + ["--flat-playlist", "--dump-single-json", shorts_url]
    shorts_code, shorts_output, shorts_error = run_json_command(shorts_cmd, timeout_seconds=1800)
    if shorts_code and not NO_SHORTS_TAB_RE.search(shorts_error):
        kind = classify_error(shorts_error)
        raise ImportErrorSafe(f"Shorts exclusion scan failed ({source['slug']}, {kind}): {shorts_error[-500:]}")
    shorts_ids: set[str] = set()
    if not shorts_code:
        shorts_response = json.loads(shorts_output)
        shorts_channel = shorts_response.get("channel_id")
        if source.get("channel_id") and shorts_channel and shorts_channel != source["channel_id"]:
            raise ImportErrorSafe(f"Shorts tab identity changed for {source['slug']}: {shorts_channel}")
        shorts_ids = {entry["id"] for entry in shorts_response.get("entries") or []
                      if isinstance(entry, dict) and valid_video_id(entry.get("id"))}
    overlapping = [video_id for video_id in ids if video_id in shorts_ids]
    for video_id in overlapping:
        record_short_skip(source, video_id, "listed_in_channel_shorts_tab")
    excluded = {video_id for video_id in ids
                if video_id in shorts_ids or (STATE_ROOT / "skipped-shorts" / f"{video_id}.json").exists()}
    ids = [video_id for video_id in ids if video_id not in excluded]
    # Channel uploads are reverse chronological; year seasons will be numbered
    # from oldest to newest after each video's full metadata supplies its date.
    ids.reverse()
    atomic_json(STATE_ROOT / "catalog" / f"{source['slug']}.json",
                {"source": source, "ids_oldest_first": ids,
                 "videos_tab_ids": videos_tab_ids,
                 "videos_tab_count": len(videos_tab_ids),
                 "shorts_tab_count": len(shorts_ids),
                 "shorts_excluded_from_videos": len(excluded),
                 "scanned_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    return ids


def record_short_skip(source: dict, video_id: str, reason: str, *, title: str | None = None) -> None:
    if not valid_video_id(video_id):
        raise ImportErrorSafe(f"Invalid Shorts ID: {video_id}")
    atomic_json(STATE_ROOT / "skipped-shorts" / f"{video_id}.json",
                {"source": source["slug"], "id": video_id, "reason": reason,
                 "title": title, "checked_at": dt.datetime.now(dt.timezone.utc).isoformat()})


def shorts_evidence(info: dict) -> str | None:
    """Use positive Shorts signals only; length alone is not enough."""
    if info.get("is_short") is True:
        return "metadata_is_short"
    for key in ("webpage_url", "original_url"):
        if re.search(r"(?:youtube\.com|youtu\.be)/shorts/[A-Za-z0-9_-]{11}", str(info.get(key) or ""), re.I):
            return f"metadata_{key}_shorts_url"
    try:
        duration = float(info.get("duration"))
    except (TypeError, ValueError):
        duration = None
    if duration is not None and duration <= 180:
        tags = [str(tag).strip().lower().lstrip("#") for tag in info.get("tags") or []]
        title = str(info.get("title") or "").lower()
        if "shorts" in tags or re.search(r"(?:^|\s)#shorts\b", title):
            return "short_duration_with_shorts_tag"
    return None


def preflight_video(source: dict, video_id: str, args: argparse.Namespace) -> None:
    """Inspect each video without media transfer before allowing fetch_video."""
    gate(wait=False)
    cmd = command_base(args) + ["--no-playlist", "--skip-download", "--dump-single-json",
                                f"https://www.youtube.com/watch?v={video_id}"]
    code, output, error = run_json_command(cmd, timeout_seconds=300)
    if code:
        kind = classify_error(error)
        if kind == "unavailable":
            atomic_json(STATE_ROOT / "unavailable" / f"{video_id}.json",
                        {"source": source["slug"], "id": video_id, "reason": error[-1000:]})
            raise ImportErrorSafe(f"UNAVAILABLE: {video_id}")
        raise ImportErrorSafe(f"STOP: metadata preflight for {video_id} failed ({kind})")
    info = json.loads(output)
    if info.get("id") != video_id:
        raise ImportErrorSafe(f"STOP: metadata preflight ID mismatch for {video_id}")
    if source.get("channel_id") and info.get("channel_id") != source["channel_id"]:
        raise ImportErrorSafe(f"STOP: metadata preflight channel mismatch for {video_id}")
    if reason := shorts_evidence(info):
        record_short_skip(source, video_id, reason, title=info.get("title"))
        raise ImportErrorSafe(f"SHORTS: {video_id} excluded ({reason})")
    if info.get("live_status") in {"is_live", "is_upcoming"}:
        raise ImportErrorSafe(f"LIVE: {video_id} is currently live or upcoming")
    if not any(item.get("url") for item in info.get("formats") or []):
        raise ImportErrorSafe(f"STOP: metadata preflight returned no usable format for {video_id}")


def probe_channel(source: dict, video_id: str, args: argparse.Namespace) -> None:
    """Require actual media extraction before the first batch download.

    Flat channel lists can succeed while individual videos are bot-blocked.
    This probe makes one full extraction request, never downloads media, and
    never retries a 429/bot response.
    """
    gate(wait=False)
    cmd = command_base(args) + ["--no-playlist", "--skip-download", "--dump-single-json",
                                f"https://www.youtube.com/watch?v={video_id}"]
    code, output, error = run_json_command(cmd, timeout_seconds=300)
    if code:
        kind = classify_error(error)
        atomic_json(STATE_ROOT / "probes" / f"{source['slug']}.json",
                    {"id": video_id, "status": kind, "checked_at": dt.datetime.now(dt.timezone.utc).isoformat()})
        if kind == "unavailable":
            raise ImportErrorSafe(f"PROBE_SKIP: {video_id} is unavailable")
        raise ImportErrorSafe(f"STOP: full-video probe for {source['slug']} failed ({kind}); no batch download started")
    info = json.loads(output)
    formats = [item for item in info.get("formats", []) if item.get("url")]
    if info.get("id") != video_id:
        raise ImportErrorSafe(f"STOP: full-video probe identity mismatch for {video_id}")
    if reason := shorts_evidence(info):
        record_short_skip(source, video_id, reason, title=info.get("title"))
        raise ImportErrorSafe(f"PROBE_SKIP: {video_id} is a Short")
    if not formats and info.get("live_status") in {"is_live", "is_upcoming"}:
        raise ImportErrorSafe(f"PROBE_SKIP: {video_id} is a live/upcoming item")
    if not formats:
        raise ImportErrorSafe(f"STOP: full-video probe returned no usable format for {video_id}")
    atomic_json(STATE_ROOT / "probes" / f"{source['slug']}.json",
                {"id": video_id, "status": "ok", "format_count": len(formats),
                 "checked_at": dt.datetime.now(dt.timezone.utc).isoformat()})


def run_json_command(cmd: list[str], *, timeout_seconds: int) -> tuple[int, str, str]:
    """Capture extractor JSON privately; cancel if the music window arrives."""
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="extract-", dir=STATE_ROOT) as folder:
        output_path = Path(folder) / "output.json"
        error_path = Path(folder) / "error.log"
        with output_path.open("wb") as output_file, error_path.open("wb") as error_file:
            process = subprocess.Popen(cmd, stdout=output_file, stderr=error_file, start_new_session=True)
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


def video_date(info: dict) -> str | None:
    raw = info.get("upload_date") or info.get("release_date")
    if raw and re.fullmatch(r"\d{8}", str(raw)):
        try:
            return dt.datetime.strptime(str(raw), "%Y%m%d").date().isoformat()
        except ValueError:
            pass
    for key in ("timestamp", "release_timestamp"):
        if info.get(key):
            return dt.datetime.fromtimestamp(int(info[key]), dt.timezone.utc).date().isoformat()
    return None


def xml_write(path: Path, tag: str, fields: dict[str, object]) -> None:
    root = ET.Element(tag)
    for key, value in fields.items():
        if value is not None and value != "":
            attrs = {"type": "youtube", "default": "true"} if key == "uniqueid" else {}
            ET.SubElement(root, key, attrs).text = xml_safe(value)
    tree = ET.ElementTree(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tree.write(temp, encoding="utf-8", xml_declaration=True)
    with temp.open("r+b") as stream:
        os.fsync(stream.fileno())
    os.replace(temp, path)


def next_episode(show_dir: Path, season: int) -> int:
    season_dir = show_dir / f"Season {season:02d}"
    pattern = re.compile(rf"S{season:02d}E(\d+)", re.I)
    numbers = [int(match.group(1)) for path in season_dir.glob("*")
               if (match := pattern.search(path.name))]
    return max(numbers, default=0) + 1


def make_nfos(source: dict, info: dict, stage: Path, target: Path,
              season: int, episode: int, aired: str | None) -> None:
    video_id = info["id"]
    title = info.get("title") or video_id
    xml_write(stage / (target.stem + ".nfo"), "episodedetails", {
        "title": title, "season": season, "episode": episode, "aired": aired,
        "plot": info.get("description") or "", "studio": info.get("channel") or source["show"],
        "uniqueid": video_id, "youtubeid": video_id,
    })


def archival_info(info: dict) -> dict:
    """Keep durable metadata, excluding signed stream URLs and credentials."""
    keys = ("id", "title", "description", "upload_date", "release_date", "timestamp",
            "channel", "channel_id", "uploader", "uploader_id", "duration", "webpage_url",
            "live_status", "availability", "categories", "tags", "chapters")
    return {key: info[key] for key in keys if info.get(key) is not None}


def show_nfos(source: dict, season: int) -> None:
    show_dir = TV_ROOT / source["show"]
    if show_dir.exists():
        show_nfo = show_dir / "tvshow.nfo"
        if show_dir.is_symlink() or not show_dir.is_dir() or not show_nfo.is_file():
            raise ImportErrorSafe(f"Unexpected existing show folder: {show_dir}")
        existing_root = ET.parse(show_nfo).getroot()
        owned = existing_root.findtext("plot") == f"Archived uploads from {source['url']}"
        if not owned and not source.get("reuse_existing_show"):
            raise ImportErrorSafe(f"Show folder belongs to another source: {show_dir}")
        if not owned and existing_root.findtext("title") != source["show"]:
            raise ImportErrorSafe(f"Reused show title mismatch: {show_dir}")
    show_dir.mkdir(parents=True, exist_ok=True)
    show_nfo = show_dir / "tvshow.nfo"
    if not show_nfo.exists():
        xml_write(show_nfo, "tvshow", {"title": source["show"],
                  "plot": f"Archived uploads from {source['url']}", "studio": source["show"]})
    # Plex reads named seasons from tvshow.nfo; it may ignore season.nfo title.
    tree = ET.parse(show_nfo)
    root = tree.getroot()
    owned = root.findtext("plot") == f"Archived uploads from {source['url']}"
    if owned and not any(node.get("number") == str(season) for node in root.findall("namedseason")):
        label = "Undated" if season == 0 else str(season)
        ET.SubElement(root, "namedseason", {"number": str(season)}).text = label
        temp = show_nfo.with_name(show_nfo.name + f".tmp-{os.getpid()}")
        tree.write(temp, encoding="utf-8", xml_declaration=True)
        os.replace(temp, show_nfo)
    season_dir = show_dir / f"Season {season:02d}"
    season_dir.mkdir(parents=True, exist_ok=True)
    season_nfo = season_dir / "season.nfo"
    if not season_nfo.exists():
        xml_write(season_nfo, "season", {"title": str(season) if season else "Undated",
                  "seasonnumber": season})


def staged_media(stage: Path, video_id: str) -> Path | None:
    files = [path for path in stage.glob(video_id + ".*")
             if (path.is_file() and not path.is_symlink()
                 and path.stem == video_id and path.suffix.lower() in MEDIA_EXTENSIONS)]
    if len(files) > 1:
        raise ImportErrorSafe(f"Multiple media files staged for {video_id}: {files}")
    return files[0] if files else None


def quarantine_stage_file(path: Path, source_slug: str, video_id: str, reason: str) -> Path:
    """Preserve uncertain stage files outside the per-video directory removed by promotion."""
    if path.parent != STAGE_ROOT / source_slug / video_id or path.is_symlink() or not path.is_file():
        raise ImportErrorSafe(f"Unsafe stage file to back up: {path}")
    backup_dir = STAGE_ROOT / ".ambiguous-media-backup" / source_slug / video_id
    for directory in (backup_dir.parent.parent, backup_dir.parent, backup_dir):
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ImportErrorSafe(f"Unsafe stage backup directory: {directory}")
        directory.mkdir(mode=0o700, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = backup_dir / f"{stamp}-{os.getpid()}-{path.name}"
    os.link(path, backup)  # Same media volume; fails rather than replacing an earlier backup.
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
        raise ImportErrorSafe(f"ffprobe failed for {path}: {result.stderr[-300:]}")
    data = json.loads(result.stdout)
    if not any(item.get("codec_type") == "video" for item in data.get("streams", [])):
        raise ImportErrorSafe(f"No video stream: {path}")
    if float(data.get("format", {}).get("duration") or 0) <= 0:
        raise ImportErrorSafe(f"No media duration: {path}")
    return data


def monitor_command(cmd: list[str], log: Path, *, reserve: int, check_window: bool) -> tuple[int, str]:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as stream:
        process = subprocess.Popen(cmd, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        reason = "exit"
        try:
            while process.poll() is None:
                time.sleep(10)
                progress("ytdlp_log", tail(log, 4000))
                if free_bytes() < reserve:
                    reason = "low_space"
                elif check_window and (quiet_window() or music_active()):
                    reason = "music_window"
                if reason != "exit":
                    terminate_process(process)
                    break
        except BaseException:
            terminate_process(process)
            raise
        return process.wait(), reason


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


def fetch_video(source: dict, video_id: str, existing_old: Path | None,
                args: argparse.Namespace) -> tuple[Path, dict]:
    stage = STAGE_ROOT / source["slug"] / video_id
    stage.mkdir(parents=True, exist_ok=True)
    info_file = stage / f"{video_id}.info.json"
    completion = stage / f"{video_id}.download-complete.json"
    media = staged_media(stage, video_id)
    if media is None and completion.is_file():
        quarantine_stage_file(completion, source["slug"], video_id,
                              "completion_marker_without_media")
    trusted_reuse = bool(media is not None and existing_old is not None
                         and existing_old.is_file() and not existing_old.is_symlink()
                         and media.stat().st_ino == existing_old.stat().st_ino
                         and media.stat().st_dev == existing_old.stat().st_dev)
    if media is not None and not completion.is_file() and not trusted_reuse:
        quarantine_stage_file(media, source["slug"], video_id,
                              "exact_media_without_completion_marker")
        media = None
    downloaded = False
    if media is None or not info_file.is_file() or not completion.is_file():
        url = f"https://www.youtube.com/watch?v={video_id}"
        output = str(stage / (video_id + ".%(ext)s"))
        cmd = command_base(args) + ["--no-playlist", "--write-info-json", "--write-description",
               "--write-thumbnail", "--convert-thumbnails", "jpg", "--write-subs", "--write-auto-subs",
               "--sub-langs", "en.*,en", "--convert-subs", "srt", "--sleep-interval", "2",
               "--max-sleep-interval", "5", "--limit-rate", "8M", "--concurrent-fragments", "1",
               "--match-filter", "live_status != is_live & live_status != is_upcoming",
               "--no-overwrites", "-o", output]
        if existing_old is not None or (media is not None and completion.is_file()):
            cmd.append("--skip-download")
        else:
            cmd.extend(["-f", "bv*+ba/b", "--merge-output-format", "mkv"])
        cmd.append(url)
        progress("phase", "downloading")
        delays = (0, 60, 120)
        last_log = None
        for attempt, delay in enumerate(delays, 1):
            if delay:
                time.sleep(delay)
            gate(wait=False)
            attempt_log = stage / f"yt-dlp-attempt-{int(time.time())}-{attempt}.log"
            last_log = attempt_log
            code, reason = monitor_command(cmd, attempt_log, reserve=RESERVE_BYTES,
                                           check_window=True)
            if reason == "music_window":
                raise ImportErrorSafe(f"DEFERRED: {video_id} partial stage kept for music window")
            if reason == "low_space":
                raise ImportErrorSafe(f"STOP: free space below 1 TiB while downloading {video_id}")
            if code == 0:
                downloaded = True
                break
            kind = classify_error(tail(attempt_log))
            if kind == "unavailable":
                atomic_json(STATE_ROOT / "unavailable" / f"{video_id}.json",
                            {"source": source["slug"], "id": video_id, "reason": tail(attempt_log, 1000)})
                raise ImportErrorSafe(f"UNAVAILABLE: {video_id}")
            if kind != "transient" or attempt == len(delays):
                raise ImportErrorSafe(f"STOP: {video_id} yt-dlp {kind}, exit={code}; see {attempt_log}")
        media = staged_media(stage, video_id)
        if media is None and last_log and re.search(r"does not pass filter|was filtered", tail(last_log), re.I):
            raise ImportErrorSafe(f"LIVE: {video_id} is currently live or upcoming; revisit later")
    if existing_old is not None and media is None:
        if existing_old.is_symlink() or not existing_old.is_file():
            raise ImportErrorSafe(f"Invalid existing media: {existing_old}")
        media = stage / (video_id + existing_old.suffix.lower())
        try:
            os.link(existing_old, media)
        except FileExistsError:
            pass
    if media is None or not info_file.exists():
        raise ImportErrorSafe(f"Incomplete staged video or metadata for {video_id}")
    info = read_json(info_file)
    if info.get("id") != video_id:
        raise ImportErrorSafe(f"Metadata ID mismatch for {video_id}")
    if source.get("channel_id") and info.get("channel_id") != source["channel_id"]:
        raise ImportErrorSafe(f"Video {video_id} belongs to unexpected channel {info.get('channel_id')}")
    if reason := shorts_evidence(info):
        record_short_skip(source, video_id, reason, title=info.get("title"))
        raise ImportErrorSafe(f"SHORTS: {video_id} excluded after staged metadata validation")
    progress("phase", "verifying")
    verify_media(media, args.ffprobe)
    if downloaded:
        atomic_json(completion, {"id": video_id, "media_name": media.name,
                    "size": media.stat().st_size,
                    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    return media, info


def target_files(stage: Path, media: Path, target: Path) -> list[tuple[Path, Path]]:
    result = []
    video_id = media.stem
    poster_files = sorted([item for item in stage.glob(video_id + ".*")
                           if item.suffix.lower() in {".jpg", ".jpeg", ".webp"}],
                          key=lambda item: (item.suffix.lower() != ".jpg", item.name))
    preferred_poster = poster_files[0] if poster_files else None
    for item in stage.iterdir():
        if not item.is_file() or item.is_symlink():
            continue
        if item == media or item.name == video_id + ".info.json" or item.name == video_id + ".description":
            continue
        if item in poster_files and item != preferred_poster:
            continue
        if item.suffix.lower() in {".srt", ".vtt", ".jpg", ".jpeg", ".webp", ".nfo"}:
            suffix = item.name[len(video_id):] if item.name.startswith(video_id + ".") else item.suffix
            if item.suffix.lower() == ".nfo":
                suffix = ".nfo"
            if item.suffix.lower() in {".jpg", ".jpeg", ".webp"}:
                suffix = ".jpg"
            result.append((item, target.with_name(target.stem + suffix)))
    # The full extractor JSON may contain signed stream URLs. Only promote a
    # curated metadata sidecar plus the original plain-text description.
    for item in (stage / f"{video_id}.source.json", stage / f"{video_id}.description"):
        if item.exists():
            result.append((item, target.with_name(target.stem + (".source.json" if item.suffix == ".json" else ".description"))))
    result.append((media, target))  # Media appears last to Plex.
    return result


def promote(source: dict, video_id: str, media: Path, info: dict,
            args: argparse.Namespace) -> dict:
    progress("phase", "publishing")
    stage = media.parent
    aired = video_date(info)
    season = int(aired[:4]) if aired else 0
    show_dir = TV_ROOT / source["show"]
    pending_file = STATE_ROOT / "pending" / f"{video_id}.json"
    if pending_file.exists():
        pending = read_json(pending_file)
        if (pending.get("source") != source["slug"] or pending.get("id") != video_id
                or pending.get("season") != season or pending.get("aired") != aired
                or not isinstance(pending.get("episode"), int) or pending["episode"] < 1):
            raise ImportErrorSafe(f"Pending video belongs to different source: {video_id}")
        target = Path(pending["target"])
        episode = int(pending["episode"])
    else:
        episode = next_episode(show_dir, season)
        stem = f"{safe_component(source['show'])} - S{season:02d}E{episode:03d} [{video_id}]"
        target = show_dir / f"Season {season:02d}" / (stem + media.suffix.lower())
        pending = {"source": source["slug"], "id": video_id, "target": str(target),
                   "season": season, "episode": episode, "aired": aired,
                   "created_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        atomic_json(pending_file, pending)
    expected_dir = show_dir / f"Season {season:02d}"
    if (target.parent != expected_dir or target.suffix.lower() != media.suffix.lower()
            or f"S{season:02d}E{episode:03d} [{video_id}]" not in target.stem
            or any(part.is_symlink() for part in (TV_ROOT, show_dir, expected_dir))
            or not target.resolve(strict=False).is_relative_to(TV_ROOT.resolve())):
        raise ImportErrorSafe(f"Unsafe target path in pending record: {target}")
    make_nfos(source, info, stage, target, season, episode, aired)
    atomic_json(stage / f"{video_id}.source.json", archival_info(info))
    show_nfos(source, season)
    files = target_files(stage, media, target)
    created = []
    for item, destination in files:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink():
            raise ImportErrorSafe(f"Target is a symlink: {destination}")
        if destination.exists():
            if destination.stat().st_size != item.stat().st_size or sha256(destination) != sha256(item):
                raise ImportErrorSafe(f"Target collision: {destination}")
        else:
            os.link(item, destination)
        created.append({"path": str(destination), "size": item.stat().st_size, "sha256": sha256(item)})
    verify_media(target, args.ffprobe)
    record = {**pending, "status": "complete", "title": info.get("title"),
              "channel": info.get("channel"), "files": created,
              "source_reused": media.stat().st_nlink > 2,
              "finished_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    atomic_json(STATE_ROOT / "records" / f"{video_id}.json", record)
    pending_file.unlink(missing_ok=True)
    shutil.rmtree(stage)
    return record


def scan_source_for_run(source: dict, args: argparse.Namespace, probed: set[str]) -> list[str]:
    while True:
        gate(wait=True)
        try:
            with music_lock_wait():
                ids = scan_channel(source, args)
                if ids and source["slug"] not in probed:
                    candidates = list(dict.fromkeys((ids[-1], ids[len(ids) // 2], ids[0])))
                    for candidate in candidates:
                        try:
                            probe_channel(source, candidate, args)
                            probed.add(source["slug"])
                            break
                        except ImportErrorSafe as error:
                            if not str(error).startswith("PROBE_SKIP:"):
                                raise
                    else:
                        raise ImportErrorSafe(f"STOP: no completed probe candidate for {source['slug']}")
            return ids
        except ImportErrorSafe as error:
            if not str(error).startswith("DEFERRED:"):
                raise
            print(error, flush=True)
            gate(wait=True)


def scan_source_for_rescan(source: dict, args: argparse.Namespace) -> list[str]:
    while True:
        gate(wait=True)
        try:
            with music_lock_wait():
                return scan_channel(source, args)
        except ImportErrorSafe as error:
            if not str(error).startswith("DEFERRED:"):
                raise
            print(error, flush=True)
            gate(wait=True)


def process_source(source: dict, ids: list[str], args: argparse.Namespace,
                   present_new: dict[str, Path], present_old: dict[str, Path],
                   imported: int) -> tuple[int, bool]:
    for index, video_id in enumerate(ids, 1):
        if (STATE_ROOT / "skipped-shorts" / f"{video_id}.json").exists():
            continue
        record_file = STATE_ROOT / "records" / f"{video_id}.json"
        if record_file.exists():
            record = read_json(record_file)
            verify_record_files(record)
            present_new[video_id] = Path(record["target"])
            continue
        if video_id in present_new and not (STATE_ROOT / "pending" / f"{video_id}.json").exists():
            verify_media(present_new[video_id], args.ffprobe)
            print(f"PRESENT {video_id}: {present_new[video_id]}", flush=True)
            continue
        if (STATE_ROOT / "unavailable" / f"{video_id}.json").exists():
            continue
        progress("item", position=index, eligible=len(ids),
                 completed=sum((STATE_ROOT / "records" / f"{other}.json").exists() for other in ids))
        progress("phase", "waiting")
        gate(wait=True)
        try:
            while True:
                try:
                    with music_lock_wait():
                        gate(wait=False)
                        progress("phase", "probing")
                        preflight_video(source, video_id, args)
                        media, info = fetch_video(source, video_id, present_old.get(video_id), args)
                        record = promote(source, video_id, media, info, args)
                        present_new[video_id] = Path(record["target"])
                        imported += 1
                        print(f"IMPORTED {video_id} S{record['season']:02d}E{record['episode']:03d}: {record['title']}", flush=True)
                    break
                except ImportErrorSafe as error:
                    if not str(error).startswith("DEFERRED:"):
                        raise
                    print(error, flush=True)
                    progress("phase", "waiting")
                    gate(wait=True)
        except ImportErrorSafe as error:
            if str(error).startswith(("UNAVAILABLE:", "LIVE:", "SHORTS:")):
                print(error, flush=True)
                continue
            raise
        if args.limit and imported >= args.limit:
            return imported, True
        time.sleep(getattr(args, "inter_video_delay", DEFAULT_INTER_VIDEO_DELAY))
    return imported, False


def shorts_presence_conflicts(source: dict, present_new: dict[str, Path], run_id: str) -> list[str]:
    """Fail closed when a later Shorts tab scan reclassifies Plex media."""
    catalog = read_json(STATE_ROOT / "catalog" / f"{source['slug']}.json")
    conflicts = []
    for video_id in catalog.get("videos_tab_ids", catalog.get("ids_oldest_first", [])):
        marker = STATE_ROOT / "skipped-shorts" / f"{video_id}.json"
        record = STATE_ROOT / "records" / f"{video_id}.json"
        if marker.exists() and (video_id in present_new or record.exists()):
            conflicts.append(video_id)
            atomic_json(STATE_ROOT / "issues" / f"shorts-present-{video_id}.json",
                        {"type": "shorts_already_in_tv", "run_id": run_id,
                         "source": source["slug"], "id": video_id,
                         "tv_path": str(present_new[video_id]) if video_id in present_new else None,
                         "record": str(record) if record.exists() else None,
                         "checked_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    return conflicts


def run(args: argparse.Namespace, sources: list[dict]) -> int:
    if not TV_ROOT.is_dir() or not OLD_ROOT.is_dir():
        raise ImportErrorSafe("Expected YouTube media libraries are missing")
    STAGE_ROOT.mkdir(parents=True, exist_ok=True)
    (STAGE_ROOT / ".plexignore").write_text("*\n", encoding="utf-8")
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    with locked(STATE_ROOT / "channel-import.lock"):
        start_progress("channels", STATE_ROOT)
        progress("phase", "scanning")
        run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}-{time.time_ns() % 1_000_000_000}"
        stop_after_source = getattr(args, "stop_after_source", None)
        full_source_scope = not bool(getattr(args, "source", None) or stop_after_source)
        if stop_after_source:
            stop_index = next((index for index, source in enumerate(sources)
                               if source["slug"] == stop_after_source), None)
            if stop_index is None:
                raise ImportErrorSafe(f"Unknown stop-after source: {stop_after_source}")
            active_sources = sources[:stop_index + 1]
        else:
            active_sources = sources
        atomic_json(STATE_ROOT / "run-status.json", {"status": "running", "run_id": run_id,
                    "terminal_rescan_verified": False,
                    "full_source_scope": full_source_scope,
                    "stopped_after_source": stop_after_source,
                    "source_boundary_verified": False,
                    "handoff_ready": False,
                    "started_at": dt.datetime.now(dt.timezone.utc).isoformat()})
        gate(wait=True)
        present_new, present_old = existing_inventory()
        imported = 0
        probed: set[str] = set()
        for scan_pass in range(1, MAX_SCAN_PASSES + 1):
            catalogs: dict[str, list[str]] = {}
            raw_before: dict[str, set[str]] = {}
            for source in active_sources:
                progress("phase", "scanning")
                ids = scan_source_for_run(source, args, probed)
                catalogs[source["slug"]] = ids
                catalog = read_json(STATE_ROOT / "catalog" / f"{source['slug']}.json")
                raw_before[source["slug"]] = set(catalog.get("videos_tab_ids", ids))
                conflicts = shorts_presence_conflicts(source, present_new, run_id)
                if conflicts:
                    write_run_status(catalogs, present_new, limited=True,
                                     terminal_rescan_verified=False, full_source_scope=full_source_scope,
                                     run_id=run_id, scan_pass=scan_pass,
                                     issues={source["slug"]: conflicts},
                                     stop_after_source=stop_after_source)
                    raise ImportErrorSafe(f"STOP: Shorts already present in TV for {source['slug']}: {', '.join(conflicts)}")
                print(f"SOURCE {source['slug']} pass {scan_pass}: "
                      f"{catalog.get('videos_tab_count', len(ids))} /videos IDs, "
                      f"{len(ids)} eligible, {catalog.get('shorts_excluded_from_videos', 0)} Shorts excluded", flush=True)
                imported, reached_limit = process_source(source, ids, args, present_new, present_old, imported)
                write_run_status(catalogs, present_new, limited=True,
                                 terminal_rescan_verified=False, full_source_scope=full_source_scope,
                                 run_id=run_id, scan_pass=scan_pass,
                                 stop_after_source=stop_after_source)
                if reached_limit:
                    print(f"LIMIT {args.limit} reached", flush=True)
                    return 0
            # A channel processed early may have gained uploads while later
            # channels ran. Rescan every processed source before completion
            # or a planned source-boundary handoff.
            latest_catalogs: dict[str, list[str]] = {}
            additions: dict[str, dict[str, list[str]]] = {}
            rescan_issues: dict[str, list[str]] = {}
            for source in active_sources:
                slug = source["slug"]
                latest_ids = scan_source_for_rescan(source, args)
                latest_catalogs[slug] = latest_ids
                latest_catalog = read_json(STATE_ROOT / "catalog" / f"{slug}.json")
                conflicts = shorts_presence_conflicts(source, present_new, run_id)
                if conflicts:
                    rescan_issues[slug] = conflicts
                latest_raw = set(latest_catalog.get("videos_tab_ids", latest_ids))
                new_raw = sorted(latest_raw.difference(raw_before[slug]))
                new_eligible = [video_id for video_id in latest_ids if video_id in new_raw]
                if new_raw:
                    additions[slug] = {"new_videos_tab_ids": new_raw,
                                       "new_eligible_ids": new_eligible}
            atomic_json(STATE_ROOT / "runs" / run_id / f"rescan-pass-{scan_pass}.json",
                        {"run_id": run_id, "scan_pass": scan_pass, "additions": additions,
                         "issues": rescan_issues,
                         "checked_at": dt.datetime.now(dt.timezone.utc).isoformat()})
            if rescan_issues:
                write_run_status(latest_catalogs, present_new, limited=True,
                                 terminal_rescan_verified=False, full_source_scope=full_source_scope,
                                 run_id=run_id, scan_pass=scan_pass,
                                 rescan_additions=additions, issues=rescan_issues,
                                 stop_after_source=stop_after_source)
                raise ImportErrorSafe(f"STOP: Shorts already present in TV: {rescan_issues}")
            if additions:
                status = write_run_status(latest_catalogs, present_new, limited=True,
                                          terminal_rescan_verified=False, full_source_scope=full_source_scope,
                                          run_id=run_id,
                                          scan_pass=scan_pass, rescan_additions=additions,
                                          stop_after_source=stop_after_source)
                added = sum(len(item["new_videos_tab_ids"]) for item in additions.values())
                print(f"RESCAN pass {scan_pass}: {added} new /videos IDs; "
                      f"{status['unresolved']} unresolved", flush=True)
                if scan_pass == MAX_SCAN_PASSES:
                    print("FOLLOW_UP_REQUIRED: source listings kept changing; state is partial and resumable", flush=True)
                    return 0
                continue
            if stop_after_source:
                status = write_run_status(latest_catalogs, present_new, limited=True,
                                          terminal_rescan_verified=False, full_source_scope=False,
                                          run_id=run_id, scan_pass=scan_pass,
                                          stop_after_source=stop_after_source,
                                          source_boundary_verified=True)
                print(f"STOP_AFTER_SOURCE {stop_after_source}: "
                      f"{status['present']}/{status['eligible_total']} eligible present; "
                      f"handoff_ready={status['handoff_ready']}; "
                      "remaining sources stay in the full manifest", flush=True)
                return 0
            status = write_run_status(latest_catalogs, present_new, limited=False,
                                      terminal_rescan_verified=True, full_source_scope=full_source_scope,
                                      run_id=run_id,
                                      scan_pass=scan_pass)
            print(f"{status['status'].upper()}: {imported} imported this run; "
                  f"{status['present']}/{status['eligible_total']} eligible present, "
                  f"{status['shorts_excluded']} Shorts excluded, "
                  f"{status['unavailable']} unavailable; terminal rescan verified", flush=True)
            return 0
    return 0


def write_run_status(catalogs: dict[str, list[str]], present_new: dict[str, Path], *,
                     limited: bool, terminal_rescan_verified: bool = False,
                     full_source_scope: bool = True,
                     run_id: str | None = None, scan_pass: int = 0,
                     rescan_additions: dict | None = None, issues: dict | None = None,
                     stop_after_source: str | None = None,
                     source_boundary_verified: bool = False) -> dict:
    raw_by_source = {}
    for slug, items in catalogs.items():
        catalog_file = STATE_ROOT / "catalog" / f"{slug}.json"
        raw_by_source[slug] = read_json(catalog_file).get("videos_tab_ids", items) if catalog_file.exists() else items
    ids = set().union(*(set(items) for items in raw_by_source.values())) if raw_by_source else set()
    skipped = {video_id for video_id in ids
               if (STATE_ROOT / "skipped-shorts" / f"{video_id}.json").exists()}
    unavailable = {video_id for video_id in ids.difference(skipped)
                   if (STATE_ROOT / "unavailable" / f"{video_id}.json").exists()}
    present = ids.intersection(present_new).difference(skipped).difference(unavailable)
    unresolved = ids.difference(present).difference(unavailable).difference(skipped)
    status = {"status": "complete" if not limited and terminal_rescan_verified
              and full_source_scope and not issues and not unresolved and not unavailable else "partial",
              "catalog_total": len(ids), "present": len(present),
              "eligible_total": len(ids) - len(skipped),
              "shorts_excluded": len(skipped),
              "unavailable": len(unavailable), "unresolved": len(unresolved),
              "limited": limited, "terminal_rescan_verified": terminal_rescan_verified,
              "full_source_scope": full_source_scope,
              "stopped_after_source": stop_after_source,
              "source_boundary_verified": source_boundary_verified,
              "handoff_ready": bool(stop_after_source and source_boundary_verified
                                    and limited and not full_source_scope
                                    and not issues and not unresolved and not unavailable),
              "run_id": run_id, "scan_pass": scan_pass,
              "rescan_additions": rescan_additions or {},
              "issues": issues or {},
              "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "sources": {slug: len(items) for slug, items in raw_by_source.items()},
              "source_eligible": {slug: len(set(items).difference(skipped)) for slug, items in raw_by_source.items()}}
    atomic_json(STATE_ROOT / "run-status.json", status)
    return status


def verify_record_files(record: dict) -> None:
    for entry in record.get("files", []):
        path = Path(entry["path"])
        if (not path.is_file() or path.is_symlink()
                or path.stat().st_size != entry["size"]
                or not path.resolve(strict=False).is_relative_to(TV_ROOT.resolve())):
            raise ImportErrorSafe(f"Recorded episode file is missing or changed: {path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "plan", "run"))
    parser.add_argument("--sources", type=Path, default=Path(__file__).with_name("channel_sources.json"))
    parser.add_argument("--source", help="Process one source slug")
    parser.add_argument("--limit", type=int, help="Maximum new videos this run")
    parser.add_argument("--stop-after-source", help="After this source, rescan processed sources and exit with resumable partial status")
    parser.add_argument("--sleep-requests", type=int, default=DEFAULT_REQUEST_SLEEP,
                        help="yt-dlp delay between requests in seconds (default: 3)")
    parser.add_argument("--inter-video-delay", type=int, default=DEFAULT_INTER_VIDEO_DELAY,
                        help="pause after each imported video in seconds (default: 120)")
    parser.add_argument("--yt-dlp", default="/usr/bin/yt-dlp")
    parser.add_argument("--ffprobe", default="/usr/bin/ffprobe")
    parser.add_argument("--cookies", type=Path, help="Optional existing yt-dlp cookies file; never copied or logged")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if args.sleep_requests < 1 or args.inter_video_delay < 0:
        parser.error("--sleep-requests must be positive and --inter-video-delay nonnegative")
    if args.stop_after_source and args.source:
        parser.error("--stop-after-source cannot be combined with --source")
    sources = load_sources(args.sources)
    if args.stop_after_source and args.stop_after_source not in {item["slug"] for item in sources}:
        parser.error("Unknown stop-after source slug")
    if args.source:
        sources = [item for item in sources if item["slug"] == args.source]
        if not sources:
            parser.error("Unknown source slug")
    if args.action == "check":
        if sys.platform.startswith("linux"):
            if not TV_ROOT.is_dir() or not OLD_ROOT.is_dir() or not MUSIC_LOCK.parent.is_dir():
                raise ImportErrorSafe("Expected Plex media roots or shared lock directory missing")
            for binary in (args.yt_dlp, args.ffprobe, "/home/ap3x/.deno/bin/deno"):
                if not Path(binary).is_file() or not os.access(binary, os.X_OK):
                    raise ImportErrorSafe(f"Missing executable: {binary}")
            gate(wait=False)
            print(f"PASS: {len(sources)} sources; media free {free_bytes()/1024**4:.2f} TiB; "
                  "artist service inactive; music window clear")
        else:
            print(f"CONFIG PASS: {len(sources)} sources (Linux host checks not run)")
        return 0
    if args.action == "plan":
        new, old = existing_inventory()
        for source in sources:
            with music_lock_wait():
                ids = scan_channel(source, args)
            catalog = read_json(STATE_ROOT / "catalog" / f"{source['slug']}.json")
            print(f"{source['slug']}: {catalog['videos_tab_count']} /videos IDs, "
                  f"{len(ids)} eligible, {catalog['shorts_excluded_from_videos']} Shorts excluded, "
                  f"{sum(v in new for v in ids)} in TV, "
                  f"{sum(v in old and v not in new for v in ids)} reusable from old library")
        return 0
    result = run(args, sources)
    progress("done")
    return result


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ImportErrorSafe, OSError, subprocess.TimeoutExpired) as error:
        progress_stopped(error)
        print(error, file=sys.stderr, flush=True)
        raise SystemExit(2)
