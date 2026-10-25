#!/usr/bin/env bash
set -euo pipefail

project=/home/ap3x/AP3X_LOFI_GIRL_20261011
state=/home/ap3x/.local/share/ap3x-lofi-girl
python="$state/venv/bin/python"
manifest="$state/releases.json"

export PATH="/home/ap3x/.deno/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PYTHONUNBUFFERED=1
cd "$project"

printf 'START artist release update %s\n' "$(date -Is)"
finish() {
    result=$?
    printf 'END artist release update %s exit=%s\n' "$(date -Is)" "$result"
}
trap finish EXIT

while :; do
    tokyo_markers=$(find '/home/md-media/media/Music/LoFi Tokyo' -mindepth 2 -maxdepth 2 \
        -type f -name '.ap3x-album.json' | wc -l)
    if [ "$tokyo_markers" -ge 153 ] && ! tmux has-session -t lofi-resume-20261010 2>/dev/null; then
        break
    fi
    printf 'WAITING: LoFi Tokyo has %s/153 album markers; checking again in 5 minutes.\n' "$tokyo_markers"
    sleep 300
done

printf 'LoFi Tokyo has 153 album markers and its tmux session has ended; scanning artist releases.\n'

"$python" "$project/catalog_registry.py" --scan --manifest "$manifest" --registry "$project/sources.json"
"$python" "$project/import_releases.py" --download --manifest "$manifest" --player-client mweb
