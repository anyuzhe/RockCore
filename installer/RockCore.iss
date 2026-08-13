; Inno Setup installer for RockCore.
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

#define AppName "RockCore"
#define AppPublisher "浙江岩创科技有限公司"
#define AppExeName "RockCore.exe"

[Setup]
AppId={{B25B2C94-8D5B-4C16-92C6-202608070001}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL=https://www.rockinnov.com/
DefaultDirName={autopf}\Rock Innovation\RockCore
DefaultGroupName={#AppName}
OutputDir=..\release
OutputBaseFilename=RockCore-Setup-{#AppVersion}-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64
UninstallDisplayIcon={app}\{#AppExeName}
SetupIconFile=..\assets\branding\rockcore.ico
PrivilegesRequired=admin
CloseApplications=yes
RestartApplications=yes

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："; Flags: unchecked

[Files]
Source: "..\dist\RockCore\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\RockCore"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\RockCore"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "启动 RockCore"; Flags: nowait postinstall skipifsilent
