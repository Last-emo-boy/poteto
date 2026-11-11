[CmdletBinding()]
param(
    [string]$InstallDir = $env:POTETO_INSTALL_DIR,
    [switch]$NoPath
)

function Install-Poteto {
    param([string]$Directory, [switch]$SkipPath)
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'
    $PSNativeCommandUseErrorActionPreference = $false
    if ($PSVersionTable.PSVersion.Major -lt 7) { throw 'Run this installer in PowerShell 7 or newer (pwsh).' }
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw 'Use install.sh on macOS or Linux. This installer is for Windows.'
    }
    $base = '@POTETO_BASE_URL@'
    $version = '@POTETO_VERSION@'
    if (-not $Directory) {
        $Directory = Join-Path $env:LOCALAPPDATA 'Programs\Poteto\bin'
    }
    $Directory = [IO.Path]::GetFullPath($Directory)
    $python = $null
    $pythonArgs = @()
    $pythonCommand = $null
    foreach ($candidate in @('py', 'python', 'python3')) {
        $command = Get-Command $candidate -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $command) { continue }
        $candidateArgs = @()
        if ($candidate -eq 'py') { $candidateArgs = @('-3') }
        & $command.Source @candidateArgs -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>$null
        if ($LASTEXITCODE -eq 0) {
            $python = $command.Source
            $pythonCommand = $candidate
            $pythonArgs = $candidateArgs
            break
        }
    }
    if (-not $python) { throw 'Install Python 3.10 or newer from python.org, enable its PATH option, and try again.' }
    New-Item -ItemType Directory -Path $Directory -Force | Out-Null
    $archive = Join-Path $Directory 'poteto.pyz'
    $launcher = Join-Path $Directory 'poteto.cmd'
    foreach ($path in @($archive, $launcher)) {
        if (Test-Path -LiteralPath $path -PathType Container) { throw "$path is a directory. Choose another InstallDir." }
    }
    $temporary = Join-Path $Directory ('.poteto-' + [guid]::NewGuid().ToString('N') + '.pyz')
    $nextLauncher = $temporary + '.cmd'
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri "$base/poteto-$version.pyz" -OutFile $temporary -UseBasicParsing -MaximumRedirection 0
        $reported = & $python @pythonArgs $temporary --version
        if ($LASTEXITCODE -ne 0 -or "$reported".Trim() -ne "poteto $version") {
            throw 'Downloaded Poteto did not report the expected version. The existing install has not been replaced.'
        }
        $invocation = $pythonCommand
        if ($pythonArgs.Count) { $invocation += ' ' + ($pythonArgs -join ' ') }
        $lines = @('@echo off', ($invocation + ' -X utf8 "%~dp0poteto.pyz" %*'), 'exit /b %errorlevel%')
        [IO.File]::WriteAllText($nextLauncher, ($lines -join [Environment]::NewLine) + [Environment]::NewLine, [Text.Encoding]::ASCII)
        Move-Item -LiteralPath $temporary -Destination $archive -Force
        Move-Item -LiteralPath $nextLauncher -Destination $launcher -Force
    }
    finally {
        foreach ($path in @($temporary, $nextLauncher)) {
            if (Test-Path -LiteralPath $path -PathType Leaf) { Remove-Item -LiteralPath $path -Force }
        }
    }
    if (-not $SkipPath) {
        $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
        $entries = @($userPath -split ';' | Where-Object { $_ })
        if ($entries -notcontains $Directory) {
            [Environment]::SetEnvironmentVariable('Path', (($entries + $Directory) -join ';'), 'User')
        }
        if (($env:Path -split ';') -notcontains $Directory) { $env:Path = $Directory + ';' + $env:Path }
    }
    Write-Output "Installed Poteto $version at $launcher"
    Write-Output 'Run: poteto login'
}
Install-Poteto -Directory $InstallDir -SkipPath:$NoPath
