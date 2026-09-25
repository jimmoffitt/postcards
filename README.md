# Postcards

Automated Bluesky photo feeds. Each feed is a Bluesky account that posts one photo at a time,
at fixed times of day, with accessibility alt text and hashtags built from the photo's location.

It runs as two parts:

- **Curation** runs on your laptop. It's a small local web app for reviewing photos, editing alt text
  and captions, and marking photos ready to post.
- **Posting** runs on an always-on machine, e.g. a Raspberry Pi. A daemon posts each feed's curated
  photos on schedule.

```
 photos ──► prepare_photos.py ──► build_metadata.py ──► regenerate_missing_alt_text.py
  (HEIC/JPG)   (convert, shrink)     (GPS, place, date)     (AI alt text, via Claude)
                                                                       │
                                                                       ▼
                     Pi: poster.py  ◄── sync_to_pi.sh ◄── curate.py (holding-pen → curated)
                (posts at 08:00/14:00/20:00)
```

**Quick reference:** [docs/CHEATSHEET.md](docs/CHEATSHEET.md) covers configuring streams, connecting to
the Pi, finding its IP, and managing services.

## Contents

- [Repository layout](#repository-layout)
- [Setup (laptop)](#setup-laptop)
- [Feeds: `feeds.json`](#feeds-feedsjson)
- [Adding photos](#adding-photos)
- [Curating](#curating)
- [Running the poster](#running-the-poster)
- [Syncing to the Pi](#syncing-to-the-pi)
- [Deploying to a Raspberry Pi](#deploying-to-a-raspberry-pi)
- [Day-to-day operations](#day-to-day-operations)
- [Troubleshooting](#troubleshooting)

## Repository layout

```
prepare_photos.py            one-off prep: HEIC → JPG, shrink to Bluesky's ~976 KB limit
generate_alt_text.py         batch alt-text generation (used by the script below)
requirements.txt             deps for the prep scripts (laptop only)
location_tags.json           custom hashtags for parts of a place, e.g. {"McIntosh": ["#McIntosh"]}
metadata_<Feed>.json         per-photo alt text, caption, place, GPS, date; not in git
geocode_cache.json           cached OpenStreetMap reverse-geocoding results; not in git
photos/<Feed>/               the photos themselves; not in git
    holding-pen/             not reviewed yet (new photos land here)
    curated/                 approved, and the poster only posts from here
    posted/                  already posted
samples/                     10 sample photos + metadata to try the app with (no GPS)
docs/CHEATSHEET.md           quick reference: streams, the Pi, services
app/
    upload_prep.py           strips photo metadata (GPS etc.) before upload
    feeds.json               feed definitions: on/off, hashtags, schedule, timezone
    poster.py                posting daemon
    curate.py                curation web app (+ static/curate.html)
    build_metadata.py        add metadata entries for new photos
    regenerate_missing_alt_text.py
    sync_to_pi.sh            laptop → Pi sync
    postcards-poster.service systemd unit for the Pi
    .local.env.example       template for secrets and paths (copy to .local.env)
    state/                   poster's schedule state + log of every post; not in git
    logs/                    not in git
```

A photo only moves forward: `holding-pen/` → `curated/` → `posted/`. Its folder is its status.
Nothing else records it.

## Setup (laptop)

Requires Python 3.9+.

```sh
git clone https://github.com/jimmoffitt/postcards.git && cd postcards

# the app: curation + poster
python3 -m venv app/.venv
app/.venv/bin/pip install -r app/requirements.txt

# the prep scripts (HEIC conversion, alt text); only needed to add photos
app/.venv/bin/pip install -r requirements.txt

cp app/.local.env.example app/.local.env   # then fill it in
```

`app/.local.env` holds, for each feed, `<FEED>_BSKY_HANDLE` and `<FEED>_BSKY_APP_PASSWORD`. The feed
name is uppercased, e.g. `POSTCARDSFROMHOME_BSKY_HANDLE`. Use a Bluesky **app password**
(Settings → Privacy and security → App passwords), never the account password.
The file is gitignored.

The alt-text step also needs an Anthropic API key in `ANTHROPIC_API_KEY`.

**Try it first with the sample photos:** `samples/` has five photos per feed with metadata, so you
can run the curation app and a dry run of the poster before adding your own photos. See
[samples/README.md](samples/README.md).

## Feeds: `feeds.json`

`app/feeds.json` is the one place a feed is defined:

```json
{
  "defaults": { "timezone": "America/Denver", "hashtags": ["#Postcards", "#Photography"] },
  "feeds": {
    "PostcardsFromHome": { "enabled": true },
    "SomePostcards":     { "enabled": false }
  }
}
```

A feed's settings come from three layers. The built-in defaults apply first, then the `defaults`
block overrides them, then the feed's own entry overrides both:

| Setting | Built-in default | Notes |
|---|---|---|
| `enabled` | `false` | |
| `hashtags` | `[]` | A feed's own list **replaces** the one in `defaults`; the two aren't merged |
| `timezone` | `UTC` | An IANA name. US Mountain time is `America/Denver`; use `America/Phoenix` for no daylight saving |
| `interval_hours` | `6` | Whole minutes, from 5 minutes to 24 hours |
| `window_start` / `window_end` | `8` / `22` | Whole hours, local time |
| `window_enabled` | `true` | If `false`, slots start at midnight and run all day |
| `order` | `seasonal` | `seasonal` or `random`; see below |
| `season_days` | `21` | For `seasonal`: how close, in days, a photo's calendar date must be to today's |
| `date_format` | `"Photo taken %Y-%m-%d"` | Last line of each post ([strftime](https://strftime.org) codes, e.g. `"Taken %B %Y"`); `null` to leave it off |

**Schedule.** Posts go out at fixed local times: `window_start`, then every `interval_hours` until
`window_end`. With the defaults that's **08:00, 14:00 and 20:00**, and the times stay on local time
through daylight-saving changes.

**Which photo.** With `"order": "seasonal"`, each slot picks at random from the curated photos
taken within `season_days` of today's calendar date, in any year. So in late September you get
September and October photos, whatever year they're from. When none are that close, the window
widens in `season_days` steps. Photos with no date go last. With `"order": "random"`, dates are
ignored. The date comes from the photo's EXIF capture time (`date_taken` in the metadata).

To keep posts in season, curate photos from around the current time of year: `--check` shows how
many queued photos are in season right now.

**Post text.** Each post is built when it's sent:

```
<caption typed in curate, if any; may include your own #hashtags>

<one tag per part of the place, most specific first> <feed hashtags>
Photo taken <date>
```

**Location tags** come from the photo's Place field, one tag per comma-separated part, in the
place's own order: most specific first, out to the state or country. "Longmont, CO" gives
`#Longmont #Colorado`: state abbreviations are expanded, and a leading "Near" is dropped.

To use a different tag for part of a place, add it to `location_tags.json`. The key is text to
match (case-insensitive) in one part of the place, and the value replaces that part's tag in the
same position. For example, `{"McIntosh": ["#McIntosh"]}` turns "McIntosh Reservoir, Longmont, CO"
into `#McIntosh #Longmont #Colorado` rather than `#McIntoshReservoir #Longmont #Colorado`. A list of
several tags inserts them all there, and an empty list `[]` drops that part's tag.

For example, a photo taken on 13 September 2020 whose place is "Longmont, CO" gets
`#Longmont #Colorado #Postcards #Photography`, then `Photo taken 2020-09-13` on the next line. A photo
with no date gets no date line. Tags you typed into the caption
aren't repeated. If a post would go over Bluesky's 300-character limit, location tags are dropped
first (from the end), then feed tags.

Hashtags aren't stored with the photos, so editing `feeds.json` or `location_tags.json` changes
every future post and needs no migration.

Unknown keys (typos like `"hashtgs"`), invalid timezones and out-of-range values are all rejected.
The running poster keeps its last good settings if an edit is invalid. Validate before syncing with
`app/.venv/bin/python3 app/poster.py --check`.

## Adding photos

1. Copy new photos (HEIC, JPG or PNG) into `photos/<Feed>/holding-pen/`.
2. Convert them to JPG and shrink them to fit Bluesky's image limit:
   ```sh
   app/.venv/bin/python3 prepare_photos.py --src photos/<Feed>/holding-pen --delete-originals
   ```
3. Add metadata entries. This reads GPS and date from EXIF and looks up the place name on
   OpenStreetMap (cached, 1 request/sec):
   ```sh
   cd app && .venv/bin/python3 build_metadata.py --account <Feed>
   ```
4. Generate alt text for anything that doesn't have it yet. This uses the Claude Message Batches
   API, so expect a few minutes:
   ```sh
   .venv/bin/python3 regenerate_missing_alt_text.py --account <Feed>
   ```

All three scripts can be run again safely. They skip work that's already done and never
overwrite your edits.

## Curating

```sh
cd app && .venv/bin/python3 curate.py     # then open http://127.0.0.1:5001
```

For each photo you can:

- Edit the alt text, caption, place and GPS.
- Look up the place from GPS.
- Check **Curated** to move the photo to `curated/`.
- Move it to another feed.
- Delete it.

The **Post preview** shows exactly what will be posted. **Sync locations → alt text** appends the
place name to alt text that doesn't mention it yet.

Changes are saved to `metadata_<Feed>.json`. These files hold GPS positions, so they're kept out of
git. `sync_to_pi.sh` copies them to the Pi, and the Pi keeps a dated copy of every version it
replaces in `.metadata-backups/`. Back up your laptop's copies too (Time Machine or similar): they
hold all your alt text and captions.

## Running the poster

```sh
cd app
.venv/bin/python3 poster.py --check                          # config, logins, queue size, schedule, a sample post
.venv/bin/python3 poster.py --once --dry-run                 # pick a photo and show the post; change nothing
.venv/bin/python3 poster.py --once --workers PostcardsFromHome  # post ONE photo now, off-schedule
.venv/bin/python3 poster.py                                  # the daemon: all enabled feeds, on schedule
```

`--once` also works for a feed that's still disabled, so you can test a new feed before turning it
on. A `--once` post doesn't shift the schedule.

**Photo metadata is removed before upload.** Bluesky stores uploaded images byte for byte, so
EXIF data (GPS location, camera, timestamps) would otherwise be downloadable by anyone. The poster
removes EXIF, XMP and IPTC data from the copy it uploads (`app/upload_prep.py`); files on disk
are never changed.

- Upright photos are stripped losslessly: the image data is untouched.
- Photos that rely on an EXIF rotation flag are rotated and re-encoded with their original
  compression settings.

Each result is checked to contain no metadata before it's posted. `--check` runs every queued photo
through this step.

The poster keeps two things in `app/state/`:

- `<Feed>.json`: the time of the feed's last post and last attempt. After a restart the poster
  never posts the same slot twice.
- `posted_log.jsonl`: one line per post, with its Bluesky URI. A photo listed here, or sitting in
  `posted/`, is never posted again, even if a sync copies it back into `curated/`.

## Syncing to the Pi

You curate on the laptop and post from the Pi. Run `app/sync_to_pi.sh` on the laptop whenever you've
curated or changed config. First set these in `app/.local.env`:

- `DEPLOY_HOST`: an SSH host, e.g. `jim@pi.local`, or a `Host` alias from `~/.ssh/config`. Prefer the
  `.local` hostname to an IP address: it keeps working if the router gives the Pi a new address.
  The [cheat sheet](docs/CHEATSHEET.md#finding-the-pis-ip-address) covers what to do if it doesn't resolve.
- `DEPLOY_DIR`: optional. The default is `projects/postcards`, relative to the Pi user's home, which
  mirrors `~/projects/postcards` on the laptop.

```sh
app/sync_to_pi.sh --dry-run    # see what would change
app/sync_to_pi.sh              # do it
app/sync_to_pi.sh --restart    # ...and restart the poster (needed after code changes)
```

It works in three steps:

1. **Pull posted photos back.** It copies the Pi's `posted/` folder to the laptop and deletes the
   laptop's leftover copies, so a posted photo can't be curated again.
2. **Code and metadata.** It refuses to run with uncommitted changes. Then it pushes the current
   commit straight from the laptop to a git repo on the Pi over SSH. This carries code,
   `feeds.json` and `location_tags.json`. GitHub isn't involved, so the Pi never needs GitHub
   access and you don't have to push first. The Pi's repo uses
   `receive.denyCurrentBranch=updateInstead`, so its files update in place. The first sync creates
   the repo. Next it copies `metadata_<Feed>.json`, which isn't in git.
3. **Push photos.** It makes the Pi's `holding-pen/` and `curated/` match the laptop's, deletes
   included. The laptop is the source of truth for curation. `posted/` is only ever added to.

`feeds.json` edits apply on the Pi within a minute of a sync, with no restart. That covers
hashtags, schedule, timezone, and pausing or resuming a running feed. A code change, or a feed that
was disabled when the poster started, needs `--restart`.

## Deploying to a Raspberry Pi

Any always-on Linux machine works. These notes assume Raspberry Pi OS (Bookworm or Trixie) on a Pi 3 or newer.
The poster is light: one Python process that wakes every 30 seconds, and a network call only when it
posts.

### Layout on the Pi

The Pi mirrors the laptop:

```
~/projects/postcards/                  ← the whole repo, created by the first sync
├── app/                               code, feeds.json, .venv/, .local.env, state/, logs/
├── metadata_*.json                    copied by sync (not in git); old versions in .metadata-backups/
├── location_tags.json
└── photos/
    ├── PostcardsFromHome/{holding-pen,curated,posted}/
    └── SomePostcards/{holding-pen,curated,posted}/
```

Only the feed folders listed in `feeds.json` are synced. Anything else under `photos/` stays on
the laptop.

### First-time setup

On the Pi, install the tools:

```sh
sudo apt update && sudo apt install -y git rsync python3-venv
```

From the laptop, create the repo and copy the photos over (about 500 MB the first time):

```sh
app/sync_to_pi.sh --dry-run && app/sync_to_pi.sh
```

Back on the Pi, set up Python and the secrets:

```sh
cd ~/projects/postcards/app
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .local.env.example .local.env && nano .local.env     # app passwords; PHOTOS_ROOT=../photos
chmod 600 .local.env
```

### Go live, one feed at a time

```sh
cd ~/postcards/app
.venv/bin/python3 poster.py --check
.venv/bin/python3 poster.py --once --dry-run --workers PostcardsFromHome
.venv/bin/python3 poster.py --once --workers PostcardsFromHome   # one real post: check it on Bluesky
```

Look at the post in the Bluesky app. Check the aspect ratio, the alt text (the ALT badge), and that
the hashtags are clickable. Then install the service:

```sh
sudo cp postcards-poster.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now postcards-poster
journalctl -u postcards-poster -f          # expect: "starting -- posts at 08:00, 14:00, 20:00 ..."
```

To add the next feed:

1. Put its credentials in the Pi's `.local.env`.
2. Test it with `--once --workers <Feed>`.
3. Set `"enabled": true` in `feeds.json`, commit, and run `app/sync_to_pi.sh --restart`.

### Tips

- **Username and paths.** `postcards-poster.service` assumes `User=jim` and
  `/home/jim/projects/postcards`. Newer Raspberry Pi OS images have no default `pi` user; you pick
  one at setup. Edit `User=`, `WorkingDirectory=`, `EnvironmentFile=` and `ExecStart=` to match
  your user, and set `DEPLOY_DIR`
  to match on the laptop.
- **Clock.** Posting is tied to the wall clock, so make sure NTP is working:
  `timedatectl` should say `System clock synchronized: yes`. A Pi has no real-time clock battery,
  so its clock is wrong after boot until the network comes up. The service waits for
  `network-online.target` for this reason. The Pi's own timezone doesn't matter, because
  `feeds.json` sets it.
- **Power cuts and reboots.** A slot missed while the Pi was down is still posted if the Pi is back
  within an hour. After that the slot is skipped and logged as "missed". The limit is
  `LATE_GRACE` in `poster.py`. `Restart=on-failure` brings the daemon back after a crash.
- **Back up `app/state/`.** `posted_log.jsonl` is the duplicate-post guard. Losing it isn't
  serious, because `posted/` is a second guard, but it's also your only record of the posts'
  Bluesky URIs.
- **SD card wear.** The only regular writes are a small state file per post and a few log lines.
  `app/logs/poster.log` is never rotated, but it grows by only a few KB a day. To avoid it, set
  `LOG_PATH=/dev/null` in `.local.env` and use journald only.
- **SSH.** Use key-based SSH (`ssh-copy-id jim@pi.local`) so `sync_to_pi.sh` doesn't prompt
  for a password on every step.
- **Don't edit files on the Pi.** Tracked files are changed only by syncing from the laptop.
  If a tracked file is edited on the Pi, the next sync's git push is refused until you revert it
  (`git -C ~/projects/postcards checkout -- .`). `app/.local.env`, `state/`, `logs/` and `photos/`
  aren't tracked, so they're safe.
- **Access to the curation app.** It's for the laptop and listens on `127.0.0.1` only. If you ever
  run it on the Pi, reach it with an SSH tunnel (`ssh -L 5001:localhost:5001 jim@pi.local`)
  rather than binding `0.0.0.0`: it has no authentication and can delete photos.
- **Updating.** Commit on the laptop, then run `app/sync_to_pi.sh --restart`. If `requirements.txt`
  changed, also run `.venv/bin/pip install -r requirements.txt` on the Pi.

## Day-to-day operations

| I want to… | Do this |
|---|---|
| See what's queued and when the next post is | `poster.py --check` (on the Pi) |
| Watch the poster | `journalctl -u postcards-poster -f` |
| See what's been posted | `app/state/posted_log.jsonl` on the Pi |
| Change hashtags, times or timezone | Edit `feeds.json`, commit, `sync_to_pi.sh` |
| Pause a feed | Set `"enabled": false`, commit, `sync_to_pi.sh` (applies within a minute) |
| Add more photos | [Adding photos](#adding-photos), curate, `sync_to_pi.sh` |
| Stop everything | `sudo systemctl stop postcards-poster` |
| Post one photo right now | `poster.py --once --workers <Feed>` |

When a feed's `curated/` folder is empty, the poster logs "nothing to do" at each slot and carries on.
Curate more photos and sync, and posting resumes at the next slot.

## Troubleshooting

- **Login failed / `AuthenticationRequired`**: wrong handle or app password in `.local.env`.
  Bluesky allows only about 10 failed logins a day, so fix the password before retrying.
  `--check` won't attempt a login while the password is still the example placeholder.
- **`feeds.json is invalid, keeping previous settings`**: the log line names the problem. Fix it and
  sync again.
- **`missed the HH:MM slot`**: the Pi was down or the clock jumped. Posting resumes at the next slot.
- **`is in curated/ but was already posted -- skipping`**: a sync copied back a photo the Pi had
  already posted. It's harmless, and the next sync cleans it up.
- **`skipping <photo>: ...`**: that photo couldn't be prepared for upload (e.g. it's over
  Bluesky's size limit). Another photo is posted instead. Run `prepare_photos.py` on it again, or
  move it back to the holding pen.
- **Posts at the wrong hour**: check `timezone` in `feeds.json` and the Pi's clock (`timedatectl`).

## License

MIT. See [LICENSE](LICENSE).
