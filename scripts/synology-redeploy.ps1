#Requires -Version 5.1
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [Alias("Host")]
    [string]$NasHost,

    [string]$NasUser = "",

    [int]$Port = 22,

    [string]$RemotePath = "/volume1/docker/Keys",

    [string]$ProjectName = "keys",

    [switch]$UseSudo,

    [switch]$UseBuildCache,

    [switch]$SkipImagePrune,

    [switch]$PruneAllUnusedImages,

    [string]$PasswordFile = "$env:USERPROFILE\.ssh\keys-nas.password",

    [string]$PythonPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function ConvertTo-ShSingleQuoted {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    if ($Value.Contains("'")) {
        throw "Single quotes are not supported in shell values for this helper: $Value"
    }

    return "'$Value'"
}

if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    throw "OpenSSH client 'ssh' was not found. Install Windows OpenSSH Client or run this script from a shell that has ssh."
}

function Resolve-PythonPath {
    if ($PythonPath) {
        if (-not (Test-Path -LiteralPath $PythonPath)) {
            throw "PythonPath does not exist: $PythonPath"
        }
        return $PythonPath
    }

    $candidates = @(
        (Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"),
        "python",
        "py"
    )

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($command) {
            return $command.Source
        }
    }

    throw "Python was not found. Install Python or pass -PythonPath."
}

$remotePathLiteral = ConvertTo-ShSingleQuoted $RemotePath
$projectNameLiteral = ConvertTo-ShSingleQuoted $ProjectName
$useSudoFlag = if ($UseSudo) { "1" } else { "0" }
$pruneImagesFlag = if ($SkipImagePrune) { "0" } else { "1" }
$pruneAllUnusedImagesFlag = if ($PruneAllUnusedImages) { "1" } else { "0" }
$buildFlags = "--pull"
if (-not $UseBuildCache) {
    $buildFlags = "$buildFlags --no-cache"
}

$remoteScript = @"
set -eu

REMOTE_PATH=$remotePathLiteral
PROJECT_NAME=$projectNameLiteral
USE_SUDO='$useSudoFlag'
PRUNE_IMAGES='$pruneImagesFlag'
PRUNE_ALL_UNUSED_IMAGES='$pruneAllUnusedImagesFlag'
BUILD_FLAGS='$buildFlags'

PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:`$PATH"
export PATH

cd "`$REMOTE_PATH"

run_sudo() {
  if [ -n "`${SUDO_PASSWORD:-}" ]; then
    printf '%s\n' "`$SUDO_PASSWORD" | sudo -S -p '' "`$@"
  else
    sudo "`$@"
  fi
}

run_docker() {
  if [ "`$USE_SUDO" = "1" ]; then
    run_sudo docker "`$@"
  else
    docker "`$@"
  fi
}

run_legacy_compose() {
  if [ "`$USE_SUDO" = "1" ]; then
    run_sudo docker-compose "`$@"
  else
    docker-compose "`$@"
  fi
}

if run_docker compose version >/dev/null 2>&1; then
  COMPOSE_KIND="docker"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE_KIND="legacy"
else
  echo "ERROR: docker compose or docker-compose was not found on the NAS." >&2
  exit 1
fi

run_compose() {
  if [ "`$COMPOSE_KIND" = "docker" ]; then
    run_docker compose "`$@"
  else
    run_legacy_compose "`$@"
  fi
}

echo "[1/5] Stopping and removing compose containers..."
run_compose --project-name "`$PROJECT_NAME" down --remove-orphans

echo "[2/5] Building image..."
run_compose --project-name "`$PROJECT_NAME" build `$BUILD_FLAGS

echo "[3/5] Starting service..."
run_compose --project-name "`$PROJECT_NAME" up -d --force-recreate --remove-orphans

if [ "`$PRUNE_IMAGES" = "1" ]; then
  if [ "`$PRUNE_ALL_UNUSED_IMAGES" = "1" ]; then
    echo "[4/5] Removing all unused images..."
    run_docker image prune -af
  else
    echo "[4/5] Removing dangling images..."
    run_docker image prune -f
  fi
else
  echo "[4/5] Skipping image prune."
fi

echo "[5/5] Compose status..."
run_compose --project-name "`$PROJECT_NAME" ps
"@

$sshTarget = if ($NasUser) { "$NasUser@$NasHost" } else { $NasHost }
$sshArgs = @()
if ($Port -ne 22) {
    $sshArgs += @("-p", [string]$Port)
}
$sshArgs += $sshTarget
$sshArgs += "sh -s"

if (-not $PSCmdlet.ShouldProcess("${sshTarget}:${RemotePath}", "redeploy Docker Compose project '$ProjectName'")) {
    return
}

if ($PasswordFile -and (Test-Path -LiteralPath $PasswordFile)) {
    $helperPath = Join-Path $PSScriptRoot "ssh_exec_password.py"
    if (-not (Test-Path -LiteralPath $helperPath)) {
        throw "Password SSH helper was not found: $helperPath"
    }

    $resolvedPython = Resolve-PythonPath
    $helperArgs = @(
        $helperPath,
        "--host", $NasHost,
        "--password-file", $PasswordFile
    )
    if ($NasUser) {
        $helperArgs += @("--user", $NasUser)
    }
    if ($Port -ne 22) {
        $helperArgs += @("--port", [string]$Port)
    }
    if ($UseSudo) {
        $helperArgs += "--provide-sudo-password"
    }

    $remoteScript | & $resolvedPython @helperArgs
    $sshExitCode = $LASTEXITCODE
} else {
    $remoteScript | & ssh @sshArgs
    $sshExitCode = $LASTEXITCODE
}

if ($sshExitCode -ne 0) {
    exit $sshExitCode
}
