# Codex Harbor user guide

[Back to the homepage](../README.md) · [简体中文主页](../README.zh-CN.md) · [Privacy](../PRIVACY.md)

## Installation and first use

The release requires macOS 14+, Python 3.11+ and a separately installed official Codex desktop app. Supported app names are `Codex.app` and `ChatGPT.app`, in `/Applications` or your user's Applications folder. Open the official app once so its macOS preferences and URL handler exist.

Harbor checks `/opt/homebrew/bin/python3`, `/usr/local/bin/python3`, then `/usr/bin/python3`, and requires Python 3.11 or later. It does not install Python or require additional packages.

Community starts empty. It does not import another Harbor installation, shell routing variables or native login files. Its root is `~/Library/Application Support/Codex Harbor Community/`. Add your own account/API and history choices. After you select an existing Codex directory, its non-routing settings and local customizations are available as defaults as described below.

Use the left **＋** to create an official account entry or a custom API. Official accounts use browser authorization. For an API, enter its Responses base URL without the `/responses` suffix, API key and actual model names. Saving alone does not send a model request. When an API URL changes, the editor asks you to confirm its destination before using a newly entered key for discovery or a probe.

Use the right **＋** to register an absolute history-directory path and a name. Saving a registration does not create an absent directory; creation is a separate explicit action. There is no migration or historical-session cleanup feature.

## Windows and shared history

The same official account or API entry can open multiple independent windows, using the same or different history locations. Select the entry and location, then click **Launch Codex** again. Each window has its own runtime configuration, desktop cache and backend. Reusable private slots preserve settings between launches.

Official windows receive access tokens in memory. One native refresh owner maintains the account's persistent login, preventing simultaneous windows from rotating the same refresh token independently. Logging out inside one Codex window detaches only that window. To change the stored account, close its windows and use Harbor's login action. When upgrading from a version with single-instance authentication, close that account's older Harbor-launched windows before opening the new ones; Harbor does not stop them automatically.

Windows using the same location share native session history. The account/API selected for the current window supplies that window's requests. Finish the current turn before continuing the same conversation elsewhere; avoid simultaneous writes to one conversation. Continuing private history through a Router sends the context to that provider.

Session lists and project organization may require reopening the target window. Different locations are independent and never synchronize automatically.

Local projects, membership, pins and ordering are read from the selected native Codex home. Harbor merges organization changes only into its own cache; it does not write those changes back to the original desktop global state. Cloud-account projects and unrelated UI preferences are excluded.

The model and permission menus are native Codex controls. Harbor does not add a testing permission preset to normal launches. Installing or updating the official Codex app can change compatibility and requires fresh verification.

## History-location availability

Drag location cards to change the automatic-selection order. **Automatic** chooses the first available enabled location. You can always select a specific local or external location instead.

An unavailable manual choice remains unavailable; Harbor does not silently redirect its history to another folder. Connect the drive or choose another location yourself. Close instances using an external drive before ejecting it.

Removing a location removes only its registration, through the recoverable list. Even confirmed permanent removal leaves the history directory and sessions intact.

## Saved Codex settings

From Community 0.9.1, normal relaunches preserve settings saved inside the Harbor-launched Codex window, including the desktop Max visibility switch, appearance, default model, reasoning effort and other user preferences. Harbor refreshes the fields it owns for account isolation, history routing and API transport. A Router model or effort removed from its mappings falls back to a currently configured choice.

Settings belong to the private instance's `config.toml`, not to the history location's original configuration:

- The first available official-account window reuses `accounts/<account-id>/codex/config.toml` under the Community data root. The first API window reuses its existing per-location runtime.
- Additional windows use `instances/<entry-id>/<location-scope>/<slot-id>/codex/config.toml` under the Community data root. A new slot starts with the entry's existing preferences, without copying credentials or history. Reopening reuses an idle slot and retains that slot's edits.

The selected history directory's `config.toml` supplies non-routing defaults, including `[desktop]` (such as the Max visibility switch), project trust, instructions, rules, MCP definitions and plugin preferences. Existing private settings take priority. Unchanged inherited defaults can follow source updates; settings changed or removed inside the Harbor window stay protected. Source login files, provider routing and account-specific remote plugin provisioning are never imported. The selected API and history location remain authoritative. Model availability still determines where Max can be used.

TOML Kit edits only changed fields and retains surrounding comments and formatting. An unchanged configuration is not rewritten. Before the first update of an existing instance config, Harbor keeps its original text in `.harbor-config-before-preservation.toml` beside it. `.harbor-managed-config.toml` records managed fields and inherited defaults. Both are private local files. Invalid configuration stops launch with a controlled error instead of replacing the file.

### Skills and the user environment

Normal launches use your real user home. Codex discovers global `~/.agents/skills` and workspace-local skills using its native rules. Harbor connects user skills from the selected Codex directory's `skills/`, while keeping system skills and writable account/plugin caches independent. Workspace skills remain scoped to their workspace. No history or skill directories are moved.

Global `AGENTS.md`, `AGENTS.override.md` and `rules/*.rules` from the selected directory get private working copies; local edits are preserved and the sources are never written by Harbor. Their tracking file, `.harbor-native-resources.json`, is private. MCP configuration may contain local secrets, so treat the entire private data root as confidential. MCP OAuth credentials and plugin account sessions stay separate; services that require authorization for the new account may still request it.

Your zsh startup files are loaded normally, followed by a small routing guard that restores this window's `CODEX_HOME` and history path and removes conflicting model-provider credential variables. Harbor does not edit your shell files. Installed plugins are still managed by Codex for the selected account; Harbor does not copy another account's plugin cache or credentials. Browser login state remains isolated per instance.

## Model parameters and probes

The selected API exposes all its configured mappings in Codex. An alias affects the picker name; it does not grant an upstream model the aliased model's capabilities. Responses is the only supported wire protocol. There is no Anthropic Messages or Chat Completions conversion layer.

Parameter discovery prefers the selected API's model metadata, then the bundled public catalogs, then a generic preset. The unknown-model preset uses a 128000-token context reference and omits reasoning parameters. These values are editable defaults, not verified provider capabilities.

Manual context or reasoning changes are protected against later discovery. Use **Restore automatic discovery** for the individual field when you want it managed automatically again. Context input accepts formatted numbers such as `1,050,000` and `1.05M` and normalizes valid input. Model catalogs are references; confirm actual capacity with your provider.

Reasoning can use the service default (no parameter), standard Responses levels, or a supported custom field/value mapping. Keep the service default if support is unknown. Max is available only where exposed by the model configuration and Codex settings. Ultra is a Codex orchestration capability, not an arbitrary Router reasoning value.

Single-model and whole-API probes are explicit user actions. Each sends only `Reply OK.` without session history or tools. At most four probes run concurrently, with a **60-second total deadline per model**. Only explicit output-budget exhaustion permits larger attempts: **32, 128, 512, 2048, 8192** output tokens. Other failures do not automatically retry. Attempts may be billed by the selected provider.

A passing probe confirms a completed short response. It does not prove desktop tool compatibility, reasoning-level correctness or session continuation. Validate those separately for the provider you plan to use.

## Official account quotas

Quota reads use the installed Codex interface and do not send a model request. Harbor displays only returned periods, including five-hour or weekly windows when present. Missing periods are not invented as full quota.

The foreground app refreshes due snapshots every five minutes; explicit refresh is also available. Up to ten reads run concurrently. Additional accounts wait for a free slot, and each result appears independently. Results include the last update time. Failed, stale or expired snapshots stay marked as such.

Reset times are absolute dates in `Asia/Shanghai`, labelled Beijing time. They are not countdowns and do not follow a different system timezone.

## Credentials, recovery and native preferences

API keys are stored in a dedicated macOS Keychain namespace; profile JSON contains opaque references. Denied or locked Keychain operations fail rather than storing plaintext. Ad-hoc signed updates can require Keychain reauthorization or re-entering the API key.

Official logins remain native Codex credential files in isolated account homes, protected by user-only filesystem permissions. Metadata such as names, URLs and history paths is local JSON and is not encrypted. Treat the whole data root as private. See the complete [privacy policy](../PRIVACY.md).

Harbor does not inspect disabled or removed history locations during background status refreshes. An enabled Desktop or removable-drive location can still require macOS permission. macOS owns those decisions; because release builds use ad-hoc signing, an update may prompt again. Keep the installed app in a consistent location. Harbor does not reset system permissions or grant itself access.

Some official macOS UI preferences and the `codex://` handler are shared across native instances. Harbor captures a local recovery snapshot before the first desktop launch. Restoration is explicit, requires all Codex instances to be closed, and does not restore account or history files.

Computer Use auxiliary-app refresh is skipped; that feature is outside the acceptance claim. App Management permission is not required merely to run this launcher.

## Build and compatibility

Use the Swift 6 toolchain and Python 3.11+:

```sh
python3 -B -m unittest discover -s tests -p 'test_*.py'
python3 -B build.py --community --universal
```

Build output is placed in `build/<source hash>/`; building neither installs the app nor reads personal configuration. The native Keychain integration suite uses a disposable synthetic keychain and accepts an exact helper path with `--helper`. It makes no live account or model call.

The universal release includes arm64 and x86_64 executables. Runtime acceptance has been performed on Apple Silicon, not a physical Intel Mac. Release builds are ad-hoc signed, not Apple Developer ID signed or notarized. Keychain authorization dialogs, other official Codex versions and every external model service are not implied to have passed acceptance.

Multi-instance official authentication uses Codex's experimental external-token app-server protocol, verified against bundled CLI `0.162.0-alpha.17.2`. An incompatible version stops with a login/compatibility error rather than falling back to concurrent refresh-token files. Recheck this integration when updating Codex.

Development and acceptance take place in a separate private development repository. The public repository receives reviewed snapshots and release artifacts; private Git history and acceptance evidence are not mirrored. See [CONTRIBUTING.md](../CONTRIBUTING.md).
