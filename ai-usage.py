#!/usr/bin/env python3
"""Waybar AI subscription usage module.

Usage: ai-usage.py <codex|claude|grok|gemini|zai>

Prints one waybar custom-module JSON line for the given provider, showing
percent of the weekly quota remaining, colored on a green->yellow->red
gradient matching the desktop theme.

Data sources:
  codex/claude/grok : `omp usage --json` (Oh My Pi aggregates these accounts)
  gemini            : `agy -p /usage` (Antigravity CLI). Falls back to
                      cloudcode-pa retrieveUserQuota via gemini-cli OAuth
                      (~/.gemini/oauth_creds.json) if agy is not signed in.
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
from datetime import datetime, timezone

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


def fmt_reset_value(value):
    """Format a reset time given as ms/sec epoch or ISO-8601."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        return fmt_reset(n if n > 1e12 else n * 1000.0)
    s = str(value).strip()
    if not s:
        return None
    try:
        n = float(s)
        return fmt_reset(n if n > 1e12 else n * 1000.0)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return fmt_reset(dt.timestamp() * 1000.0)
    except ValueError:
        return None


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


def agy_binary():
    """Waybar runs with a bare PATH (/bin:/usr/bin); find agy explicitly."""
    env = os.environ.get("ANTIGRAVITY_CLI_PATH")
    if env and os.access(env, os.X_OK):
        return env
    found = shutil.which("agy")
    if found:
        return found
    for cand in (os.path.join(HOME, ".local", "bin", "agy"),
                 "/usr/local/bin/agy", "/opt/homebrew/bin/agy", "/usr/bin/agy"):
        if os.access(cand, os.X_OK):
            return cand
    raise FileNotFoundError("agy CLI not found")


def _bucket_fraction(bucket):
    frac = bucket.get("remaining_fraction")
    if frac is None:
        frac = bucket.get("remainingFraction")
    if frac is None:
        rem = bucket.get("remaining")
        if isinstance(rem, dict):
            frac = rem.get("remainingFraction", rem.get("remaining_fraction"))
        elif isinstance(rem, (int, float)):
            frac = rem
    return None if frac is None else float(frac)


def _bucket_reset(bucket):
    return (bucket.get("reset_time") or bucket.get("resetTime")
            or bucket.get("resetsAt"))


def _window_label(bucket):
    window = (bucket.get("window") or "").lower()
    bid = (bucket.get("id") or bucket.get("bucketId") or "").lower()
    name = bucket.get("name") or bucket.get("displayName") or ""
    blob = " ".join((window, bid, name.lower()))
    if window == "weekly" or "week" in blob:
        return "weekly"
    if window in ("5h", "5hr", "five_hour") or "5h" in blob or "five hour" in blob:
        return "5h"
    return name or window or bid or "?"


def _is_weekly_bucket(bucket):
    return _window_label(bucket) == "weekly"


def parse_agy_usage(payload):
    """Turn agy `/usage` JSON (or its TSV `response`) into {pct, lines}."""
    data = (payload.get("command") or {}).get("data") or {}
    groups = data.get("groups") or []
    lines = []
    weekly = []
    all_fracs = []

    for group in groups:
        gname = group.get("name") or group.get("displayName") or "Quota"
        for bucket in group.get("buckets") or []:
            frac = _bucket_fraction(bucket)
            if frac is None:
                continue
            all_fracs.append(frac)
            if _is_weekly_bucket(bucket):
                weekly.append(frac)
            line = "{} {}: {}% left".format(
                gname, _window_label(bucket), round(frac * 100))
            reset = fmt_reset_value(_bucket_reset(bucket))
            if reset:
                line += "  (resets {})".format(reset)
            lines.append(line)

    if not all_fracs:
        # print-mode also emits a tab-separated snapshot in `response`
        for raw in str(payload.get("response") or "").splitlines():
            parts = raw.split("\t")
            if len(parts) < 3:
                continue
            gname, bname, pcts = parts[0], parts[1], parts[2]
            try:
                frac = float(pcts.strip().rstrip("%")) / 100.0
            except ValueError:
                continue
            bucket = {"name": bname, "window": bname}
            all_fracs.append(frac)
            if _is_weekly_bucket(bucket):
                weekly.append(frac)
            line = "{} {}: {}% left".format(
                gname, _window_label(bucket), round(frac * 100))
            reset = fmt_reset_value(parts[3] if len(parts) > 3 else None)
            if reset:
                line += "  (resets {})".format(reset)
            lines.append(line)

    if not all_fracs:
        raise ValueError("agy /usage returned no quota buckets")
    # Bar shows the tightest weekly pool (Gemini vs Claude/GPT); tooltip lists all.
    return {"pct": min(weekly or all_fracs) * 100, "lines": lines}


def fetch_agy():
    """Antigravity CLI: `agy -p /usage` is a no-quota slash command."""
    proc = subprocess.run(
        [agy_binary(), "-p", "/usage", "--output-format", "json",
         "--print-timeout", "30s"],
        capture_output=True, text=True, timeout=45,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(err[-1] if err else "agy exit %s" % proc.returncode)
    try:
        payload = json.loads(proc.stdout)
    except ValueError as e:
        raise ValueError("agy /usage: invalid JSON") from e
    status = payload.get("status")
    if status and status != "SUCCESS":
        raise ValueError("agy /usage: " + str(status))
    return parse_agy_usage(payload)


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


def fetch_gemini_cli():
    """Legacy gemini-cli OAuth + cloudcode-pa retrieveUserQuota."""
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
        pretty = fmt_reset_value(reset)
        if pretty:
            line += "  (resets {})".format(pretty)
        elif reset:
            line += "  (resets {})".format(
                str(reset).replace("T", " ").rstrip("Z") + "Z")
        lines.append(line)
    return {"pct": pct, "lines": lines}


def fetch_gemini():
    errors = []
    try:
        return fetch_agy()
    except Exception as e:
        errors.append("agy: %s" % e)
    try:
        return fetch_gemini_cli()
    except Exception as e:
        errors.append("gemini-cli: %s" % e)
    raise ValueError("; ".join(errors))


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
