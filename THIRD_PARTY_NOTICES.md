# Third-party notices

This document preserves third-party attribution; it does
not replace any upstream license or assert a single license for the whole product.
See [NOTICE.md](NOTICE.md).

## Upstream foundation

[HKUDS/OpenOPC](https://github.com/HKUDS/OpenOPC) is the original codebase foundation.
Copyright (c) 2026 OpenOPC Authors. Its complete MIT text is retained in
[OpenOPC-MIT-LICENSE](licenses/OpenOPC-MIT-LICENSE). The original repository URL
now redirects to HKUDS/AutoSwarm. The historical import commit is not established.

## Adapted source

### OpenAI Codex — Apache-2.0

Source: [openai/codex](https://github.com/openai/codex), revision
`8e6a44b428e31f91b21edc97904fcdf4f0931ade`, and Python SDK version `0.147.0`.
Copyright 2025 OpenAI. Complete texts:
[LICENSE](ampuly/layer3_agent/licenses/codex/LICENSE) and
[NOTICE](ampuly/layer3_agent/licenses/codex/NOTICE).

- `ampuly/layer3_agent/managed_codex.py` adapts the SDK `client.py` launch seam.
  The adapted source-file SHA-256 is
  `76bdb1e63c62987c3530ea763e9655a06b308cbc4e18cb51958e85b6c23aec3b`.
  Changes include a replacement process environment, version-matched executable,
  hidden Windows launch and explicit model routing. Transport, approval and
  protocol behavior continue to use the SDK.
- `ampuly/llm/custom_tool_codec.py` embeds the reviewed
  `codex-rs/core/assets/tools/apply_patch.lark` grammar and the Environment ID
  variant associated with `tools/handlers/apply_patch_spec.rs`. Parsing is handled
  by Lark; syntax acceptance does not grant execution permission.

### UI-TARS — Apache-2.0

Source: [bytedance/UI-TARS-desktop](https://github.com/bytedance/UI-TARS-desktop),
revision `c2ad42e3eb9b27830db41a3e6f51ca7179d9b168`.
Copyright (c) 2025 Bytedance, Inc. and its affiliates.
Complete license: [UI-TARS-LICENSE](licenses/UI-TARS-LICENSE).

`ampuly/computer_use/vision_actions.py` reimplements the action-string grammar in
`packages/ui-tars/action-parser/src/actionParser.ts` and resize constants from
`packages/ui-tars/shared/src/constants/vlm.ts`. Ampuly adds validation, bound
geometry and independent completion checks. The GUI agent loop and NutJS operator
are not bundled by this adaptation.

### OpenAdapt — MIT

Source: [OpenAdaptAI/openadapt-flow](https://github.com/OpenAdaptAI/openadapt-flow),
revision `be44b4a4553cbca1f8ab2646ae0c2c04a6e1a91f`.
Copyright (c) 2026 OpenAdapt.AI (MLDSAI Inc.).
Complete license: [OpenAdapt-LICENSE](licenses/OpenAdapt-LICENSE).

`ampuly/computer_use/windows_desktop_native.py` adapts window/process matching,
DPI and elevation checks, geometry normalization and input-count validation from
`openadapt_flow/backends/win32_window_client.py`. It uses the Ampuly capability
contract and a bounded set of actions; the upstream agent/cloud runtime is not
included.

### Browser Use — MIT

Source: [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast).
Copyright (c) 2026 Browser Use.
Complete license: [BrowserUse-LICENSE](licenses/BrowserUse-LICENSE).

`ampuly/plugins/office_ui/browser_decider.py` follows probability-distribution
validation and unchanged-page blocking checks. The existing provenance record
does not pin the exact source commit for those checks. This is not a claim that
the upstream browser harness is bundled or that its behavior is interchangeable.

### Easel — Apache-2.0

Source: [ZJU-REAL/Easel](https://github.com/ZJU-REAL/Easel), revision
`3f1d4fb8b99694f7231ee97163b637df89b50d3b`.
Complete license: [Easel-LICENSE](licenses/Easel-LICENSE).

- `ampuly/layer4_tools/selfmedia/content_guard.py` ports the outbound text gate
  from `skills/shared/scripts/content_guard.py`, adding an explicit environment-file
  argument instead of implicitly reading a working-directory `.env`.
- `ampuly/layer4_tools/selfmedia/readback.py` ports the pure verdict portion of
  `skills/shared/scripts/platform_readback.py`. Browser capture remains separate,
  and the result does not authorize automatic republication.

The upstream `skills/builtin/gzh-design` component has a separate AGPL-3.0 license;
no part of that component is included in this source adaptation.

### BLA — MIT notice for optional admission metadata

Source: [qybaihe/bla](https://github.com/qybaihe/bla), revision
`71cf950485bb267bb13a7c0405ef05ddab65b559`.
Copyright (c) 2026 BLA Contributors.
Complete license: [BLA-LICENSE](licenses/BLA-LICENSE).

`ampuly/plugins/office_ui/bla_admission.py` records fixed upstream admission
metadata and read-only planning rules. Its source and published-package versions
are intentionally reported separately. The code does not authorize installation,
MCP setup or hardening. Public inert examples are in `examples/isolation/`.

## User-interface notices

The following license texts and source descriptions remain with the frontend:

- [ORB_SOURCE_NOTICE.txt](ampuly/plugins/office_ui/frontend_src/observatory/ORB_SOURCE_NOTICE.txt):
  Jarvis Orb visual inspiration, [TheStack-ai/jarvis-orb](https://github.com/TheStack-ai/jarvis-orb),
  revision `d4345141e02054bafb8987ce0012123ca528a528`; MIT, Copyright (c) 2026 thestack-ai.
- [RELATIONS_SOURCE_NOTICE.txt](ampuly/plugins/office_ui/frontend_src/observatory/RELATIONS_SOURCE_NOTICE.txt):
  `react-force-graph-2d`, `force-graph` (MIT, Copyright (c) 2018 Vasco Asturiano)
  and `three` (MIT, Copyright © 2010-2026 three.js authors).
- [public/observatory-source-notice.txt](ampuly/plugins/office_ui/frontend_src/public/observatory-source-notice.txt)
  carries the Orb notice as a frontend public asset.

Existing attribution records identify the following as architecture or visual
references rather than vendored source trees:

| Project | Reviewed revision | Recorded license / reference |
| --- | --- | --- |
| [microsoft/autogen](https://github.com/microsoft/autogen) | `004749e6b7102c8fd317b9b1499ce5807223c278` | MIT code; documentation has a separate CC-BY-4.0 license |
| [cft0808/edict](https://github.com/cft0808/edict) | `14a207557719c046af0f993a7bff1cc5a5015b33` | MIT; dashboard information hierarchy |
| [wickedapp/openclaw-office](https://github.com/wickedapp/openclaw-office) | `659fdd9e19ad9cfb224f9f52dd4fc5d8fe77dc59` | MIT; employee-card hierarchy |
| [mudrii/openclaw-dashboard](https://github.com/mudrii/openclaw-dashboard) | `b63ac259867785c720166bc6b6e94e194c1014e8` | MIT; command-center layout |
| [dvelton/Agent-Management-System](https://github.com/dvelton/Agent-Management-System) | `abfe41c58da429a6e09f8759c9f2640f654fb2e7` | MIT; organization vocabulary |
| [agentscope-ai/agentscope-studio](https://github.com/agentscope-ai/agentscope-studio) | `20e7638273d3726732eb317b440bfa34e6694e17` | Apache-2.0; trace-list layout |
| [useroneoneone/neural-vault](https://github.com/useroneoneone/neural-vault) | Not pinned in existing attribution | PolyForm Noncommercial; visual ideas only |
| [cyberdaddyleo/agent-display](https://github.com/cyberdaddyleo/agent-display) | `7a6b262d88befe91d0eda8ebb6b8059fc322166b` | PolyForm Noncommercial 1.0.0; visual ideas only, Copyright (c) 2026 赛博老爸 |

The latter two licenses do not grant a general right to copy their source into a
commercial distribution. The existing implementation notices state that their
code, shaders, assets and text were not copied. This attribution must not be read
as importing those projects under the Ampuly license.

## Installed dependencies

Dependencies are declared in `pyproject.toml` and the frontend `package.json` /
`package-lock.json`. They are separate from the source adaptations listed above.
Notable existing references include LiteLLM (MIT), Lark (MIT), OpenAI Codex SDK/CLI
(Apache-2.0), and yt-dlp (Unlicense). The yt-dlp integration is an Ampuly subprocess
wrapper; yt-dlp and FFmpeg binaries are not part of this source snapshot.

An artifact SBOM must be generated from the actual built artifact and resolved
dependencies. The source inventory and a declared dependency constraint are not
proof of an installed version, a completed build or production readiness.

## Role prompt boundary

The imported third-party talent collection is excluded from this snapshot. The
only distributed role prompt, `distribution/prompts/talent/general-default-employee.md`,
is a newly authored generic template. It does not contain imported agency role
text, customer workflows or private VI business instructions. Its licensing is
part of the separate terms for original Ampuly additions.
