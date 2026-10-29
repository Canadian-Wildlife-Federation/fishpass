# Local FishPass Development Setup on Windows

This guide describes how to set up the **FishPass modelling engine** for local testing on a Windows machine without administrator permissions.

The local setup mirrors the important parts of the GitHub Actions workflow: it uses **Python 3.10**, installs the FishPass dependencies in an isolated virtual environment, loads database credentials as environment variables, and runs the modelling engine directly from PowerShell.

> [!IMPORTANT]
> Database credentials must never be committed to the repository. Each developer must create their own `local_env.ps1` file as described below.

## Prerequisites

Before starting, you will need:

- Windows
- PowerShell
- Git
- A local clone of the FishPass repository
- Python Install Manager
- Access to the FishPass PostgreSQL database
- The required FishPass database credentials

These instructions assume that you **do not have administrator permissions**.

---

## One-time setup

### 1. Open the FishPass repository in PowerShell

Navigate to the repository root:

```powershell
cd C:\Users\<username>\GitHub\fishpass
```

All commands below should be run from the repository root unless otherwise specified.

### 2. Install Python 3.10

FishPass local development uses **Python 3.10**.

First, confirm that the Python Install Manager is available:

```powershell
pymanager --version
```

You can view the Python versions available through the manager with:

```powershell
py list --online
```

Install Python 3.10:

```powershell
pymanager install 3.10
```

Confirm that Python 3.10 is installed:

```powershell
py list
```

> [!NOTE]
> Installing Python 3.10 does not necessarily make a plain `python` command use Python 3.10. The next step explicitly creates the FishPass virtual environment using Python 3.10.

### 3. Create the virtual environment

From the repository root, create a virtual environment named `.venv` using Python 3.10:

```powershell
py -3.10 -m venv .venv
```

#### Replacing an existing virtual environment

If `.venv` already exists but was created with another Python version, deactivate it if necessary:

```powershell
deactivate
```

Delete the existing environment:

```powershell
Remove-Item -Recurse -Force .venv
```

Recreate it with Python 3.10:

```powershell
py -3.10 -m venv .venv
```

### 4. Activate the virtual environment

Activate `.venv`:

```powershell
.\.venv\Scripts\Activate.ps1
```

After activation, the PowerShell prompt will typically begin with `(.venv)`, for example:

```text
(.venv) PS C:\Users\<username>\GitHub\fishpass>
```

### 5. Verify that `.venv` is using Python 3.10

Do not skip this check when initially creating the environment.

Run:

```powershell
python --version
```

The result should be:

```text
Python 3.10.x
```

You can also verify exactly which Python executable is being used:

```powershell
python -c "import sys; print(sys.executable)"
```

The path should point to the `.venv` directory in your FishPass repository, for example:

```text
C:\Users\<username>\GitHub\fishpass\.venv\Scripts\python.exe
```

If `python --version` does not report Python 3.10, recreate `.venv`:

```powershell
deactivate
Remove-Item -Recurse -Force .venv
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python --version
```

### 6. Install FishPass dependencies

With `.venv` activated, install the dependencies used by the modelling engine:

```powershell
python -m pip install -r fishpass_engine\scripts\requirements.txt
```

Using `python -m pip` ensures that packages are installed into the Python environment that is currently active.

---

## Local database configuration

### 7. Create `local_env.ps1`

The FishPass database connection expects the following environment variables:

```text
FISHPASS_HOST
FISHPASS_PORT
FISHPASS_DBNAME
FISHPASS_USER
FISHPASS_PASSWORD
```

Each developer must create their own file named `local_env.ps1` in the **repository root**.

Use the following format:

```powershell
$env:FISHPASS_HOST="<host>"
$env:FISHPASS_PORT="<port>"
$env:FISHPASS_DBNAME="<database>"
$env:FISHPASS_USER="<username>"
$env:FISHPASS_PASSWORD="<password>"
```

Replace the placeholder values with the appropriate FishPass database credentials.

> [!WARNING]
> `local_env.ps1` contains database credentials and must never be committed to Git.

Ensure `.gitignore` contains:

```gitignore
local_env.ps1
```

Before committing changes, verify with:

```powershell
git status
```

`local_env.ps1` should not appear as an untracked or modified file.

### 8. Load the local environment variables

With `.venv` activated, load the database variables into the current PowerShell session:

```powershell
. .\local_env.ps1
```

> [!IMPORTANT]
> The period followed by a space at the beginning of this command is intentional. This dot-sources the script so the variables remain available in the current PowerShell session.

The variables apply only to the current PowerShell session. After opening a new PowerShell terminal, `local_env.ps1` must be loaded again.

### 9. Verify the database environment

Check selected non-sensitive values:

```powershell
$env:FISHPASS_HOST
$env:FISHPASS_PORT
$env:FISHPASS_DBNAME
$env:FISHPASS_USER
```

Avoid printing `$env:FISHPASS_PASSWORD` unnecessarily.

PowerShell environment variables use this syntax:

```powershell
$env:FISHPASS_HOST
```

Do not use Command Prompt syntax in PowerShell:

```text
%FISHPASS_HOST%
```

---

## Running FishPass locally

### 10. Run the model

The GitHub Actions workflow ultimately runs:

```text
python fishpass_engine/scripts/run_model.py <plan_code>
```

The equivalent PowerShell command is:

```powershell
python fishpass_engine\scripts\run_model.py <plan_code>
```

For example:

```powershell
python fishpass_engine\scripts\run_model.py wcrp
```

Replace `wcrp` with the plan code being tested.

---

## Everyday development

Once the one-time setup is complete, Python does not need to be reinstalled and `.venv` does not need to be recreated for every run.

For each new PowerShell session, use:

```powershell
# Navigate to the repository
cd C:\Users\<username>\GitHub\fishpass

# Activate the virtual environment
.\.venv\Scripts\Activate.ps1

# Load database credentials
. .\local_env.ps1

# Optional: verify the active Python version
python --version

# Run FishPass
python fishpass_engine\scripts\run_model.py <plan_code>
```

For example:

```powershell
python fishpass_engine\scripts\run_model.py wcrp
```

---

## Updating dependencies

If `requirements.txt` changes after pulling updates from Git, activate `.venv`:

```powershell
.\.venv\Scripts\Activate.ps1
```

Reinstall the requirements:

```powershell
python -m pip install -r fishpass_engine\scripts\requirements.txt
```

The entire virtual environment does not normally need to be recreated when dependencies change.

---

## Deactivating the environment

When finished working with FishPass, run:

```powershell
deactivate
```

The `(.venv)` prefix should disappear from the PowerShell prompt.

---

## Troubleshooting

### `python --version` does not show Python 3.10

Check the available Python installations:

```powershell
py list
```

If Python 3.10 is installed but the virtual environment uses another version, recreate `.venv`:

```powershell
deactivate
Remove-Item -Recurse -Force .venv
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python --version
```

Reinstall the project dependencies:

```powershell
python -m pip install -r fishpass_engine\scripts\requirements.txt
```

### `Missing required environment variable(s)`

FishPass requires:

```text
FISHPASS_HOST
FISHPASS_PORT
FISHPASS_DBNAME
FISHPASS_USER
FISHPASS_PASSWORD
```

Reload the environment configuration:

```powershell
. .\local_env.ps1
```

Verify the non-sensitive values:

```powershell
$env:FISHPASS_HOST
$env:FISHPASS_PORT
$env:FISHPASS_DBNAME
$env:FISHPASS_USER
```

If a value is blank, check the corresponding entry in `local_env.ps1`.

### `Activate.ps1` cannot be executed

If PowerShell reports that script execution is disabled, the PowerShell execution policy may be controlled by your organization's security configuration.

Do not change machine-wide security settings or bypass an organizational policy without appropriate approval.

### `.venv` exists but appears to be broken

It is generally safe to recreate the virtual environment because installed Python packages are defined by `requirements.txt`, rather than `.venv` itself.

From the repository root:

```powershell
deactivate
Remove-Item -Recurse -Force .venv

py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1

python --version
python -m pip install -r fishpass_engine\scripts\requirements.txt
```

Reload the local database configuration:

```powershell
. .\local_env.ps1
```

---

## Quick reference

For an existing, fully configured local environment:

```powershell
cd C:\Users\<username>\GitHub\fishpass

.\.venv\Scripts\Activate.ps1

. .\local_env.ps1

python --version

python fishpass_engine\scripts\run_model.py <plan_code>
```

## Expected local files

The repository will contain the developer-specific virtual environment and environment configuration:

```text
fishpass/
|-- .venv/
|-- local_env.ps1
|-- config/
|-- fishpass_engine/
|   `-- scripts/
|       |-- requirements.txt
|       `-- run_model.py
`-- ...
```

Neither `.venv` nor `local_env.ps1` should be committed to the repository.
