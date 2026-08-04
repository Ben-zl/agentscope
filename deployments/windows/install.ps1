<#
.SYNOPSIS
  One-time installer for the AgentScope Windows workspace supervisor.

.DESCRIPTION
  Installs:
  - Directory structure under $env:PROGRAMDATA\AgentScope
  - uv (the Python package manager) at a fixed path
  - win_runner.ps1 (the exec_shell helper) at a fixed path
  - A Python venv for the supervisor
  - The supervisor as a Windows Service (via nssm)

  Must be run as Administrator.

.PARAMETER ServiceCredential
  Credentials for the Windows user used by both SSH sessions and the
  supervisor service. Keeping these identities aligned prevents privilege
  escalation through workspace-controlled gateway configuration.

.EXAMPLE
  $cred = Get-Credential -UserName ".\AgentScopeUser"
  .\install.ps1 -ServiceCredential $cred
#>
param(
    [Parameter(Mandatory=$true)]
    [System.Management.Automation.PSCredential]$ServiceCredential
)

$ErrorActionPreference = "Stop"

# --- Check admin ---
$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent()
)
if (-not $principal.IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)) {
    Write-Error "This script must be run as Administrator."
    exit 1
}

$ROOT = Join-Path $env:PROGRAMDATA "AgentScope"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SshUser = $ServiceCredential.UserName

Write-Host "=== AgentScope Windows Workspace Installer ===" -ForegroundColor Cyan
Write-Host "Root: $ROOT"
Write-Host "SSH user: $SshUser"
Write-Host ""

# --- 1. Directory structure ---
Write-Host "[1/6] Creating directories..." -ForegroundColor Yellow
$dirs = @(
    (Join-Path $ROOT "ws"),
    (Join-Path $ROOT "runner"),
    (Join-Path $ROOT "uv"),
    (Join-Path $ROOT "supervisor"),
    (Join-Path $ROOT "tmp")
)
foreach ($d in $dirs) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}

# --- 2. Install uv ---
Write-Host "[2/6] Installing uv..." -ForegroundColor Yellow
$UvExe = Join-Path $ROOT "uv\uv.exe"
if (-not (Test-Path $UvExe)) {
    $PreviousUvInstallDir = $env:UV_INSTALL_DIR
    try {
        $env:UV_INSTALL_DIR = Join-Path $ROOT "uv"
        Invoke-RestMethod https://astral.sh/uv/install.ps1 |
            Invoke-Expression
    } finally {
        $env:UV_INSTALL_DIR = $PreviousUvInstallDir
    }
    if (-not (Test-Path $UvExe)) {
        throw "uv installer did not create $UvExe"
    }
} else {
    Write-Host "  uv already installed."
}

# --- 3. Deploy runner script ---
Write-Host "[3/6] Deploying win_runner.ps1..." -ForegroundColor Yellow
$RunnerSrc = Join-Path $ScriptDir "runner\win_runner.ps1"
$RunnerDst = Join-Path $ROOT "runner\win_runner.ps1"
Copy-Item $RunnerSrc $RunnerDst -Force

# --- 4. Supervisor venv ---
Write-Host "[4/6] Creating supervisor venv..." -ForegroundColor Yellow
$SupVenv = Join-Path $ROOT "supervisor\.venv"
$SupPython = Join-Path $SupVenv "Scripts\python.exe"
if (-not (Test-Path $SupPython)) {
    # Use system Python to create the venv.
    & $UvExe venv $SupVenv
}
& $UvExe pip install --python $SupPython fastapi uvicorn httpx pydantic

# Deploy supervisor script.
$SupSrc = Join-Path $ScriptDir "ws_supervisor\ws_supervisor.py"
$SupDst = Join-Path $ROOT "supervisor\ws_supervisor.py"
Copy-Item $SupSrc $SupDst -Force

# --- 5. Directory ACLs ---
Write-Host "[5/6] Setting directory ACLs..." -ForegroundColor Yellow
$FullControlDirs = @("ws", "supervisor", "tmp")
foreach ($name in $FullControlDirs) {
    $path = Join-Path $ROOT $name
    icacls $path /inheritance:r | Out-Null
    icacls $path /grant:r `
        "${SshUser}:(OI)(CI)F" `
        "SYSTEM:(OI)(CI)F" `
        "BUILTIN\Administrators:(OI)(CI)F" | Out-Null
}

foreach ($name in @("runner", "uv")) {
    $path = Join-Path $ROOT $name
    icacls $path /inheritance:r | Out-Null
    icacls $path /grant:r `
        "${SshUser}:(OI)(CI)RX" `
        "SYSTEM:(OI)(CI)F" `
        "BUILTIN\Administrators:(OI)(CI)F" | Out-Null
}

# --- 6. Register Windows Service ---
Write-Host "[6/6] Registering supervisor service..." -ForegroundColor Yellow

# Check if nssm is available.
$nssm = Get-Command nssm -ErrorAction SilentlyContinue
if (-not $nssm) {
    throw "nssm is required. Install it and rerun this installer."
} else {
    $svc = Get-Service AgentScopeSupervisor -ErrorAction SilentlyContinue
    if ($svc) {
        Write-Host "  Service already exists. Stopping and reconfiguring..."
        Stop-Service AgentScopeSupervisor -Force -ErrorAction SilentlyContinue
    } else {
        & nssm install AgentScopeSupervisor $SupPython "ws_supervisor.py"
    }
    & nssm set AgentScopeSupervisor AppDirectory (Join-Path $ROOT "supervisor")
    & nssm set AgentScopeSupervisor AppEnvironmentExtra "AS_ROOT=$ROOT"
    # Run as the SSH user (trusted single-tenant — same identity).
    $ServicePassword = $ServiceCredential.GetNetworkCredential().Password
    & nssm set AgentScopeSupervisor ObjectName $SshUser $ServicePassword
    Start-Service AgentScopeSupervisor
    Write-Host "  Service started." -ForegroundColor Green
}

Write-Host ""
Write-Host "=== Installation complete ===" -ForegroundColor Cyan
Write-Host "Supervisor: http://127.0.0.1:7550/healthz (loopback)"
Write-Host "Root:       $ROOT"
Write-Host "Runner:     $ROOT\runner\win_runner.ps1"
Write-Host "uv:         $ROOT\uv\uv.exe"
