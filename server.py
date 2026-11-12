#!/usr/bin/env python3
"""Game Finder: local server for the sports tracker page and its Fantasy AI tab.

Run:  python3 server.py   ->  http://localhost:8790
The AI tab goes through the Claude Code CLI at ~/.local/bin/claude, so questions
bill to your Claude subscription. There is no API key. Keep this local: anyone who
could reach the server could spend your usage.
"""

import json
import os
import shutil
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
PORT = int(os.environ.get("PORT", 8790))
# Sonnet keeps each question cheap on a Pro plan. Switch to "opus" for harder questions.
MODEL = os.environ.get("GF_MODEL", "sonnet")

API = "https://site.api.espn.com/apis/site/v2/sports/"
API_STANDINGS = "https://site.api.espn.com/apis/v2/sports/"
LEAGUES = {
    "nfl": ("NFL", "football/nfl"),
    "nba": ("NBA", "basketball/nba"),
    "mlb": ("MLB", "baseball/mlb"),
    "epl": ("Premier League", "soccer/eng.1"),
    "ucl": ("Champions League", "soccer/uefa.champions"),
}
KOREA_ID = "451"
KOREA_COMPS = ["fifa.friendly", "fifa.worldq.afc", "afc.asian.cup", "fifa.world"]

SYSTEM_PROMPT = """\
You are the user's fantasy sports manager and sports analyst. They follow the NFL, NBA, \
MLB, the Premier League, the Champions League and the South Korea men's national team. \
Their favorite teams are listed in the briefing.

Each question comes with a LIVE BRIEFING pulled from ESPN a few minutes ago: games and \
scores, TV channels, betting lines, standings, injury reports and headlines. Treat it as \
the current state of the world. It is newer than your training data.

How to answer:
- Lead with the answer or the recommendation (start/sit, add/drop, trade yes/no), then \
the 2-4 reasons that decide it. Keep it short unless asked for depth.
- Back claims with the briefing (injury status, matchup, opponent record, Vegas line, \
recent news). Say which item you are relying on.
- If the briefing doesn't cover something you need, such as a player's recent stats, \
snap counts, target share or depth chart, use WebSearch or WebFetch to look it up. \
Prefer espn.com, nfl.com, nba.com, mlb.com, rotowire.com and team sites.
- Never invent stats, injuries or news. If you can't confirm something, say so.
- Betting lines are context for game script only. Don't push the user to gamble.
- When they've shared their roster, tie the advice to it.
- Write plain text with short paragraphs or simple dashes. No tables, no headers."""

# ---------------------------------------------------------------- ESPN fetch
_cache = {}


def get_json(url, ttl=300):
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    req = urllib.request.Request(url, headers={"User-Agent": "GameFinder/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode("utf-8"))
    _cache[url] = (time.time(), data)
    return data


def safe(fn, *a):
    try:
        return fn(*a)
    except Exception as e:  # one feed failing shouldn't sink the whole briefing
        return f"(unavailable: {e})"


def fmt_game(ev):
    c = ev["competitions"][0]
    st = ev["status"]["type"]
    teams = sorted(c["competitors"], key=lambda t: t.get("homeAway") != "away")
    names = []
    for t in teams:
        rec = next((r.get("summary") for r in t.get("records") or []), "")
        score = f" {t.get('score')}" if st["state"] != "pre" else ""
        names.append(f"{t['team'].get('abbreviation') or t['team'].get('displayName')}{score}" + (f" ({rec})" if rec else ""))
    when = st.get("shortDetail") or ev["date"]
    tv = sorted({n for b in c.get("broadcasts") or [] for n in b.get("names") or []})
    o = (c.get("odds") or [{}])[0]
    line = ", ".join(x for x in [o.get("details"), f"O/U {o['overUnder']}" if o.get("overUnder") else None] if x)
    return f"- {' @ '.join(names)} | {when}" + (f" | TV: {', '.join(tv)}" if tv else "") + (f" | Line: {line}" if line else "")


def scoreboard(path, day):
    evs = get_json(f"{API}{path}/scoreboard?dates={day}").get("events", [])
    return [fmt_game(e) for e in evs]


def news(path, n=8):
    arts = get_json(f"{API}{path}/news?limit={n}").get("articles", [])
    return [f"- {a.get('published', '')[:10]} {a.get('headline')}: {a.get('description', '')}" for a in arts]


def standings(path):
    d = get_json(f"{API_STANDINGS}{path}/standings", ttl=1800)
    out = []

    def walk(node):
        entries = (node.get("standings") or {}).get("entries") or []
        if entries:
            rows = []
            for e in entries:
                s = {x.get("abbreviation"): x.get("displayValue") for x in e.get("stats", [])}
                rec = s.get("Total") or "-".join(v for v in [s.get("W"), s.get("L")] if v)
                rows.append(f"{e['team'].get('abbreviation', e['team']['displayName'])} {rec}")
            out.append(f"{node.get('name')}: " + ", ".join(rows))
        for ch in node.get("children") or []:
            walk(ch)

    walk(d)
    return out


def recent_injury_updates(path, hours=48, limit=30):
    """Newest injury-report changes league-wide, with ESPN's news note, so the AI sees game-day news."""
    d = get_json(f"{API}{path}/injuries", ttl=60)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%MZ")
    rows = [(i.get("date", ""), team.get("displayName"), i) for team in d.get("injuries", [])
            for i in team.get("injuries", []) if i.get("date", "") >= cutoff and i.get("status") != "Active"]
    rows.sort(key=lambda r: r[0], reverse=True)
    return [f"- {date.replace('T', ' ')} UTC | {i['athlete'].get('displayName')} ({(i['athlete'].get('position') or {}).get('abbreviation', '')}, {team}) "
            f"{i.get('status')}: {(i.get('shortComment') or '')[:220]}" for date, team, i in rows[:limit]]


def injuries(path, teams=None):
    d = get_json(f"{API}{path}/injuries", ttl=60)
    out = []
    for team in d.get("injuries", []):
        if teams and team.get("displayName") not in teams:
            continue
        hurt = [f"{i['athlete'].get('displayName')} ({(i['athlete'].get('position') or {}).get('abbreviation', '')}) {i.get('status')}"
                for i in team.get("injuries", [])]
        if hurt:
            out.append(f"{team.get('displayName')}: " + "; ".join(hurt))
    return out


def team_schedule(path, team_id, label):
    evs = get_json(f"{API}{path}/teams/{team_id}/schedule").get("events", [])
    nxt = [e for e in evs if e["competitions"][0]["status"]["type"]["state"] != "post"][:3]
    last = [e for e in evs if e["competitions"][0]["status"]["type"]["state"] == "post"][-3:]
    lines = []
    for e in last + nxt:
        c = e["competitions"][0]
        state = c["status"]["type"]["state"]
        score = " ".join(f"{t['team'].get('abbreviation')} {(t.get('score') or {}).get('displayValue', '') if isinstance(t.get('score'), dict) else t.get('score', '')}" for t in c["competitors"]) if state == "post" else ""
        tv = sorted({b.get("media", {}).get("shortName") for b in c.get("broadcasts") or [] if b.get("media")})
        lines.append(f"- {e['date'][:10]} {e.get('name')} {'FINAL ' + score if state == 'post' else ''}" + (f" TV: {', '.join(x for x in tv if x)}" if tv else ""))
    return [f"{label} recent/upcoming:"] + lines


def korea_schedule():
    evs = []
    for comp in KOREA_COMPS:
        try:
            evs += get_json(f"{API}soccer/{comp}/teams/{KOREA_ID}/schedule").get("events", [])
        except Exception:
            pass
    evs.sort(key=lambda e: e["date"])
    now = datetime.now(timezone.utc).isoformat()
    upcoming = [e for e in evs if e["date"] >= now][:3]
    recent = [e for e in evs if e["date"] < now][-3:]
    lines = [f"- {e['date'][:10]} {e.get('name')}" for e in recent]
    lines += [f"- UPCOMING {e['date'][:10]} {e.get('name')}" for e in upcoming] or ["- No upcoming fixtures listed by ESPN yet"]
    return ["South Korea men's national team:"] + lines


_brief = {"t": 0, "text": ""}


def briefing(favs):
    # Short cache: injury news before games moves fast, so a briefing is never more than a minute old.
    if time.time() - _brief["t"] < 60 and _brief.get("favs") == favs:
        return _brief["text"]
    today = datetime.now()
    days = [(today + timedelta(days=i)).strftime("%Y%m%d") for i in range(0, 3)]
    jobs = {}
    with ThreadPoolExecutor(max_workers=12) as pool:
        for k, (label, path) in LEAGUES.items():
            for d in days:
                jobs[f"games:{label}:{d}"] = pool.submit(safe, scoreboard, path, d)
            jobs[f"news:{label}"] = pool.submit(safe, news, path, 10 if k in ("nfl", "nba") else 6)
            jobs[f"standings:{label}"] = pool.submit(safe, standings, path)
        jobs["injuries:NFL"] = pool.submit(safe, injuries, "football/nfl")
        jobs["injuries:NBA"] = pool.submit(safe, injuries, "basketball/nba")
        jobs["recent:NFL"] = pool.submit(safe, recent_injury_updates, "football/nfl")
        jobs["recent:NBA"] = pool.submit(safe, recent_injury_updates, "basketball/nba")
        for f in favs:
            if f.get("league") == "kor":
                jobs[f"fav:{f['name']}"] = pool.submit(safe, korea_schedule)
            elif f.get("league") in LEAGUES:
                jobs[f"fav:{f['name']}"] = pool.submit(safe, team_schedule, LEAGUES[f["league"]][1], f["id"], f["name"])
    res = {k: v.result() for k, v in jobs.items()}

    def block(title, items):
        if isinstance(items, str):
            return f"{title}\n{items}"
        return f"{title}\n" + ("\n".join(items) if items else "- none")

    parts = [f"LIVE BRIEFING generated {today.strftime('%A %B %d %Y %I:%M %p')} local time"]
    parts.append("FAVORITE TEAMS: " + (", ".join(f["name"] for f in favs) or "none"))
    parts.append(block("LATEST NFL INJURY NEWS (last 48h, newest first; this is the freshest info, trust it over older items):", res["recent:NFL"]))
    parts.append(block("LATEST NBA INJURY NEWS (last 48h, newest first):", res["recent:NBA"]))
    for k, v in res.items():
        if k.startswith("fav:"):
            parts.append(block("", v).strip())
    for k, (label, _) in LEAGUES.items():
        for d in days:
            parts.append(block(f"{label} games {d[:4]}-{d[4:6]}-{d[6:]}:", res[f"games:{label}:{d}"]))
        parts.append(block(f"{label} standings:", res[f"standings:{label}"]))
        parts.append(block(f"{label} headlines:", res[f"news:{label}"]))
    parts.append(block("NFL injury report:", res["injuries:NFL"]))
    parts.append(block("NBA injury report:", res["injuries:NBA"]))
    text = "\n\n".join(parts)
    _brief.update(t=time.time(), text=text, favs=favs)
    return text


# ---------------------------------------------------------------- Claude CLI
def find_claude_cli():
    local = Path.home() / ".local" / "bin" / "claude"
    return str(local) if local.exists() else shutil.which("claude")


IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
MAX_IMAGES = 4


def ask_claude(question, history, roster, favs, images=()):
    cli = find_claude_cli()
    if not cli:
        raise RuntimeError("Can't find the Claude CLI at ~/.local/bin/claude.")
    images = [i for i in images if i.get("media_type") in IMAGE_TYPES and i.get("data")][:MAX_IMAGES]
    convo = "\n".join(f"{m['role'].upper()}: {m['text']}" for m in history[-8:])
    prompt = (
        briefing(favs)
        + ("\n\nMY FANTASY ROSTER / LEAGUE NOTES:\n" + roster if roster.strip() else "")
        + ("\n\nCONVERSATION SO FAR:\n" + convo if convo else "")
        + (f"\n\nATTACHED: {len(images)} photo(s), likely screenshots of a fantasy app, roster, "
           "matchup or trade. Read them carefully and use what they show." if images else "")
        + "\n\nQUESTION: " + question
    )
    # stream-json input is how the CLI takes images: they go straight to the model as content blocks.
    content = [{"type": "image", "source": {"type": "base64", "media_type": i["media_type"], "data": i["data"]}} for i in images]
    content.append({"type": "text", "text": prompt})
    msg = json.dumps({"type": "user", "message": {"role": "user", "content": content}})
    try:
        r = subprocess.run(
            [cli, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
             "--model", MODEL, "--append-system-prompt", SYSTEM_PROMPT,
             "--tools", "WebSearch,WebFetch", "--allowedTools", "WebSearch,WebFetch",
             "--strict-mcp-config", "--no-session-persistence"],
            input=msg + "\n", capture_output=True, text=True, timeout=300, cwd=str(HERE),
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Claude took over 5 minutes and was stopped. Try a narrower question.")
    combined = ((r.stderr or "") + (r.stdout or "")).lower()
    if r.returncode != 0:
        if "login" in combined or "authenticate" in combined:
            raise RuntimeError("The Claude CLI isn't signed in. Run ~/.local/bin/claude in Terminal and type /login.")
        if "limit" in combined or "quota" in combined:
            raise RuntimeError("Your Claude usage limit was hit. Wait for it to reset and ask again.")
        raise RuntimeError(f"Claude CLI failed: {(r.stderr or r.stdout).strip()[:400]}")
    wrapper = next((json.loads(line) for line in reversed(r.stdout.splitlines())
                    if line.startswith("{") and '"type":"result"' in line.replace(" ", "")), None)
    if wrapper is None:
        raise RuntimeError(f"Claude CLI returned no result: {(r.stderr or r.stdout).strip()[:400]}")
    if wrapper.get("is_error"):
        res = str(wrapper.get("result"))
        if "authenticate" in res.lower() or "login" in res.lower():
            raise RuntimeError("The Claude CLI isn't signed in. Run ~/.local/bin/claude auth login in Terminal.")
        raise RuntimeError(f"Claude error: {str(wrapper.get('result'))[:400]}")
    return wrapper.get("result", "").strip()


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj).encode(), "application/json")

    STATIC = {"/manifest.webmanifest": "application/manifest+json", "/sw.js": "text/javascript",
              "/icons/icon-192.png": "image/png", "/icons/icon-512.png": "image/png",
              "/icons/apple-touch-icon.png": "image/png"}

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path in self.STATIC:
            self._send(200, (HERE / self.path.lstrip("/")).read_bytes(), self.STATIC[self.path])
        elif self.path == "/api/health":
            self._json(200, {"ok": True, "cli": bool(find_claude_cli()), "model": MODEL})
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path == "/api/watchlist":
            # The page saves your teams and roster here so injury_watch.py can alert with the page closed.
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 200_000)) or b"{}")
                watch = {"favs": [{"league": str(f.get("league")), "id": str(f.get("id")), "name": str(f.get("name"))}
                                  for f in body.get("favs") or []][:50],
                         "roster": str(body.get("roster") or "")[:20_000]}
                (HERE / "watchlist.json").write_text(json.dumps(watch, indent=2))
                return self._json(200, {"ok": True})
            except Exception as e:
                return self._json(400, {"error": str(e)})
        if self.path != "/api/ask":
            return self._send(404, b"not found", "text/plain")
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > 30 * 1024 * 1024:
                return self._json(413, {"error": "Those photos are too big. Try fewer or smaller ones."})
            body = json.loads(self.rfile.read(length) or b"{}")
            images = body.get("images") or []
            q = (body.get("question") or "").strip() or ("What do you see here, and what should I do?" if images else "")
            if not q:
                return self._json(400, {"error": "Ask a question first."})
            answer = ask_claude(q, body.get("history") or [], body.get("roster") or "", body.get("favs") or [], images)
            self._json(200, {"answer": answer})
        except Exception as e:
            self._json(500, {"error": str(e)})

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")


if __name__ == "__main__":
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        if e.errno == 48:  # EADDRINUSE on macOS
            raise SystemExit(f"Game Finder is already running. Open http://localhost:{PORT}")
        raise
    print(f"Game Finder running at http://localhost:{PORT}  (AI model: {MODEL})")
    server.serve_forever()
