# Codex Harbor Community

A macOS launcher that selects an account/API and a history location independently, then opens an isolated Codex desktop window.

为 macOS Codex 桌面分别选择账号/API 与会话位置，支持多账号、多模型映射、共享历史和本地项目分组。

## Download and requirements

Download the app ZIP and SHA256SUMS from [Releases](https://github.com/dongshuyan/codex-harbor-community/releases). Extract the ZIP and put **Codex Harbor Community.app** in Applications. The app is locally ad-hoc signed, **not Apple Developer ID signed or notarized**. macOS may require a user decision before opening it. Building the source locally is also supported.

- macOS 14 or later.
- An official Codex desktop app installed as `Codex.app` or `ChatGPT.app` in `/Applications` or your user's `Applications` folder. Open the official app once first so its macOS preferences and URL handler exist. Codex itself is not included in this download.
- Python 3.11 or later from python.org or Homebrew. No Python packages are required. The launcher checks `/opt/homebrew/bin/python3`, `/usr/local/bin/python3`, then `/usr/bin/python3`.
- Releases identify their supported CPU architectures. Compatibility with future official Codex updates needs fresh verification.

The universal release contains Apple Silicon and Intel slices. Runtime acceptance was performed on Apple Silicon; a physical Intel Mac has not been tested.

## First use / 首次使用

**A fresh installation contains no accounts, API URLs, keys, or registered history paths.** It does not automatically import another Harbor installation, shell routes, or native login files.

1. 左上角 **＋** 添加官方账号或自定义 Responses API。官方账号由浏览器授权；API 填入自己的 URL、Key 与实际模型名，可配置多个映射。
2. 右上角 **＋** 添加会话位置：命名并选择绝对路径。可选已有 Codex home 或新目录。保存只登记；新目录需要另行明确建立。会话不会被迁移或删除。
3. 两侧选好后启动。具体模型与权限在 Codex 原生菜单选择。自动选择按右侧顺序寻找首个可用位置；插盘时仍可手选本地。

The full model catalog of the selected API is available in Codex. Mapping a model name does not grant the upstream that model's capabilities. Responses is the only supported wire protocol; there is no Anthropic/Chat Completions conversion layer. Parameter discovery prefers the selected API, then a bundled public catalog, then a generic preset. Manual edits are protected; catalog values are references rather than proven limits.

Single-model and whole-API probes are explicit user actions. They send `Reply OK.` without history/tools, use at most four concurrent probes, and share a 60-second deadline per model. Only explicit output-budget exhaustion permits larger attempts (32, 128, 512, 2048, 8192). Those attempts may be billed by the provider. Saving alone does not send a model request.

## Multiple windows and history

Different official accounts and APIs can run concurrently. Use each official account entry at one history location at a time. Do not add the same real account as duplicate entries. Complete a turn and close its old instance before continuing that conversation in another window.

Windows using the same location share native session history. Each window uses its currently selected account/API; continuing private history through a Router sends that context to your chosen provider. Lists and project organization may require reopening the target window. Different locations are independent and do not synchronize automatically.

Local projects, membership, pins and ordering are read from the selected native home. Harbor merges only organization changes in its own cache, and does not write them back into the original desktop global state. Cloud-account projects and unrelated UI preferences are excluded. Permission menus remain native Codex defaults.

Official account quota bars show only periods actually returned by Codex, with last-update time. Reset times are absolute `Asia/Shanghai` dates/times. Reads do not send a model request. Expired/failed snapshots are marked rather than invented as full quota.

## Privacy and credentials

Read [PRIVACY.md](PRIVACY.md) before entering credentials. API keys are stored in a dedicated macOS Keychain namespace, never as plaintext fallback in profile JSON. Native Codex login credentials remain in isolated account homes protected by filesystem permissions. URLs, names and paths are local private configuration, not uploaded to this repository.

Community uses its own data root: `~/Library/Application Support/Codex Harbor Community/`. It can be configured to the same account/API/history choices as another installation, but has no automatic importer. This app never provides historical-session cleanup.

Some official macOS UI preferences and the `codex://` handler are shared across native Codex instances. A local recovery snapshot is captured before the first desktop launch. Restoration is explicit, requires all Codex instances to be closed, and does not restore account/history files. Computer Use auxiliary-app refresh is skipped; that feature is not included in the acceptance claim. Do not grant App Management merely to run this launcher.

Updates may prompt macOS Keychain authorization. With ad-hoc signing, a changed helper may require reauthorization or re-entering the API Key. Denied/locked Keychain operations fail with an error; no plaintext alternative is used.

## Build and checks

With the Swift 6 toolchain and Python 3.11+:

```sh
python3 -B -m unittest discover -s tests -p 'test_*.py'
python3 -B build.py --community --universal
```

Builds are written to `build/<source hash>/`; building does not install or read personal configuration. The Keychain integration test accepts a helper path and uses a disposable test-only keychain. No live account/model call is part of these tests.

Development and acceptance take place in a separate private development repository. This public repository receives reviewed source snapshots and release artifacts, with fresh public history. See [CONTRIBUTING.md](CONTRIBUTING.md).

MIT for Harbor's original code. Bundled public model metadata has separate notices in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md). This is an independent launcher, not an official OpenAI product.
