[CmdletBinding()]
param(
    [string]$Version = '',
    [string]$Python = '',
    [string]$IsccPath = '',
    [switch]$SkipTests,
    [switch]$SkipInstaller
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Program failed with exit code $LASTEXITCODE"
    }
}

function Get-Sha256 {
    param([string]$Path)
    # Use .NET directly so minimal PowerShell environments need no extra modules.
    $algorithm = [Security.Cryptography.SHA256]::Create()
    $stream = $null
    try {
        $stream = [IO.File]::OpenRead($Path)
        return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
    } finally {
        if ($stream) { $stream.Dispose() }
        $algorithm.Dispose()
    }
}

function Resolve-BuildPython {
    param([string]$Command, [string[]]$PrefixArguments = @())
    try {
        # Probe the interpreter, not just its command name. Ignore broken venvs,
        # unavailable launcher versions and incompatible Python installations.
        $probe = "import sys, struct, tkinter, venv; assert sys.version_info[:2] == (3, 12); assert struct.calcsize('P') == 8; print(sys.executable)"
        $result = & $Command @PrefixArguments -c $probe 2>$null
        if ($LASTEXITCODE -eq 0 -and $result) { return ([string]@($result)[-1]).Trim() }
    } catch { }
    return $null
}

function Find-Python {
    if ($Python) {
        $command = (Get-Command $Python -ErrorAction Stop).Source
        $resolvedPython = Resolve-BuildPython -Command $command
        if ($resolvedPython) { return $resolvedPython }
        throw "Selected Python must be 64-bit Python 3.12 with tkinter: $Python"
    }
    foreach ($relativePath in @('build\venv\Scripts\python.exe', '.venv\Scripts\python.exe', 'venv\Scripts\python.exe')) {
        $candidate = Join-Path $PSScriptRoot $relativePath
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $resolvedPython = Resolve-BuildPython -Command $candidate
            if ($resolvedPython) { return $resolvedPython }
        }
    }
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        $resolvedPython = Resolve-BuildPython -Command $launcher.Source -PrefixArguments @('-3.12')
        if ($resolvedPython) { return $resolvedPython }
    }
    foreach ($name in @('python', 'python3')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) {
            $resolvedPython = Resolve-BuildPython -Command $command.Source
            if ($resolvedPython) { return $resolvedPython }
        }
    }
    throw 'No usable build environment found. Install 64-bit Python 3.12 with tkinter, or run build.bat -Python C:\path\to\python.exe.'
}

function Find-Iscc {
    if ($IsccPath) { return (Get-Command $IsccPath -ErrorAction Stop).Source }
    $installedCompiler = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($installedCompiler) { return $installedCompiler.Source }
    foreach ($programDirectory in @(${env:ProgramFiles(x86)}, $env:ProgramFiles)) {
        if ($programDirectory) {
            $candidate = Join-Path $programDirectory 'Inno Setup 6\ISCC.exe'
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
        }
    }

    # Official Inno Setup portable mode installs build tools only inside build/.
    # SHA-256 is the digest published on the official GitHub release asset.
    $toolDirectory = Join-Path $buildDirectory 'tools\inno-6.7.1'
    $compiler = Join-Path $toolDirectory 'ISCC.exe'
    if (Test-Path -LiteralPath $compiler -PathType Leaf) { return $compiler }
    $download = Join-Path $buildDirectory 'tools\innosetup-6.7.1.exe'
    $expectedHash = '4D11E8050B6185E0D49BD9E8CC661A7A59F44959A621D31D11033124C4E8A7B0'
    New-Item -ItemType Directory -Path (Split-Path $download) -Force | Out-Null
    if (-not (Test-Path -LiteralPath $download -PathType Leaf)) {
        Write-Host 'Downloading official Inno Setup 6.7.1 compiler...'
        Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/jrsoftware/issrc/releases/download/is-6_7_1/innosetup-6.7.1.exe' -OutFile $download
    }
    if ((Get-Sha256 $download) -ne $expectedHash) {
        throw "Inno Setup checksum mismatch. Remove $download and retry."
    }
    $installerArguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-',
                            '/PORTABLE=1', '/CURRENTUSER', ('/DIR="{0}"' -f $toolDirectory))
    $process = Start-Process -FilePath $download -ArgumentList $installerArguments -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $compiler -PathType Leaf)) {
        throw "Inno Setup portable extraction failed (exit code $($process.ExitCode))."
    }
    return $compiler
}

$previousLocation = Get-Location
$previousCache = $env:PYINSTALLER_CONFIG_DIR
$previousPipCache = $env:PIP_CACHE_DIR
$previousEncoding = $env:PYTHONUTF8
try {
    if ($env:OS -ne 'Windows_NT') { throw 'Build the Windows installer on Windows.' }
    Set-Location -LiteralPath $PSScriptRoot
    if (-not $Version) { $Version = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'VERSION') -Raw).Trim() }
    $Version = $Version -replace '^v', ''
    if ($Version -notmatch '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$') {
        throw 'Version must be X.Y.Z or vX.Y.Z (for example 0.1.0).'
    }
    if (@($Version.Split('.') | Where-Object { [long]$_ -gt 65535 }).Count) {
        throw 'Each version component must be between 0 and 65535.'
    }
    $buildDirectory = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'build'))
    $distDirectory = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'dist'))
    $appDirectory = Join-Path $distDirectory 'ssh-tunnels'
    # PyInstaller may replace its output folder. Keep it strictly inside this project.
    if (-not $appDirectory.StartsWith($PSScriptRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Build output must remain inside the project directory.'
    }
    New-Item -ItemType Directory -Path $buildDirectory, $distDirectory -Force | Out-Null
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $buildDirectory 'pyinstaller-cache'
    $env:PIP_CACHE_DIR = Join-Path $buildDirectory 'pip-cache'
    $env:PYTHONUTF8 = '1'
    $basePython = Find-Python
    Write-Host "Build Python: $basePython"
    $venvPython = Join-Path $buildDirectory 'venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        Invoke-Checked $basePython @('-m', 'venv', (Join-Path $buildDirectory 'venv'))
    }
    Invoke-Checked $venvPython @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', (Join-Path $PSScriptRoot 'requirements-build.txt'))
    if (-not $SkipTests) {
        Invoke-Checked $venvPython @('-m', 'unittest', 'discover', '-s', 'tests', '-v')
    }

    $versionNumbers = ($Version.Replace('.', ',') + ',0')
    $versionFile = Join-Path $buildDirectory 'version-info.txt'
    $versionResource = @"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($versionNumbers), prodvers=($versionNumbers), mask=0x3f,
    flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'nextyale-10'),
    StringStruct('FileDescription', 'SSH Tunnels'),
    StringStruct('FileVersion', '$Version'),
    StringStruct('InternalName', 'ssh-tunnels'),
    StringStruct('OriginalFilename', 'ssh-tunnels.exe'),
    StringStruct('ProductName', 'SSH Tunnels'),
    StringStruct('ProductVersion', '$Version')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])])
"@
    [IO.File]::WriteAllText($versionFile, $versionResource, [Text.UTF8Encoding]::new($false))
    $appIcon = Join-Path $PSScriptRoot 'assets\ssh-tunnels.ico'
    Invoke-Checked $venvPython @('-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--windowed', '--log-level', 'WARN',
        '--noupx', '--name', 'ssh-tunnels', '--distpath', $distDirectory,
        '--workpath', (Join-Path $buildDirectory 'pyinstaller'), '--specpath', $buildDirectory,
        '--icon', $appIcon, '--add-data', ($appIcon + ';assets'),
        '--version-file', $versionFile, (Join-Path $PSScriptRoot 'ssh_tunnels.py'))
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'README.md') -Destination $appDirectory
    # .NET Framework's windowless helper forwards OpenSSH prompts to the GUI.
    $askpassCompiler = Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
    Invoke-Checked $askpassCompiler @('/nologo', '/target:winexe', '/optimize+',
        ('/out:{0}' -f (Join-Path $appDirectory 'ssh-askpass.exe')), (Join-Path $PSScriptRoot 'packaging\askpass.cs'))
    $pythonLicense = & $basePython -c "from pathlib import Path; import sys; print(Path(sys.base_prefix) / 'LICENSE.txt')"
    if ($LASTEXITCODE -ne 0) { throw 'Could not locate the bundled Python license.' }
    if (Test-Path -LiteralPath $pythonLicense -PathType Leaf) {
        $licenseDirectory = Join-Path $appDirectory 'licenses'
        New-Item -ItemType Directory -Path $licenseDirectory -Force | Out-Null
        Copy-Item -LiteralPath $pythonLicense -Destination (Join-Path $licenseDirectory 'Python-LICENSE.txt')
    }
    $appExecutable = Join-Path $appDirectory 'ssh-tunnels.exe'
    if (-not (Test-Path -LiteralPath $appExecutable -PathType Leaf)) { throw 'Application executable was not generated.' }
    if (-not $SkipTests) {
        Invoke-Checked $venvPython @((Join-Path $PSScriptRoot 'packaging\smoke_package.py'), $appExecutable)
    }
    Write-Host "Application: $appExecutable"
    if ($SkipInstaller) { return }

    $compiler = Find-Iscc
    Invoke-Checked $compiler @('/Q', "/DAppVersion=$Version", "/DAppSourceDir=$appDirectory", "/DInstallerOutputDir=$distDirectory",
                              (Join-Path $PSScriptRoot 'packaging\installer.iss'))
    $installer = Join-Path $distDirectory "ssh-tunnels-$Version-windows-x64-setup.exe"
    if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) { throw 'Installer was not generated.' }
    $checksum = Get-Sha256 $installer
    [IO.File]::WriteAllText("$installer.sha256", "$checksum  $([IO.Path]::GetFileName($installer))`n", [Text.Encoding]::ASCII)
    Write-Host "Installer: $installer"
    Write-Host "SHA-256:   $installer.sha256"
} catch {
    [Console]::Error.WriteLine("Build failed: {0}", $_.Exception.Message)
    exit 1
} finally {
    Set-Location -LiteralPath $previousLocation
    $env:PYINSTALLER_CONFIG_DIR = $previousCache
    $env:PIP_CACHE_DIR = $previousPipCache
    $env:PYTHONUTF8 = $previousEncoding
}
