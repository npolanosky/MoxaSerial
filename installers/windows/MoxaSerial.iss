; MoxaSerial -- Inno Setup 6 script
;
; Builds dist\MoxaSerial-<version>-setup.exe, a per-user installer that drops
; the add-in into Fusion's per-user AddIns folder:
;
;     %APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\MoxaSerial
;
; Nothing is written to Program Files, HKLM or anywhere else needing admin
; rights: PrivilegesRequired=lowest means the installer never elevates, and the
; uninstall entry is registered under HKCU.
;
; Build (Inno Setup 6, ISCC.exe on PATH), from the repository root:
;
;     iscc /DMyAppVersion=0.1.0 installers\windows\MoxaSerial.iss
;
; MyAppVersion is required; keep it in step with MoxaSerial.manifest, e.g.
;
;     for /f %v in ('python tools\make_release_zip.py --print-version') do ^
;         iscc /DMyAppVersion=%v installers\windows\MoxaSerial.iss
;
; The file list below must match installers\payload.py -- that module is the
; single source of truth for what ships. If you add a runtime file, add it in
; both places (and in tools\public_manifest.txt).
;
; Signing (unsigned is fine for internal use; SmartScreen will warn):
;     iscc /DMyAppVersion=0.1.0 ^
;          "/Sbyparam=signtool.exe sign /f cert.pfx /p $p /tr http://timestamp.digicert.com /td sha256 /fd sha256 $f" ^
;          /DSignTool=byparam installers\windows\MoxaSerial.iss
; and add  SignTool=byparam  to [Setup] (see the commented line below).

#define MyAppName "MoxaSerial"
#define MyAppPublisher "P3D"
#define MyAppURL "https://github.com/p3d/MoxaSerial"

#ifndef MyAppVersion
  #error MyAppVersion is not defined. Build with:  iscc /DMyAppVersion=x.y.z installers\windows\MoxaSerial.iss
#endif

[Setup]
; Never change AppId -- it is what ties an upgrade to the previous install.
AppId={{08FEAE53-7193-5C76-B2DA-68408EEF5479}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}/releases
VersionInfoVersion={#MyAppVersion}
VersionInfoProductName={#MyAppName}
VersionInfoDescription={#MyAppName} Fusion add-in installer

; Per-user install. No elevation, ever. PrivilegesRequiredOverridesAllowed is
; left at its default (empty), so neither the command line nor a dialog can
; talk Setup into elevating.
PrivilegesRequired=lowest

; Fusion finds add-ins by folder name, so the folder must be called MoxaSerial
; and must sit in the per-user AddIns folder. There is nothing useful for the
; user to choose, so the directory page is hidden.
DefaultDirName={userappdata}\Autodesk\Autodesk Fusion 360\API\AddIns\{#MyAppName}
DisableDirPage=yes
DisableProgramGroupPage=yes
UsePreviousAppDir=yes
CreateAppDir=yes
Uninstallable=yes
UninstallDisplayName={#MyAppName} {#MyAppVersion}

; Paths below are relative to SourceDir, which is relative to this .iss file.
SourceDir=..\..
OutputDir=dist
OutputBaseFilename={#MyAppName}-{#MyAppVersion}-setup
LicenseFile=LICENSE

Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; No compiled binaries in the payload, so a single 32-bit-compatible setup
; runs everywhere; do not force 64-bit mode.

; Uncomment together with a /S<name>=... signtool definition to sign:
;SignTool=byparam
;SignedUninstaller=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
WelcomeLabel2=This will install [name/ver] into your personal Autodesk Fusion add-ins folder:%n%n%n      %%APPDATA%%\Autodesk\Autodesk Fusion 360\API\AddIns\MoxaSerial%n%n%nNo administrator rights are needed and nothing is installed system-wide.%n%nPlease close Autodesk Fusion before continuing. An add-in already in that folder is renamed to MoxaSerial.bak-<date> before the new one is written.
FinishedLabel=Setup has installed [name] on your computer.%n%nIn Fusion: Utilities tab > ADD-INS > Scripts and Add-Ins (Shift+S), then the Add-Ins tab > MoxaSerial > Run. Tick "Run on Startup" so it loads with Fusion.%n%nThe buttons appear in the Manufacture workspace: a "Moxa DNC" panel on the P3DTools tab, plus "Send Last Program" beside Post Process.

[Files]
; --- top level -----------------------------------------------------------
Source: "MoxaSerial.py";        DestDir: "{app}"; Flags: ignoreversion
Source: "MoxaSerial.manifest";  DestDir: "{app}"; Flags: ignoreversion
Source: "moxaserial_loader.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "LICENSE";              DestDir: "{app}"; Flags: ignoreversion
Source: "README.md";            DestDir: "{app}"; Flags: ignoreversion
; --- package and resources ----------------------------------------------
; Kept on single lines on purpose: backslash line-spanning is an ISPP feature
; that is only documented for preprocessor directives, not for ordinary
; sections, so it is not relied on here.
Source: "moxaserial\*"; DestDir: "{app}\moxaserial"; Excludes: "__pycache__,*.pyc,*.pyo,.DS_Store"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "resources\*"; DestDir: "{app}\resources"; Excludes: "__pycache__,*.pyc,*.pyo,.DS_Store"; Flags: ignoreversion recursesubdirs createallsubdirs

[UninstallDelete]
; Fusion writes __pycache__ folders inside the add-in directory at runtime;
; they were not installed by Setup, so remove the whole folder on uninstall.
Type: filesandordirs; Name: "{app}"

[Code]

{ Move any existing install aside before the new files are laid down.

  Setup overlays files rather than replacing the folder, so without this an
  upgrade would leave behind modules that the new version deleted -- and the
  stale .py would still be importable. Renaming also rescues the "developer
  symlink" case, where the AddIns entry is a junction/symlink pointing at a
  source checkout: renaming the link keeps Setup from writing through it into
  the developer's working tree. }

function BackupName(const Target: String): String;
var
  Stamp, Candidate: String;
  I: Integer;
begin
  Stamp := GetDateTimeString('yyyymmdd', #0, #0) + '-' + GetDateTimeString('hhnnss', #0, #0);
  Candidate := Target + '.bak-' + Stamp;
  I := 1;
  while DirExists(Candidate) or FileExists(Candidate) do
  begin
    I := I + 1;
    Candidate := Target + '.bak-' + Stamp + '-' + IntToStr(I);
  end;
  Result := Candidate;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Target, Backup: String;
begin
  if CurStep = ssInstall then
  begin
    Target := ExpandConstant('{app}');
    if DirExists(Target) then
    begin
      Backup := BackupName(Target);
      if RenameFile(Target, Backup) then
        Log('MoxaSerial: moved existing install to ' + Backup)
      else
        Log('MoxaSerial: could not move ' + Target + ' aside; installing over it');
    end;
  end;
end;

{ On uninstall, offer to clear out the MoxaSerial.bak-* folders too, so the
  AddIns folder does not slowly fill up with old versions. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Parent, Pattern: String;
  FindRec: TFindRec;
  Leftovers: TStringList;
  I: Integer;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;

  Parent := ExpandConstant('{app}\..');
  Pattern := AddBackslash(Parent) + 'MoxaSerial.bak-*';

  Leftovers := TStringList.Create;
  try
    if FindFirst(Pattern, FindRec) then
    begin
      try
        repeat
          if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
            Leftovers.Add(AddBackslash(Parent) + FindRec.Name);
        until not FindNext(FindRec);
      finally
        FindClose(FindRec);
      end;
    end;

    if Leftovers.Count > 0 then
    begin
      if SuppressibleMsgBox(
           'Also delete ' + IntToStr(Leftovers.Count) +
           ' backed-up MoxaSerial folder(s) left by earlier upgrades?',
           mbConfirmation, MB_YESNO, IDNO) = IDYES then
      begin
        for I := 0 to Leftovers.Count - 1 do
          DelTree(Leftovers[I], True, True, True);
      end;
    end;
  finally
    Leftovers.Free;
  end;
end;
