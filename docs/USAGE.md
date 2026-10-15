# Codex Harbor user guide

[Back to the homepage](../README.md) · [简体中文主页](../README.zh-CN.md) · [Privacy](../PRIVACY.md)

## Installation and first use

The release requires macOS 14+, Python 3.11+ and a separately installed official Codex desktop app. Supported app names are `Codex.app` and `ChatGPT.app`, in `/Applications` or your user's Applications folder. Open the official app once so its macOS preferences and URL handler exist.

Harbor checks `/opt/homebrew/bin/python3`, `/usr/local/bin/python3`, then `/usr/bin/python3`, and requires Python 3.11 or later. It does not install Python or require additional packages.

Community starts empty. It does not import another Harbor installation, shell routing variables or native login files. Its root is `~/Library/Application Support/Codex Harbor Community/`. It can be configured with the same account/API and history choices as another installation by adding those choices yourself.

Use the left **＋** to create an official account entry or a custom API. Official accounts use browser authorization. For an API, enter its Responses base URL without the `/responses` suffix, API key and actual model names. Saving alone does not send a model request. When an API URL changes, the editor asks you to confirm its destination before using a newly entered key for discovery or a probe.

Use the right **＋** to register an absolute history-directory path and a name. Saving a registration does not create an absent directory; creation is a separate explicit action. There is no migration or historical-session cleanup feature.

## Windows and shared history

Different official accounts and API routes can run concurrently. Use each official account entry at one history location at a time. Do not add the same real account as duplicate entries.

Windows using the same location share native session history. The account/API selected for the current window supplies that window's requests. Finish the current turn and close the old instance before continuing the same conversation elsewhere. Continuing private history through a Router sends the context to that provider.

Session lists and project organization may require reopening the target window. Different locations are independent and never synchronize automatically.

Local projects, membership, pins and ordering are read from the selected native Codex home. Harbor merges organization changes only into its own cache; it does not write those changes back to the original desktop global state. Cloud-account projects and unrelated UI preferences are excluded.

The model and permission menus are native Codex controls. Harbor does not add a testing permission preset to normal launches. Installing or updating the official Codex app can change compatibility and requires fresh verification.

## History-location availability

Drag location cards to change the automatic-selection order. **Automatic** chooses the first available enabled location. You can always select a specific local or external location instead.

An unavailable manual choice remains unavailable; Harbor does not silently redirect its history to another folder. Connect the drive or choose another location yourself. Close instances using an external drive before ejecting it.

Removing a location removes only its registration, through the recoverable list. Even confirmed permanent removal leaves the history directory and sessions intact.

## Model parameters and probes

The selected API exposes all its configured mappings in Codex. An alias affects the picker name; it does not grant an upstream model the aliased model's capabilities. Responses is the only supported wire protocol. There is no Anthropic Messages or Chat Completions conversion layer.

Parameter discovery prefers the selected API's model metadata, then the bundled public catalogs, then a generic preset. The unknown-model preset uses a 128000-token context reference and omits reasoning parameters. These values are editable defaults, not verified provider capabilities.

Manual context or reasoning changes are protected against later discovery. Use **Restore automatic discovery** for the individual field when you want it managed automatically again. Context input accepts formatted numbers such as `1,050,000` and `1.05M` and normalizes valid input. Model catalogs are references; confirm actual capacity with your provider.

Reasoning can use the service default (no parameter), standard Responses levels, or a supported custom field/value mapping. Keep the service default if support is unknown. Max is available only where exposed by the model configuration and Codex settings. Ultra is a Codex orchestration capability, not an arbitrary Router reasoning value.

Single-model and whole-API probes are explicit user actions. Each sends only `Reply OK.` without session history or tools. At most four probes run concurrently, with a **60-second total deadline per model**. Only explicit output-budget exhaustion permits larger attempts: **32, 128, 512, 2048, 8192** output tokens. Other failures do not automatically retry. Attempts may be billed by the selected provider.

A passing probe confirms a completed short response. It does not prove desktop tool compatibility, reasoning-level correctness or session continuation. Validate those separately for the provider you plan to use.

## Official account quotas

Quota reads use the installed Codex interface and do not send a model request. Harbor displays only returned periods, including five-hour or weekly windows when present. Missing periods are not invented as full quota.

The foreground app refreshes due snapshots every five minutes; explicit refresh is also available. Results include the last update time. Failed, stale or expired snapshots stay marked as such.

Reset times are absolute dates in `Asia/Shanghai`, labelled Beijing time. They are not countdowns and do not follow a different system timezone.

## Credentials, recovery and native preferences

API keys are stored in a dedicated macOS Keychain namespace; profile JSON contains opaque references. Denied or locked Keychain operations fail rather than storing plaintext. Ad-hoc signed updates can require Keychain reauthorization or re-entering the API key.

Official logins remain native Codex credential files in isolated account homes, protected by user-only filesystem permissions. Metadata such as names, URLs and history paths is local JSON and is not encrypted. Treat the whole data root as private. See the complete [privacy policy](../PRIVACY.md).

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

Development and acceptance take place in a separate private development repository. The public repository receives reviewed snapshots and release artifacts; private Git history and acceptance evidence are not mirrored. See [CONTRIBUTING.md](../CONTRIBUTING.md).
