# Privacy and credential storage

Codex Harbor Community has no account-sync, analytics, crash-upload, or configuration-upload service of its own. Saving settings writes locally. The application is distributed with no saved accounts, routes or history locations.

## Local files

`~/Library/Application Support/Codex Harbor Community/` is the independent private root. Harbor creates directories with mode 0700, writes atomic settings/credential copies with mode 0600, and applies a restrictive 0077 process umask. Native Codex owns its logs/caches; the containing root remains private. Metadata (names, URLs, model mappings, registered paths) is JSON. These files are not encrypted and must not be included in support reports.

API keys are kept in macOS Keychain under the dedicated service `org.codexharbor.community.api-keys`; metadata stores opaque references only. Secrets travel to the bundled helper over private stdin/stdout pipes, not command arguments or environment variables. The app never silently falls back to plaintext storage. Ownership references are journaled so interrupted saves remain traceable and cleanup can be retried. Removing a route is recoverable; confirmed permanent removal deletes its known Keychain references. A failed, unpublished save can leave an encrypted pending item; a later explicit save reclaims one orphan at a time. Do not delete the local root before credential cleanup if you need those references.

Official logins use Codex's native OAuth flow. Persistent access/refresh credentials stay in `accounts/<ID>/codex/auth.json`, within this application's private root. They are protected by user-only filesystem permissions and are **not** converted to Keychain storage by Harbor. A serialized native refresh process uses a private `refresh-owner/` working directory and `refresh-transaction.json` recovery journal beside that home. Completed refreshes remove the working copy; an interrupted or uncertain refresh retains it for safe recovery or explicit re-login.

Each official desktop backend uses native ephemeral authentication. Harbor passes its access token and account ID through private stdin/stdout pipes, never command arguments, environment variables or logs. The refresh token is not passed to desktop instances. Private runtime slots in `instances/` retain per-window settings; `instance-slots/` contains ownership locks. Treat those directories and any interrupted refresh working copy as confidential.

History stays in the selected directory. Project organization is cached locally in `organization/`; it can contain project names, absolute paths and session IDs. Recovery preferences, runtime jobs, synthetic configuration files and gateway event metadata also remain local. Harbor gateway events exclude key/header values and conversation bodies. Native Codex may create its own local logs and caches: treat the entire data root as private, not only files named auth.json.

Native settings are retained in each instance's `config.toml`. Selecting an existing Codex directory permits Harbor to read its non-routing preferences, instructions, rules and skill locations as defaults. Source credentials and account-specific remote plugin provisioning are excluded; original settings are never written by Harbor. Your global user environment and workspace-specific skills are discovered normally. Local changes in Harbor take priority over inherited defaults.

The preservation backup and tracking files (`.harbor-config-before-preservation.toml`, `.harbor-managed-config.toml`, `.harbor-native-resources.json`) can contain private configuration, paths and MCP settings, including secrets already present in a user's MCP definitions. They are protected and excluded from source/release exports. Plugin and MCP OAuth credential files are not copied from another account. See [saved Codex settings](docs/USAGE.md#saved-codex-settings) for the scope of persistence.

MCP OAuth uses per-home files by default. An explicit native `auto`/`keyring` policy, or deleting Harbor's managed default to use native `auto`, can share ordinary MCP Keychain authorization for the same server name and URL. Refresh and logout then affect that shared entry. Explicitly shared log directories can also combine native logs. Harbor preserves these choices without migrating existing credentials or changing source files.

The `runtime-payloads.noindex/` directory stores verified program versions used by running instances. It contains no copied account or history data. Private and Community editions use separate roots. Content hashes check integrity; they do not protect against malicious code running as the same macOS user. Running versions and unresolved launch intents are retained when old program files are cleaned up.

## Network

- Official login and quota reads use the installed Codex. Official desktop model calls use Codex’s native transport directly to the official service. Harbor does not listen on an HTTP port or forward model traffic for official accounts. Its account coordinator supplies native ephemeral authentication over private pipes and serializes refreshes; original conversation history is not rewritten.
- API discovery sends authenticated GET requests to the URL the user selected. A probe or Router conversation sends a model request to that provider. Continuing an old conversation can include its prior context.
- The Router-only local gateway listens on loopback, requires a per-instance token, rebuilds upstream authentication headers, rejects redirects and hides raw upstream error bodies. It records request metadata and fixed transport-failure categories, not conversation bodies, key/header values or exception strings. Interrupted connections are closed without automatic replay by Harbor; Codex may retry under its own policy.
- Harbor does not synchronize these files to GitHub. Publishing a repository/release is a separate maintainer operation, never an app feature.

The source/release process uses an explicit file allowlist, a fresh public Git history and scans the exported tree, app and extracted archive. Runtime directories, logs, evidence, credentials and machine defaults are excluded. Scanning is a release check, not a claim that arbitrary downstream changes cannot leak data.

Filesystem permissions and Keychain do not protect against malware running as the same user, an unlocked compromised account, screenshots, or manually uploading your data. Only send support information you have checked and redacted. Use the operating system's Keychain UI if a damaged installation needs manual removal of this app's entries; never post keys, OAuth files or entire data folders to Issues.
