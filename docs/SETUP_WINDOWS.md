# Windows setup

Pionir runs as its own small Python environment and calls Atani through Atani's existing CLI.
Theo remains a separately managed local service. This keeps dependency upgrades and failures
inside each source project.

## Prerequisites

- Python 3.12 for Atani and the recommended Pionir environment.
- Atani installed in `C:\src\Atani\.venv`.
- Theo's `machine-learning` branch installed and its authenticated local bridge available on
  `127.0.0.1:8765`.
- Ollama with the models configured by Atani and Theo.

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
$env:PIONIR_THEO_URL = "http://127.0.0.1:8765"
$env:PIONIR_THEO_TOKEN = (Get-Content "$env:USERPROFILE\.techsupport_agent\bridge_token.txt" -Raw).Trim()
$env:PIONIR_ATANI_COMMAND_JSON = '["C:\\src\\Atani\\.venv\\Scripts\\atani.exe"]'
$env:PIONIR_BRYO_STATUS_COMMAND_JSON = '["C:\\src\\terrarium\\.venv\\Scripts\\python.exe","-m","bryo.status"]'
$env:PIONIR_PROBABILITY_URL = "http://127.0.0.1:8791"
$env:PIONIR_GENESIS_URL = "http://127.0.0.1:8000"
```

If Atani's environment uses a different path, change only
`PIONIR_ATANI_COMMAND_JSON`. Pionir never invokes it through a shell.

## Verify

```powershell
.\.venv\Scripts\pionir.exe doctor
.\.venv\Scripts\pionir.exe capabilities
```

`doctor` verifies Pionir's audit chain and checks Atani and Theo independently. One unavailable
specialist does not corrupt or erase another specialist's state.

## Launch the desktop shell

```powershell
.\scripts\launch-desktop.ps1
```

To add a `Pionir` shortcut to the current user's Windows desktop:

```powershell
.\scripts\install-desktop-shortcut.ps1
```

The launcher supplies the standard `C:\src\Atani`, `C:\src\terrarium`,
`C:\src\autogenesis`, `C:\src\Probability`, and `C:\Users\Ian\genesis-agent` paths and reads
Theo's existing bridge token from his private state file for that process only. Override the
launcher parameters if those checkouts live elsewhere. The shell exposes explicit Atani
normal/depth and Theo safe-peer routes plus doctor, capabilities, Bryo, Autogenesis,
Probability, and Genesis status. Automatic intent routing is deliberately not implied yet.

## First bounded calls

```powershell
.\.venv\Scripts\pionir.exe ask-atani "Explain the evidence for this decision"
.\.venv\Scripts\pionir.exe ask-theo "Give Atani your actual view of this design"
.\.venv\Scripts\pionir.exe bryo-status
.\.venv\Scripts\pionir.exe probability-status
.\.venv\Scripts\pionir.exe genesis-status
.\.venv\Scripts\pionir.exe run-atani-plan .\examples\atani-plan.json
```

The Theo peer command uses his conversation-only route. It cannot call his tools or read Ian's
private memory. Ordinary user-facing Theo chat remains outside Pionir until the source runtime
offers an action boundary that cannot bypass Atani's approvals.

Probability must already be running on loopback port 8791 and Genesis on loopback port 8000.
Pionir does not start or stop either process. This preserves each agent's watchdog, state,
Discord integration, and failure recovery.
