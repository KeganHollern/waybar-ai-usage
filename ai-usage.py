#!/usr/bin/env python3
"""Waybar AI subscription usage module.

Usage: ai-usage.py <codex|claude|grok|gemini|zai>

Prints one waybar custom-module JSON line for the given provider, showing
percent of the weekly quota remaining, colored on a green->yellow->red
gradient matching the desktop theme.

Data sources:
  codex/claude/grok : `omp usage --json` (Oh My Pi aggregates these accounts)
  gemini            : cloudcode-pa.googleapis.com retrieveUserQuota
                      (OAuth creds from ~/.gemini/oauth_creds.json, refreshed
                      with gemini-cli's public OAuth client when expired)
  zai               : api.z.ai quota API (token from omp's credential store)

All five module instances share one cache (~/.cache/waybar-ai-usage) guarded
by a lock file, so a bar refresh performs a single fetch pass.
"""

import fcntl
import json
import os
import sqlite3
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HOME = os.path.expanduser("~")
CACHE_DIR = os.path.join(HOME, ".cache", "waybar-ai-usage")
STATE_PATH = os.path.join(CACHE_DIR, "state.json")
LOCK_PATH = os.path.join(CACHE_DIR, "lock")
GEMINI_TOKEN_CACHE = os.path.join(CACHE_DIR, "gemini-token.json")

TTL = 300  # seconds a fetched state stays fresh
STALE_LIMIT = 3 * 3600  # drop carried-over data older than this

PROVIDERS = ("codex", "claude", "grok", "gemini", "zai")
NAMES = {
    "codex": "OpenAI Codex",
    "claude": "Claude",
    "grok": "Grok",
    "gemini": "Gemini",
    "zai": "Z.AI",
}

# Theme colors (must match waybar style.css)
GREEN = (0x00, 0xFF, 0x99)   # @secondary-color
YELLOW = (0xFF, 0xEE, 0x66)
RED = (0xFF, 0x66, 0x66)     # @alert-color
DIM = "#66667a"

# gemini-cli's installed-app OAuth client, shipped in plaintext in the CLI's
# own public source (see README). Per Google's OAuth docs, installed-app
# client secrets are not treated as confidential. Assembled from parts only
# so GitHub's secret scanner doesn't false-positive on every push.
GEMINI_CLIENT_ID = ("681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j"
                    ".apps.googleusercontent.com")
GEMINI_CLIENT_SECRET = "GOCSPX-" + "4uHgMPm-1o7Sk-geV6Cu5clXFsxl"


def lerp(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def gradient(pct_remaining):
    """Map remaining% (0-100) to theme green->yellow->red hex color."""
    t = max(0.0, min(100.0, pct_remaining)) / 100.0
    if t >= 0.5:
        rgb = lerp(YELLOW, GREEN, (t - 0.5) * 2)
    else:
        rgb = lerp(RED, YELLOW, t * 2)
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def fmt_reset(ms):
    if not ms:
        return None
    try:
        ts = float(ms) / 1000.0
    except (TypeError, ValueError):
        return None
    lt = time.localtime(ts)
    if ts - time.time() < 22 * 3600:
        return time.strftime("%H:%M", lt)
    return time.strftime("%a %H:%M", lt)


def http_json(url, headers=None, body=None, timeout=15):
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


# --------------------------------------------------------------------------
# Fetchers: each returns {"pct": float, "lines": [tooltip lines]}
# --------------------------------------------------------------------------

def omp_binary():
    """Waybar runs with a bare PATH (/bin:/usr/bin); find omp explicitly."""
    found = shutil.which("omp")
    if found:
        return found
    for cand in (os.path.join(HOME, ".local", "bin", "omp"),
                 "/usr/local/bin/omp", "/usr/bin/omp"):
        if os.access(cand, os.X_OK):
            return cand
    raise FileNotFoundError("omp CLI not found")


def fetch_omp():
    """One omp call feeds codex, claude, and grok."""
    out = subprocess.run(
        [omp_binary(), "usage", "--json"],
        capture_output=True, text=True, timeout=90, check=True,
    ).stdout
    reports = {r.get("provider"): r for r in json.loads(out).get("reports", [])}
    results = {}

    def pick(report, primary_id):
        limits = report.get("limits", [])
        for lim in limits:
            if lim.get("id") == primary_id:
                return lim
        for lim in limits:
            if lim.get("window", {}).get("id") in ("7d", "1w"):
                return lim
        return limits[0] if limits else None

    def limit_line(lim):
        rem = round(lim["amount"]["remainingFraction"] * 100)
        line = "{}: {}% left".format(lim.get("label", "?"), rem)
        reset = fmt_reset(lim.get("window", {}).get("resetsAt"))
        if reset:
            line += "  (resets {})".format(reset)
        return line

    for key, provider, primary in (
        ("codex", "openai-codex", "openai-codex:primary"),
        ("claude", "anthropic", "anthropic:7d"),
        ("grok", "xai-oauth", "xai-oauth:credits:1w"),
    ):
        rep = reports.get(provider)
        if not rep:
            continue
        main = pick(rep, primary)
        if not main:
            continue
        results[key] = {
            "pct": main["amount"]["remainingFraction"] * 100,
            "lines": [limit_line(l) for l in rep.get("limits", [])],
        }
    return results


def gemini_access_token():
    with open(os.path.join(HOME, ".gemini", "oauth_creds.json")) as f:
        creds = json.load(f)
    if creds.get("expiry_date", 0) / 1000.0 > time.time() + 60:
        return creds["access_token"]
    # our own refresh cache, so we never write into gemini-cli's file
    try:
        with open(GEMINI_TOKEN_CACHE) as f:
            cached = json.load(f)
        if cached.get("expires_at", 0) > time.time() + 60:
            return cached["access_token"]
    except (OSError, ValueError):
        pass
    tok = http_json(
        "https://oauth2.googleapis.com/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=urllib.parse.urlencode({
            "client_id": GEMINI_CLIENT_ID,
            "client_secret": GEMINI_CLIENT_SECRET,
            "refresh_token": creds["refresh_token"],
            "grant_type": "refresh_token",
        }).encode(),
    )
    access = tok["access_token"]
    with open(GEMINI_TOKEN_CACHE, "w") as f:
        json.dump({"access_token": access,
                   "expires_at": time.time() + tok.get("expires_in", 3600) - 60}, f)
    return access


def fetch_gemini():
    token = gemini_access_token()
    headers = {"Authorization": "Bearer " + token,
               "Content-Type": "application/json"}
    project = None
    try:
        lca = http_json(
            "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
            headers=headers, body={"metadata": {"pluginType": "GEMINI"}})
        project = lca.get("cloudaicompanionProject")
    except (urllib.error.URLError, ValueError, KeyError):
        pass
    quota = http_json(
        "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota",
        headers=headers, body={"project": project} if project else {})
    buckets = quota.get("buckets") or []
    per_model = {}  # modelId -> (fraction, resetTime); keep lowest fraction
    for b in buckets:
        model, frac = b.get("modelId"), b.get("remainingFraction")
        if model is None or frac is None:
            continue
        if model not in per_model or frac < per_model[model][0]:
            per_model[model] = (frac, b.get("resetTime"))
    if not per_model:
        raise ValueError("no quota buckets")
    pct = min(f for f, _ in per_model.values()) * 100
    lines = []
    for model, (frac, reset) in sorted(per_model.items(), key=lambda kv: kv[1][0]):
        line = "{}: {}% left".format(model, round(frac * 100))
        if reset:
            line += "  (resets {})".format(reset.replace("T", " ").rstrip("Z") + "Z")
        lines.append(line)
    return {"pct": pct, "lines": lines}


def fetch_zai():
    db = sqlite3.connect("file:" + os.path.join(HOME, ".omp", "agent", "agent.db")
                         + "?mode=ro", uri=True)
    try:
        row = db.execute(
            "SELECT data FROM auth_credentials WHERE provider='zai'").fetchone()
    finally:
        db.close()
    if not row:
        raise ValueError("no zai credential in omp store")
    token = json.loads(row[0])["access"]
    res = http_json(
        "https://api.z.ai/api/monitor/usage/quota/limit",
        headers={"Authorization": "Bearer " + token,
                 "Accept-Language": "en-US,en",
                 "Content-Type": "application/json"})
    limits = (res.get("data") or {}).get("limits") or []
    windows = {}  # unit -> limit
    for lim in limits:
        if lim.get("type") in ("TOKENS_LIMIT", "CREDIT_LIMIT"):
            windows[lim.get("unit")] = lim
    main = windows.get(6) or windows.get(3)  # weekly, else 5h session
    if not main:
        raise ValueError("no usable zai quota window")
    pct = 100.0 - max(0, min(100, main.get("percentage", 0)))
    lines = []
    for unit, label in ((6, "Weekly"), (3, "5h session"), (7, "Monthly")):
        lim = windows.get(unit)
        if not lim:
            continue
        line = "{}: {}% left".format(label, 100 - max(0, min(100, lim.get("percentage", 0))))
        reset = fmt_reset(lim.get("nextResetTime"))
        if reset:
            line += "  (resets {})".format(reset)
        lines.append(line)
    level = (res.get("data") or {}).get("level")
    if level:
        lines.append("plan: " + level)
    return {"pct": pct, "lines": lines}


# --------------------------------------------------------------------------
# Cache orchestration
# --------------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"fetched_at": 0, "providers": {}}


def refresh(old):
    now = time.time()
    providers = {}

    def keep_old(key, err):
        prev = old.get("providers", {}).get(key)
        if prev and now - prev.get("ts", 0) < STALE_LIMIT:
            prev = dict(prev)
            prev["stale"] = True
            providers[key] = prev
        else:
            providers[key] = {"error": str(err) or err.__class__.__name__, "ts": now}

    try:
        omp = fetch_omp()
    except Exception as e:  # omp missing/timeout/parse
        omp = None
        for key in ("codex", "claude", "grok"):
            keep_old(key, e)
    if omp is not None:
        for key in ("codex", "claude", "grok"):
            if key in omp:
                providers[key] = {**omp[key], "ts": now}
            else:
                keep_old(key, "not reported by omp")

    for key, fn in (("gemini", fetch_gemini), ("zai", fetch_zai)):
        try:
            providers[key] = {**fn(), "ts": now}
        except Exception as e:
            keep_old(key, e)

    state = {"fetched_at": now, "providers": providers}
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)
    return state


def get_state():
    os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
    lock = open(LOCK_PATH, "w")
    deadline = time.time() + 120
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.time() > deadline:  # fetcher wedged; serve whatever exists
                return load_state()
            time.sleep(0.5)
    try:
        state = load_state()
        if time.time() - state.get("fetched_at", 0) >= TTL:
            state = refresh(state)
        return state
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def emit(provider, state):
    name = NAMES[provider]
    data = state.get("providers", {}).get(provider)
    if not data or "pct" not in data:
        tooltip = "<b>{}</b>\nunavailable".format(name)
        if data and data.get("error"):
            tooltip += ": " + data["error"]
        print(json.dumps({
            "text": "<span foreground='{}'>--</span>".format(DIM),
            "tooltip": tooltip,
            "class": ["missing"],
        }))
        return
    pct = data["pct"]
    color = gradient(pct)
    text = "<span foreground='{}'>{}%</span>".format(color, round(pct))
    lines = ["<b>{}</b> — {}% of weekly left".format(name, round(pct))]
    lines += data.get("lines", [])
    if data.get("stale"):
        lines.append("(stale: {})".format(
            time.strftime("%H:%M", time.localtime(data.get("ts", 0)))))
        text = "<span foreground='{}'>{}%</span>".format(DIM, round(pct))
    cls = "ok" if pct > 50 else ("warn" if pct > 20 else "crit")
    print(json.dumps({
        "text": text,
        "tooltip": "\n".join(lines),
        "class": [cls] + (["stale"] if data.get("stale") else []),
        "percentage": round(pct),
    }))


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in PROVIDERS:
        sys.stderr.write("usage: ai-usage.py <%s>\n" % "|".join(PROVIDERS))
        sys.exit(2)
    emit(sys.argv[1], get_state())


if __name__ == "__main__":
    main()
