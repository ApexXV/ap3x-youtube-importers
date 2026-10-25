# AP3X YouTube importers

Source of the rackmain1 yt-dlp importers that feed Plex's YouTube libraries, imported from the live host on 2026-10-25. The live copies still run from their original folders; this repository is their reviewed history.

| Folder | Live folder | Unit |
| --- | --- | --- |
| `mianite/` | `~/AP3X_MIANITE_STREAMS_20261014/` | `mianite-stream-import.service` |
| `channels/` | `~/AP3X_YOUTUBE_CHANNELS_20261014/` | `youtube-channel-import.service` |
| `playlists/` | `~/AP3X_YOUTUBE_PLAYLISTS_20261014/` | `youtube-playlist-import.service` |
| `artist/` | `~/AP3X_LOFI_GIRL_20261011/` | `artist-archive-import.service` (timer disabled) |

`mianite/playlist_archive_import.py` and `playlists/playlist_archive_import.py` are the same file; Mianite imports it for its download helpers. Keep them identical. The `*.service.txt` files are the current user units. Cookies, inventories, records, logs, and Plex data are not in this repository.

## Wall progress

The three video importers report live status to the Command 57 wall through `ap3x_progress.py` from `ap3x-wall-data-collectors` (installed to `~/.local/lib/ap3x/`). They write `<state dir>/current.json` with only a phase (`scanning`, `waiting`, `probing`, `downloading`, `verifying`, `publishing`, `complete`, `failed`), the item position and counts, and download bytes, speed, and ETA read from the last progress line of the yt-dlp attempt log. A stop is recorded as its class only (`bot_or_429`, `unavailable`, `transient`, `low_space`, `stopped`). The yt-dlp commands, retries, locks, music-window gates, and record handling are unchanged. A missing helper, any write error, or `AP3X_PROGRESS=off` in the unit environment turns reporting off without affecting the import.

Install with `deploy/install-wall-progress.sh` (or `--check` first). It refuses while any importer unit is running, replaces a live file only if it still matches the originally imported version, backs up every file it replaces, runs the progress tests, and prints the restore command. It starts nothing.

## Tests

`tests/test_wall_progress.py` runs the real download monitor against a fake yt-dlp and checks that a missing or broken helper is harmless. The per-importer tests need the inventory JSON files from the live folders, which are not copied here.
