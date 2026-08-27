"""Tests for the Waybar AI usage data and output rules."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit


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
        self.assertTrue(result["grok"]["exhausted"])
        self.assertEqual(result["grok"]["blockers"], [
            {"label": "Credits", "resets_at": None},
        ])

    def test_grok_on_demand_is_an_alternative_to_included_quota(self):
        payload = {
            "reports": [{
                "provider": "xai-oauth",
                "limits": [
                    omp_limit(
                        "xai-oauth:credits:1w", 0,
                        status="exhausted", label="Credits"),
                    omp_limit(
                        "xai-oauth:on-demand", 0.5,
                        status="available", label="On-demand"),
                ],
            }],
        }

        result = ai_usage.parse_omp_usage(payload)["grok"]

        self.assertFalse(result["exhausted"])
        self.assertEqual(result["blockers"], [])

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
        self.assertTrue(any(line.startswith("key-100:")
                            for line in result["lines"]))
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
        self.assertNotIn("must-not-appear", "\n".join(result["lines"]))
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
        self.assertIn("out of paid credits", output["tooltip"])
        self.assertEqual(output["class"], ["crit", "exhausted"])
        self.assertEqual(output["percentage"], 0)


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
        self.assertIn("out of usage (5h session)", output["tooltip"])
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


class WaybarStyleTests(unittest.TestCase):
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
