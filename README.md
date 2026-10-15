<p align="center">
  <img src="docs/images/app-icon.png" width="112" height="112" alt="Codex Harbor Community icon">
</p>

<h1 align="center">Codex Harbor Community</h1>

<p align="center">
  <strong>Choose your Codex account and history location independently.</strong><br>
  A native macOS launcher for official accounts and Responses-compatible APIs.
</p>

<p align="center">
  <a href="https://github.com/dongshuyan/codex-harbor-community/releases/latest"><strong>Download for macOS</strong></a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="docs/USAGE.md">User guide</a> ·
  <a href="README.zh-CN.md">简体中文</a>
</p>

<p align="center">macOS 14+ · Apple Silicon &amp; Intel · MIT</p>

![Native launcher overview with a demo Responses Router, two official accounts, portable and local history locations, and numbered setup annotations.](docs/images/overview.png)

> Real native interface with fictional demo data and added English annotations. Accounts, URLs, paths, model mappings and quota values are illustrative. The app interface is currently in Simplified Chinese.

## One launcher, two independent choices

Pick an official account or your own API on the left. Pick the folder that holds your Codex history on the right. Review the combination and open a separate Codex window.

| Capability | What you can do |
| --- | --- |
| Official accounts | Sign in through OpenAI's browser authorization flow and keep accounts separate. See quota windows returned by Codex. |
| Custom APIs | Set a Responses base URL and API key, then map multiple upstream models to names available in Codex. All configured mappings are enabled together. |
| History locations | Register local folders or external drives. Choose explicitly or let availability and your priority order decide. Drag cards to reorder. |
| Workspace organization | Read local projects, session groups, pins and ordering from the selected native Codex home. |
| Model parameters | Discover context and reasoning settings, inspect their sources, and make protected manual edits. Probe one model or every model in the selected API. |
| Native Codex windows | Choose the model and permissions in Codex itself. Different accounts and API routes can run concurrently. |

## Quick start

### Install

1. Download the universal App ZIP from [Releases](https://github.com/dongshuyan/codex-harbor-community/releases/latest). A `SHA256SUMS` file is included alongside it.
2. Extract the ZIP and move **Codex Harbor Community.app** to your Applications folder.
3. Open Harbor and add your own sources. **A fresh installation has no accounts, API configuration or registered history locations.**

You will need:

- **macOS 14 or later.** The universal package contains Apple Silicon and Intel executables. Runtime acceptance has been performed on Apple Silicon; a physical Intel Mac has not been tested.
- **The official Codex desktop app**, installed as `Codex.app` or `ChatGPT.app` in `/Applications` or your user's Applications folder. Open it once before using Harbor. Codex is installed separately.
- **Python 3.11 or later**, from python.org or Homebrew. No additional Python packages are required.

Release builds are locally ad-hoc signed and are **not Apple Developer ID signed or notarized**. macOS may require approval before opening a downloaded build.

### Configure and launch

1. **Add an account or API.** Use the left **＋** to sign in to an official account, or enter a Responses URL, API key and model mappings.
2. **Add a history location.** Use the right **＋** to name a folder and register its absolute path. Use an existing native Codex home or explicitly create a new directory.
3. **Choose both, then launch.** Select the two sources and review the center panel. Choose the specific model inside the new Codex window.

Registering or removing a location never moves or deletes its history. When an external drive is connected, you can still choose a local folder manually. Different locations do not synchronize automatically.

## Accounts and quota at a glance

![Official account details with separate login identity, five-hour and weekly quota bars, and absolute reset dates in Beijing time. All values are illustrative.](docs/images/account-quota.png)

See which account you are selecting, its returned quota windows, and when they reset. Missing or stale results remain visible as such. Reset times use full dates in **Asia/Shanghai (Beijing time)**. Reading quota does not send a model request.

Use each official account entry at one history location at a time. Finish a turn and close the old instance before continuing that conversation in another account or API window. Shared history lists may require reopening the destination window. See the [window and history guide](docs/USAGE.md#windows-and-shared-history).

## Model parameters with visible sources

![Model editor with an upstream model mapping, automatic parameter discovery, protected manual context and reasoning settings, and an individual draft probe.](docs/images/model-parameters.png)

Discovery prefers metadata from your API, then bundled public model catalogs, then a generic preset. Context and reasoning settings stay editable; refreshing does not overwrite manual changes. Catalog values are references, and an alias does not give an upstream model new capabilities.

**Responses is the supported API protocol.** Harbor does not convert Chat Completions or Anthropic Messages. Probes are explicit, billable requests with bounded budgets and a 60-second deadline per model. A passing probe checks a short response; desktop tools and session continuation need separate acceptance. [Parameter and probe details →](docs/USAGE.md#model-parameters-and-probes)

## Private configuration stays on your Mac

- API keys use a dedicated **macOS Keychain** namespace, with no plaintext fallback in profile JSON.
- Official logins use isolated native Codex credential files protected by filesystem permissions.
- Names, URLs, mappings and paths stay in Harbor's local data directory. Harbor has no configuration upload, account sync or analytics service of its own.
- Model requests go to the account or API you select. Continuing history through a Router sends that context to that provider.

Community has its own data root at `~/Library/Application Support/Codex Harbor Community/` and no automatic importer. Read the [privacy and credential storage policy](PRIVACY.md), including native preferences shared by Codex instances and Keychain authorization after updates.

## Build from source

With the Swift 6 toolchain and Python 3.11+:

```sh
python3 -B -m unittest discover -s tests -p 'test_*.py'
python3 -B build.py --community --universal
```

Output is written to `build/<source hash>/`. Building does not install the app or read personal configuration. See the [build and compatibility notes](docs/USAGE.md#build-and-compatibility) and [contribution workflow](CONTRIBUTING.md).

## Support and license

Report reproducible problems through [GitHub Issues](https://github.com/dongshuyan/codex-harbor-community/issues). Include app and Codex versions, with synthetic examples. Never attach keys, login files or entire data folders.

Harbor's original code is [MIT licensed](LICENSE). Bundled public model metadata retains its [upstream licenses and notices](THIRD-PARTY-NOTICES.md).

Codex Harbor Community is an independent project and is not affiliated with or endorsed by OpenAI.
