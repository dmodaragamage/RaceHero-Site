; RaceHero Link — Inno Setup script.
; Build the exe first (build-exe.bat), then run build-installer.bat.

[Setup]
AppName=RaceHero Link
AppVersion=2.5
AppPublisher=RaceHero
DefaultDirName={autopf}\RaceHero Link
OutputDir=installer
OutputBaseFilename=RaceHeroLinkSetup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
WizardStyle=modern
SetupIconFile=racehero.ico
UninstallDisplayIcon={app}\RaceHeroLink.exe

[Files]
Source: "dist\RaceHeroLink\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Tasks]
Name: "startup"; Description: "Start automatically with Windows"; \
    GroupDescription: "Startup:"; Flags: checkedonce

[Icons]
Name: "{autoprograms}\RaceHero Link"; Filename: "{app}\RaceHeroLink.exe"
Name: "{autostartup}\RaceHero Link"; Filename: "{app}\RaceHeroLink.exe"; Tasks: startup

[Run]
Filename: "{app}\RaceHeroLink.exe"; \
    Description: "Launch RaceHero Link now"; Flags: nowait postinstall
