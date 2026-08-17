# waybar-ai-usage

Waybar modules that show how much of your **AI coding subscription quota** is
left this week — one module per provider, colored on a green → yellow → red
gradient.

![AI usage island](screenshots/island.png)

Left to right: OpenAI Codex, Claude, Grok, Gemini, Z.AI. Hovering a module
shows a tooltip with every limit the provider reports (weekly / session /
per-model) and its reset time. Left-click forces a refresh.

In context, on a Hyprland bar:

![Full bar](screenshots/bar.png)

## How it works

`ai-usage.py <provider>` prints one Waybar `custom` module JSON line. All five
module instances share a single cache (`~/.cache/waybar-ai-usage`) guarded by
a lock file, so one bar refresh performs a single fetch pass — not five.
Fetched data stays fresh for 5 minutes; if a provider fetch fails, the last
known value is shown dimmed (up to 3 hours) before falling back to `--`.

| Provider | Data source |
|---|---|
| `codex`, `claude`, `grok` | `omp usage --json` — the [Oh My Pi](https://github.com/acidsugarx/oh-my-pi) CLI aggregates these accounts |
| `gemini` | `cloudcode-pa.googleapis.com` user quota API, using your existing `gemini-cli` login (`~/.gemini/oauth_creds.json`) |
| `zai` | `api.z.ai` quota API, token read from omp's credential store (`~/.omp/agent/agent.db`) |

Each fetcher is a small self-contained function; if you don't use one of
these tools, adapting a fetcher to your own token source is a ~40-line job.

## Requirements

- [Waybar](https://github.com/Alexays/Waybar)
- Python 3 (stdlib only — no pip packages)
- Per provider:
  - **codex / claude / grok** — `omp` CLI, logged into the accounts
  - **gemini** — `gemini-cli`, logged in via Google OAuth
  - **zai** — a Z.AI credential in omp's store

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
- The script also emits `class` (`ok` / `warn` / `crit` / `stale` /
  `missing`) and `percentage`, so you can style states purely in CSS if you
  prefer.

## Notes

- The Gemini client ID/secret embedded in the script belong to
  **gemini-cli's public installed-app OAuth client** — they ship in
  [gemini-cli's source](https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/code_assist/oauth2.ts)
  and are not a leaked secret (Google's docs: for installed apps, "the client
  secret is obviously not treated as a secret"). The script only *reads* your
  `gemini-cli` refresh token and caches its own access token; it never writes
  to gemini-cli's files.
- The script runs read-only against provider APIs and local credential
  stores; the only state it writes lives in `~/.cache/waybar-ai-usage/`.
- Provider logos are trademarks of their respective owners, used here solely
  to identify the services.

## License

[MIT](LICENSE)
