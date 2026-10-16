# Credits

Codex Harbor Community builds on public tools, protocols and documentation. Thank you to the maintainers and authors who make this work possible.

## Included libraries and model data

| Source | Use in Harbor | Attribution and license |
| --- | --- | --- |
| [TOML Kit](https://github.com/python-poetry/tomlkit/tree/0.13.3) | Style-preserving local edits to Codex configuration; vendored, without a global Python installation. | [MIT](LICENSE-TOMLKIT), version 0.13.3. Package checksum in [third-party notices](THIRD-PARTY-NOTICES.md). |
| [OpenAI Codex](https://github.com/openai/codex) | Bundled model catalog, used for Codex model names and capabilities. The official Codex app is installed separately. | [Apache-2.0](LICENSE-Codex); exact source revision and checksum in [third-party notices](THIRD-PARTY-NOTICES.md). |
| [Cherry Studio provider registry](https://github.com/CherryHQ/cherry-studio/tree/d33b9b0266bda30d0e4715ae16650eee7b845d70/packages/provider-registry) | A derived subset of model context and reasoning metadata, with source labels in the editor. | The provider-registry package declares [MIT](https://github.com/CherryHQ/cherry-studio/blob/d33b9b0266bda30d0e4715ae16650eee7b845d70/packages/provider-registry/package.json). Its [retained license](LICENSE-CHERRY-PROVIDER-REGISTRY) applies to the included data; Harbor does not bundle the Cherry Studio application. |
| [OpenRouter model metadata](https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties) | A public reference snapshot of model identifiers and context-window numbers. | Provenance is recorded in [context-catalog.json](engine/context-catalog.json). This is reference metadata, not a bundled SDK or provider connection. |

Catalog entries are reference values. They do not prove that a particular Router supports a model, context limit or reasoning option.

## Implementation and interaction references

The following projects and examples informed our design and compatibility research. Their applications and source implementations are not bundled as Harbor dependencies.

| Reference | What we learned from it |
| --- | --- |
| [CC Switch](https://github.com/farion1231/cc-switch) | Provider configuration, Codex model catalogs, tool compatibility and probe design. |
| [CodexBar](https://github.com/steipete/CodexBar) | Native quota queries, bounded waits, and separation of account identity and cached results. |
| [Cockpit Tools](https://github.com/jlcodes99/cockpit-tools) | Comparison of Codex session and project-index handling. Harbor keeps registered history in place. |
| [CodeEdit](https://github.com/CodeEditApp/CodeEdit) | Native macOS sidebar organization and standard list-reordering patterns during the initial UI design. |
| [Nate Thompson's NSTableView drag-and-drop guide](https://www.natethompson.io/2019/03/23/nstableview-drag-and-drop.html) | Native drag sessions, pasteboard validation and row reordering. |
| [Apple AppKit](https://developer.apple.com/documentation/appkit/nstableview) | The final sidebar uses `NSTableView` drag previews, gap feedback, cancellation and row movement, integrated with SwiftUI cards. |

Harbor communicates with the separately installed Codex through its [app-server protocol](https://learn.chatgpt.com/docs/app-server). Custom APIs use [OpenAI Responses](https://developers.openai.com/api/reference/resources/responses); Harbor does not claim support for a provider merely because it uses the same model name.

## Community

Special thanks to **[LINUX DO](https://linux.do/)** and its community for a place to share open-source work, exchange feedback and help projects reach more users.

These acknowledgements do not imply sponsorship, partnership or endorsement. Harbor's original code remains [MIT licensed](LICENSE); included third-party material retains its own license.
