# Development and release flow

The public repository is a distribution mirror. Development, tests and acceptance are performed in the maintainer's separate private repository. Only a reviewed snapshot is exported; private Git history and acceptance evidence are not mirrored.

A release must have:

1. Passing behavior/regression tests in the development repository and checks of the exact exported runtime sources.
2. A clean first-run check: no accounts, saved API URLs/keys, default location registrations or imported sessions.
3. Keychain and credential-isolation checks using only disposable synthetic data; never use a contributor's real account to prove packaging.
4. An allowlisted export, matching source hashes, privacy scans of source and compiled binaries, locally verified code signatures, extracted-archive comparison and SHA-256 checksums.
5. Documentation of supported macOS/CPU/dependencies and any untested device, signing or service boundaries.

Do not attach data roots, auth.json, profile files, API keys, real conversation logs or unredacted screenshots to Issues. Report a reproduction with synthetic paths/keys and the app/Codex versions. Public feedback can inform the private development work; fixes arrive as a later reviewed snapshot.
