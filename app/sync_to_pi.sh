#!/usr/bin/env bash
# Sync curation work from the Mac to the Pi. Run on the Mac, any time.
#
#   1. Pull the Pi's posted/ photos back, and drop the Mac's now-stale copies
#      of them from curated/ and holding-pen/ (so they can't be re-curated).
#   2. Push code, feeds.json and location_tags.json with git, straight from
#      here to a repo on the Pi over ssh (no GitHub involved; the Pi's
#      checkout updates in place). The first run creates that repo. Refuses
#      to run with uncommitted changes, so the Pi only ever runs committed
#      code. Then copy the photo metadata (metadata_<Feed>.json -- not in
#      git, since it holds GPS positions); the Pi keeps a dated copy of any
#      version it replaces in .metadata-backups/.
#   3. Mirror the Mac's holding-pen/ and curated/ onto the Pi (the Mac is the
#      source of truth for curation decisions, including deletes). posted/ is
#      only ever added to, never deleted from, in either direction.
#
# Hashtag and schedule edits in feeds.json take effect on the Pi within a
# minute of step 2 -- no restart. Code changes, or enabling a feed, need
# --restart.
#
# Even if a photo is posted on the Pi mid-sync and step 3 copies it back into
# curated/, poster.py won't post it again (it checks posted/ and its log).
#
# Usage: ./sync_to_pi.sh [--dry-run] [--restart]
# Needs DEPLOY_HOST (and optionally DEPLOY_DIR) in .local.env.

set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$APP_DIR")"

DRY_RUN=0
RESTART=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --restart) RESTART=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

env_value() {  # read one KEY=value from .local.env without sourcing it
  { grep -E "^$1=" "$APP_DIR/.local.env" 2>/dev/null || true; } | tail -1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//'
}

DEPLOY_HOST="${DEPLOY_HOST:-$(env_value DEPLOY_HOST)}"
DEPLOY_DIR="${DEPLOY_DIR:-$(env_value DEPLOY_DIR)}"
DEPLOY_DIR="${DEPLOY_DIR:-projects/postcards}"  # relative = under the Pi user's home
if [[ -z "$DEPLOY_HOST" ]]; then
  echo "Set DEPLOY_HOST (e.g. pi@raspberrypi.local) in app/.local.env" >&2
  exit 1
fi

LOCAL_PHOTOS="$(cd "$APP_DIR" && python3 -c 'import config; config.load_dotenv(); print(config.app_path("PHOTOS_ROOT", "../photos"))')"
REMOTE_PHOTOS="$DEPLOY_DIR/photos"
FEEDS="$(cd "$APP_DIR" && python3 -c 'import config; print(" ".join(config.load_feeds()))')"  # also validates feeds.json

RSYNC=(rsync -a --itemize-changes --include='*.jpg' --include='*.jpeg' --include='*.JPG' --include='*.JPEG' --exclude='*')
(( DRY_RUN )) && RSYNC+=(--dry-run)
run() { if (( DRY_RUN )); then echo "  [dry-run] $*"; else "$@"; fi; }

echo "Mac photos: $LOCAL_PHOTOS"
echo "Pi:         $DEPLOY_HOST:$DEPLOY_DIR"
echo "Feeds:      $FEEDS"

for feed in $FEEDS; do
  if [[ ! -d "$LOCAL_PHOTOS/$feed" ]]; then
    # Guard: a wrong PHOTOS_ROOT would otherwise mirror empty folders over the Pi's.
    echo "Missing $LOCAL_PHOTOS/$feed -- refusing to sync (check PHOTOS_ROOT, or create it for a new feed)" >&2
    exit 1
  fi
  mkdir -p "$LOCAL_PHOTOS/$feed"/{holding-pen,curated,posted}
done

if [[ -n "$(git -C "$ROOT" status --porcelain --untracked-files=no)" ]]; then
  echo "Uncommitted changes in the repo -- commit them first (the Pi only runs committed state):" >&2
  git -C "$ROOT" status --short --untracked-files=no >&2
  exit 1
fi

echo
echo "== 1. Pull posted photos back from the Pi"
for feed in $FEEDS; do
  ssh "$DEPLOY_HOST" "mkdir -p '$REMOTE_PHOTOS/$feed/posted'"
  "${RSYNC[@]}" "$DEPLOY_HOST:$REMOTE_PHOTOS/$feed/posted/" "$LOCAL_PHOTOS/$feed/posted/"
  # In dry-run the pull above didn't happen, so ask the Pi for its list.
  if (( DRY_RUN )); then
    posted_list="$( { ssh "$DEPLOY_HOST" "ls '$REMOTE_PHOTOS/$feed/posted'"; ls "$LOCAL_PHOTOS/$feed/posted"; } | sort -u)"
  else
    posted_list="$(ls "$LOCAL_PHOTOS/$feed/posted")"
  fi
  while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    for sub in curated holding-pen; do
      if [[ -f "$LOCAL_PHOTOS/$feed/$sub/$f" ]]; then
        echo "  $feed: $f was posted -- removing stale copy from $sub/"
        run rm "$LOCAL_PHOTOS/$feed/$sub/$f"
      fi
    done
  done <<< "$posted_list"
done

echo
echo "== 2. Code via git (laptop -> Pi, directly), then photo metadata"
# updateInstead: a push to the Pi's checked-out branch updates its files too.
# It refuses if the Pi has uncommitted edits to tracked files, which is what
# we want -- the laptop is the only place code is changed.
run ssh "$DEPLOY_HOST" "mkdir -p '$DEPLOY_DIR' && cd '$DEPLOY_DIR' && { [ -d .git ] || git init -q -b main; } && git config receive.denyCurrentBranch updateInstead"
run git -C "$ROOT" push "$DEPLOY_HOST:$DEPLOY_DIR" HEAD:main

metadata_files=()
for feed in $FEEDS; do
  if [[ -f "$ROOT/metadata_$feed.json" ]]; then
    metadata_files+=("$ROOT/metadata_$feed.json")
  else
    echo "  warning: no metadata_$feed.json -- the Pi can't post $feed photos without it" >&2
  fi
done
if (( ${#metadata_files[@]} )); then
  DATA_RSYNC=(rsync -a --itemize-changes --checksum --backup --backup-dir=".metadata-backups/$(date +%Y%m%d-%H%M%S)")
  (( DRY_RUN )) && DATA_RSYNC+=(--dry-run)
  "${DATA_RSYNC[@]}" "${metadata_files[@]}" "$DEPLOY_HOST:$DEPLOY_DIR/"
fi

echo
echo "== 3. Push curation state to the Pi"
for feed in $FEEDS; do
  for sub in holding-pen curated; do
    ssh "$DEPLOY_HOST" "mkdir -p '$REMOTE_PHOTOS/$feed/$sub'"
    "${RSYNC[@]}" --delete "$LOCAL_PHOTOS/$feed/$sub/" "$DEPLOY_HOST:$REMOTE_PHOTOS/$feed/$sub/"
  done
  "${RSYNC[@]}" --ignore-existing "$LOCAL_PHOTOS/$feed/posted/" "$DEPLOY_HOST:$REMOTE_PHOTOS/$feed/posted/"
done

if (( RESTART )); then
  echo
  echo "== 4. Restart poster"
  run ssh -t "$DEPLOY_HOST" "sudo systemctl restart postcards-poster && systemctl --no-pager status postcards-poster | head -5"
fi

echo
echo "Done.$( (( DRY_RUN )) && echo ' (dry run -- nothing changed)')"
