#!/usr/bin/env bash
# One-command release of the YouTube importers on rackmain1 (alias: importers-release).
#
#   deploy/rackmain1-release.sh          # fetch main, test, plan, ask, apply, verify
#   deploy/rackmain1-release.sh --yes    # no confirmation prompt
#   deploy/rackmain1-release.sh --plan   # show the plan only
#
# Refuses while any importer unit is running and never starts one. A live file
# is replaced only when it matches some version of that file in this repo's
# history, so edits made on the host are never overwritten. Each replaced file
# is backed up and an undo command printed.
main() {
  set -uo pipefail
  local repo; repo="$(cd "$(dirname "$0")/.." && pwd)"
  local releases="$HOME/importer-releases"
  local mode="ask"
  case "${1:-}" in
    "") ;; --yes|-y) mode="yes" ;; --plan) mode="plan" ;;
    -h|--help) sed -n '2,11p' "$0"; return 0 ;;
    *) echo "Unknown option: $1 (use --yes, --plan, or --help)" >&2; return 2 ;;
  esac
  say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
  fail() { printf '\n\033[1;31mSTOPPED: %s\033[0m\n' "$*" >&2; exit 1; }
  [ "$(id -un)" != "root" ] || fail "run as ap3x, not root"
  export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -i $HOME/.ssh/ap3x_youtube_importers -o IdentitiesOnly=yes}"

  # repo file | live file
  local payload=(
    "playlists/playlist_archive_import.py|$HOME/AP3X_YOUTUBE_PLAYLISTS_20261014/playlist_archive_import.py"
    "mianite/playlist_archive_import.py|$HOME/AP3X_MIANITE_STREAMS_20261014/playlist_archive_import.py"
    "mianite/mianite_stream_import.py|$HOME/AP3X_MIANITE_STREAMS_20261014/mianite_stream_import.py"
    "channels/channel_archive_import.py|$HOME/AP3X_YOUTUBE_CHANNELS_20261014/channel_archive_import.py"
  )

  if [ -z "${AP3X_RELEASE_REEXEC:-}" ]; then
    say "Fetching latest main"
    git -C "$repo" fetch --quiet origin main || fail "could not fetch from GitHub"
    git -C "$repo" checkout --quiet --detach origin/main || fail "could not check out origin/main"
    # Continue from the fetched copy of this script so a newer payload list applies.
    AP3X_RELEASE_REEXEC=1 exec bash "$repo/deploy/rackmain1-release.sh" "$@"
  fi
  local sha; sha="$(git -C "$repo" rev-parse --short HEAD)"
  echo "Commit: $(git -C "$repo" log -1 --format='%h %s' HEAD)"

  say "Safety checks"
  local unit state
  for unit in mianite-stream-import youtube-channel-import youtube-playlist-import artist-archive-import; do
    state="$(systemctl --user show -p ActiveState --value "$unit.service" 2>/dev/null)"
    case "$state" in activating|active|reloading|deactivating) fail "$unit.service is $state; release only while every importer is stopped" ;; esac
    echo "$unit.service: ${state:-unknown}"
  done
  cmp -s "$repo/mianite/playlist_archive_import.py" "$repo/playlists/playlist_archive_import.py" ||
    fail "mianite/ and playlists/ copies of playlist_archive_import.py differ; keep them identical"

  say "Tests"
  (cd "$repo" && AP3X_PROGRESS_LIB="$HOME/.local/lib/ap3x" python3 -m unittest tests.test_wall_progress -q 2>&1 | tail -1) | grep -q '^OK' || fail "wall progress tests failed"
  echo "wall progress: OK"
  local dir
  for dir in mianite channels playlists; do
    if (cd "$repo/$dir" && python3 -m unittest discover -p 'test_*.py' -q 2>&1 | tail -1) | grep -q '^OK'; then echo "$dir: OK"
    else fail "$dir importer tests failed; if an inventory JSON is missing, copy it in from the live folder (README). Details: cd $repo/$dir && python3 -m unittest discover -p 'test_*.py'"; fi
  done

  say "Plan"
  local changes=() line src live
  for line in "${payload[@]}"; do
    IFS='|' read -r src live <<<"$line"
    [ -f "$live" ] || fail "live file missing: $live"
    local new have known
    new="$(git -C "$repo" hash-object "$repo/$src")"
    have="$(git -C "$repo" hash-object "$live")"
    if [ "$have" = "$new" ]; then echo "CURRENT   $live"; continue; fi
    known="$(git -C "$repo" log --format=%H HEAD -- "$src" | while read -r c; do git -C "$repo" rev-parse -q --verify "$c:$src" 2>/dev/null; done | grep -cx "$have")"
    [ "$known" -gt 0 ] || fail "$live differs from every version in this repo (edited on the host?); review it first. Nothing changed."
    python3 -m py_compile "$repo/$src" || fail "$src does not compile"
    echo "REPLACE   $live"; changes+=("$line")
  done
  echo "${#changes[@]} change(s) from $sha"
  [ "$mode" = "plan" ] && { say "Plan only; nothing applied"; return 0; }
  [ "${#changes[@]}" -gt 0 ] || { say "Already current"; return 0; }
  if [ "$mode" = "ask" ]; then
    printf '\nApply these %s change(s)? Importers are not started. [y/N] ' "${#changes[@]}"
    local reply; read -r reply
    case "$reply" in y|Y|yes|YES) ;; *) say "Cancelled; nothing applied"; return 0 ;; esac
  fi

  say "Applying"
  local backup; backup="$releases/backups/$(date -u +%Y%m%dT%H%M%SZ)-$sha"
  install -d -m 700 "$backup"
  local undo=""
  for line in "${changes[@]}"; do
    IFS='|' read -r src live <<<"$line"
    local name tmp; name="$(echo "$src" | tr / _)"
    cp -p "$live" "$backup/$name" || fail "backup failed: $live"
    undo+="cp -p '$backup/$name' '$live'; "
    tmp="$(mktemp "$(dirname "$live")/.release.XXXXXX")"
    cp "$repo/$src" "$tmp" && chmod --reference="$live" "$tmp" && mv "$tmp" "$live" || { rm -f "$tmp"; fail "could not replace $live; to undo so far: $undo"; }
    echo "installed $live"
  done
  printf '%s\n' "$undo" > "$backup/UNDO.sh"

  say "Verifying"
  for line in "${payload[@]}"; do
    IFS='|' read -r src live <<<"$line"
    [ "$(git -C "$repo" hash-object "$live")" = "$(git -C "$repo" hash-object "$repo/$src")" ] || fail "$live does not match $src after install. To undo: $undo"
    echo "verified $live"
  done
  say "Done: $sha installed (nothing was started)"
  echo "To undo:  $undo"
  echo "(also saved in $backup/UNDO.sh)"
}

main "$@"
exit
