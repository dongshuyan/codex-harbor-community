# Third-party notices

Harbor original source is licensed under MIT (LICENSE). The official Codex application is not bundled or redistributed; users install it separately.

## TOML Kit

`engine/vendor/tomlkit/` contains the unmodified Python package from TOML Kit 0.13.3, MIT licensed. The license is retained in LICENSE-TOMLKIT.

Source: https://github.com/python-poetry/tomlkit/tree/0.13.3

PyPI wheel SHA-256: `c89c649d79ee40629a9fda55f8ace8c6a1b42deb912b2a8fd8d942ddadb606b0`.

## Codex model catalog

`engine/models.json` is identical to the public OpenAI Codex catalog at commit `740e5af33c71225640e0c1c1555c514c2c93ab74`, path `codex-rs/models-manager/models.json`.

Source: https://github.com/openai/codex/blob/740e5af33c71225640e0c1c1555c514c2c93ab74/codex-rs/models-manager/models.json

SHA-256: `943ca7fe1d19ed019054158303f0dbbd80b3bef43b3aa425a71aa7cb3fb52c2b`.

License: Apache-2.0, retained in LICENSE-Codex. Catalog text and capabilities are upstream content; Harbor does not claim authorship.

## Cherry provider registry

`engine/cherry-catalog.json` derives from Cherry Studio's MIT provider-registry data. Attribution and license are retained in LICENSE-CHERRY-PROVIDER-REGISTRY. The public-context catalog embeds its provenance in the JSON. Reference metadata is not proof of a Router's actual limits or supported effort levels.

macOS frameworks, Swift and the separately installed Python/Codex runtimes remain under their respective licenses. Vendor app icons are read from the user's local installed app at runtime and are not included in this release.

## Public context reference data

`engine/context-catalog.json` contains model identifiers and context-window numbers from OpenRouter's public models endpoint. Its source URL and snapshot date are embedded in the file. It includes no provider credentials or user API configuration. No OpenRouter SDK is bundled.

See [Credits](CREDITS.md) for design, protocol and interaction references, which are distinct from the model data included above.

## Magpie hosted-search replay compatibility

The historical search-declaration adapter in `tests/official_responses.py` adapts
Magpie’s `DeclareSearch` implementation at commit
`c4aeaec740b98a3f0f772f60dcea517ed144354b`. It is retained as a regression-test
control and is not included in the app runtime. Official model requests use
Codex’s native transport. Magpie is MIT licensed; the copyright notice and
license remain in [LICENSE-MAGPIE](LICENSE-MAGPIE).

Source: https://github.com/yetone/magpie/blob/c4aeaec740b98a3f0f772f60dcea517ed144354b/internal/provider/codex_request.go
