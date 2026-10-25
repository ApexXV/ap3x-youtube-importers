# YouTube playlist import candidate (2026-10-14)

## State and start condition

The importer files are staged for offline review only. No playlist media has been downloaded and no playlist service has been scheduled. The current full-video YouTube probe from `rackmain1` returns bot verification. Do not run the importer until that access issue is resolved and the six-channel queue has finished its baseline catalog. `playlist_archive_import.py run` enforces both conditions: it requires the six-source channel status to explicitly certify a fresh terminal rescan and a free channel lock, then requires a successful full-format probe before fetching media.

The input is `requested-youtube-playlists-inventory-20261014.json`: 16 playlists, 749 source positions, 736 distinct video IDs, and 140 untitled or unavailable candidates. The importer preserves the pinned order. A fresh flat scan journals newly added items for a later inventory revision while proceeding with pinned positions; it stops on a removed or reordered titled baseline item. Known private entries may disappear from a fresh flat scan without renumbering the pinned episodes. A final rescan checks for additions during a long run.

## TV library mapping

Each playlist maps to a separate Plex show under `Youtube-Series`, with a Season 01 by default. This deliberately retains the original list order and duplicate list positions. Video files contain the YouTube ID in brackets. Show NFO GUIDs combine playlist ID and show slug; episode NFO GUIDs combine playlist ID and one-based source position. The actual YouTube ID remains in `<youtubeid>` and sanitized `.source.json`. The three repeated COPS N ROBBERS video IDs receive distinct episode GUIDs and numbers but share one media inode via hardlinks on Linux.

Reviewed exceptions:

- Room Mates positions 1–10 become Season 01 episodes 1–10; positions 11–20 become Season 02 episodes 1–10.
- Maze Runner and Pirates source position 9 is episode **10**. Episode 9 stays absent.
- Minecraft University positions 1–6 remain in University; four off-topic Minecraft High School videos become a separate show with episode numbers **2, 3, 5, 6**.
- Nostalgia Craft positions 1–10 remain in the series, including the private position 9 as a gap. The unrelated `GdTvQlpWPys` “1620 French Fry Challenge” is filed as an extra in a separate Sky Does Everything show.
- Six cross-playlist video IDs are shared between the two Nobodies Live GTA shows; four are shared between the two Jacksepticeye Subnautica lists. Their media paths are separate Plex episodes but hardlink to one inode.
- Video IDs identified as Shorts from full metadata (explicit Shorts flag/URL/tag, or portrait video up to three minutes) are marked `shorts_excluded` without fetching media. A video marked private/unavailable gets a gap record. Normal unavailable/Short gaps do not count as downloaded episodes.

## Operational safeguards

The importer downloads **one video at a time** using highest available `bv*+ba/b`, merges to MKV, and keeps English captions, descriptions, thumbnails, source metadata, and episode/show/season NFOs where available. It requires a successful metadata-only extraction first, so a detected Short is never fetched as media. Reused local media with a legacy or channel `.source.json` is probed afresh unless its sidecar carries this importer's explicit non-Short preflight proof for the same video ID. yt-dlp internal retries are set to zero; the wrapper retries only classified transient failures at 0/60/120/240 seconds. Bot verification/HTTP 429 and unknown failures stop immediately. Partial `.part` files remain in a Plex-ignored staging directory for resume.

It reserves 1 TiB of array free space, holds the existing shared music importer lock per video, and yields during an active artist service and 03:30–05:30 America/New_York. It maintains an exclusive playlist-run lock and checks the channel queue's terminal status and lock before each placement. Promotion hardlinks sidecars first and media last, with a per-file intent journal and a completed record written only after `ffprobe` succeeds. Each record contains paths, sizes, SHA-256 checksums, source playlist slot, and episode mapping. `verify --full-hash` checks completed records. `rollback --playlist-id ... --position ... --confirm-rollback` removes only checksum-matching files attributed to that one placement; it leaves original library files and other shared hardlinks intact.

## Review and dry checks

Local dry checks completed:

```text
python playlist_archive_import.py plan
# TOTAL: 749 positions, 736 IDs
python -m unittest -v test_playlist_archive_import.py
# 9 tests passed
```

Before any remote run, independently review the code and source mapping, place both the script and pinned inventory together on `rackmain1`, and run `check` and `plan`. The first controlled run should use `--limit 1`, verify the NFO/thumbnail/subtitle result and Plex episode identity, then resume the full baseline. The bot-verification blocker must already be resolved with one successful format probe. No service or timer should start until the six-channel queue has finished and the pilot is verified.

One compatibility point for the pilot: confirm the installed Plex NFO Series agent honors composite per-playlist episode GUIDs and doesn't merge two COPS positions that share a YouTube video ID. The files and source metadata are already disambiguated independently of the Plex GUID.
