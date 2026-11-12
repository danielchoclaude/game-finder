#!/usr/bin/env python3
"""Background injury watcher: Mac notifications even when Game Finder isn't open.

macOS runs this once a minute (see com.gamefinder.injurywatch.plist). Each run fetches
ESPN's NFL/NBA/MLB injury reports, compares them with the last run, and posts a Mac
notification for changes to your favorite teams or fantasy roster players.

Your teams and roster come from watchlist.json, which the Game Finder page saves
whenever you change them. Without that file, it watches the Lions and Pistons.

Run by hand:   python3 injury_watch.py           (one check)
               python3 injury_watch.py --test    (sends a sample notification)
"""

import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
WATCHLIST = HERE / "watchlist.json"
SNAPSHOT = Path(os.environ.get("SNAPSHOT_PATH") or HERE / ".injury_snapshot.json")
LOG = HERE / "injury_watch.log"
# Phone alerts: set NTFY_TOPIC (a long random name only you know) and subscribe to it in the ntfy app.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
APP_URL = os.environ.get("APP_URL", "").strip()
IN_CLOUD = bool(os.environ.get("GITHUB_ACTIONS"))
API = "https://site.api.espn.com/apis/site/v2/sports/"
LEAGUES = {"nfl": "football/nfl", "nba": "basketball/nba", "mlb": "baseball/mlb"}
DEFAULT_WATCH = {"favs": [{"league": "nfl", "id": "8", "name": "Detroit Lions"},
                          {"league": "nba", "id": "8", "name": "Detroit Pistons"}], "roster": ""}
MAX_NOTIFICATIONS = 5


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n"
    if IN_CLOUD:
        print(line, end="")
        return
    with LOG.open("a") as f:
        f.write(line)
    # Keep the log small.
    lines = LOG.read_text().splitlines(True)
    if len(lines) > 500:
        LOG.write_text("".join(lines[-300:]))


def notify(title, body):
    if NTFY_TOPIC:
        # JSON publishing keeps non-ASCII text (arrows, accents in names) intact.
        msg = {"topic": NTFY_TOPIC, "title": title, "message": body or title, "tags": ["rotating_light"], "priority": 4}
        if APP_URL:
            msg["click"] = APP_URL
        req = urllib.request.Request(NTFY_SERVER, data=json.dumps(msg).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "GameFinder/1.0"})
        try:
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as e:
            log(f"ntfy send failed: {e}")
    if sys.platform != "darwin" or IN_CLOUD:
        return
    # Text goes in as arguments, never pasted into the AppleScript, so quotes in news notes can't break it.
    subprocess.run(["osascript", "-e", "on run argv",
                    "-e", 'display notification (item 2 of argv) with title (item 1 of argv) sound name "Ping"',
                    "-e", "end run", title, body[:240]], check=False, timeout=10)


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def fetch(path):
    req = urllib.request.Request(f"{API}{path}/injuries", headers={"User-Agent": "GameFinder/1.0", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def is_watched(watch, league, team_id, name):
    roster = (watch.get("roster") or "").lower()
    return any(f.get("league") == league and str(f.get("id")) == str(team_id) for f in watch.get("favs", [])) \
        or (bool(roster) and name.lower() in roster)


def check():
    watch = load_json(WATCHLIST, DEFAULT_WATCH)
    prev = load_json(SNAPSHOT, None)
    snap, info, failed = {}, {}, []
    for k, path in LEAGUES.items():
        try:
            data = fetch(path)
        except Exception as e:
            failed.append(k)
            log(f"{k}: fetch failed ({e})")
            continue
        for team in data.get("injuries", []):
            for x in team.get("injuries", []):
                name = (x.get("athlete") or {}).get("displayName")
                if not name:
                    continue
                key = f"{k}:{name}"
                snap[key] = {"s": x.get("status"), "d": x.get("date"), "t": str(team.get("id"))}
                info[key] = {"team": team.get("displayName", ""), "short": x.get("shortComment") or ""}
    if len(failed) == len(LEAGUES):
        return  # offline: keep the old snapshot, try again next minute
    if prev:
        for key, p in prev.items():  # a league that failed this run keeps its old entries
            if key.split(":", 1)[0] in failed:
                snap[key] = p

    changes = []
    if prev:
        for key, cur in snap.items():
            k, name = key.split(":", 1)
            if not is_watched(watch, k, cur["t"], name):
                continue
            p, who = prev.get(key), f"{name} ({info.get(key, {}).get('team', '')})"
            short = info.get(key, {}).get("short", "")
            if p is None:
                if cur["s"] != "Active":
                    changes.append((f"{who}: {cur['s']}", f"Added to injury report. {short}"))
            elif p["s"] != cur["s"]:
                changes.append((f"{who}: {p['s']} → {cur['s']}", short))
            elif p["d"] != cur["d"] and cur["s"] != "Active":
                changes.append((f"{who}: {cur['s']}", f"New update. {short}"))
        for key, p in prev.items():
            k, name = key.split(":", 1)
            if key not in snap and p["s"] != "Active" and is_watched(watch, k, p["t"], name):
                changes.append((f"{name}: off the injury report", f"Was {p['s']}."))

    for title, body in changes[:MAX_NOTIFICATIONS]:
        notify(title, body)
        log(f"ALERT {title} | {body[:160]}")
    if len(changes) > MAX_NOTIFICATIONS:
        notify(f"+{len(changes) - MAX_NOTIFICATIONS} more injury updates", "Open Game Finder's Injuries tab for the rest.")
    SNAPSHOT.write_text(json.dumps(snap))
    if prev is None:
        log(f"baseline saved ({len(snap)} players); alerts start next run")


if __name__ == "__main__":
    if "--test" in sys.argv:
        notify("Game Finder test: Jahmyr Gibbs (Detroit Lions)", "Active → Questionable. This is a sample alert, not real news.")
        log("sent test notification")
    else:
        check()
