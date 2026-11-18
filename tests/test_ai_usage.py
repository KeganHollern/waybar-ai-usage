"""Tests for the Waybar AI usage data and output rules."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit
import xml.etree.ElementTree as ET


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "ai-usage.py"
SPEC = importlib.util.spec_from_file_location("ai_usage", SCRIPT_PATH)
ai_usage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ai_usage)


def emit_json(provider, state):
    """Run emit and return its one JSON object."""
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        ai_usage.emit(provider, state)
    return json.loads(output.getvalue())


def zai_limit(unit, percentage, **values):
    """Make one Z.AI token limit."""
    return {
        "type": "TOKENS_LIMIT",
        "unit": unit,
        "percentage": percentage,
        **values,
    }


def omp_limit(limit_id, fraction, status=None, label=None):
    """Make one OMP quota limit."""
    limit = {
        "id": limit_id,
        "label": label or limit_id,
        "amount": {"remainingFraction": fraction},
        "window": {},
    }
    if status is not None:
        limit["status"] = status
    return limit


class ZaiUsageTests(unittest.TestCase):
    def test_weekly_eighty_with_empty_five_hour_window_is_exhausted(self):
        result = ai_usage.parse_zai_usage({
            "data": {
                "level": "Pro",
                "limits": [
                    zai_limit(6, 20),
                    zai_limit(3, 100),
                ],
            },
        })

        self.assertEqual(result["pct"], 80)
        self.assertEqual(result["lines"], [
            "Weekly: 80% left",
            "5h session: 0% left",
            "plan: Pro",
        ])
        self.assertTrue(result["exhausted"])
        self.assertEqual(result["blockers"], [
            {"label": "5h session", "resets_at": None},
        ])

    def test_exact_counts_take_precedence_over_rounded_percentage(self):
        cases = (
            ({"remaining": 1, "percentage": 100}, False),
            ({"remaining": 0, "percentage": 20}, True),
            ({"usage": 100, "currentValue": 99, "percentage": 100}, False),
            ({"usage": 100, "currentValue": 100, "percentage": 20}, True),
        )

        for limit, expected in cases:
            with self.subTest(limit=limit):
                self.assertEqual(
                    ai_usage._zai_limit_exhausted(limit), expected)

    def test_monthly_unit_and_time_limit_do_not_block_provider(self):
        result = ai_usage.parse_zai_usage({
            "data": {
                "limits": [
                    zai_limit(6, 20),
                    zai_limit(7, 100),
                    {
                        "type": "TIME_LIMIT",
                        "unit": 3,
                        "percentage": 100,
                    },
                ],
            },
        })

        self.assertEqual(result["pct"], 80)
        self.assertIn("Monthly: 0% left", result["lines"])
        self.assertNotIn("5h session: 0% left", result["lines"])
        self.assertFalse(result["exhausted"])
        self.assertEqual(result["blockers"], [])


class OmpUsageTests(unittest.TestCase):
    def test_status_takes_precedence_and_fraction_is_the_fallback(self):
        cases = (
            (omp_limit("one", 0, status="available"), False),
            (omp_limit("two", 1, status="EXHAUSTED"), True),
            (omp_limit("three", 0), True),
            (omp_limit("four", 0.01), False),
        )

        for limit, expected in cases:
            with self.subTest(limit=limit["id"]):
                self.assertEqual(
                    ai_usage._omp_limit_exhausted(limit), expected)

    def test_only_provider_wide_limit_ids_create_blockers(self):
        payload = {
            "reports": [
                {
                    "provider": "openai-codex",
                    "limits": [
                        omp_limit(
                            "openai-codex:primary", 0.8,
                            status="available", label="Weekly"),
                        omp_limit(
                            "openai-codex:secondary", 0.4,
                            status="exhausted", label="Session"),
                        omp_limit(
                            "openai-codex:gpt-model", 0,
                            status="exhausted", label="One model"),
                    ],
                },
                {
                    "provider": "anthropic",
                    "limits": [
                        omp_limit(
                            "anthropic:7d", 0.7,
                            status="available", label="Weekly"),
                        omp_limit("anthropic:5h", 0, label="Session"),
                        omp_limit(
                            "anthropic:sonnet", 0,
                            status="exhausted", label="Sonnet"),
                    ],
                },
                {
                    "provider": "xai-oauth",
                    "limits": [
                        omp_limit(
                            "xai-oauth:credits:1w", 0,
                            status="exhausted", label="Credits"),
                    ],
                },
            ],
        }

        result = ai_usage.parse_omp_usage(payload)

        self.assertEqual(result["codex"]["pct"], 80)
        self.assertEqual(result["codex"]["blockers"], [
            {"label": "Session", "resets_at": None},
        ])
        self.assertEqual(result["claude"]["blockers"], [
            {"label": "Session", "resets_at": None},
        ])
        self.assertNotIn("grok", result)

    def test_healthy_second_account_keeps_provider_available(self):
        payload = {
            "reports": [
                {
                    "provider": "anthropic",
                    "limits": [omp_limit(
                        "anthropic:7d", 0.8, status="exhausted")],
                },
                {
                    "provider": "anthropic",
                    "limits": [omp_limit(
                        "anthropic:7d", 0.6, status="available")],
                },
            ],
        }

        result = ai_usage.parse_omp_usage(payload)["claude"]

        self.assertEqual(result["pct"], 60)
        self.assertFalse(result["exhausted"])
        self.assertEqual(result["blockers"], [])
        self.assertIn("Account 1:", result["lines"])
        self.assertIn("Account 2:", result["lines"])

    def test_account_without_usage_prevents_false_exhaustion(self):
        payload = {
            "reports": [{
                "provider": "anthropic",
                "limits": [omp_limit(
                    "anthropic:7d", 0, status="exhausted")],
            }],
            "accountsWithoutUsage": [{"provider": "anthropic"}],
        }

        result = ai_usage.parse_omp_usage(payload)["claude"]

        self.assertFalse(result["exhausted"])
        self.assertEqual(result["blockers"], [])
        self.assertIn("Another account: usage unavailable", result["lines"])


class GrokUsageTests(unittest.TestCase):
    def test_native_weekly_usage_and_display_safe_fields(self):
        result = ai_usage.parse_grok_usage({
            "config": {
                "creditUsagePercent": 2.0,
                "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY",
                                  "end": "2030-01-01T00:00:00Z"},
                "onDemandCap": {}, "onDemandUsed": {}, "prepaidBalance": {},
                "history": [{"accountId": "must-not-be-cached"}],
            },
            "subscription_tier": "X Premium+",
            "token": "must-not-be-cached",
        })
        self.assertEqual(result["pct"], 98)
        self.assertTrue(result["lines"][0].startswith("Weekly: 98% left"))
        self.assertIn("Prepaid credits: $0.00", result["lines"])
        self.assertIn("plan: X Premium+", result["lines"])
        self.assertEqual(result["source"], "grok-build")
        self.assertFalse(result["exhausted"])
        self.assertNotIn("must-not-be-cached", json.dumps(result))
        output = emit_json("grok", {"providers": {"grok": result}})
        self.assertEqual(output["percentage"], 98)
        self.assertIn("98%", output["text"])
        ET.fromstring("<root>" + output["tooltip"] + "</root>")

    def test_exhaustion_has_a_reset_and_allows_paid_alternatives(self):
        for extra, enabled, exhausted in (
                ({}, None, True),
                ({"prepaidBalance": {"val": 100}}, False, False),
                ({"onDemandCap": {"val": 500}, "onDemandUsed": {}}, True, False),
                ({"onDemandCap": {"val": 500}, "onDemandUsed": {}}, False, True),
                ({"onDemandCap": {"val": 500},
                  "onDemandUsed": {"val": 500}}, True, True)):
            with self.subTest(extra=extra, enabled=enabled):
                result = ai_usage.parse_grok_usage({
                    "config": {"creditUsagePercent": 100,
                               "currentPeriod": {
                                   "type": "USAGE_PERIOD_TYPE_WEEKLY",
                                   "end": "2030-01-01T00:00:00Z"}, **extra},
                    "on_demand_enabled": enabled,
                })
                self.assertEqual(result["exhausted"], exhausted)
                self.assertEqual(result["blockers"], [
                    {"label": "Weekly", "resets_at": 1893456000000},
                ] if exhausted else [])

    def test_legacy_cents_and_zero_scalar_default(self):
        result = ai_usage.parse_grok_usage({"config": {
            "monthlyLimit": {"val": 2000}, "used": {},
        }})
        self.assertEqual(result["pct"], 100)
        result = ai_usage.parse_grok_usage({"config": {
            "monthlyLimit": {"val": 2000}, "used": {"val": 500},
        }})
        self.assertEqual(result["pct"], 75)
        self.assertEqual(result["summary"], "75% of monthly quota left")

    def test_percent_precedes_legacy_counts_and_clamps(self):
        for used, expected in ((0, 100), (20, 80), (120, 0), (-5, 100)):
            with self.subTest(used=used):
                result = ai_usage.parse_grok_usage({"config": {
                    "creditUsagePercent": used,
                    "monthlyLimit": {"val": 2000}, "used": {"val": 1000},
                    "currentPeriod": {"type": "USAGE_PERIOD_TYPE_MONTHLY"},
                }})
                self.assertEqual(result["pct"], expected)
                self.assertTrue(result["lines"][0].startswith("Monthly:"))
                self.assertEqual(result["summary"],
                                 "{}% of monthly quota left".format(expected))

    def test_missing_or_invalid_quota_never_becomes_full_allowance(self):
        for payload in (None, {}, {"config": {}}, {"config": None},
                        {"config": {"creditUsagePercent": True}},
                        {"config": {"creditUsagePercent": "NaN"}},
                        {"config": {"monthlyLimit": {}, "used": {}}}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                ai_usage.parse_grok_usage(payload)

    @contextlib.contextmanager
    def fake_cli(self, mode="ok"):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "grok"
            binary.write_text("#!" + sys.executable + "\n" +
                              "mode = " + repr(mode) + "\n" + textwrap.dedent('''
                import json, os, pathlib, sys, time
                pathlib.Path("pid").write_text(str(os.getpid()))
                assert sys.argv[1:] == ["agent", "--no-leader", "stdio"]
                def read():
                    return json.loads(sys.stdin.readline())
                init = read()
                assert init["method"] == "initialize"
                assert init["params"]["clientCapabilities"] == {}
                print(json.dumps({"jsonrpc": "2.0", "id": init["id"],
                                  "result": {"protocolVersion": 1}}))
                print(json.dumps({"jsonrpc": "2.0", "method": "notice"}), flush=True)
                billing = read()
                assert billing["method"] == "_x.ai/billing"
                assert billing["params"] == {}
                if mode == "eof":
                    sys.exit(0)
                if mode == "timeout":
                    time.sleep(60)
                if mode == "invalid":
                    print("invalid-json", flush=True)
                elif mode == "server_request":
                    print(json.dumps({"id": 99, "method": "session/request_permission"}), flush=True)
                else:
                    response = {"jsonrpc": "2.0", "id": billing["id"],
                                "result": {"config": {"creditUsagePercent": 2}}}
                    if mode == "error":
                        response = {"id": billing["id"],
                                    "error": {"code": -32603, "message": "secret-token"}}
                        print("secret-token", file=sys.stderr)
                    encoded = json.dumps(response) + "\\n"
                    sys.stdout.write(encoded[:10]); sys.stdout.flush()
                    time.sleep(0.01)
                    sys.stdout.write(encoded[10:]); sys.stdout.flush()
                # The client must close this process after the response/error.
                time.sleep(60)
            '''))
            binary.chmod(0o755)
            with mock.patch.object(ai_usage, "grok_binary", return_value=str(binary)), \
                    mock.patch.object(ai_usage, "HOME", tmp):
                yield Path(tmp)
            pid = int((Path(tmp) / "pid").read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_cli_protocol_reads_quota_without_a_prompt(self):
        with self.fake_cli():
            self.assertEqual(ai_usage.fetch_grok()["pct"], 98)

    def test_cli_failures_stop_process_and_do_not_expose_errors(self):
        for mode, error in (("error", RuntimeError), ("invalid", ValueError),
                            ("eof", RuntimeError), ("server_request", RuntimeError)):
            with self.subTest(mode=mode), self.fake_cli(mode):
                with self.assertRaises(error) as caught:
                    ai_usage.grok_billing(timeout=2)
                self.assertNotIn("secret-token", str(caught.exception))

    def test_cli_timeout_stops_process(self):
        with self.fake_cli("timeout"):
            with self.assertRaises(TimeoutError):
                ai_usage.grok_billing(timeout=0.2)

    def test_minimal_waybar_path_finds_native_install(self):
        with mock.patch.object(ai_usage.shutil, "which", return_value=None), \
                mock.patch.object(ai_usage.os, "access", side_effect=(False, True)):
            self.assertEqual(ai_usage.grok_binary(),
                             os.path.join(ai_usage.HOME, ".grok", "bin", "grok"))

    def test_refresh_keeps_grok_independent_from_omp(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(ai_usage, "STATE_PATH", tmp + "/state"))
            for name in ("fetch_gemini", "fetch_zai", "fetch_openrouter", "fetch_meta"):
                stack.enter_context(mock.patch.object(ai_usage, name, return_value={"pct": 50}))
            omp = stack.enter_context(mock.patch.object(ai_usage, "fetch_omp"))
            grok = stack.enter_context(mock.patch.object(ai_usage, "fetch_grok"))
            omp.side_effect = RuntimeError("OMP failed")
            grok.return_value = {"pct": 98, "source": "grok-build"}
            state = ai_usage.refresh({})
            self.assertEqual(state["providers"]["grok"]["pct"], 98)

            omp.side_effect = None
            omp.return_value = {"grok": {"pct": 30}}
            grok.side_effect = RuntimeError("Native usage failed")
            state = ai_usage.refresh(state)
            self.assertEqual(state["providers"]["grok"]["pct"], 98)
            self.assertTrue(state["providers"]["grok"]["stale"])

            old = {"providers": {"grok": {"pct": 30, "ts": time.time()}}}
            state = ai_usage.refresh(old)
            self.assertNotIn("pct", state["providers"]["grok"])
            self.assertEqual(state["providers"]["grok"]["error"], "Native usage failed")


class OpenRouterUsageTests(unittest.TestCase):
    def test_invalid_environment_key_does_not_leak(self):
        secret = "secret-value\nsecond-line"
        with mock.patch.dict(
                ai_usage.os.environ,
                {"OPENROUTER_MANAGEMENT_API_KEY": secret}, clear=False):
            with self.assertRaises(ValueError) as error:
                ai_usage.openrouter_management_key()

        self.assertNotIn("secret-value", str(error.exception))

    def test_fetch_uses_auth_and_gets_all_key_pages(self):
        test_key = "unit-test-management-key"
        first_page = [{"name": "key-{}".format(i)} for i in range(100)]
        second_page = [{"name": "key-100"}]
        key_urls = []
        auth_values = []

        def fake_http_json(url, headers=None, **_kwargs):
            auth_values.append((headers or {}).get("Authorization"))
            if url.endswith("/credits"):
                return {"data": {"total_credits": 20, "total_usage": 5}}

            key_urls.append(url)
            query = parse_qs(urlsplit(url).query)
            if query.get("offset") == ["0"]:
                return {"data": first_page}
            if query.get("offset") == ["100"]:
                return {"data": second_page}
            self.fail("unexpected OpenRouter key request")

        with mock.patch.object(
                ai_usage, "openrouter_management_key", return_value=test_key), \
                mock.patch.object(
                    ai_usage, "http_json", side_effect=fake_http_json):
            result = ai_usage.fetch_openrouter()

        self.assertEqual(auth_values, ["Bearer " + test_key] * 3)
        self.assertEqual([
            parse_qs(urlsplit(url).query) for url in key_urls
        ], [
            {"offset": ["0"], "include_disabled": ["true"]},
            {"offset": ["100"], "include_disabled": ["true"]},
        ])
        self.assertTrue(any(line.startswith("key-0:")
                            for line in result["lines"]))
        self.assertFalse(any(line.startswith("key-100:")
                             for line in result["lines"]))
        self.assertEqual(len(result["openrouter"]["keys"]), 10)
        self.assertEqual(result["openrouter"]["hidden_key_count"], 91)
        self.assertIn("91 more keys are not shown", result["lines"])
        output = emit_json("openrouter", {
            "providers": {"openrouter": result},
        })
        self.assertIn("91 more keys are not shown", output["tooltip"])
        self.assertNotIn(test_key, json.dumps(result))

    def test_balance_and_key_lines_are_formatted(self):
        result = ai_usage.parse_openrouter_usage(
            {"data": {"total_credits": 100, "total_usage": 25}},
            [{
                "name": "R&D <prod>",
                "usage": 12.5,
                "usage_daily": 0.005,
                "usage_weekly": 1.25,
                "usage_monthly": 4,
                "limit": 10,
                "limit_remaining": 0,
                "limit_reset": "monthly <cycle>",
                "byok_usage": 0.2,
                "byok_usage_monthly": 0.1,
                "disabled": True,
                "expires_at": 900,
                "hash": "must-not-appear",
            }],
            now=1000,
        )

        self.assertEqual(result["pct"], 100)
        self.assertEqual(result["text"], "$75.00")
        self.assertEqual(result["summary"], "$75.00 credits left")
        self.assertEqual(result["lines"], [
            "Credits: $75.00 left; $25.00 used; $100.00 purchased",
            "API keys:",
            ("R&D <prod> [disabled, expired, limit used]: $12.50 total; "
             "$0.0050 today; $1.25 week; $4.00 month"),
            "  limit: $0.00 of $10.00 left (monthly <cycle>)",
            "  BYOK: $0.20 total; $0.10 month",
        ])
        self.assertNotIn("must-not-appear", json.dumps(result))
        self.assertFalse(result["exhausted"])

    def test_emit_escapes_openrouter_key_text(self):
        data = ai_usage.parse_openrouter_usage(
            {"data": {"total_credits": 100, "total_usage": 25}},
            [{
                "name": "R&D <prod>",
                "usage": 1,
                "usage_daily": 0,
                "usage_weekly": 1,
                "usage_monthly": 1,
                "limit": 5,
                "limit_remaining": 4,
                "limit_reset": "monthly <cycle>",
            }],
            now=1000,
        )

        output = emit_json("openrouter", {"providers": {"openrouter": data}})

        self.assertIn("$75.00", output["text"])
        self.assertIn("R&amp;D &lt;prod&gt;", output["tooltip"])
        self.assertIn("monthly &lt;cycle&gt;", output["tooltip"])
        self.assertNotIn("R&D <prod>", output["tooltip"])
        self.assertIn(">ACCOUNT</span>", output["tooltip"])
        self.assertIn(">API KEYS</span>", output["tooltip"])
        ET.fromstring("<root>" + output["tooltip"] + "</root>")
        self.assertEqual(output["class"], ["ok"])
        self.assertEqual(output["percentage"], 100)

    def test_zero_credit_emit_uses_dollars_and_exhausted_style(self):
        data = ai_usage.parse_openrouter_usage(
            {"data": {"total_credits": 10, "total_usage": 10}},
            [],
        )

        output = emit_json("openrouter", {"providers": {"openrouter": data}})

        self.assertIn("$0.00", output["text"])
        self.assertNotIn("0%", output["text"])
        self.assertNotIn("strikethrough", output["text"])
        self.assertNotIn("\N{COMBINING LONG STROKE OVERLAY}", output["text"])
        self.assertIn("weight='ultrabold'", output["text"])
        self.assertIn("foreground='#ffcc66'", output["text"])
        self.assertIn("Out of paid credits", output["tooltip"])
        self.assertEqual(output["class"], ["crit", "exhausted"])
        self.assertEqual(output["percentage"], 0)

    def test_old_cache_tooltip_handles_colons_and_control_characters(self):
        data = {
            "pct": 100,
            "text": "$1.00",
            "summary": "$1.00 credits left",
            "lines": [
                "Model: R&D\x00: 50% left  (resets 03:07)",
                "note: first line\nfalse section",
            ],
        }

        output = emit_json("openrouter", {
            "providers": {"openrouter": data},
        })

        self.assertIn(">Model: R&amp;D </span>", output["tooltip"])
        self.assertIn("Resets 03:07", output["tooltip"])
        self.assertIn("first line false section", output["tooltip"])
        self.assertNotIn("\x00", output["tooltip"])
        ET.fromstring("<root>" + output["tooltip"] + "</root>")


class MetaSubscriptionTests(unittest.TestCase):
    def test_native_muse_auth_precedes_omp_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth_path = Path(tmp) / "auth.json"
            auth_path.write_text(json.dumps({
                "providers": {"meta": {"api_key": "native-muse-key"}},
            }))
            auth_path.chmod(0o600)
            with mock.patch.dict(ai_usage.os.environ, {}, clear=True), \
                    mock.patch.object(
                        ai_usage, "MUSE_AUTH_PATH", str(auth_path)):
                value = ai_usage.meta_api_key()

        self.assertEqual(value, "native-muse-key")

    def test_omp_fallback_uses_newest_active_meta_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / ".omp" / "agent" / "agent.db"
            db_path.parent.mkdir(parents=True)
            db = sqlite3.connect(db_path)
            try:
                db.execute(
                    "CREATE TABLE auth_credentials ("
                    "id INTEGER PRIMARY KEY, provider TEXT, data TEXT, "
                    "disabled_cause TEXT, updated_at INTEGER)")
                db.executemany(
                    "INSERT INTO auth_credentials VALUES (?, 'meta', ?, ?, ?)",
                    [
                        (1, json.dumps({"key": "old-key"}), None, 10),
                        (2, json.dumps({"key": "disabled-key"}), "old", 30),
                        (3, json.dumps({"key": "new-key"}), None, 20),
                    ])
                db.commit()
            finally:
                db.close()
            with mock.patch.dict(ai_usage.os.environ, {}, clear=True), \
                    mock.patch.object(ai_usage, "HOME", tmp), \
                    mock.patch.object(
                        ai_usage, "MUSE_AUTH_PATH", str(Path(tmp) / "missing")):
                value = ai_usage.meta_api_key()

        self.assertEqual(value, "new-key")

    def test_model_comes_from_native_muse_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "settings.json"
            settings_path.write_text(json.dumps({
                "model": "muse-spark-test-contributor",
            }))
            with mock.patch.dict(ai_usage.os.environ, {}, clear=True), \
                    mock.patch.object(
                        ai_usage, "MUSE_SETTINGS_PATH", str(settings_path)):
                value = ai_usage.meta_model()

        self.assertEqual(value, "muse-spark-test-contributor")

    def test_window_and_weekly_parse_with_plan(self):
        now_ms = int(time.time() * 1000)
        result = ai_usage.parse_meta_subscription({
            "window": {
                "used_percent": 27,
                "resets_at": now_ms + 3600 * 1000,
                "window_duration_mins": 300,
            },
            "weekly": {"used_percent": 40},
            "plan": "Everyday Usage",
        })

        self.assertEqual(result["pct"], 60)
        self.assertEqual(result["summary"], "60% left")
        self.assertTrue(
            result["lines"][0].startswith("5h prompts: 73% left"))
        self.assertIn("  (resets ", result["lines"][0])
        self.assertEqual(result["lines"][1], "Weekly: 60% left")
        self.assertEqual(result["lines"][2], "plan: Everyday Usage")
        self.assertFalse(result["exhausted"])
        self.assertEqual(result["blockers"], [])

    def test_numeric_plan_id_is_not_shown(self):
        result = ai_usage.parse_meta_subscription({
            "window": {"used_percent": 27},
            "weekly": {"used_percent": 40},
            "plan": "27681527378179523",
        })

        self.assertFalse(any(line.startswith("plan:")
                             for line in result["lines"]))

    def test_exhausted_window_sets_blocker(self):
        now_ms = int(time.time() * 1000)
        result = ai_usage.parse_meta_subscription({
            "window": {
                "used_percent": 100,
                "resets_at": now_ms + 3600 * 1000,
                "window_duration_mins": 300,
            },
            "weekly": {"used_percent": 10},
        })

        self.assertEqual(result["pct"], 0)
        self.assertTrue(result["exhausted"])
        self.assertEqual(result["blockers"], [
            {"label": "5h prompts", "resets_at": float(now_ms + 3600 * 1000)},
        ])

    def test_bare_window_snapshot_has_no_weekly_line(self):
        result = ai_usage.parse_meta_subscription({"used_percent": 10})

        self.assertEqual(result["pct"], 90)
        self.assertEqual(result["lines"], ["window prompts: 90% left"])
        self.assertFalse(result["exhausted"])

    def test_window_labels_follow_duration(self):
        self.assertEqual(
            ai_usage._meta_window_label({"window_duration_mins": 300}), "5h")
        self.assertEqual(
            ai_usage._meta_window_label({"window_duration_mins": 45}), "45m")
        self.assertEqual(ai_usage._meta_window_label({}), "window")

    def test_sse_takes_last_snapshot_and_skips_noise(self):
        snapshot = ai_usage.meta_snapshot_from_sse([
            '{"type": "response.created", "response": {"id": "r1"}}',
            "not json",
            json.dumps({"subscription": {
                "window": {"used_percent": 50},
            }}),
            "[DONE]",
            json.dumps({
                "window": {"used_percent": 20, "window_duration_mins": 300},
                "weekly": {"used_percent": 30},
            }),
        ])

        self.assertEqual(snapshot["weekly"]["used_percent"], 30)

    def test_sse_without_snapshot_raises(self):
        with self.assertRaises(ValueError):
            ai_usage.meta_snapshot_from_sse([
                '{"type": "response.completed"}',
                "[DONE]",
            ])

    def test_invalid_environment_key_does_not_leak(self):
        secret = "secret-value with-space"
        with mock.patch.dict(
                ai_usage.os.environ,
                {"META_API_KEY": secret}, clear=False):
            with self.assertRaises(ValueError) as error:
                ai_usage.meta_api_key()

        self.assertNotIn("secret-value", str(error.exception))

    def test_fetch_reuses_fresh_snapshot_without_http(self):
        snapshot = {
            "window": {
                "used_percent": 25,
                "resets_at": int(time.time() * 1000) + 7200 * 1000,
                "window_duration_mins": 300,
            },
            "weekly": {"used_percent": 10},
        }

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "meta.json")
            Path(path).write_text(json.dumps(
                {"snapshot": snapshot, "ts": time.time()}))
            with mock.patch.object(
                    ai_usage, "META_SNAPSHOT_PATH", path), \
                    mock.patch.object(
                        ai_usage, "http_sse_data_lines",
                        side_effect=AssertionError("must not fetch")), \
                    mock.patch.object(
                        ai_usage, "meta_api_key",
                        side_effect=AssertionError("must not read key")):
                result = ai_usage.fetch_meta()

        self.assertEqual(result["pct"], 75)

    def test_fetch_streams_and_caches_when_no_snapshot(self):
        now_ms = int(time.time() * 1000) + 3600 * 1000
        payload = {"window": {
            "used_percent": 12,
            "resets_at": now_ms,
            "window_duration_mins": 300,
            "account_id": "must-not-be-cached",
        }, "plan": "27681527378179523"}
        seen = {}

        def fake_sse(url, headers=None, body=None, timeout=None):
            seen["url"] = url
            seen["headers"] = headers
            seen["body"] = body
            return [
                '{"type": "response.created"}',
                json.dumps({
                    "type": "response.subscription_usage",
                    "subscription": payload,
                }),
            ]

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "meta.json")
            with mock.patch.object(
                    ai_usage, "META_SNAPSHOT_PATH", path), \
                    mock.patch.object(
                        ai_usage, "meta_api_key",
                        return_value="unit-test-key"), \
                    mock.patch.object(
                        ai_usage, "http_sse_data_lines",
                        side_effect=fake_sse):
                result = ai_usage.fetch_meta()

            saved = json.loads(Path(path).read_text())

        self.assertEqual(result["pct"], 88)
        self.assertEqual(seen["url"], "https://api.meta.ai/v1/responses")
        self.assertEqual(
            seen["headers"]["Authorization"], "Bearer unit-test-key")
        self.assertTrue(seen["body"]["stream"])
        self.assertEqual(seen["body"]["max_output_tokens"], 16)
        self.assertEqual(saved["snapshot"], {"window": {
            "used_percent": 12,
            "resets_at": now_ms,
            "window_duration_mins": 300,
        }})
        self.assertNotIn("unit-test-key", json.dumps(saved))
        self.assertNotIn("must-not-be-cached", json.dumps(saved))
        self.assertNotIn("27681527378179523", json.dumps(saved))

    def test_emit_shows_muse_percentage(self):
        output = emit_json("muse", {
            "providers": {"muse": {
                "pct": 60,
                "summary": "60% left",
                "lines": ["5h prompts: 60% left", "Weekly: 60% left"],
                "exhausted": False,
                "blockers": [],
            }},
        })

        self.assertIn("60%", output["text"])
        self.assertIn(">USAGE</span>", output["tooltip"])
        ET.fromstring("<root>" + output["tooltip"] + "</root>")
        self.assertEqual(output["class"], ["ok"])
        self.assertEqual(output["percentage"], 60)


class BlockerAndOutputTests(unittest.TestCase):
    def test_active_blockers_omit_limits_at_or_after_reset(self):
        data = {
            "blockers": [
                {"label": "past", "resets_at": 999_000},
                {"label": "now", "resets_at": 1_000_000},
                {"label": "future", "resets_at": 1_001_000},
                {"label": "unknown", "resets_at": None},
            ],
        }

        self.assertEqual(ai_usage.active_blockers(data, now=1000), [
            {"label": "future", "resets_at": 1_001_000},
            {"label": "unknown", "resets_at": None},
        ])

    def test_legacy_exhausted_value_is_an_active_blocker(self):
        self.assertEqual(
            ai_usage.active_blockers({"exhausted": True}, now=1000),
            [{"label": "quota", "resets_at": None}],
        )
        self.assertEqual(
            ai_usage.active_blockers({"exhausted": False}, now=1000), [])

    def test_alternative_path_reset_clears_provider_block(self):
        data = {
            "blocker_paths": [
                [{"label": "Gemini", "resets_at": 999_000}],
                [{"label": "Claude", "resets_at": None}],
            ],
        }

        self.assertEqual(ai_usage.active_blockers(data, now=1000), [])
        self.assertEqual(
            [item["label"] for item in ai_usage.active_blockers(data, now=998)],
            ["Gemini", "Claude"],
        )

    def test_reset_after_fetch_requests_early_cache_refresh(self):
        state = {
            "fetched_at": 950,
            "providers": {
                "zai": {
                    "blockers": [
                        {"label": "5h session", "resets_at": 980_000},
                    ],
                },
            },
        }

        self.assertTrue(ai_usage.state_needs_refresh(state, now=1000))
        self.assertFalse(ai_usage.state_needs_refresh(state, now=970))

        state["providers"]["zai"]["blockers"][0]["resets_at"] = 940_000
        self.assertFalse(ai_usage.state_needs_refresh(state, now=1000))

    def test_exhausted_emit_strikes_value_and_adds_class(self):
        state = {
            "providers": {
                "zai": {
                    "pct": 80,
                    "lines": ["Weekly: 80% left", "5h session: 0% left"],
                    "exhausted": True,
                    "blockers": [
                        {"label": "5h session", "resets_at": None},
                    ],
                },
            },
        }

        output = emit_json("zai", state)

        self.assertNotIn("strikethrough", output["text"])
        self.assertNotIn("\N{COMBINING LONG STROKE OVERLAY}", output["text"])
        self.assertIn("80%", output["text"])
        self.assertIn("weight='ultrabold'", output["text"])
        self.assertIn("foreground='#ffcc66'", output["text"])
        self.assertIn("Out of usage: 5h session", output["tooltip"])
        self.assertIn(">USAGE</span>", output["tooltip"])
        self.assertTrue(output["tooltip"].startswith(
            "<span size='small' weight='normal'>"))
        ET.fromstring("<root>" + output["tooltip"] + "</root>")
        self.assertEqual(output["class"], ["ok", "exhausted"])
        self.assertEqual(output["percentage"], 80)

    def test_normal_emit_does_not_strike_value(self):
        state = {
            "providers": {
                "zai": {
                    "pct": 80,
                    "lines": ["Weekly: 80% left", "5h session: 50% left"],
                    "exhausted": False,
                    "blockers": [],
                },
            },
        }

        output = emit_json("zai", state)

        self.assertNotIn("strikethrough", output["text"])
        self.assertIn("80% of weekly left", output["tooltip"])
        self.assertEqual(output["class"], ["ok"])
        self.assertEqual(output["percentage"], 80)

    def test_missing_tooltip_escapes_the_error(self):
        output = emit_json("codex", {
            "providers": {"codex": {"error": "bad <token> & value"}},
        })

        self.assertIn("Usage data is not available", output["tooltip"])
        self.assertIn("bad &lt;token&gt; &amp; value", output["tooltip"])
        self.assertNotIn("bad <token>", output["tooltip"])
        ET.fromstring("<root>" + output["tooltip"] + "</root>")
        self.assertEqual(output["class"], ["missing"])


class WaybarStyleTests(unittest.TestCase):
    def test_tooltip_style_matches_the_desktop_popup_theme(self):
        css = (SCRIPT_PATH.parent / "waybar" / "style.css").read_text()
        tooltip_rule = css[css.index("tooltip {"):]
        tooltip_rule = tooltip_rule[:tooltip_rule.index("}")]
        label_rule = css[css.index("tooltip label {"):]
        label_rule = label_rule[:label_rule.index("}")]

        self.assertIn("rgba(17, 17, 27, 0.96)", tooltip_rule)
        self.assertIn("1px solid rgba(51, 204, 255, 0.38)", tooltip_rule)
        self.assertIn("border-radius: 12px", tooltip_rule)
        self.assertIn("box-shadow: 0 12px 30px", tooltip_rule)
        self.assertNotIn("font-size", label_rule)
        self.assertNotIn("font-weight", label_rule)
        self.assertIn("padding: 10px 12px", label_rule)

    def test_exhausted_style_uses_a_double_amber_strike_without_background(self):
        css = (SCRIPT_PATH.parent / "waybar" / "style.css").read_text()
        exhausted_rule = css[css.index("#custom-ai-codex.exhausted"):]
        exhausted_rule = exhausted_rule[:exhausted_rule.index("}")]

        self.assertIn("#custom-ai-zai.exhausted", exhausted_rule)
        self.assertIn("text-decoration-line: line-through", exhausted_rule)
        self.assertIn("text-decoration-style: double", exhausted_rule)
        self.assertIn("text-decoration-color: #ffcc66", exhausted_rule)
        self.assertNotIn("background", exhausted_rule)


if __name__ == "__main__":
    unittest.main()
