<#
Installs the proofing engine as a Windows service with NSSM (https://nssm.cc).
Run in an elevated PowerShell on the proofing station.

  .\install_service.ps1 -Python C:\ProofingEngine\.venv\Scripts\python.exe `
      -JobsDir \\voom-fs\proofing\jobs -Tokens "printssistant:<token>,zohodesk:<token>" `
      -DisplayProfile "C:\Windows\System32\spool\drivers\color\CG2700X.icc"

The API listens on all interfaces at -Port; restrict it to the LAN with the firewall rule below.
#>
param(
  [Parameter(Mandatory)] [string] $Python,
  [Parameter(Mandatory)] [string] $JobsDir,
  [Parameter(Mandatory)] [string] $Tokens,
  [string] $DisplayProfile = "",
  [string] $Nssm = "nssm.exe",
  [string] $Service = "VoomProofingEngine",
  [int] $Port = 8750,
  [int] $Workers = 1,
  [string] $LanSubnet = "LocalSubnet"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$logs = Join-Path $root "logs"
New-Item -ItemType Directory -Force $logs | Out-Null

& $Nssm install $Service $Python "-m" "proofing_engine.cli" "serve" "--host" "0.0.0.0" "--port" $Port "--jobs" $JobsDir
& $Nssm set $Service AppDirectory $root
& $Nssm set $Service DisplayName "Voom Proofing Engine"
& $Nssm set $Service Description "Colour inventory, checks and soft proofs (LAN API)"
& $Nssm set $Service Start SERVICE_AUTO_START
& $Nssm set $Service AppStdout (Join-Path $logs "service.log")
& $Nssm set $Service AppStderr (Join-Path $logs "service.log")
& $Nssm set $Service AppRotateFiles 1
& $Nssm set $Service AppRotateBytes 10485760
& $Nssm set $Service AppExit Default Restart
$envs = @("PROOF_API_TOKENS=$Tokens", "PROOF_WORKERS=$Workers", "PROOF_JOBS_DIR=$JobsDir", "PROOF_GS=gswin64c")
if ($DisplayProfile) { $envs += "PROOF_DISPLAY_PROFILE=$DisplayProfile" }
& $Nssm set $Service AppEnvironmentExtra $envs

New-NetFirewallRule -DisplayName "Voom Proofing Engine" -Direction Inbound -Protocol TCP -LocalPort $Port `
  -RemoteAddress $LanSubnet -Action Allow -Profile Domain,Private | Out-Null

& $Nssm start $Service
Write-Host "Started $Service on port $Port. Check: Invoke-RestMethod http://localhost:$Port/health"
