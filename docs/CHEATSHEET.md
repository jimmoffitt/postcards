# Cheat sheet

The commands used most often, in one place. The [README](../README.md) has the full explanation.
Examples assume the Pi's hostname is `pi`, its user is `jim`, and the repo is at
`~/projects/postcards`. Adjust for your setup.

- [Configuring streams](#configuring-streams)
- [Curating and syncing](#curating-and-syncing)
- [Connecting to the Pi](#connecting-to-the-pi)
- [Finding the Pi's IP address](#finding-the-pis-ip-address)
- [Services (systemd)](#services-systemd)
- [The postcards service](#the-postcards-service)
- [Pi health and upkeep](#pi-health-and-upkeep)

---

## Configuring streams

Everything is in `app/feeds.json`. Edit it on the laptop, commit, and run `app/sync_to_pi.sh`.
Changes apply on the Pi within a minute. No restart is needed unless noted.

```json
{
  "defaults": { "timezone": "America/Denver", "hashtags": ["#Postcards", "#Photography"] },
  "feeds": {
    "PostcardsFromHome": { "enabled": true },
    "SomePostcards":     { "enabled": false }
  }
}
```

A feed's own entry overrides `defaults`, which overrides the built-in defaults: UTC, every 6 h,
from 8 to 22.

| To… | Set |
|---|---|
| Pause / resume a stream | `"enabled": false` / `true` (a stream that was off when the poster started needs `sync_to_pi.sh --restart`) |
| Change the shared hashtags | `defaults.hashtags` |
| Give one stream its own hashtags | `"hashtags": [...]` in that feed (this **replaces** the defaults list; they aren't merged) |
| Change the local time zone | `defaults.timezone`, e.g. `America/Denver`, `America/Phoenix` (no DST), `UTC` |
| Post at 08:00 14:00 20:00 | nothing; this is the default |
| Post at 08:00 12:00 16:00 20:00 | `"interval_hours": 4` |
| Post twice a day at 09:00 and 17:00 | `"window_start": 9, "window_end": 18, "interval_hours": 8` |
| Post once a day at noon | `"window_start": 12, "window_end": 13, "interval_hours": 24` |
| Post photos taken on today's date (any year) first, else random | nothing; `"order": "on_this_day"` is the default |
| Prefer photos from this time of year | `"order": "seasonal"` (±21 days, any year) |
| Loosen / tighten "this time of year" | `"season_days": 45` / `"season_days": 10` (with `seasonal`) |
| Ignore dates, pick at random | `"order": "random"` |
| Don't repeat a state/country for 3 posts | `"region_cooldown": 3` (default 2; `0` turns it off) |
| Post a particular photo next | curate app: set its status to **Up next** (moves it to `up-next/`), then sync |
| Set the order Up next photos go out | curate app: Status **Up next**, then **▲ ▼ / Top** on each row (top posts first) |
| Post 2–4 photos together in one post | curate app: tick them in the list, click **Group**, write the group's message |
| Change the "Photo taken 2020-09-13" line | `"date_format": "Taken %B %Y"` → `Taken September 2020`; `null` to leave it off |

Posts go out at `window_start`, then every `interval_hours` until `window_end`.
`poster.py --check` prints each stream's resulting times.

**Post text:** `<caption, if any>` + blank line + `<place tags, most specific first> <feed hashtags>`
+ new line + `Photo taken <date>`, e.g.

```
#Longmont #Colorado #Postcards #Photography
Photo taken 2020-09-13
```

- **Place tags** come from each photo's Place field, which you edit in the curate app.
- **Custom tag for part of a place:** add it to `location_tags.json`. It replaces the matching
  part's tag in the same position. `{"McIntosh": ["#McIntosh"]}` turns "McIntosh Reservoir, Longmont, CO"
  into `#McIntosh #Longmont #Colorado`. Matching is case-insensitive substring; `[]` drops that part.
- **One photo:** type a caption and/or hashtags into its caption box in the curate app.

**Add a new stream:**

1. Add an entry to `feeds.json`.
2. Create the folder `photos/<Name>/`.
3. Put `<NAME>_BSKY_HANDLE` and `<NAME>_BSKY_APP_PASSWORD` in the Pi's `app/.local.env`.
4. Test with `poster.py --once --workers <Name>`.
5. Set `"enabled": true` and run `sync_to_pi.sh --restart`.

**Typos are safe.** An invalid `feeds.json` (unknown key, bad time zone, bad value) is rejected
with a clear message, and the running poster keeps its previous settings. Check before syncing:
`app/.venv/bin/python3 app/poster.py --check`.

---

## Curating and syncing

You curate on the laptop, and the Pi posts. `app/sync_to_pi.sh` moves work in both directions, so
the routine is **sync, curate, sync**:

```sh
~/projects/postcards/app/sync_to_pi.sh                       # 1. pull back what the Pi has posted
cd ~/projects/postcards/app && .venv/bin/python3 curate.py   # 2. curate at http://127.0.0.1:5001 (Ctrl+C to stop)
~/projects/postcards/app/sync_to_pi.sh                       # 3. send your changes to the Pi
```

1. **Sync first.** Photos the Pi has posted since your last sync move into `posted/` on the laptop.
   That keeps the curate app and its "days remain" banner accurate, and you won't re-curate
   something that already went out.
2. **Curate.** For each photo, check the alt text and place, then set its status: **Curated** joins
   the queue, and **Up next** goes out before anything in Curated. The **Post preview** shows the exact
   post. When the banner's "days remain" gets low, it's time for another session.
3. **Sync again.** Newly curated and up-next photos join the Pi's queue. Anything you un-curate or
   delete leaves it, and your alt-text, caption and place edits go over too. The poster uses them at
   its next slot, with no restart. The Pi keeps a dated copy of every metadata version it replaces
   in `.metadata-backups/`.

**When to commit before syncing:**

| You changed… | Before syncing |
|---|---|
| Photos, alt text, captions, places (in the curate app) | nothing; just sync (these aren't in git) |
| `feeds.json` or `location_tags.json` | commit; the sync refuses uncommitted changes |
| Code | commit, then `sync_to_pi.sh --restart` |

**Good to know:**

- **Sync any time.** Even if the Pi posts a photo mid-sync, it won't be posted twice, and the next
  sync tidies up.
- **The laptop is in charge.** Don't curate or edit files on the Pi: the next sync overwrites them.
  The Pi only posts and reports back what it posted.
- **Preview a sync** with `sync_to_pi.sh --dry-run`.
- **Back up the laptop** (Time Machine or similar). Your alt text and captions live in
  `metadata_*.json`, which isn't in git. The Pi's copy is the only other one.
- **See what posted:** `ssh pi 'journalctl -u postcards-poster --since today -o cat'`.

---

## Connecting to the Pi

```sh
ssh jim@pi.local                     # by hostname; works on most home networks (mDNS)
ssh jim@192.168.1.50                 # by IP address, if pi.local doesn't resolve
```

**Shortcut.** Add this to `~/.ssh/config` on the laptop, then `ssh pi` just works (so does
`DEPLOY_HOST=pi` for the sync script):

```
Host pi
  HostName pi.local        # or the IP; pi.local survives the router handing out a new IP
  User jim
```

**No password prompts.** Set up key-based login once:

```sh
ssh-keygen -t ed25519            # only if ~/.ssh/id_ed25519 doesn't exist yet
ssh-copy-id pi
```

**Useful SSH forms:**

```sh
ssh pi 'uptime'                                  # run one command and return
ssh -t pi 'sudo systemctl restart postcards-poster'   # -t when the command needs a sudo password
scp file.txt pi:~/                               # copy a file to the Pi
ssh -L 5001:localhost:5001 pi                    # tunnel: localhost:5001 on the laptop -> the Pi
```

From Claude Code, prefix with `!` to run it in the session, e.g. `! ssh -t pi 'sudo …'`.

---

## Finding the Pi's IP address

Try these in order:

| Where | Command |
|---|---|
| Laptop | `ping -c1 pi.local`: the reply shows the IP |
| Laptop (macOS) | `dscacheutil -q host -a name pi.local` |
| Laptop | `arp -a \| grep -iE 'b8:27:eb\|dc:a6:32\|d8:3a:dd\|e4:5f:01\|2c:cf:67'`: these are Raspberry Pi hardware (MAC) prefixes. Only devices the laptop has talked to recently show up. |
| Laptop | `nmap -sn 192.168.1.0/24` (`brew install nmap`; use your network's range), then look for "Raspberry Pi" |
| Router | Its admin page (often http://192.168.1.1) → *DHCP clients* / *connected devices* |
| On the Pi | `hostname -I` |

**Keep the IP from changing:** add a *DHCP reservation* for the Pi in your router, or use `pi.local`
everywhere instead of the IP.

**Network details, on the Pi** (Raspberry Pi OS Bookworm and later use NetworkManager):

```sh
nmcli device status                  # interfaces and whether they're connected
nmcli connection show --active       # which Wi-Fi network it's on
nmcli device wifi list               # Wi-Fi networks in range
ip -4 route | grep default           # the router's address
```

---

## Services (systemd)

Long-running programs on the Pi run as systemd services, each defined by a unit file in
`/etc/systemd/system/<name>.service`.

| To… | Command |
|---|---|
| See if it's running (and its latest log lines) | `systemctl status <name>` |
| Start / stop / restart | `sudo systemctl start\|stop\|restart <name>` |
| Start on boot (and now) | `sudo systemctl enable --now <name>` |
| Don't start on boot (and stop now) | `sudo systemctl disable --now <name>` |
| Follow its log live | `journalctl -u <name> -f` |
| Last 100 log lines | `journalctl -u <name> -n 100 --no-pager` |
| Logs since … | `journalctl -u <name> --since "1 hour ago"` (or `today`, `"2026-09-25 08:00"`) |
| Only warnings and errors | `journalctl -u <name> -p warning` |
| Logs from the previous boot | `journalctl -u <name> -b -1` (logs are kept across reboots) |
| All running services | `systemctl list-units --type=service --state=running` |
| Anything that failed | `systemctl --failed` |

**Install a service:**

```sh
sudo cp <name>.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now <name>
```

**After editing a unit file:** run `sudo systemctl daemon-reload && sudo systemctl restart <name>`.
Forgetting `daemon-reload` is the classic mistake: systemd keeps using the old copy.

**Remove a service:**

```sh
sudo systemctl disable --now <name>
sudo rm /etc/systemd/system/<name>.service
sudo systemctl daemon-reload
```

---

## The postcards service

The service is named `postcards-poster`. It runs `app/poster.py` as user `jim` and restarts
itself if it crashes.

```sh
# first install (on the Pi)
sudo cp ~/projects/postcards/app/postcards-poster.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now postcards-poster

systemctl status postcards-poster
journalctl -u postcards-poster -f
```

| On the laptop | Does |
|---|---|
| `app/sync_to_pi.sh --dry-run` | Shows what would change |
| `app/sync_to_pi.sh` | Pulls back posted photos, then pushes code, metadata and curation |
| `app/sync_to_pi.sh --restart` | …and restarts the service (needed after code changes) |

Run these on the Pi, in `~/projects/postcards/app`:

| Command | Does |
|---|---|
| `.venv/bin/python3 poster.py --check` | Checks config and logins, then shows queue sizes, the schedule, and a sample post |
| `.venv/bin/python3 poster.py --once --dry-run --workers <Feed>` | Shows what it would post, without posting |
| `.venv/bin/python3 poster.py --once --workers <Feed>` | Posts one photo now, off schedule |
| `tail -f logs/poster.log` | The same log as journalctl, as a file |
| `tail state/posted_log.jsonl` | Recent posts, with their Bluesky links |
| `cat state/<Feed>.json` | That feed's last post and last attempt |

Secrets live in `app/.local.env` on the Pi (`chmod 600`). It's never synced or committed.

---

## Pi health and upkeep

| Check | Command | Good |
|---|---|---|
| Temperature | `vcgencmd measure_temp` | under ~70 °C |
| Power / throttling | `vcgencmd get_throttled` | `throttled=0x0`; anything else usually means a weak power supply |
| Disk | `df -h /` | plenty free |
| Memory | `free -h` | |
| Uptime / load | `uptime` | |
| Clock | `timedatectl` | `System clock synchronized: yes` |

| Task | Command |
|---|---|
| Update the OS | `sudo apt update && sudo apt full-upgrade -y` (then `sudo reboot` if the kernel was updated) |
| Reboot | `sudo reboot` |
| Shut down | `sudo shutdown -h now`. Wait for the green light to stop blinking before pulling the power; pulling it while running can corrupt the SD card. |
| Change hostname | `sudo raspi-config` → *System Options* → *Hostname* |
| Update Python packages for postcards | `cd ~/projects/postcards/app && .venv/bin/pip install -r requirements.txt`, then restart the service |
