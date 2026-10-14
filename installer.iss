; Pulse Connect installer (per-user, no admin rights needed)
#define AppVer GetEnv("PULSE_VERSION")

[Setup]
AppId={{8E7C4A51-3B1D-4C8B-9A4E-5B2E7C1F0A11}
AppName=Pulse Connect
AppVersion={#AppVer}
AppVerName=Pulse Connect {#AppVer}
AppPublisher=Maple Peak
AppPublisherURL=https://tms-pulse-blush.vercel.app/start
VersionInfoVersion={#AppVer}
VersionInfoCompany=Maple Peak
VersionInfoDescription=Pulse Connect setup - links Tally to Business Pulse
DefaultDirName={localappdata}\Programs\PulseConnect
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
DisableWelcomePage=no
PrivilegesRequired=lowest
OutputDir=out
OutputBaseFilename=PulseConnectSetup
SetupIconFile=pulse.ico
UninstallDisplayIcon={app}\PulseConnect.exe
UninstallDisplayName=Pulse Connect
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
CloseApplications=force
RestartApplications=no

[Messages]
WelcomeLabel2=This installs Pulse Connect, which reads your Tally books (read-only) and keeps your Business Pulse dashboard up to date.%n%nIt takes a few seconds.

[Files]
Source: "dist\pulse_connect.dist\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; the older single-file version of Pulse Connect
Type: files; Name: "{localappdata}\PulseConnect\PulseConnect.exe"

[Icons]
Name: "{userprograms}\Pulse Connect"; Filename: "{app}\PulseConnect.exe"

[Run]
Filename: "{app}\PulseConnect.exe"; Parameters: "{code:CodeParam}"; Flags: nowait postinstall; Description: "Open Pulse Connect"; Check: not WizardSilent
Filename: "{app}\PulseConnect.exe"; Parameters: "--background"; Flags: nowait; Check: WizardSilent

[UninstallRun]
Filename: "{app}\PulseConnect.exe"; Parameters: "--uninstall"; Flags: waituntilterminated; RunOnceId: "PulseUninstall"

[Code]
function CodeParam(Param: String): String;
var N: String; P: Integer;
begin
  { the setup code travels in the download's file name: PulseConnectSetup-K7M2QX9P.exe }
  Result := '';
  N := Uppercase(ExtractFileName(ExpandConstant('{srcexe}')));
  P := Pos('SETUP-', N);
  if (P > 0) and (Length(N) >= P + 13) then
    Result := '--code=' + Copy(N, P + 6, 8);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var RC: Integer;
begin
  { stop a running copy (old single-file version included) so files can be replaced }
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/f /im PulseConnect.exe', '', SW_HIDE, ewWaitUntilTerminated, RC);
  Result := '';
end;
