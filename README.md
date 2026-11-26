# waybar-ai-usage

Waybar modules that show your **AI coding quota and OpenRouter credits**. Each
provider has one module. Quota values use a green → yellow → red gradient.

![AI usage island](screenshots/island.png)

The screenshot shows OpenAI Codex, Claude, Grok, Gemini, and Z.AI. The new
OpenRouter module appears after Z.AI, and the Muse module appears after
OpenRouter. Hovering a module shows a formatted
summary, each reported limit, and its reset time. The OpenRouter tooltip shows
the account credit total and usage for each API key. Left-click forces a refresh.

If a hard limit is empty, the value has a soft amber double line.
For example, Z.AI can show a marked `80%` when the weekly pool has 80% left
but the 5-hour pool has 0% left.

In context, on a Hyprland bar:

![Full bar](screenshots/bar.png)

## How it works

`ai-usage.py <provider>` prints one Waybar `custom` module JSON line.
Each module fetches its own source. Codex and Claude share one OMP request.
Each source can save its results while another source waits for a response.
The modules share one cache in `~/.cache/waybar-ai-usage`.

The script checks for fresh data every 5 minutes or on a manual refresh.
It also checks for a quota reset when the module runs.
If a request fails, the module shows the last value in dim text for up to
3 hours after the last successful request. Then it shows `--`.

For an upgrade, copy the new `on-click` commands from `waybar/config.jsonc`.
They mark the cache for a refresh and keep the last successful values.

| Provider | Data source |
|---|---|
| `codex`, `claude` | `omp usage --json` — the [Oh My Pi](https://github.com/acidsugarx/oh-my-pi) CLI aggregates these accounts |
| `grok` | Grok Build CLI's native ACP billing interface, with the Grok Build login |
| `gemini` | `agy -p /usage --output-format json` — [Antigravity CLI](https://antigravity.google/docs/cli/getting-started) (`agy`). Falls back to the old `cloudcode-pa` quota API via `gemini-cli` OAuth (`~/.gemini/oauth_creds.json`) if `agy` is not signed in |
| `zai` | `api.z.ai` quota API, token read from omp's credential store (`~/.omp/agent/agent.db`) |
| `openrouter` | OpenRouter [`/credits`](https://openrouter.ai/docs/api/api-reference/credits/get-credits) and [`/keys`](https://openrouter.ai/docs/api/api-reference/api-keys/list) management APIs |
| `muse` | Meta Model API response stream (`POST https://api.meta.ai/v1/responses` with `stream: true`) — the same subscription usage snapshot the `muse` `/usage` command shows |

Each fetcher is a small self-contained function; if you don't use one of
these tools, adapting a fetcher to your own token source is a ~40-line job.

## Requirements

- [Waybar](https://github.com/Alexays/Waybar)
- Python 3 (stdlib only — no pip packages)
- Per provider:
  - **codex / claude** — `omp` CLI, logged into the accounts
  - **grok** — Grok Build CLI, authenticated with `grok login`
  - **gemini** — `agy` (Antigravity CLI), signed in via Google; or `gemini-cli` as a fallback
  - **zai** — a Z.AI credential in omp's store
  - **openrouter** — an OpenRouter management API key
  - **muse** — `muse login` (key read from omp's store), or `META_API_KEY` in the Waybar environment

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

### Grok Build setup

Install [Grok Build](https://docs.x.ai/build/overview), then run `grok login`.
The module finds `grok` on `PATH`, in `~/.local/bin`, or in `~/.grok/bin`.
This also works with Waybar's minimal `PATH`.

The module starts `grok agent --no-leader stdio` and sends the ACP
`initialize` and `_x.ai/billing` requests. This is the native interface that
Grok Build's `/usage` view uses. Grok Build manages the login and token refresh.
The module sends no prompt and creates no conversation. It stops the CLI
process after the response or a timeout.

Grok Build 1.0.89 rejects `grok usage --json` on its native shell route.
The ACP billing interface works on that route. The module uses this interface
directly and does not use OMP for Grok data.

The value shows the percentage of the included quota that is left.
The tooltip shows the reset time, plan, prepaid balance, and available on-demand
credit. Prepaid credit or available on-demand credit prevents a false exhausted
state when the included quota is empty. The cache keeps only display data.
Old OMP Grok data cannot replace native Grok data after a failed request.

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

### Muse setup

The Muse module reads the subscription snapshot from one minimal streamed
response (`ping`, max 16 output tokens). It uses the same event the `muse`
`/usage` command shows: 5-hour prompt use, weekly use, and reset times.

Key lookup order:

1. `META_API_KEY` in the Waybar environment.
2. The native Muse login file (`~/.config/muse/auth.json`), written by
   `muse login`. Set `MUSE_AUTH_PATH` if you use a different path.
3. The newest active `meta` credential in omp's store
   (`~/.omp/agent/agent.db`) as a fallback. The script opens both stores
   read-only.

Cost control: the snapshot is cached in
`~/.cache/waybar-ai-usage/meta-subscription.json` (usage data only, never
keys or account IDs) and reused until its reset time. A refresh pass costs at
most one prompt per quota window, not one per poll. A left-click removes this
snapshot and gets a new value. Set `META_MUSE_MODEL` to change the model used
for the probe. By default, the script uses the model in the Muse settings and
then falls back to `muse-spark-1.3`.

If the module shows `401 invalid_api_key`, the stored login key is stale:
re-run `muse login` (or any omp Meta request, which refreshes it) and force
a refresh with a left-click.

### Dropping providers

Remove the provider from `group/ai` to stop its requests. If either Codex
or Claude is active, OMP still reports both in one request.

## Theming

- **Gradient**: `GREEN` / `YELLOW` / `RED` constants at the top of
  `ai-usage.py` (defaults match the screenshots: `#00ff99` → `#ffee66` →
  `#ff6666`).
- **Icons**: monochrome SVGs tinted via their `fill` attribute (default
  `#33ccff`) — edit the hex in `icons/*.svg` to match your theme.
- **Island**: the `#ai` block in `waybar/style.css` uses a gradient-border
  trick; replace it with your own group styling if you have one.
- **Tooltip**: the `tooltip` blocks change the surface, padding, and base text
  color for all Waybar tooltips. They match the desktop popups. The script adds
  section and value styles only to the AI usage tooltips.
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
- The script reads provider APIs and local credential stores.
  Its own state stays in `~/.cache/waybar-ai-usage/`.
  Provider CLIs can update their own login files, logs, and caches.
  The Muse probe is the one exception: one minimal streamed response per
  quota window (see Muse setup).
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
