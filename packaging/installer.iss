; Build via build.ps1. AppId stays fixed across versions for in-place upgrades.
#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef AppSourceDir
  #error AppSourceDir is required
#endif
#ifndef InstallerOutputDir
  #error InstallerOutputDir is required
#endif

[Setup]
AppId={{7FBFE95E-59B1-4A96-9F33-FF871E5F9C72}
AppName=SSH Tunnels
AppVersion={#AppVersion}
AppVerName=SSH Tunnels {#AppVersion}
AppPublisher=nextyale-10
AppPublisherURL=https://github.com/nextyale-10/tunnel-management
AppSupportURL=https://github.com/nextyale-10/tunnel-management/issues
AppUpdatesURL=https://github.com/nextyale-10/tunnel-management/releases
DefaultDirName={localappdata}\Programs\SSH Tunnels
DefaultGroupName=SSH Tunnels
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#InstallerOutputDir}
OutputBaseFilename=ssh-tunnels-{#AppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile={#AppSourceDir}\_internal\assets\ssh-tunnels.ico
UninstallDisplayIcon={app}\ssh-tunnels.exe
CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#AppSourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\SSH Tunnels"; Filename: "{app}\ssh-tunnels.exe"; WorkingDir: "{app}"; IconFilename: "{app}\_internal\assets\ssh-tunnels.ico"; IconIndex: 0; AppUserModelID: "nextyale10.SSHTunnels"
Name: "{autodesktop}\SSH Tunnels"; Filename: "{app}\ssh-tunnels.exe"; WorkingDir: "{app}"; IconFilename: "{app}\_internal\assets\ssh-tunnels.ico"; IconIndex: 0; AppUserModelID: "nextyale10.SSHTunnels"; Tasks: desktopicon

[Run]
Filename: "{app}\ssh-tunnels.exe"; Description: "{cm:LaunchProgram,SSH Tunnels}"; Flags: nowait postinstall skipifsilent

; User SSH config, keys and %LOCALAPPDATA%\ssh-tunnels are never part of the payload
; or uninstall deletion rules.
