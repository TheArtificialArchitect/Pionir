# Windows setup

Pionir runs as its own small Python environment and calls Atani through Atani's existing CLI.
Each specialist remains a separately managed local process. This keeps dependency upgrades and
failures inside each source project.

## Prerequisites

- Python 3.12 for Atani and the recommended Pionir environment.
- Atani installed in `C:\src\Atani\.venv`.
- Ollama with the models configured by Atani, plus `nomic-embed-text` for hybrid memory recall.

## Install Pionir

From PowerShell in the Pionir checkout:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\bootstrap.ps1
```

## Configure the process environment

Do not put the real bridge token in `.env.example` or Git. Set it in the user environment or
in the shell that launches Pionir:

```powershell
$env:PIONIR_STATE_ROOT = "$env:USERPROFILE\.pionir"
$env:PIONIR_ATANI_COMMAND_JSON = '["C:\\src\\Atani\\.venv\\Scripts\\atani.exe"]'
$env:PIONIR_BRYO_STATUS_COMMAND_JSON = '["C:\\src\\terrarium\\.venv\\Scripts\\python.exe","-m","bryo.status"]'
```

If Atani's environment uses a different path, change only
`PIONIR_ATANI_COMMAND_JSON`. Pionir never invokes it through a shell.

## Verify

```powershell
.\.venv\Scripts\pionir.exe doctor
.\.venv\Scripts\pionir.exe capabilities
```

`doctor` verifies Pionir's audit chain and checks each specialist independently. One unavailable
specialist does not corrupt or erase another specialist's state.

## Launch the desktop shell

```powershell
.\scripts\launch-desktop.ps1
```

To add a `Pionir` shortcut to the current user's Windows desktop:

```powershell
.\scripts\install-desktop-shortcut.ps1
```

The launcher supplies the standard `C:\src\Atani` and `C:\src\terrarium` paths. Override the
launcher parameters if those checkouts live elsewhere. The shell exposes Atani's reasoning
(`reasoning.atani_answer`, one lean 4B - the separate depth tier was dropped 2026-09-13) plus
doctor, capabilities, and Bryo status. The conversational voice, Galatea, is wired in when
`PIONIR_GALATEA_URL` is set: plain conversation then routes to her (`conversation.galatea_reply`);
with no voice configured it has no route and the router asks rather than guessing.

## First bounded calls

```powershell
.\.venv\Scripts\pionir.exe ask-atani "Explain the evidence for this decision"
.\.venv\Scripts\pionir.exe bryo-status
.\.venv\Scripts\pionir.exe run-atani-plan .\examples\atani-plan.json
```

Bryo's read-only status is read through its own module in its own environment; Pionir does not
start, stop, or mutate it, preserving its governor, watchdog, and state.
