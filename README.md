# waybar-ai-usage

Waybar modules that show your **AI coding quota and OpenRouter credits**. Each
provider has one module. Quota values use a green → yellow → red gradient.

![AI usage island](screenshots/island.png)

The screenshot shows OpenAI Codex, Claude, Grok, Gemini, and Z.AI. The new
OpenRouter module appears after Z.AI. Hovering a module shows each reported
limit and its reset time. The OpenRouter tooltip shows the account credit
total and usage for each API key. Left-click forces a refresh.

If a hard limit is empty, the value has a soft amber double line.
For example, Z.AI can show a marked `80%` when the weekly pool has 80% left
but the 5-hour pool has 0% left.

In context, on a Hyprland bar:

![Full bar](screenshots/bar.png)

## How it works

`ai-usage.py <provider>` prints one Waybar `custom` module JSON line. All
module instances share one cache (`~/.cache/waybar-ai-usage`) guarded by a
lock file, so one bar refresh performs one fetch pass.
Fetched data stays fresh for 5 minutes; if a provider fetch fails, the last
known value is shown dimmed (up to 3 hours) before falling back to `--`.
If an empty limit resets before 5 minutes, the script refreshes at that reset.

| Provider | Data source |
|---|---|
| `codex`, `claude`, `grok` | `omp usage --json` — the [Oh My Pi](https://github.com/acidsugarx/oh-my-pi) CLI aggregates these accounts |
| `gemini` | `agy -p /usage --output-format json` — [Antigravity CLI](https://antigravity.google/docs/cli/getting-started) (`agy`). Falls back to the old `cloudcode-pa` quota API via `gemini-cli` OAuth (`~/.gemini/oauth_creds.json`) if `agy` is not signed in |
| `zai` | `api.z.ai` quota API, token read from omp's credential store (`~/.omp/agent/agent.db`) |
| `openrouter` | OpenRouter [`/credits`](https://openrouter.ai/docs/api/api-reference/credits/get-credits) and [`/keys`](https://openrouter.ai/docs/api/api-reference/api-keys/list) management APIs |

Each fetcher is a small self-contained function; if you don't use one of
these tools, adapting a fetcher to your own token source is a ~40-line job.

## Requirements

- [Waybar](https://github.com/Alexays/Waybar)
- Python 3 (stdlib only — no pip packages)
- Per provider:
  - **codex / claude / grok** — `omp` CLI, logged into the accounts
  - **gemini** — `agy` (Antigravity CLI), signed in via Google; or `gemini-cli` as a fallback
  - **zai** — a Z.AI credential in omp's store
  - **openrouter** — an OpenRouter management API key

## Install

```sh
git clone https://github.com/KeganHollern/waybar-ai-usage
cd waybar-ai-usage

mkdir -p ~/.config/waybar/scripts ~/.config/waybar/icons/ai
install -m 755 ai-usage.py ~/.config/waybar/scripts/
cp icons/*.svg ~/.config/waybar/icons/ai/
```

Then:

1. Merge [`waybar/config.jsonc`](waybar/config.jsonc) into
   `~/.config/waybar/config.jsonc` and add `"group/ai"` to a module list,
   e.g. `"modules-right": ["group/ai", ...]`.
2. Append [`waybar/style.css`](waybar/style.css) to
   `~/.config/waybar/style.css`.
3. Reload Waybar: `pkill -SIGUSR2 waybar`

Sanity-check a provider from a terminal first:

```sh
~/.config/waybar/scripts/ai-usage.py claude
```

### OpenRouter setup

OpenRouter needs a [management API key](https://openrouter.ai/docs/guides/overview/auth/management-api-keys)
to read the account credit total and the full API key list. A normal API key
can only read its own data. Management keys can change API keys, but this
script only sends `GET` requests.

Put the management key in a private file:

```sh
install -d -m 700 ~/.config/waybar-ai-usage
install -m 600 /path/to/your/key-file \
  ~/.config/waybar-ai-usage/openrouter-management-key
```

The key file must contain only the key. The script rejects a file that is
accessible by the group or by other users. It never writes the key to the
shared cache or tooltip.

You can use `OPENROUTER_MANAGEMENT_API_KEY` instead. You can also set
`OPENROUTER_MANAGEMENT_KEY_FILE` to use a different private file. Make sure
the Waybar process receives these environment variables.

The bar shows the credit balance in USD. A positive balance is green. A zero
balance is amber and has a double line through it. The tooltip shows total,
daily, weekly, and monthly usage for each key in the default workspace. Disabled,
expired, and limit-used keys have a state label.

### Dropping providers

Each provider is an independent module: to skip one, remove its
`custom/ai-<name>` entry and its line in `group/ai`. A refresh pass still
tries every fetcher regardless; providers without credentials just fail
quietly into the shared cache, so removing the module is all that's needed.

## Theming

- **Gradient**: `GREEN` / `YELLOW` / `RED` constants at the top of
  `ai-usage.py` (defaults match the screenshots: `#00ff99` → `#ffee66` →
  `#ff6666`).
- **Icons**: monochrome SVGs tinted via their `fill` attribute (default
  `#33ccff`) — edit the hex in `icons/*.svg` to match your theme.
- **Island**: the `#ai` block in `waybar/style.css` uses a gradient-border
  trick; replace it with your own group styling if you have one.
- The script also emits `class` (`ok` / `warn` / `crit` / `exhausted` /
  `stale` / `missing`) and `percentage`, so you can style states in CSS.

## Notes

- Gemini quota is read from `agy` first (Google replaced Gemini CLI with
  Antigravity CLI). The Gemini client ID/secret still embedded in the script
  belong to **gemini-cli's public installed-app OAuth client** — they ship in
  [gemini-cli's source](https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/code_assist/oauth2.ts)
  and are not a leaked secret (Google's docs: for installed apps, "the client
  secret is obviously not treated as a secret"). They are only used for the
  `gemini-cli` fallback: the script *reads* that refresh token and caches its
  own access token; it never writes to gemini-cli's files.
- The script runs read-only against provider APIs and local credential
  stores; the only state it writes lives in `~/.cache/waybar-ai-usage/`.
- A zero OpenRouter credit balance means that paid requests are out of credit.
  OpenRouter free models can still have separate request limits.
- Provider logos are trademarks of their respective owners, used here solely
  to identify the services.

## Tests

```sh
python3 -m unittest discover -v
```

## License

[MIT](LICENSE)
