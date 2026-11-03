# Maine Vintage Toys Stock Watcher

Checks [Maine Vintage Toys](https://www.mainevintagetoys.com) about every 3
minutes and sends a push to your phone (via [ntfy](https://ntfy.sh)) when
English Pokemon **30th Celebration** or **Delta Reign** product is in stock.

This doesn't watch a single product page like the Innova watcher does. It
watches searches. Each check searches the store for every term in
`WATCH_TERMS` and keeps the products that match. Japanese, Chinese, and other
non-English listings, plus accessories (sleeves, binders, Ultra Pro, etc.) and
events, are filtered out. A product listed next week is picked up
automatically.

What you get on your phone:

| Notification | When | Priority |
| --- | --- | --- |
| **IN STOCK at MVT** + price + quantity + tap-to-open link | a known product comes back in stock (2 consecutive reads, ~3-6 min) | urgent |
| **NEW + IN STOCK at MVT** | a product that wasn't listed before shows up already in stock (immediately) | urgent |
| **PRE-ORDER OPEN at MVT** | the store turns on pre-ordering for a matching product | urgent |
| "New listing at MVT (sold out)" | a matching product is listed but not buyable yet (e.g. Delta Reign before release) | default |
| "Still in stock (n/3)" | every 15 min while a product stays in stock, max 3 per product | high |
| "Watcher may be blocked or broken" | no reliable reading for 30+ min (once per incident) | high |
| "Watcher recovered" | first good reading after a failure alert | low |
| "Watcher is alive" | daily at 09:00 America/New_York, with a count of what's in stock | low |

No auto-checkout, no add-to-cart, no bot-protection evasion. It just watches and tells you.

> **On the first run** you'll get an alert for anything that's *already* in
> stock. As of 2026-11-02 that's the 30th Celebration Battle Deck. After that
> you only hear about changes.

---

## Quick start

```bash
cd mvt-stock-checker
python3 -m venv .venv            # Python 3.11+
.venv/bin/pip install -r requirements.txt
cp .env.example .env             # then set NTFY_TOPIC (your Innova topic works)

.venv/bin/python watcher.py --list            # what matches right now (no state, no pushes)
.venv/bin/python watcher.py --test-notify     # a test push should arrive
.venv/bin/python watcher.py --once --dry-run  # one full check, pushes only logged
```

Then pick a deployment option below. Setting up ntfy on your phone works the
same as for the Innova watcher: install the app and subscribe to your topic.

---

## Deployment

### GitHub Actions (no server needed, same as Innova)

`.github/workflows/watch.yml` runs `watcher.py --once` every 5 minutes and
commits `state.json` back when it changes.

1. Push this folder to a **public** GitHub repo (private repos run out of free minutes).
2. Repo → **Settings → Secrets and variables → Actions** → add `NTFY_TOPIC`.
3. **Actions** tab → *MVT stock watcher* → **Run workflow** to check it works.

All the Innova trade-offs apply: GitHub often delays scheduled runs by 10-30
min, `CONFIRMATIONS` is `1` in the workflow, and GitHub disables schedules after
60 days without repo activity (the daily heartbeat commit counts). To change
`WATCH_TERMS`/`EXCLUDE` here, add them as `env:` entries in the workflow's
*Check stock* step.

### Option 1: Always-on Linux host with systemd (most reliable)

```bash
sudo git clone <this repo> /opt/mvt-stock-watcher
cd /opt/mvt-stock-watcher
sudo python3 -m venv .venv && sudo .venv/bin/pip install -r requirements.txt
sudo cp .env.example .env && sudo nano .env && sudo chmod 600 .env
sudo cp mvt-stock-watcher.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now mvt-stock-watcher
journalctl -u mvt-stock-watcher -f
```

### Option 2: Docker

```bash
docker compose up -d --build && docker compose logs -f
```

### Option 3: This Mac (launchd)

Only runs while the Mac is awake.

```bash
sed "s|/PATH/TO/mvt-stock-checker|$PWD|g" com.mvtstockwatcher.plist > ~/Library/LaunchAgents/com.mvtstockwatcher.plist
launchctl load ~/Library/LaunchAgents/com.mvtstockwatcher.plist
tail -f /tmp/mvt-stock-watcher.log
# stop: launchctl unload ~/Library/LaunchAgents/com.mvtstockwatcher.plist
```

---

## Changing what it watches

```bash
# .env
WATCH_TERMS=30th celebration,delta reign,prismatic evolutions
```

A term matches when **every word** in it appears in the product name, in any
order and with other words in between. So `30th celebration` also matches
"30th *Anniversary* Celebration", but `delta reign` doesn't match "Chilling
Reign". Run `--list` after any change. It prints the matches and also the
products that were dropped by `EXCLUDE`, so you can see whether the filter is
too strict.

`EXCLUDE` is a list of words and phrases. A product whose name contains any of
them as whole words is dropped. The defaults cover non-English languages
(including the store's "Japansese" typo), accessories, and events. Set
`EXCLUDE=none` to see everything.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `NTFY_TOPIC` | **required** | ntfy topic name |
| `WATCH_TERMS` | `30th celebration,delta reign` | Search terms (comma-separated) |
| `EXCLUDE` | languages + accessories + `event` | Words/phrases that drop a product |
| `ALERT_NEW_LISTINGS` | `true` | Heads-up for new matching listings that aren't buyable yet |
| `NTFY_SERVER` / `NTFY_TOKEN` | `https://ntfy.sh` / unset | Self-hosted or protected ntfy |
| `POLL_INTERVAL_SECONDS` | `180` | Base poll interval |
| `JITTER_PERCENT` | `20` | ± random jitter per cycle (0-50) |
| `CONFIRMATIONS` | `2` | Consecutive in-stock reads before alerting on a known product |
| `HEARTBEAT_TIME` / `TIMEZONE` | `09:00` / `America/New_York` | Daily "alive" message |
| `STATE_FILE` | `./state.json` | Persisted state |
| `LOG_LEVEL` | `INFO` | `DEBUG` also logs every matched product each check |
| `REMINDER_INTERVAL_SECONDS` / `MAX_REMINDERS` | `900` / `3` | "Still in stock" reminders per product |
| `FAILURE_ALERT_AFTER_SECONDS` | `1800` | Time without a reliable reading before alerting |
| `API_BASE` | MVT's Square store API | Only change if the store moves |

The watcher refuses to start if the settings could poll more often than once per
120 seconds.

## Command-line flags

| Flag | Does |
| --- | --- |
| *(none)* | Run forever |
| `--once` | One check (updates state, may notify), then exit |
| `--dry-run` | Log notifications instead of sending them |
| `--test-notify` | Send a test push and exit |
| `--list` | Print current matches (and what `EXCLUDE` dropped), then exit. Doesn't touch state |

---

## How it works

The store runs on **Square Online**. Its search box calls a public JSON API
on Square's CDN:

```
https://cdn5.editmysite.com/app/store/api/v28/editor/users/132547976/sites/272811402825390393/products?q=delta+reign&per_page=100
```

One check = one request per watch term (2 by default), spaced 5 s apart as
the store's `robots.txt` `Crawl-delay` asks. That's about 40 requests an hour,
all to Square's API rather than the storefront pages, and each one is the same
request a shopper's search box makes. The search is fuzzy (`delta reign`
returns ~18 products, `30th celebration` ~78), so one page of 100 normally
covers it. Up to 3 pages are followed if needed.

Each product's stock is read from three signals, which must all agree:

| Signal | Sold out | In stock |
| --- | --- | --- |
| `badges.out_of_stock` | `true` | `false` |
| `inventory.all_variations_sold_out` | `true` | `false` |
| `inventory.total` | `0` | `> 0` |

Disagreement, a hidden product, or missing fields → **UNKNOWN** for that
product, which never alerts. A pre-order flag (`preordering.*`) counts as
buyable. `inventory.enabled` is `false` even for tracked items, so it's ignored.

Store-level failures (timeouts, 403/429/5xx, a non-JSON body such as a bot
challenge, or a search that returns nothing at all) mark the **whole check**
as failed. Products are left exactly as they were, so a blip can't make
something look sold out and re-alert later. Blocking-type failures double the
wait (cap 30 min, longer if `Retry-After` says so), and 30 minutes of failures
sends the "may be blocked or broken" push.

A product that drops out of the results (hidden or delisted) keeps its last
state. If it comes back during the same in-stock episode, you don't get a
duplicate alert.

## Logs

One JSON line per check:

```json
{"ts": "2026-11-03T00:28:46+00:00", "level": "INFO", "msg": "check", "ok": true,
 "reason": "6 matching products", "matched": 6,
 "in_stock": ["Pokemon 30th Celebration Battle Deck (*In-Store Pick-up Only!*)"],
 "requests": 2, "latency_ms": 314, "next_check_s": 152, "backoff": false}
```

Products classified UNKNOWN are logged at `WARNING` with their signals.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

`tests/fixtures/` holds real search responses saved from the store on
2026-11-02 (trimmed to the fields used). Restocks and new listings are
simulated by editing real records. If the store changes and checks start
failing, save a fresh response with `curl "<API_BASE>/products?q=delta+reign&per_page=100"`,
run `--list`, and adjust `detect.py`.

## Files

```
.github/workflows/watch.yml   GitHub Actions schedule (one check per run)
watcher.py     main loop, HTTP, search pagination, backoff, CLI
detect.py      pure matching + stock classification of API records
state.py       persisted per-product state + pure transition function (alerts, reminders, heartbeat)
notify.py      Notifier interface + ntfy implementation (same as Innova)
config.py      env loading and validation
tests/         pytest suite + saved API fixtures
```
