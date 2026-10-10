param([switch]$CheckOnly)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

# Native programs do not always trigger PowerShell exceptions.
function Check-ExitCode([string]$Step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed. Exit code: $LASTEXITCODE"
    }
}

function Get-GpuProfile([string]$Name, [version]$Driver, [int]$MemoryMiB) {
    if ($Name -notmatch '(?:GTX\s+(10|16)|RTX\s+(20|30|40|50))\d{2}(?:\D|$)') {
        throw "Supported GPUs: GeForce GTX 10/16 or RTX 20/30/40/50 series (4GB+). Detected: $Name"
    }
    $Series = if ($Matches[1]) { $Matches[1] } else { $Matches[2] }
    if ($MemoryMiB -lt 4000) {
        throw "This release requires a 4GB-class GPU or larger."
    }
    $CudaBuild = 'cu126'
    $CudaVersion = '12.6'
    $MinimumDriver = [version]'560.76'
    if ($Series -eq '50') {
        $CudaBuild = 'cu130'
        $CudaVersion = '13.0'
        $MinimumDriver = [version]'580.88'
    }
    if ($Driver -lt $MinimumDriver) {
        throw "$Name requires driver $MinimumDriver or newer for $CudaBuild. Detected: $Driver"
    }
    return [pscustomobject]@{
        Torch = "2.14.1+$CudaBuild"
        Cuda = $CudaVersion
        Index = "https://download.pytorch.org/whl/$CudaBuild"
    }
}

try {
    # 1. Check platform.
    if (
        -not [Environment]::Is64BitOperatingSystem -or
        $env:PROCESSOR_ARCHITECTURE -ne "AMD64"
    ) {
        throw "This installer supports Windows x64 only."
    }

    # 2. Check NVIDIA GPU and driver before downloading large packages.
    Write-Host "`n[1/5] Checking GPU..."

    $Smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue

    if (-not $Smi) {
        throw "NVIDIA driver not found. Install the NVIDIA driver first."
    }

    $GpuRows = @(
        & $Smi.Source `
            --query-gpu=name,driver_version,memory.total `
            --format=csv,noheader,nounits
    )
    Check-ExitCode "GPU detection"

    if ($GpuRows.Count -ne 1) {
        throw "This first installer supports one NVIDIA GPU only."
    }

    $Gpu = $GpuRows[0] -split ","
    $GpuName = $Gpu[0].Trim()
    $DriverVersion = [version]$Gpu[1].Trim()
    $MemoryMiB = [int]$Gpu[2].Trim()

    Write-Host "GPU: $GpuName"
    Write-Host "Driver: $DriverVersion"
    Write-Host "VRAM: $MemoryMiB MiB"

    $GpuProfile = Get-GpuProfile $GpuName $DriverVersion $MemoryMiB
    Write-Host "Selected PyTorch: $($GpuProfile.Torch) / CUDA $($GpuProfile.Cuda)"
    if ($GpuName -match 'GTX\s+10\d{2}') {
        Write-Host 'Pascal: default Whisper INT8/FP32. Qwen FP32 is experimental (high VRAM usage).'
    }
    if ($CheckOnly) {
        Write-Host "Hardware selection passed. No files or packages were changed."
        exit 0
    }
    if (-not (Test-Path -LiteralPath ".env")) {
        Copy-Item -LiteralPath ".env.example" -Destination ".env"
    }

    # 3. Install uv locally, without adding it to the user's PATH.
    Write-Host "`n[2/5] Preparing uv..."

    $ToolsDirectory = Join-Path $PSScriptRoot ".tools"
    $Uv = Join-Path $ToolsDirectory "uv.exe"

    if (-not (Test-Path -LiteralPath $Uv)) {
        New-Item -ItemType Directory -Force `
            -Path $ToolsDirectory | Out-Null

        $env:UV_INSTALL_DIR = $ToolsDirectory
        $env:UV_NO_MODIFY_PATH = "1"

        $Installer = Join-Path $ToolsDirectory "install-uv.ps1"

        Invoke-WebRequest `
            -Uri "https://astral.sh/uv/install.ps1" `
            -OutFile $Installer `
            -UseBasicParsing

        & powershell.exe -NoProfile -ExecutionPolicy Bypass `
            -File $Installer
        Check-ExitCode "uv installation"

        if (-not (Test-Path -LiteralPath $Uv)) {
            throw "uv.exe was not installed."
        }
    }

    # 4. Download managed Python and create a project environment.
    Write-Host "`n[3/5] Preparing Python 3.11..."

    $env:UV_PYTHON_INSTALL_DIR = Join-Path $PSScriptRoot ".python"

    $Python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

    if (-not (Test-Path -LiteralPath $Python)) {
        & $Uv venv --python 3.11 --managed-python ".venv"
        Check-ExitCode "Python environment creation"
    }

    # Pin the GPU build before installing the remaining dependencies.
    Write-Host "`n[4/5] Installing dependencies..."

    & $Uv pip install --python $Python `
        "torch==$($GpuProfile.Torch)" `
        --index-url $GpuProfile.Index
    Check-ExitCode "PyTorch installation"

    & $Uv pip install --python $Python -r "requirements.txt"
    Check-ExitCode "Dependency installation"

    & $Uv pip check --python $Python
    Check-ExitCode "Dependency verification"

    # 5. Verify actual GPU execution and model loading.
    Write-Host "`n[5/5] Verifying installation..."
    Write-Host "The first run downloads the ASR model."

    & $Python -X utf8 "verify_install.py" `
        --expected-torch $GpuProfile.Torch --expected-cuda $GpuProfile.Cuda
    Check-ExitCode "Runtime verification"

    Write-Host "`nReady. Configure OPENAI_API_KEY, then run start.bat."
    exit 0
}
catch {
    Write-Host "`nInstallation failed:" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
