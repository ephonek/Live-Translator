param([switch]$CheckOnly)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Invoke-ProjectGit {
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$GitArgs)
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $result = @(& git @GitArgs 2>&1 | ForEach-Object { $_.ToString() })
        $gitExit = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $previousPreference }
    if ($gitExit -ne 0) {
        throw ($result -join "`n")
    }
    return $result
}

function Assert-CleanCheckout {
    # Ignored settings, models and .env stay untouched. Untracked developer files
    # are permitted; Git itself refuses an update that would overwrite them.
    $changes = @(Invoke-ProjectGit status --porcelain --untracked-files=no)
    if ($changes.Count) {
        Write-Host ($changes -join "`n")
        throw 'Local tracked files have changes. Commit or back them up before updating. Nothing was discarded.'
    }
    foreach ($item in @('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply')) {
        $path = (Invoke-ProjectGit rev-parse --git-path $item | Select-Object -First 1).ToString()
        if (Test-Path -LiteralPath $path) { throw 'An unfinished Git operation exists. Finish it before updating.' }
    }
    # Also refuse repositories that accidentally track personal data.
    $personal = @(Invoke-ProjectGit ls-files -- .env '.env.*' config/ models/ .venv/ .python/ .tools/ |
        Where-Object { $_ -ne '.env.example' })
    if ($personal.Count) { throw 'Personal files are tracked by Git. Remove them from tracking before updating.' }
}

function Get-DependencyStamp {
    $pieces = foreach ($name in @('requirements.txt', 'install.ps1', 'verify_install.py')) {
        if (Test-Path -LiteralPath $name) {
            $hash = [System.Security.Cryptography.SHA256]::Create()
            try { [BitConverter]::ToString($hash.ComputeHash([IO.File]::ReadAllBytes((Join-Path $PSScriptRoot $name)))) }
            finally { $hash.Dispose() }
        }
        else { 'missing' }
    }
    return ($pieces -join ':')
}

try {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw 'Git was not found. Install Git for Windows from https://git-scm.com/download/win, then reopen update.bat.'
    }
    # Do not accidentally update an enclosing repository for a ZIP installation.
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.git'))) {
        throw 'This is a ZIP installation without .git. Use git clone https://github.com/ephonek/Live-Translator.git in a NEW folder, run install.bat there, and copy your .env and config folder there. Keep this folder until the new copy works.'
    }
    $branch = (Invoke-ProjectGit symbolic-ref --quiet --short HEAD | Select-Object -First 1).ToString()
    $remote = (Invoke-ProjectGit config --get "branch.$branch.remote" | Select-Object -First 1).ToString()
    $mergeRef = (Invoke-ProjectGit config --get "branch.$branch.merge" | Select-Object -First 1).ToString()
    if (-not $remote -or $remote -eq '.' -or -not $mergeRef.StartsWith('refs/heads/')) {
        throw 'The current branch needs an upstream remote branch. Configure its upstream before updating.'
    }
    Assert-CleanCheckout
    $running = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
        $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -and
        $_.CommandLine.IndexOf($PSScriptRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        $_.CommandLine -match '(live_qwen3|subtitle_window|asr_service)\.py'
    })
    if ($running.Count) { throw 'Close this project''s translator and subtitle window before updating.' }
    Write-Host "Branch: $branch / remote: $remote"
    if ($CheckOnly) {
        Write-Host 'Local checks passed. No fetch, file update or package installation was performed.'
        exit 0
    }

    $before = (Invoke-ProjectGit rev-parse HEAD | Select-Object -First 1).ToString()
    $oldDependencies = Get-DependencyStamp
    Write-Host '[1/2] Fetching the upstream branch...'
    Invoke-ProjectGit fetch --no-tags -- $remote $mergeRef | ForEach-Object { Write-Host $_ }
    $target = (Invoke-ProjectGit rev-parse FETCH_HEAD | Select-Object -First 1).ToString()

    # A remotely tracked private path would overwrite an ignored local file.
    $incoming = @(Invoke-ProjectGit ls-tree -r --name-only $target)
    if (@($incoming | Where-Object { ($_ -match '^\.env($|\.)' -and $_ -ne '.env.example') -or
          $_ -match '^(config|models|\.venv|\.python|\.tools)/' }).Count) {
        throw 'The incoming version tracks personal data directories. Update refused to protect local files.'
    }
    Assert-CleanCheckout
    Invoke-ProjectGit merge --ff-only --no-edit --no-overwrite-ignore $target | ForEach-Object { Write-Host $_ }
    $after = (Invoke-ProjectGit rev-parse HEAD | Select-Object -First 1).ToString()
    Write-Host "Code: $($before.Substring(0, 8)) -> $($after.Substring(0, 8))"

    $marker = Join-Path $PSScriptRoot '.update-dependencies-pending'
    $needsDependencies = ($oldDependencies -ne (Get-DependencyStamp)) -or (Test-Path -LiteralPath $marker)
    if ($needsDependencies) {
        # Keep the marker on failure so rerunning update retries dependencies,
        # even though Git already points to the new commit.
        Set-Content -LiteralPath $marker -Value $after -Encoding ASCII
        if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
            throw 'Code updated, but this project has no installer-managed .venv. For a Conda/developer installation, update that environment manually and remove .update-dependencies-pending when verified; or run install.bat to create .venv for start.bat.'
        }
        Write-Host '[2/2] Updating dependencies and verifying the installation...'
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'install.ps1')
        if ($LASTEXITCODE -ne 0) { throw 'Code updated, but dependency verification failed. Fix the error above and rerun update.bat.' }
        Remove-Item -LiteralPath $marker
    }
    else { Write-Host '[2/2] Dependency files unchanged; no package reinstall needed.' }
    Write-Host 'Update completed. Your .env, settings and model cache were preserved. Start with start.bat.'
    exit 0
}
catch {
    Write-Host "`nUpdate stopped:" -ForegroundColor Yellow
    Write-Host $_.Exception.Message
    exit 1
}
