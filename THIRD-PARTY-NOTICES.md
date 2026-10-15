# Third-party notices

Harbor original source is licensed under MIT (LICENSE). The official Codex application is not bundled or redistributed; users install it separately.

## Codex model catalog

`engine/models.json` is identical to the public OpenAI Codex catalog at commit `740e5af33c71225640e0c1c1555c514c2c93ab74`, path `codex-rs/models-manager/models.json`.

Source: https://github.com/openai/codex/blob/740e5af33c71225640e0c1c1555c514c2c93ab74/codex-rs/models-manager/models.json

SHA-256: `943ca7fe1d19ed019054158303f0dbbd80b3bef43b3aa425a71aa7cb3fb52c2b`.

License: Apache-2.0, retained in LICENSE-Codex. Catalog text and capabilities are upstream content; Harbor does not claim authorship.

## Cherry provider registry

`engine/cherry-catalog.json` derives from Cherry Studio's MIT provider-registry data. Attribution and license are retained in LICENSE-CHERRY-PROVIDER-REGISTRY. The public-context catalog embeds its provenance in the JSON. Reference metadata is not proof of a Router's actual limits or supported effort levels.

macOS frameworks, Swift and the separately installed Python/Codex runtimes remain under their respective licenses. Vendor app icons are read from the user's local installed app at runtime and are not included in this release.
