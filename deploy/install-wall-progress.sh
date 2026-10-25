#!/usr/bin/env bash
# Install the reviewed importer files (wall progress hooks) into the live importer folders on rackmain1.
#
#   cd ~/src/ap3x-youtube-importers && GIT_SSH_COMMAND="ssh -i ~/.ssh/ap3x_youtube_importers -o IdentitiesOnly=yes" git pull --ff-only
#   deploy/install-wall-progress.sh            # check, back up, replace
#   deploy/install-wall-progress.sh --check    # check only; change nothing
#
# Refuses while any importer unit is running. Replaces a live file only when it
# is byte-identical to the version first imported into this repo (BASE), so
# edits made on the host since then are never overwritten. Backs up each file
# before replacing it and prints the restore command. Starts nothing.
set -uo pipefail

BASE=352341222292fe74472865277569044d1b622b1c
repo="$(cd "$(dirname "$0")/.." && pwd)"
mode="${1:-install}"
say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
stop() { printf '\n\033[1;31mSTOPPED: %s\033[0m\n' "$*" >&2; exit 1; }
[ "$(id -un)" != "root" ] || stop "run as ap3x, not root"

declare -A FILES=(
  [playlists/playlist_archive_import.py]="$HOME/AP3X_YOUTUBE_PLAYLISTS_20261014/playlist_archive_import.py"
  [mianite/playlist_archive_import.py]="$HOME/AP3X_MIANITE_STREAMS_20261014/playlist_archive_import.py"
  [mianite/mianite_stream_import.py]="$HOME/AP3X_MIANITE_STREAMS_20261014/mianite_stream_import.py"
  [channels/channel_archive_import.py]="$HOME/AP3X_YOUTUBE_CHANNELS_20261014/channel_archive_import.py"
)

say "Safety checks"
for unit in mianite-stream-import youtube-channel-import youtube-playlist-import artist-archive-import; do
  state="$(systemctl --user show -p ActiveState --value "$unit.service" 2>/dev/null)"
  case "$state" in activating|active|reloading|deactivating) stop "$unit.service is $state; install only while every importer is stopped" ;; esac
  echo "$unit.service: ${state:-unknown}"
done
lib="$HOME/.local/lib/ap3x/ap3x_progress.py"
[ -f "$lib" ] || stop "$lib missing; run ops/install-media-collectors.sh from ap3x-wall-data-collectors first"
echo "progress helper: present"
git -C "$repo" cat-file -e "$BASE^{commit}" 2>/dev/null || stop "base commit $BASE not in this clone (run git pull)"

say "Files"
pending=()
for src in "${!FILES[@]}"; do
  live="${FILES[$src]}"
  [ -f "$live" ] || stop "live file missing: $live"
  new="$(sha256sum < "$repo/$src" | cut -d' ' -f1)"
  have="$(sha256sum < "$live" | cut -d' ' -f1)"
  base="$(git -C "$repo" show "$BASE:$src" | sha256sum | cut -d' ' -f1)"
  if [ "$have" = "$new" ]; then echo "CURRENT   $live"
  elif [ "$have" = "$base" ]; then echo "REPLACE   $live"; pending+=("$src")
  else stop "$live changed on the host since it was imported; review it before installing (nothing replaced)"
  fi
  python3 -m py_compile "$repo/$src" || stop "$src does not compile"
done

say "Offline test"
(cd "$repo" && AP3X_PROGRESS_LIB="$HOME/.local/lib/ap3x" python3 -m unittest tests.test_wall_progress -q) || stop "wall progress tests failed"

[ "$mode" = "--check" ] && { say "Check only; nothing changed"; exit 0; }
[ "${#pending[@]}" -gt 0 ] || { say "Already installed"; exit 0; }

say "Installing"
backup="$HOME/.local/share/ap3x-importer-backups/$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 700 "$backup"
restore=""
for src in "${pending[@]}"; do
  live="${FILES[$src]}"
  name="$(echo "$src" | tr / _)"
  cp -p "$live" "$backup/$name" || stop "backup failed for $live"
  tmp="$(mktemp "$(dirname "$live")/.wall-progress.XXXXXX")"
  cp "$repo/$src" "$tmp" && chmod --reference="$live" "$tmp" && mv "$tmp" "$live" || { rm -f "$tmp"; stop "could not replace $live (backup: $backup)"; }
  echo "installed $live"
  restore+="cp -p '$backup/$name' '$live'; "
done
say "Done"
echo "Backups: $backup"
echo "To undo:  $restore"
echo "The next scheduled or manual importer run reports live progress; nothing was started."
