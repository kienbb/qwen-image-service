# Qwen Image Service installer for Windows (PowerShell 5.1+). Run in an elevated PowerShell:
#   irm https://<host>/install.ps1 | iex
# With options (or set QIS_* environment variables before `irm | iex`):
#   & ([scriptblock]::Create((irm https://<host>/install.ps1))) -Tier T1 -Yes -TunnelToken <token>
#
# It installs uv + Python into <Root>, fetches the app, then hands over to `qis setup` which detects the GPU,
# lets you pick the model profile, installs ComfyUI + PyTorch + models, registers the Windows service and
# (optionally) cloudflared. Re-running it is safe: finished steps are skipped.
param(
    [string]$Root = $(if ($env:QIS_ROOT) { $env:QIS_ROOT } else { "C:\qis" }),
    [string]$Tier = $env:QIS_TIER,
    [string]$Variant = $(if ($env:QIS_VARIANT) { $env:QIS_VARIANT } else { "recommended" }),
    [int]$Port = $(if ($env:QIS_PORT) { [int]$env:QIS_PORT } else { 8080 }),
    [int]$ComfyPort = $(if ($env:QIS_COMFY_PORT) { [int]$env:QIS_COMFY_PORT } else { 8188 }),
    [string]$ModelsDir = $env:QIS_MODELS_DIR,
    [string[]]$ReuseModels = @(),
    [string]$AdminUser = $(if ($env:QIS_ADMIN_USER) { $env:QIS_ADMIN_USER } else { "admin" }),
    [string]$AdminEmail = $env:QIS_ADMIN_EMAIL,
    [string]$AdminPassword = $env:QIS_ADMIN_PASSWORD,
    [string]$TunnelToken = $env:QIS_TUNNEL_TOKEN,
    [string]$PublicUrl = $env:QIS_PUBLIC_URL,
    [string]$HfToken = $env:HF_TOKEN,
    [string]$Repo = $(if ($env:QIS_REPO) { $env:QIS_REPO } else { "https://github.com/kienbb/qwen-image-service.git" }),
    [string]$Ref = $(if ($env:QIS_REF) { $env:QIS_REF } else { "master" }),
    [string]$GithubToken = $env:QIS_GITHUB_TOKEN,
    [string]$TorchIndex = $env:QIS_TORCH_INDEX,
    [switch]$Yes = [bool]$env:QIS_YES,
    [switch]$Lan,
    [switch]$NoService,
    [switch]$SkipModels,
    [switch]$SkipBenchmark,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is 10x slower with the progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Info($m) { Write-Host "    $m" }
function Warn($m) { Write-Host "    ! $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "`nERROR: $m" -ForegroundColor Red; exit 1 }

Write-Host "Qwen Image Service installer -> $Root" -ForegroundColor Green

# ---------------------------------------------------------------- preflight
Step "Preflight"
if (-not [Environment]::Is64BitOperatingSystem) { Fail "64-bit Windows is required" }
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin -and -not $NoService) {
    Fail "run PowerShell as Administrator (needed to register the Windows service), or pass -NoService"
}
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if (-not $smi -and (Test-Path "$env:SystemRoot\System32\nvidia-smi.exe")) { $smi = "$env:SystemRoot\System32\nvidia-smi.exe" }
if ($smi) {
    $gpu = & $smi --query-gpu=name,memory.total,driver_version --format=csv,noheader 2>$null | Select-Object -First 1
    Info "GPU: $gpu"
} else {
    Warn "nvidia-smi not found: no NVIDIA GPU/driver. Only the tools-only profile (T0) will be offered."
}
$driveName = (Split-Path -Qualifier ([IO.Path]::GetFullPath($Root))).TrimEnd(":")
$freeGb = [math]::Round((Get-PSDrive $driveName).Free / 1GB)
Info "free space on ${driveName}: $freeGb GB"
if ($freeGb -lt 30 -and -not $SkipModels -and -not $Force) { Fail "need at least ~30 GB free on ${driveName}: (models + PyTorch)" }
New-Item -ItemType Directory -Force -Path $Root, "$Root\bin" | Out-Null

# ---------------------------------------------------------------- uv + python
Step "uv (Python manager)"
$uv = "$Root\bin\uv.exe"
if (-not (Test-Path $uv)) {
    $env:UV_INSTALL_DIR = "$Root\bin"
    $env:UV_NO_MODIFY_PATH = "1"
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    if (-not (Test-Path $uv)) { Fail "uv installation failed" }
}
Info (& $uv --version)
$env:UV_PYTHON_INSTALL_DIR = "$Root\python"
$env:UV_CACHE_DIR = "$Root\cache\uv"
$env:PYTHONIOENCODING = "utf-8"

# ---------------------------------------------------------------- app
Step "Application ($Ref)"
$app = "$Root\app"
$git = Get-Command git -ErrorAction SilentlyContinue
$authRepo = $Repo
if ($GithubToken -and $Repo -like "https://github.com/*") { $authRepo = $Repo -replace "^https://", "https://x-access-token:$GithubToken@" }
if ($git) {
    if (Test-Path "$app\.git") {
        & git -C $app fetch --tags origin 2>&1 | Out-Null
        & git -C $app checkout --force $Ref 2>&1 | Out-Null
        & git -C $app reset --hard "origin/$Ref" 2>&1 | Out-Null
    } else {
        & git clone --branch $Ref $authRepo $app
        if ($LASTEXITCODE -ne 0) { Fail "git clone failed (private repo? pass -GithubToken)" }
        & git -C $app remote set-url origin $Repo
    }
} else {
    $slug = ($Repo -replace "^https://github.com/", "") -replace "\.git$", ""
    $headers = @{ "User-Agent" = "qis-installer" }
    if ($GithubToken) { $headers["Authorization"] = "Bearer $GithubToken" }
    $zip = "$Root\app.zip"
    Invoke-WebRequest -UseBasicParsing -Headers $headers -Uri "https://api.github.com/repos/$slug/zipball/$Ref" -OutFile $zip
    $tmp = "$Root\app_x"
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    Expand-Archive $zip $tmp
    Remove-Item -Recurse -Force $app -ErrorAction SilentlyContinue
    Move-Item (Get-ChildItem $tmp | Select-Object -First 1).FullName $app
    Remove-Item -Recurse -Force $tmp, $zip
    Warn "installed from a zip: install git to use 'qis update'"
}

Step "Python environment"
$py = "$Root\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    & $uv python install 3.13
    & $uv venv --python 3.13 "$Root\.venv"
    if ($LASTEXITCODE -ne 0) { Fail "could not create the virtual environment" }
}
& $uv pip install --python $py -e $app
if ($LASTEXITCODE -ne 0) { Fail "installing the application failed" }

# ---------------------------------------------------------------- hand over to qis setup
Step "qis setup"
$argsList = @("setup", "--root", $Root, "--variant", $Variant, "--port", $Port, "--comfy-port", $ComfyPort, "--admin-user", $AdminUser)
if ($Tier) { $argsList += @("--tier", $Tier) }
if ($ModelsDir) { $argsList += @("--models-dir", $ModelsDir) }
foreach ($r in $ReuseModels) { $argsList += @("--reuse-models", $r) }
if ($AdminEmail) { $argsList += @("--admin-email", $AdminEmail) }
if ($AdminPassword) { $argsList += @("--admin-password", $AdminPassword) }
if ($TunnelToken) { $argsList += @("--tunnel-token", $TunnelToken) }
if ($PublicUrl) { $argsList += @("--public-url", $PublicUrl) }
if ($HfToken) { $argsList += @("--hf-token", $HfToken) }
if ($TorchIndex) { $argsList += @("--torch-index", $TorchIndex) }
if ($Yes) { $argsList += "--yes" }
if ($Lan) { $argsList += "--lan" }
if ($NoService) { $argsList += "--no-service" }
if ($SkipModels) { $argsList += "--skip-models" }
if ($SkipBenchmark) { $argsList += "--skip-benchmark" }
if ($Force) { $argsList += "--force" }
$env:UV_INSTALL_DIR = "$Root\bin"
& "$Root\.venv\Scripts\qis.exe" @argsList
if ($LASTEXITCODE -ne 0) { Fail "qis setup failed (rerun the installer to resume)" }
