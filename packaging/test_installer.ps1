[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$InstallerPath,
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$Python = 'python'
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$projectDirectory = Split-Path $PSScriptRoot -Parent
$smokeDirectory = Join-Path $projectDirectory ('build\installer-smoke\' + [Guid]::NewGuid().ToString('N'))
$installDirectory = Join-Path $smokeDirectory 'installed app'
New-Item -ItemType Directory -Path $smokeDirectory -Force | Out-Null
$installer = (Resolve-Path -LiteralPath $InstallerPath).Path
$uninstaller = Join-Path $installDirectory 'unins000.exe'
try {
    $installArguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', '/NOICONS',
                          ('/DIR="{0}"' -f $installDirectory), ('/LOG="{0}"' -f (Join-Path $smokeDirectory 'install.log')))
    $process = Start-Process -FilePath $installer -ArgumentList $installArguments -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -ne 0) { throw "Installer failed: $($process.ExitCode)" }
    $executable = Join-Path $installDirectory 'ssh-tunnels.exe'
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) { throw 'Installed application is missing.' }
    if ((Get-Item -LiteralPath $executable).VersionInfo.ProductVersion -ne $Version) {
        throw 'Installed executable version does not match the release.'
    }
    & $Python (Join-Path $PSScriptRoot 'smoke_package.py') $executable
    if ($LASTEXITCODE -ne 0) { throw 'Installed application smoke test failed.' }
} finally {
    if (Test-Path -LiteralPath $uninstaller -PathType Leaf) {
        $uninstallProcess = Start-Process -FilePath $uninstaller -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') -Wait -PassThru -WindowStyle Hidden
        if ($uninstallProcess.ExitCode -ne 0) { throw "Uninstaller failed: $($uninstallProcess.ExitCode)" }
    }
}
if (Test-Path -LiteralPath (Join-Path $installDirectory 'ssh-tunnels.exe')) { throw 'Uninstall did not remove the executable.' }
Write-Host 'INSTALLER PASS: install, version, frozen GUI, uninstall'
