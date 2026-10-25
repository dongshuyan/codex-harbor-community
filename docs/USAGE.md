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

Official-account model requests connect directly through Codex's native transport. Harbor does not forward those requests, add tool declarations, or override native compaction, WebSocket or reconnection behavior. Account coordination still keeps concurrent windows from refreshing the same credential independently. Only custom APIs use the local Router bridge for model and parameter mapping.

Community 0.10.9 replaces the earlier official HTTP bridge. Tests with the bundled CLI `0.163.0-alpha.11` passed Router-to-official continuation, hosted-search history and native compaction without modifying stored conversations. Launch a new Codex window after upgrading, then reopen the existing chat; windows already running keep their previous components. Model limits and provider capabilities still apply.

Session lists and project organization may require reopening the target window. Different locations are independent and never synchronize automatically.

New windows using the same resolved history directory and volume share local projects, pins and ordering. Open windows keep their current view. Changes merge automatically without a confirmation dialog: independent pins are kept, unchanged older windows cannot undo newer changes, and conflicting older edits keep the committed shared value. Conflicting reorders keep the committed relative order while retaining new entries. Selection and expansion remain individual window preferences.

Harbor verifies the native sidebar cache at startup and requests a native rebuild when needed. A running window is not treated as proof of a complete catalog. Missing file references, unindexed rollout files and incomplete scans remain visible as unverified results; the original history is not repaired or deleted automatically.

The common local organization record lives under `~/Library/Application Support/Codex Harbor Shared/organization/`. Credentials, API configuration and cloud-account projects remain separate. Integrated native entry points may update only allowlisted local organization fields in an idle original profile. Directly opening the official app bypasses this check and does not guarantee the latest shared organization.

The shared-startup adapter is verified for desktop `26.1021.31345`, build `21828`. An unverified desktop version stops the affected launch before shared-state projection. Existing windows continue to work. Check Harbor compatibility before updating the official app. Smart integration and native shortcut installation are separate opt-in delivery steps; Community does not install either automatically.

The model and permission menus are native Codex controls. Harbor does not add a testing permission preset to normal launches. Installing or updating the official Codex app can change compatibility and requires fresh verification.

From Community 0.10.7, Harbor leaves the native **Check for Updates…** menu enabled. Before updating Codex, finish your work and quit the other Codex instances. After the update, reopen the desired account and history location from Harbor. The native updater’s automatic relaunch does not retain Harbor’s launch settings; do not treat that window as the previous Harbor instance.

## Interrupted connections

Official connections use Codex's native network handling. Sleep, network changes or an unavailable service can still interrupt a turn. Restoring native transport removes Harbor's HTTP bridge from this path; it does not guarantee uninterrupted generation.

Each Router request uses its own upstream connection. When Codex disconnects or the instance closes, Harbor cancels the corresponding connection and releases its request slot. A later request uses a fresh connection. Complete response lengths are preserved, and incomplete streams remain detectable by the client.

Router connections have a 30-second timeout per TCP/TLS setup operation and a 120-second read-inactivity timeout. Cancellation during DNS, TCP or TLS setup waits for setup to return or fail; DNS and attempts across multiple addresses can exceed 30 seconds in total. These are not whole-request time limits. Harbor does not replay model requests automatically; Codex may apply its own retry policy. After an interruption, check the turn's outcome before sending it again.

Local gateway events contain fixed failure categories such as timeout or incomplete response, without request bodies, key values or upstream error text. A completed HTTP transfer does not by itself prove the model emitted `response.completed`.

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

From Community 0.10.8, a fresh official-account configuration leaves the model and reasoning effort to Codex instead of supplying launcher defaults. Saved choices remain intact. Ordinary history settings and an explicit log directory are inherited; relative log paths resolve against the source configuration's directory. Explicitly sharing a log directory can combine logs from several instances.

TOML Kit edits only changed fields and retains surrounding comments and formatting. An unchanged configuration is not rewritten. Before the first update of an existing instance config, Harbor keeps its original text in `.harbor-config-before-preservation.toml` beside it. `.harbor-managed-config.toml` records managed fields and inherited defaults. Both are private local files. Invalid configuration stops launch with a controlled error instead of replacing the file.

### Skills and the user environment

Normal launches use your real user home. Codex discovers global `~/.agents/skills` and workspace-local skills using its native rules. Harbor connects user skills from the selected Codex directory's `skills/`, while keeping system skills and writable account/plugin caches independent. Workspace skills remain scoped to their workspace. No history or skill directories are moved.

Global `AGENTS.md`, `AGENTS.override.md` and `rules/*.rules` from the selected directory get private working copies; local edits are preserved and the sources are never written by Harbor. Their tracking file, `.harbor-native-resources.json`, is private. MCP configuration may contain local secrets, so treat the entire private data root as confidential. Plugin account sessions remain separate; services that require authorization for the new account may still request it.

MCP OAuth defaults to per-home file storage. From Community 0.10.8, an explicit `file`, `auto` or `keyring` policy is respected, as is a deliberate deletion of Harbor's managed default. Native `auto`/`keyring` storage can share ordinary MCP authorization between instances with the same server name and URL, including refresh and logout effects. This is separate from ChatGPT account authentication. Harbor does not move or delete existing MCP credentials.

Your zsh startup files are loaded in place. A small routing guard preserves this window's home and selected `CODEX_HOME`/history path; your other tool exports remain available. Only backend launch clears conflicting model-provider credential variables. Harbor does not edit your shell files. Normal desktop launches also leave PATH, language settings and the backend's default working directory to Codex; an explicitly selected workspace still takes precedence. Installed plugins remain managed by Codex for the selected account; Harbor does not copy another account's plugin cache or credentials. Browser login state remains isolated per instance.

Community 0.10.7 fixes bundled-plugin errors caused by different path spellings for the same instance directory. The adapter normalizes only local plugin read/install paths and retains native source validation. Per-home bundled marketplace registrations are created by Codex for the current instance instead of inherited from another home. Manually edited registrations and plugin preferences remain protected. These paths were checked against bundled CLI `0.163.0-alpha.11`; the native update menu, browser settings and existing project organization were also accepted in the private build with the same runtime sources.

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

From Community 0.10.2, Harbor launches Codex through macOS `NSWorkspace` with a new application instance. Codex and its backend then own their file access, instead of inheriting Harbor's application identity. Launch preparation also avoids filesystem probes of unrelated registered locations. A native helper retains instance locks and records the exact process returned by macOS; an uncertain launch is not automatically retried.

When updating from an earlier version, finish work and quit Codex windows opened by the old Harbor before opening new ones. Existing processes retain their old permission attribution until they exit. Keep account and history directories intact. This change does not require Full Disk Access, Accessibility, Automation or App Management permission. A location you explicitly enabled can still require its own macOS folder permission.

Some official macOS UI preferences and the `codex://` handler are shared across native instances. Harbor captures a local recovery snapshot before the first desktop launch. Restoration is explicit, requires all Codex instances to be closed, and does not restore account or history files.

From Community 0.10.7, normal launches leave native updates and Computer Use auxiliary-app refresh enabled. Harbor does not grant system permissions. Auxiliary-app refresh and signature checks passed in isolated desktop tests; Computer Use permission actions were not tested. App Management permission is not required merely to run this launcher.

Community 0.10.1 reads official-account icon bytes before decoding them. Earlier versions passed another app's icon URL to AppKit, which also queried its file-access attributes and could trigger an App Management notification. Harbor does not need permission to update or delete other apps to display an icon. Leave that permission disabled; this fix changes no account, history, or system permission settings.

## Build and compatibility

Use the Swift 6 toolchain and Python 3.11+:

```sh
python3 -B -m unittest discover -s tests -p 'test_*.py'
python3 -B build.py --community --universal
```

Build output is a verified ZIP in `release-artifacts/`. Normal exit, including handled build failures, attempts to remove temporary app bundles; cleanup errors report the remaining path. A forced termination can leave temporary files, which the next build reclaims only when their ownership marker is valid and no active build holds their lock. Building neither installs the app nor reads personal configuration. Custom `--output` paths must use real directories without symlink ancestors; on macOS use `/private/tmp`, rather than `/tmp`, for a temporary output.

Optional `--identity` and `--keychain` arguments select an already-working user-supplied signing identity for Apple's `codesign` tool. They do not create or import certificates, change Keychain preferences or trust, or notarize the app. The default remains ad-hoc signing. Certificate-based signing is outside this release's validation.

The native Keychain integration suite uses a disposable synthetic keychain and accepts an exact helper path with `--helper`. It makes no live account or model call.

The universal release includes arm64 and x86_64 executables. Runtime acceptance has been performed on Apple Silicon, not a physical Intel Mac. Release builds are ad-hoc signed, not Apple Developer ID signed or notarized. Keychain authorization dialogs, other official Codex versions and every external model service are not implied to have passed acceptance.

Multi-instance official authentication uses Codex's experimental external-token app-server protocol. This release was checked against bundled CLI `0.163.0-alpha.11`, including isolated cross-source continuation and compaction tests. The private build with identical runtime sources also passed a user-operated Router → official → Router chat test, including a reply after quitting Harbor. Earlier long-history desktop acceptance remains a separate result. An incompatible version stops with a login/compatibility error rather than falling back to concurrent refresh-token files. Recheck this integration when updating Codex; these tests do not cover every provider or future native version.

Development and acceptance take place in a separate private development repository. The public repository receives reviewed snapshots and release artifacts; private Git history and acceptance evidence are not mirrored. See [CONTRIBUTING.md](../CONTRIBUTING.md).

## Updating while Codex is open

Harbor keeps each launched instance on its own verified runtime version, outside the replaceable app bundle. Closing the Harbor window leaves those compatibility processes running until the corresponding Codex instance exits. They handle Router translation and official account coordination. Official model traffic goes directly from Codex to the official service.

Current launch components live under `runtime-payloads.noindex/` in the Community data folder. They contain program files and license notices, not accounts or conversation copies. Harbor retains active versions and one recent fallback; it removes only verified inactive versions during a later launch. Unresolved startup records and unknown files are retained for inspection.

Older Harbor instances that still use files inside the app must be closed for the first transition. Future incompatible runtime protocols also require closing existing instances. These guarantees concern Harbor updates; Python and the installed Codex remain external dependencies.
