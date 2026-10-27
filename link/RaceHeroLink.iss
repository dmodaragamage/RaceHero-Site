; RaceHero Link — Inno Setup script.
; Build the exe first (build-exe.bat), then run build-installer.bat.

[Setup]
AppName=RaceHero Link
AppVersion=2.7.3
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

[InstallDelete]
; Script-era installs added a startup .bat that launches
; racehero_link.py with pythonw. Next to the [Icons] startup
; shortcut below, that means TWO copies racing at every boot.
; The .bat's copy is pre-funnel and pre-single-instance — the
; exact zombie that can hold UDP 20781 and eat the phone's
; beacons while the real Link heartbeats happily. Remove it.
Type: files; Name: "{userstartup}\RaceHero Link.bat"

[Tasks]
Name: "startup"; Description: "Start automatically with Windows"; \
    GroupDescription: "Startup:"; Flags: checkedonce

[Icons]
Name: "{autoprograms}\RaceHero Link"; Filename: "{app}\RaceHeroLink.exe"
Name: "{autostartup}\RaceHero Link"; Filename: "{app}\RaceHeroLink.exe"; Tasks: startup

[Run]
Filename: "{app}\RaceHeroLink.exe"; \
    Description: "Launch RaceHero Link now"; Flags: nowait postinstall

[Code]
function KillRunningLinks(): Boolean;
var
  ResultCode: Integer;
begin
  { Kill every running copy BEFORE any file is copied. An
    over-the-top install that leaves the old tray copy alive
    hands it first claim on UDP 20781 for the rest of the
    session: the new version's single-instance guard then keeps
    the NEW code from ever starting, or — where the port ends up
    shared — the old copy silently eats the phone's discovery
    beacons (the v2.7.1 field failure). The founder never has to
    kill anything by hand again. }
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM RaceHeroLink.exe',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  { Legacy script-era copies: a pythonw running racehero_link.py
    (the old START_HERE / startup-.bat flow). Matched by command
    line, so unrelated Python processes are untouched. }
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
       '-NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance ' +
       'Win32_Process | Where-Object { $_.CommandLine -like ' +
       '''*racehero_link.py*'' } | ForEach-Object { Stop-Process -Id ' +
       '$_.ProcessId -Force -ErrorAction SilentlyContinue }"',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(800);
  Result := True;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  KillRunningLinks();
  Result := '';
end;

procedure RunFirewallScript(const ExePath: String;
  const AddRules: Boolean);
var
  ResultCode: Integer;
  BatPath: String;
  Lines: String;
begin
  { ONE elevated run for all rules (a single UAC prompt). The
    phone's discovery beacon is the ONLY inbound flow Link has,
    and until now it depended on the founder having answered a
    Windows Firewall prompt correctly at some point in the past.
    Rules are scoped to the Link program + the two UDP ports.
    profile=any is deliberate: home networks are routinely
    classified "Public" by Windows, and a Private-only rule
    silently does nothing there — an invisible no-op of exactly
    the kind this release exists to eliminate. Failures are
    non-fatal: beacon + presence discovery self-heals around a
    missing rule wherever the network allows it at all. }
  BatPath := ExpandConstant('{tmp}\racehero_fw.bat');
  Lines := '@echo off' + #13#10;
  Lines := Lines + 'netsh advfirewall firewall delete rule ' +
    'name="RaceHero Link Discovery (UDP 20781)"' + #13#10;
  Lines := Lines + 'netsh advfirewall firewall delete rule ' +
    'name="RaceHero Link Inputs (UDP 20782)"' + #13#10;
  if AddRules then
  begin
    Lines := Lines + 'netsh advfirewall firewall add rule ' +
      'name="RaceHero Link Discovery (UDP 20781)" dir=in ' +
      'action=allow protocol=UDP localport=20781 program="%~1" ' +
      'profile=any' + #13#10;
    Lines := Lines + 'netsh advfirewall firewall add rule ' +
      'name="RaceHero Link Inputs (UDP 20782)" dir=in ' +
      'action=allow protocol=UDP localport=20782 program="%~1" ' +
      'profile=any' + #13#10;
  end;
  if SaveStringToFile(BatPath, Lines, False) then
    ShellExec('runas', BatPath, '"' + ExePath + '"', '',
              SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    RunFirewallScript(ExpandConstant('{app}\RaceHeroLink.exe'), True);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RunFirewallScript(ExpandConstant('{app}\RaceHeroLink.exe'), False);
end;
