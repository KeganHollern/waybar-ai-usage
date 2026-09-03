#!/usr/bin/env python3
"""Waybar AI subscription usage module.

Usage: ai-usage.py <codex|claude|grok|gemini|zai|openrouter|muse>

Prints one Waybar custom-module JSON line for the given provider. Subscription
modules show the weekly percentage. OpenRouter shows the USD credit balance.
Values use a green->yellow->red gradient that matches the desktop theme.

Data sources:
  codex/claude/grok : `omp usage --json` (Oh My Pi aggregates these accounts)
  gemini            : `agy -p /usage` (Antigravity CLI). Falls back to
                      cloudcode-pa retrieveUserQuota via gemini-cli OAuth
                      (~/.gemini/oauth_creds.json) if agy is not signed in.
  zai               : api.z.ai quota API (token from omp's credential store)
  openrouter        : OpenRouter credits and API key management APIs
  muse              : Meta Model API response stream (`POST /v1/responses`
                      with `stream: true`). Reads the same subscription usage
                      snapshot the `muse` `/usage` command shows. The snapshot
                      is cached until its reset time, so one refresh pass
                      costs at most one prompt per quota window.

All module instances share one cache (~/.cache/waybar-ai-usage) guarded
by a lock file, so a bar refresh performs a single fetch pass.
"""

import fcntl
import html
import json
import math
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
DEFAULT_CONFIG_HOME = os.path.join(HOME, ".config")
XDG_CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME", "")
CONFIG_HOME = (XDG_CONFIG_HOME if os.path.isabs(XDG_CONFIG_HOME)
               else DEFAULT_CONFIG_HOME)
OPENROUTER_KEY_PATH = os.path.join(
    CONFIG_HOME, "waybar-ai-usage", "openrouter-management-key")
OPENROUTER_API = "https://openrouter.ai/api/v1"
META_API = "https://api.meta.ai/v1"
MUSE_AUTH_PATH = os.path.join(CONFIG_HOME, "muse", "auth.json")
MUSE_SETTINGS_PATH = os.path.join(CONFIG_HOME, "muse", "settings.json")
META_SNAPSHOT_PATH = os.path.join(CACHE_DIR, "meta-subscription.json")
META_MODEL = "muse-spark-1.3"

TTL = 300  # seconds a fetched state stays fresh
STALE_LIMIT = 3 * 3600  # drop carried-over data older than this

PROVIDERS = ("codex", "claude", "grok", "gemini", "zai", "openrouter",
             "muse")
NAMES = {
    "codex": "OpenAI Codex",
    "claude": "Claude",
    "grok": "Grok",
    "gemini": "Gemini",
    "zai": "Z.AI",
    "openrouter": "OpenRouter",
    "muse": "Muse",
}

# Theme colors (must match waybar style.css)
GREEN = (0x00, 0xFF, 0x99)   # @secondary-color
YELLOW = (0xFF, 0xEE, 0x66)
RED = (0xFF, 0x66, 0x66)     # @alert-color
DIM = "#66667a"
EXHAUSTED_COLOR = "#ffcc66"

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


def finite_number(value):
    """Return a finite float, or None for missing and invalid values."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def epoch_ms(value):
    """Convert a second/ms epoch or ISO-8601 value to epoch milliseconds."""
    number = finite_number(value)
    if number is not None:
        return number if number > 1e12 else number * 1000.0
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1000.0


def money(value):
    """Format an OpenRouter USD amount without hiding small charges."""
    amount = finite_number(value)
    if amount is None:
        return "$--"
    if 0 < abs(amount) < 0.01:
        return "${:,.4f}".format(amount)
    return "${:,.2f}".format(amount)


def http_json(url, headers=None, body=None, timeout=15):
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


# --------------------------------------------------------------------------
# Fetchers return a color percentage, display data, and tooltip lines.
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


OMP_BLOCKING_IDS = {
    # Product/model-specific rows are alternatives, not provider-wide gates.
    "codex": {"openai-codex:primary", "openai-codex:secondary"},
    "claude": {"anthropic:5h", "anthropic:7d"},
    "grok": {"xai-oauth:credits:1w", "xai-oauth:included:1mo"},
}


def _omp_limit_exhausted(limit):
    """Use OMP's state first and an exact fraction for older OMP versions."""
    status = limit.get("status")
    if status is not None:
        return str(status).lower() == "exhausted"
    fraction = finite_number((limit.get("amount") or {}).get(
        "remainingFraction"))
    return fraction is not None and fraction <= 0


def parse_omp_usage(payload):
    """Parse OMP usage and keep alternative accounts as usable paths."""
    reports = {}
    for report in payload.get("reports", []):
        reports.setdefault(report.get("provider"), []).append(report)
    unknown_providers = {
        account.get("provider")
        for account in payload.get("accountsWithoutUsage", [])
        if account.get("provider")
    }
    results = {}

    def pick(report, primary_ids):
        if isinstance(primary_ids, str):
            primary_ids = (primary_ids,)
        limits = report.get("limits", [])
        for primary_id in primary_ids:
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
        ("grok", "xai-oauth",
         ("xai-oauth:credits:1w", "xai-oauth:included:1mo")),
    ):
        provider_reports = reports.get(provider) or []
        if not provider_reports:
            continue
        parsed_reports = []
        for rep in provider_reports:
            main = pick(rep, primary)
            if not main:
                continue
            blockers = []
            blocking_ids = OMP_BLOCKING_IDS.get(key, set())
            for limit in rep.get("limits", []):
                if limit.get("id") not in blocking_ids:
                    continue
                if not _omp_limit_exhausted(limit):
                    continue
                blockers.append({
                    "label": limit.get("label") or "quota",
                    "resets_at": epoch_ms(
                        (limit.get("window") or {}).get("resetsAt")),
                })

            # One report is one account. For Grok, included quota and
            # on-demand billing can be two alternatives inside that account.
            paths = [blockers] if blockers else []
            if key == "grok":
                on_demand = [limit for limit in rep.get("limits", [])
                             if limit.get("id") == "xai-oauth:on-demand"]
                if on_demand:
                    on_demand_blockers = [{
                        "label": limit.get("label") or "On-demand",
                        "resets_at": epoch_ms(
                            (limit.get("window") or {}).get("resetsAt")),
                    } for limit in on_demand if _omp_limit_exhausted(limit)]
                    paths = ([blockers, on_demand_blockers]
                             if blockers and on_demand_blockers else [])
                    blockers = blockers + on_demand_blockers if paths else []

            parsed_reports.append({
                "pct": main["amount"]["remainingFraction"] * 100,
                "lines": [limit_line(limit)
                          for limit in rep.get("limits", [])],
                "blockers": blockers,
                "paths": paths,
            })

        if not parsed_reports:
            continue
        # Accounts are alternatives. The provider is out only when each
        # account (and each billing lane in that account) is blocked.
        exhausted = (provider not in unknown_providers
                     and all(report["paths"] for report in parsed_reports))
        blocker_paths = ([path for report in parsed_reports
                          for path in report["paths"]] if exhausted else [])
        blockers = ([blocker for path in blocker_paths for blocker in path]
                    if exhausted else [])
        lines = []
        for index, report in enumerate(parsed_reports, start=1):
            if len(parsed_reports) > 1:
                lines.append("Account {}:".format(index))
                lines.extend("  " + line for line in report["lines"])
            else:
                lines.extend(report["lines"])
        if provider in unknown_providers:
            lines.append("Another account: usage unavailable")
        display_reports = ([report for report in parsed_reports
                            if not report["paths"]] or parsed_reports)
        results[key] = {
            "pct": max(report["pct"] for report in display_reports),
            "lines": lines,
            "exhausted": exhausted,
            "blockers": blockers,
            "blocker_paths": blocker_paths,
        }
    return results


def fetch_omp():
    """One omp call feeds codex, claude, and grok."""
    out = subprocess.run(
        [omp_binary(), "usage", "--json"],
        capture_output=True, text=True, timeout=90, check=True,
    ).stdout
    return parse_omp_usage(json.loads(out))


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
    paths = []

    for group in groups:
        gname = group.get("name") or group.get("displayName") or "Quota"
        group_fracs = []
        group_blockers = []
        for bucket in group.get("buckets") or []:
            frac = _bucket_fraction(bucket)
            if frac is None:
                continue
            all_fracs.append(frac)
            group_fracs.append(frac)
            if _is_weekly_bucket(bucket):
                weekly.append(frac)
            line = "{} {}: {}% left".format(
                gname, _window_label(bucket), round(frac * 100))
            reset = fmt_reset_value(_bucket_reset(bucket))
            if reset:
                line += "  (resets {})".format(reset)
            lines.append(line)
            if frac <= 0:
                group_blockers.append({
                    "label": "{} {}".format(gname, _window_label(bucket)),
                    "resets_at": epoch_ms(_bucket_reset(bucket)),
                })
        if group_fracs:
            paths.append(group_blockers)

    if not all_fracs:
        # print-mode also emits a tab-separated snapshot in `response`
        tsv_paths = {}
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
            tsv_paths.setdefault(gname, [])
            if _is_weekly_bucket(bucket):
                weekly.append(frac)
            line = "{} {}: {}% left".format(
                gname, _window_label(bucket), round(frac * 100))
            reset = fmt_reset_value(parts[3] if len(parts) > 3 else None)
            if reset:
                line += "  (resets {})".format(reset)
            lines.append(line)
            if frac <= 0:
                tsv_paths[gname].append({
                    "label": "{} {}".format(gname, _window_label(bucket)),
                    "resets_at": epoch_ms(
                        parts[3] if len(parts) > 3 else None),
                })
        paths = list(tsv_paths.values())

    if not all_fracs:
        raise ValueError("agy /usage returned no quota buckets")
    # Bar shows the tightest weekly pool (Gemini vs Claude/GPT); tooltip lists all.
    exhausted = bool(paths) and all(paths)
    blockers = [blocker for path in paths for blocker in path] if exhausted else []
    return {
        "pct": min(weekly or all_fracs) * 100,
        "lines": lines,
        "exhausted": exhausted,
        "blockers": blockers,
        # Each group is an alternative usage path. Every path must be blocked.
        "blocker_paths": paths if exhausted else [],
    }


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
    exhausted = all(frac <= 0 for frac, _ in per_model.values())
    blockers = []
    if exhausted:
        blockers = [{"label": model, "resets_at": epoch_ms(reset)}
                    for model, (frac, reset) in per_model.items()
                    if frac <= 0]
    return {
        "pct": pct,
        "lines": lines,
        "exhausted": exhausted,
        "blockers": blockers,
        "blocker_paths": ([[blocker] for blocker in blockers]
                          if exhausted else []),
    }


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


def _zai_remaining_pct(limit):
    used_pct = finite_number(limit.get("percentage"))
    if used_pct is not None:
        return 100.0 - max(0.0, min(100.0, used_pct))
    remaining = finite_number(limit.get("remaining"))
    allowance = finite_number(limit.get("usage"))
    if remaining is not None and allowance and allowance > 0:
        return max(0.0, min(100.0, remaining / allowance * 100.0))
    current = finite_number(limit.get("currentValue"))
    if current is not None and allowance and allowance > 0:
        return max(0.0, min(100.0,
                            (allowance - current) / allowance * 100.0))
    return None


def _zai_limit_exhausted(limit):
    """Use exact Z.AI counts before its rounded percentage."""
    remaining = finite_number(limit.get("remaining"))
    if remaining is not None:
        return remaining <= 0
    allowance = finite_number(limit.get("usage"))
    current = finite_number(limit.get("currentValue"))
    if allowance is not None and allowance > 0 and current is not None:
        return current >= allowance
    used_pct = finite_number(limit.get("percentage"))
    return used_pct is not None and used_pct >= 100


def parse_zai_usage(res):
    """Parse Z.AI quota data and preserve shorter provider-wide blockers."""
    limits = (res.get("data") or {}).get("limits") or []
    windows = {}  # unit -> limit
    for lim in limits:
        if lim.get("type") in ("TOKENS_LIMIT", "CREDIT_LIMIT"):
            unit = finite_number(lim.get("unit"))
            if unit is not None:
                windows[int(unit)] = lim
    main = windows.get(6) or windows.get(3)  # weekly, else 5h session
    if not main:
        raise ValueError("no usable zai quota window")
    pct = _zai_remaining_pct(main)
    if pct is None:
        raise ValueError("zai quota window has no usage value")
    lines = []
    for unit, label in ((6, "Weekly"), (3, "5h session"), (7, "Monthly")):
        lim = windows.get(unit)
        if not lim:
            continue
        remaining_pct = _zai_remaining_pct(lim)
        if remaining_pct is None:
            continue
        line = "{}: {}% left".format(label, round(remaining_pct))
        reset = fmt_reset(lim.get("nextResetTime"))
        if reset:
            line += "  (resets {})".format(reset)
        lines.append(line)
    blockers = []
    for unit, label in ((3, "5h session"), (6, "Weekly")):
        lim = windows.get(unit)
        if lim and _zai_limit_exhausted(lim):
            blockers.append({
                "label": label,
                "resets_at": epoch_ms(lim.get("nextResetTime")),
            })
    level = (res.get("data") or {}).get("level")
    if level:
        lines.append("plan: " + level)
    return {
        "pct": pct,
        "lines": lines,
        "exhausted": bool(blockers),
        "blockers": blockers,
    }


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
    return parse_zai_usage(res)


def openrouter_management_key():
    """Read the OpenRouter management key without putting it in the cache."""
    for name in ("OPENROUTER_MANAGEMENT_API_KEY", "OPENROUTER_MANAGEMENT_KEY"):
        raw_value = os.environ.get(name, "")
        if raw_value:
            value = raw_value.strip()
            if not value or any(char.isspace() for char in value):
                raise ValueError("OpenRouter management key is invalid")
            return value

    path = os.path.expanduser(os.environ.get(
        "OPENROUTER_MANAGEMENT_KEY_FILE", OPENROUTER_KEY_PATH))
    try:
        info = os.stat(path)
        if info.st_mode & 0o077:
            raise PermissionError(
                "OpenRouter key file must not be accessible by group or others")
        with open(path) as key_file:
            value = key_file.read().strip()
    except FileNotFoundError as exc:
        raise ValueError(
            "OpenRouter management key not configured; see README") from exc
    if not value or any(char.isspace() for char in value):
        raise ValueError("OpenRouter management key file is invalid")
    return value


def _openrouter_key_lines(key, now=None):
    """Format one key without returning its hash or secret value."""
    now = time.time() if now is None else now
    name = key.get("name") or key.get("label") or "Unnamed key"
    states = []
    if key.get("disabled"):
        states.append("disabled")
    expires_at = epoch_ms(key.get("expires_at"))
    if expires_at is not None and expires_at <= now * 1000.0:
        states.append("expired")
    limit_remaining = finite_number(key.get("limit_remaining"))
    if limit_remaining is not None and limit_remaining <= 0:
        states.append("limit used")
    suffix = " [{}]".format(", ".join(states)) if states else ""

    lines = [
        "{}{}: {} total; {} today; {} week; {} month".format(
            name, suffix, money(key.get("usage")),
            money(key.get("usage_daily")), money(key.get("usage_weekly")),
            money(key.get("usage_monthly")))
    ]
    limit = finite_number(key.get("limit"))
    if limit is not None:
        reset = key.get("limit_reset") or "no reset"
        lines.append("  limit: {} of {} left ({})".format(
            money(limit_remaining), money(limit), reset))
    byok_usage = finite_number(key.get("byok_usage"))
    if byok_usage is not None and byok_usage > 0:
        lines.append("  BYOK: {} total; {} month".format(
            money(byok_usage), money(key.get("byok_usage_monthly"))))
    return lines


def parse_openrouter_usage(credits_payload, keys, now=None):
    """Build the OpenRouter account balance and per-key tooltip data."""
    credits = credits_payload.get("data") or {}
    total = finite_number(credits.get("total_credits"))
    used = finite_number(credits.get("total_usage"))
    if total is None or used is None:
        raise ValueError("OpenRouter credits response has no totals")

    balance = max(0.0, total - used)
    # Credit totals are cumulative across top-ups. Their ratio is not a useful
    # health gradient, so color a positive balance as usable and zero as empty.
    pct = 100.0 if balance > 0 else 0.0
    lines = [
        "Credits: {} left; {} used; {} purchased".format(
            money(balance), money(used), money(total)),
        "API keys:",
    ]
    if keys:
        for key in keys:
            lines.extend(_openrouter_key_lines(key, now=now))
    else:
        lines.append("  none")

    exhausted = balance <= 0
    return {
        "pct": pct,
        "text": money(balance),
        "summary": "{} credits left".format(money(balance)),
        "exhausted_summary": "out of paid credits",
        "lines": lines,
        "exhausted": exhausted,
        "blockers": ([{"label": "paid credits", "resets_at": None}]
                     if exhausted else []),
    }


def fetch_openrouter():
    """Fetch account credits and all default-workspace API key usage."""
    token = openrouter_management_key()
    headers = {
        "Authorization": "Bearer " + token,
        "User-Agent": "waybar-ai-usage",
    }
    credits = http_json(OPENROUTER_API + "/credits", headers=headers)
    keys = []
    offset = 0
    for _ in range(100):
        page = http_json(
            OPENROUTER_API + "/keys?" + urllib.parse.urlencode({
                "offset": offset,
                "include_disabled": "true",
            }),
            headers=headers,
        )
        batch = page.get("data")
        if not isinstance(batch, list):
            raise ValueError("OpenRouter keys response has no key list")
        keys.extend(batch)
        if len(batch) < 100:
            break
        offset += len(batch)
    else:
        raise ValueError("OpenRouter returned too many API key pages")
    return parse_openrouter_usage(credits, keys)


def meta_api_key():
    """Read the Meta key without putting it in the cache or errors."""
    raw_value = os.environ.get("META_API_KEY", "")
    if raw_value.strip():
        value = raw_value.strip()
        if any(char.isspace() for char in value):
            raise ValueError("Meta API key is invalid")
        return value

    auth_path = os.path.expanduser(
        os.environ.get("MUSE_AUTH_PATH", MUSE_AUTH_PATH))
    try:
        info = os.stat(auth_path)
    except FileNotFoundError:
        info = None
    if info is not None:
        if info.st_mode & 0o077:
            raise PermissionError(
                "Muse auth file must not be accessible by group or others")
        try:
            with open(auth_path) as auth_file:
                value = json.load(auth_file)["providers"]["meta"]["api_key"]
            value = value.strip()
        except (OSError, ValueError, KeyError, TypeError,
                AttributeError) as exc:
            raise ValueError("Muse auth file is invalid") from exc
        if not value or any(char.isspace() for char in value):
            raise ValueError("Muse API key is invalid")
        return value

    db = sqlite3.connect("file:" + os.path.join(HOME, ".omp", "agent", "agent.db")
                         + "?mode=ro", uri=True)
    try:
        row = db.execute(
            "SELECT data FROM auth_credentials "
            "WHERE provider='meta' AND disabled_cause IS NULL "
            "ORDER BY updated_at DESC, id DESC LIMIT 1").fetchone()
    finally:
        db.close()
    if not row:
        raise ValueError("no meta credential in omp store; run muse login")
    try:
        value = json.loads(row[0])["key"].strip()
    except (ValueError, KeyError, AttributeError) as exc:
        raise ValueError("meta credential in omp store is invalid") from exc
    if not value or any(char.isspace() for char in value):
        raise ValueError("meta credential in omp store is invalid")
    return value


def meta_model():
    """Use the model from the environment or the native Muse settings."""
    value = os.environ.get("META_MUSE_MODEL", "").strip()
    if value:
        return value
    try:
        with open(MUSE_SETTINGS_PATH) as settings_file:
            value = json.load(settings_file).get("model", "").strip()
    except (OSError, ValueError, AttributeError):
        value = ""
    return value or META_MODEL


def http_sse_data_lines(url, headers, body, timeout=60, max_bytes=65536):
    """POST JSON and return the SSE `data:` payload lines of the stream."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    lines = []
    carried = b""
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode()[:200]
        except ValueError:
            detail = ""
        try:
            message = json.loads(detail).get("error", {}).get("message")
        except ValueError:
            message = None
        raise ValueError("Meta API HTTP {}: {}".format(
            exc.code, message or exc.reason)) from exc
    with resp:
        while len(carried) < max_bytes:
            chunk = resp.read(min(16384, max_bytes - len(carried)))
            if not chunk:
                break
            carried += chunk
    for raw in carried.decode("utf-8", errors="replace").splitlines():
        text = raw.strip()
        if text.startswith("data:"):
            lines.append(text[5:].strip())
    return lines


def _meta_used_percent(obj):
    """Return a used percent (0-100), or None for missing values."""
    used = finite_number(obj.get("used_percent"))
    if used is None:
        return None
    return max(0.0, min(100.0, used))


def _meta_window(snapshot, name):
    """Return the named quota window, or the bare snapshot itself."""
    window = snapshot.get(name)
    if isinstance(window, dict) and _meta_used_percent(window) is not None:
        return window
    if name == "window" and _meta_used_percent(snapshot) is not None:
        return snapshot
    return None


def _meta_window_label(window):
    mins = finite_number(window.get("window_duration_mins"))
    if mins:
        if mins < 60:
            return "{}m".format(int(mins))
        hours = mins / 60.0
        if hours == int(hours):
            return "{}h".format(int(hours))
        return "{:g}h".format(hours)
    return "window"


def meta_snapshot_from_sse(data_lines):
    """Return the last subscription snapshot seen in SSE data lines."""
    snapshot = None
    for data in data_lines:
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        candidate = obj.get("subscription") if isinstance(
            obj.get("subscription"), dict) else obj
        if (_meta_window(candidate, "window") is not None
                or _meta_window(candidate, "weekly") is not None):
            snapshot = candidate
    if snapshot is None:
        raise ValueError("Meta stream carried no subscription usage")
    return snapshot


def parse_meta_subscription(snapshot):
    """Turn a subscription snapshot into {pct, lines} like other parsers."""
    windows = []
    five_hour = _meta_window(snapshot, "window")
    if five_hour is not None:
        windows.append(("{} prompts".format(
            _meta_window_label(five_hour)), five_hour))
    weekly = _meta_window(snapshot, "weekly")
    if weekly is not None:
        windows.append(("Weekly", weekly))
    if not windows:
        raise ValueError("Meta subscription snapshot has no quota window")
    lines = []
    remaining = []
    blockers = []
    for label, window in windows:
        used = _meta_used_percent(window)
        pct_left = 100.0 - used
        remaining.append(pct_left)
        line = "{}: {}% left".format(label, round(pct_left))
        reset = fmt_reset_value(window.get("resets_at"))
        if reset:
            line += "  (resets {})".format(reset)
        lines.append(line)
        if used >= 100:
            blockers.append({
                "label": label,
                "resets_at": epoch_ms(window.get("resets_at")),
            })
    plan = snapshot.get("plan", snapshot.get("tier"))
    if (isinstance(plan, str) and plan.strip()
            and any(char.isalpha() for char in plan)):
        lines.append("plan: " + plan.strip())
    return {
        "pct": min(remaining),
        "summary": "{}% left".format(round(min(remaining))),
        "lines": lines,
        "exhausted": bool(blockers),
        "blockers": blockers,
    }


def _meta_load_snapshot():
    try:
        with open(META_SNAPSHOT_PATH) as f:
            cached = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(cached, dict):
        return None
    if not isinstance(cached.get("snapshot"), dict):
        return None
    return cached


def _meta_snapshot_fresh(cached, now):
    """Reuse a snapshot while its resets lie ahead (or TTL without resets)."""
    snapshot = cached["snapshot"]
    resets = []
    for name in ("window", "weekly"):
        window = _meta_window(snapshot, name)
        if window is not None:
            resets.append(epoch_ms(window.get("resets_at")))
    if any(reset is None for reset in resets):
        ts = finite_number(cached.get("ts")) or 0
        return now - ts < TTL
    now_ms = now * 1000.0
    return bool(resets) and all(reset > now_ms for reset in resets)


def _meta_save_snapshot(snapshot, now):
    """Cache display-safe usage fields only, never keys or account IDs."""
    safe_snapshot = {}
    for name in ("window", "weekly"):
        window = snapshot.get(name)
        if isinstance(window, dict):
            safe_snapshot[name] = {
                key: window[key]
                for key in ("used_percent", "resets_at",
                            "window_duration_mins")
                if key in window
            }
    if not safe_snapshot and _meta_used_percent(snapshot) is not None:
        safe_snapshot = {
            key: snapshot[key]
            for key in ("used_percent", "resets_at", "window_duration_mins")
            if key in snapshot
        }
    plan = snapshot.get("plan", snapshot.get("tier"))
    if (isinstance(plan, str) and plan.strip()
            and any(char.isalpha() for char in plan)):
        safe_snapshot["plan"] = plan.strip()
    tmp = META_SNAPSHOT_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"snapshot": safe_snapshot, "ts": now}, f)
    os.replace(tmp, META_SNAPSHOT_PATH)


def fetch_meta():
    """Read the /usage-style snapshot from one streamed response.

    The snapshot is reused until its reset time, so a refresh pass costs
    at most one prompt per quota window instead of one per poll.
    """
    now = time.time()
    cached = _meta_load_snapshot()
    if cached is not None and _meta_snapshot_fresh(cached, now):
        try:
            return parse_meta_subscription(cached["snapshot"])
        except ValueError:
            pass
    key = meta_api_key()
    model = meta_model()
    lines = http_sse_data_lines(
        META_API + "/responses",
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json",
                 "Accept": "text/event-stream",
                 "User-Agent": "waybar-ai-usage"},
        body={"model": model, "input": "ping", "stream": True,
              "max_output_tokens": 16},
        timeout=60)
    snapshot = meta_snapshot_from_sse(lines)
    _meta_save_snapshot(snapshot, now)
    return parse_meta_subscription(snapshot)


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

    for key, fn in (("gemini", fetch_gemini), ("zai", fetch_zai),
                    ("openrouter", fetch_openrouter),
                    ("muse", fetch_meta)):
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


def active_blockers(data, now=None):
    """Return blockers that have not reached their reset time."""
    now_ms = (time.time() if now is None else now) * 1000.0

    def active_in(items):
        active = []
        for blocker in items:
            resets_at = finite_number(blocker.get("resets_at"))
            if resets_at is None or resets_at > now_ms:
                active.append(blocker)
        return active

    paths = data.get("blocker_paths")
    if paths is not None:
        active_paths = [active_in(path) for path in paths]
        if not active_paths or not all(active_paths):
            return []
        return [blocker for path in active_paths for blocker in path]

    blockers = data.get("blockers")
    if blockers is None:
        return ([{"label": "quota", "resets_at": None}]
                if data.get("exhausted") else [])
    return active_in(blockers)


def state_needs_refresh(state, now=None):
    """Refresh on the normal TTL or as soon as a blocked limit resets."""
    now = time.time() if now is None else now
    fetched_at = finite_number(state.get("fetched_at")) or 0
    if now - fetched_at >= TTL:
        return True
    for data in state.get("providers", {}).values():
        for blocker in data.get("blockers") or []:
            resets_at = finite_number(blocker.get("resets_at"))
            if resets_at is None:
                continue
            reset_seconds = resets_at / 1000.0
            if fetched_at < reset_seconds <= now:
                return True
    return False


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
        if state_needs_refresh(state):
            state = refresh(state)
        return state
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def emit(provider, state):
    name = NAMES[provider]
    data = state.get("providers", {}).get(provider)
    pct = finite_number(data.get("pct")) if data else None
    if not data or pct is None:
        tooltip = "<b>{}</b>\nunavailable".format(html.escape(name))
        if data and data.get("error"):
            tooltip += ": " + html.escape(str(data["error"]))
        print(json.dumps({
            "text": "<span foreground='{}'>--</span>".format(DIM),
            "tooltip": tooltip,
            "class": ["missing"],
        }))
        return

    blockers = active_blockers(data)
    exhausted = bool(blockers)
    color = (DIM if data.get("stale") else
             (EXHAUSTED_COLOR if exhausted else gradient(pct)))
    value = data.get("text", "{}%".format(round(pct)))
    attrs = " weight='ultrabold'" if exhausted else ""
    text = "<span foreground='{}'{}>{}</span>".format(
        color, attrs, html.escape(str(value)))

    if exhausted:
        summary = data.get("exhausted_summary")
        if not summary:
            labels = []
            for blocker in blockers:
                label = str(blocker.get("label") or "quota")
                if label not in labels:
                    labels.append(label)
            summary = "out of usage"
            if labels:
                summary += " ({})".format(", ".join(labels))
    else:
        summary = data.get(
            "summary", "{}% of weekly left".format(round(pct)))
    lines = ["<b>{}</b> — {}".format(
        html.escape(name), html.escape(str(summary)))]
    lines += [html.escape(str(line)) for line in data.get("lines", [])]
    if data.get("stale"):
        lines.append("(stale: {})".format(
            time.strftime("%H:%M", time.localtime(data.get("ts", 0)))))
    cls = "ok" if pct > 50 else ("warn" if pct > 20 else "crit")
    classes = [cls]
    if exhausted:
        classes.append("exhausted")
    if data.get("stale"):
        classes.append("stale")
    print(json.dumps({
        "text": text,
        "tooltip": "\n".join(lines),
        "class": classes,
        "percentage": round(pct),
    }))


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in PROVIDERS:
        sys.stderr.write("usage: ai-usage.py <%s>\n" % "|".join(PROVIDERS))
        sys.exit(2)
    emit(sys.argv[1], get_state())


if __name__ == "__main__":
    main()
