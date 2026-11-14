# Ampuly Engine

[简体中文](README.md) | English

> **Source available, with conditions for commercial use.** Ampuly's original
> contributions are governed by [LICENSE](LICENSE). Upstream and third-party rights
> are retained; see [NOTICE.md](NOTICE.md). This is not an OSI-approved open-source
> license. This snapshot is a developer preview, not a fully validated installer.

Ampuly Engine is a Python engine for project tasks and business collaboration.
The source includes Agent execution, tool calls, approvals, project data management,
and a browser-based console. Its early foundation came from HKUDS/OpenOPC; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for attribution and licenses.

This snapshot contains the general engine. It excludes private VI workflows,
customer projects, runtime databases, API keys, development records, conversation
history, and internal Git history. Previous installers, browser binaries, Python
environments, and the built frontend are also excluded.

## Prepare a development environment

Run the following commands manually in a **fresh checkout**. Use the tested
Python 3.12; the declared minimum Python version is 3.10. The frontend requires
Node.js `^20.19.0 || >=22.12.0` and npm.

Dependency installation downloads software. Review `pyproject.toml`, the frontend
`package.json`, and `package-lock.json` before installing.

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[superones-runtime]"
Push-Location ampuly/plugins/office_ui/frontend_src
npm ci
npm run build
Pop-Location
```

`superones-runtime` is an existing compatibility name for an optional dependency
group. It includes aiohttp, protocol dependencies, and software such as the Codex
SDK/CLI. Installing the group does not sign into accounts, authorize external
Agents, or enable a real model. You can omit it when only reviewing backend source.
There is no complete frozen Python dependency lock; these steps are not a promise
of reproducible offline installation.

## Initialize with a fresh data directory

These variables apply only to the current PowerShell process and its children.
They do not change system environment settings. Use a fresh checkout where
`.ampuly` contains no existing business data.

```powershell
$env:AMPULY_HOME = Join-Path (Get-Location) '.ampuly'
$env:AMPULY_WORKPLACE_ROOT = Join-Path $env:AMPULY_HOME 'workplace'
$env:APPDATA = Join-Path $env:AMPULY_HOME 'user-config'
$env:LOCALAPPDATA = Join-Path $env:AMPULY_HOME 'user-local'
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $env:AMPULY_HOME 'browsers'
.\.venv\Scripts\ampuly.exe init --no-external-agent-preflight --no-trust-external-agents
```

Initialization creates local configuration, skill, and memory files and records
configuration trust for this project under the designated APPDATA directory.
The flags disable external-Agent initialization preflight and automatic trust.
Do not reuse a production HOME or another project's data. Do not copy real
accounts, browser sessions, or production configuration into an example.

Provide model credentials through your own local configuration or explicitly
supported environment variables. Credential fields in the templates are empty.
Never commit populated configuration, `.env`, browser state, or databases.

## Start the local console

Confirm that the port is free, then run in the same PowerShell session:

```powershell
.\.venv\Scripts\ampuly.exe ui --host 127.0.0.1 --port 8875
```

Open `http://127.0.0.1:8875/`. Stop the foreground process with Ctrl+C.
Follow the UI to create your own local account. Do not bind to a public interface
before setting up authentication and reviewing network access.

The current UI startup code may attempt to install aiohttp if it is missing and
run npm installation/build if the frontend is missing or stale. The explicit
preparation steps above perform these tasks in advance; `ampuly ui` is not a
side-effect-free inspection command. Browser automation additionally requires
compatible Playwright browser binaries, which users must explicitly install.
This source snapshot does not install those binaries automatically.

## Source and samples

- `ampuly/`: general engine, tools, and console source.
- `config/`: initialization templates with credentials removed.
- `skills/core/`: general skill text.
- `distribution/prompts/talent/general-default-employee.md`: one newly written
  general role template; no third-party role library is bundled.
- `examples/isolation/`: newly written synthetic samples for read-only isolation
  planning, with no business data.
- `licenses/`: additional third-party license texts.

Users create their own workflows or import them under separate authorization.
Private VI workflows are not included. See [SECURITY.md](SECURITY.md) for security
boundaries and reporting.

## Validation status — November 14, 2026

For public source commit `be6d2bc219be338bdf2220efbb2128215722f4d9`, we used
Python 3.12, Node.js 22.23.1, and synthetic data in a fresh Windows test environment
to complete:

- Dependency installation in a new virtual environment and `pip check`.
- Frontend `npm ci` / `npm run build`, plus Python wheel build and resource checks.
- Local console health, home and static resources, first-account creation,
  login, and creation/listing of an empty project.

These results cover basic flows only. **Overall acceptance has not passed.**
Controlled checks also found attempts by background notifier/relay components
to discover Windows user configuration through the registry. The checks blocked
these attempts before values were read. The data-directory settings above are
not an operating-system sandbox and do not establish complete isolation or
fully offline operation. Editable installation also left a task process, which
was stopped at the end of testing. Use neither production accounts nor real
business data for an initial evaluation.

The wheel does not include the built frontend or a standalone runtime. Console
checks used a separately built source frontend. Real-model/external-Agent
end-to-end operation, production workflows, browser end-to-end interaction,
installation on a second physical computer, and full-system regression have not
been verified. There is no one-click installer or complete frozen Python
dependency lock in this preview.

## Try it and share feedback

Developers familiar with Python/Node are welcome to start with an empty project
and synthetic data. Use [Discussions](https://github.com/amplichina/ampuly-engine-source/discussions)
for use cases, installation experience, and design feedback. File reproducible
bugs in [Issues](https://github.com/amplichina/ampuly-engine-source/issues).

Include your OS, Python/Node versions, source commit, reproduction steps, and
expected versus actual behavior. Share only minimal, redacted error excerpts.
Do not upload keys, configuration files, databases, account details, or private
workflows. Follow [SECURITY.md](SECURITY.md) for security reports.

## Commercial licensing and voluntary support

Commercial use of covered contributions requires written authorization.
The shared-design option waives the software license fee in exchange for
authorized, redacted workflow designs, used only for Ampuly's internal research
and product improvement. The confidential option does not require design sharing
and charges 3% of revenue actually received from delivery directly performed
through the engine, less refunds and pass-through taxes. Both options require
only the necessary revenue summaries under an explicit agreement, not public
disclosure. [LICENSE](LICENSE) defines the complete scope; its Chinese text controls.

Local console accounts are distinct from commercial authorization accounts.
Commercial accounts, design uploads, revenue reporting, and payment services
are not implemented. This snapshot adds no background collection or automatic
charging for these plans. Installation or sponsorship does not grant a commercial
license or change existing third-party rights.

[Support Ampuly development](https://www.ampuly.com/sponsor) through the
[official PayPal payment link](https://www.paypal.com/ncp/payment/WFVSM8PJC8TG6).
Support is a one-time contribution in USD at an amount you choose, with no
automatic renewal or additional commercial license. Domestic payment options
will be added later.

## Optional network features

Configured model, messaging, and browser-proxy functions send relevant inputs
to the selected services. The optional Puly feature requires a user-supplied
key and sends task and interaction state to its TypeSafe service; this is not
the commercial reporting service described above. Review implementations and
provider policies before supplying credentials or business content. Real-Agent
integration tests require separate environment switches and credentials and
are not part of default offline checks.
