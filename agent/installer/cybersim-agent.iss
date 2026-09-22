; CyberSim Agent installer.
;
; Build (from agent/, with dist/cybersim-agent.exe already built via
; PyInstaller -- see docs/README.md):
;     iscc installer\cybersim-agent.iss
; Output: agent/installer/output/cybersim-agent-setup.exe
;
; "Autolinks to the server": if a file named install-defaults.txt sits
; next to Setup.exe when it's run (server/app.py's /install/agent-bundle
; endpoint zips one up per download, pre-filled with the requesting
; server's own base URL, host_id, persona, and that host's freshly
; minted/reused bearer token), its four lines pre-fill the wizard fields
; below. Absent that file, the fields are just blank -- the wizard still
; works standalone (paste in a token issued some other way).
;
; Runs the agent via a Scheduled Task set to fire at user logon (not a
; SYSTEM service): puppet hosts are meant to look like a real logged-in
; user working, which is also what a real user's session actually is --
; not a background service. See docs/README.md.

#define MyAppName "CyberSim Agent"
; #ifndef, not a plain #define, so CI can pass a version derived from
; the git tag (e.g. `iscc /DMyAppVersion=1.2.3 cybersim-agent.iss`)
; without this line's own #define conflicting with it -- see
; .github/workflows/release.yml. Local manual builds keep this default.
#ifndef MyAppVersion
  #define MyAppVersion "0.2.0"
#endif
#define MyAppExeName "cybersim-agent.exe"

[Setup]
AppId={{666DFDB4-4C6B-4853-9A53-A175AE693F90}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=CyberSim
; Admin-elevated install: creating a "run at logon" scheduled task
; (below) needs elevated rights to register on standard Windows policy
; configurations (confirmed by hand while building this -- a per-user,
; non-admin install hit "Access is denied" specifically on the onlogon
; trigger, even though other trigger types worked fine unprivileged).
; Matches the existing Ansible provisioning's own elevated pattern
; (become: true / WinRM admin) for puppet host setup anyway.
DefaultDirName={autopf}\cybersim-agent
DefaultGroupName=CyberSim Agent
DisableProgramGroupPage=yes
OutputDir=output
OutputBaseFilename=cybersim-agent-setup
Compression=lzma
SolidCompression=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible

[Files]
Source: "..\dist\cybersim-agent.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Uninstall CyberSim Agent"; Filename: "{uninstallexe}"

[Code]
var
  ConnectionPage: TInputQueryWizardPage;

function DefaultsFilePath(): String;
begin
  Result := ExtractFilePath(ExpandConstant('{srcexe}')) + 'install-defaults.txt';
end;

function GetDefault(Index: Integer; Fallback: String): String;
var
  Lines: TArrayOfString;
begin
  Result := Fallback;
  if FileExists(DefaultsFilePath()) then
    if LoadStringsFromFile(DefaultsFilePath(), Lines) then
      if GetArrayLength(Lines) > Index then
        if Trim(Lines[Index]) <> '' then
          Result := Trim(Lines[Index]);
end;

procedure InitializeWizard;
begin
  ConnectionPage := CreateInputQueryPage(wpSelectDir,
    'Orchestrator Connection', 'Where is the CyberSim server, and who is this host?',
    'These get written to config.yaml. You can edit that file by hand later if anything needs to change.');
  ConnectionPage.Add('Server URL:', False);
  ConnectionPage.Add('Host ID (must match a scenario''s "hosts" list on the server):', False);
  ConnectionPage.Add('Persona:', False);
  ConnectionPage.Add('Agent Token (from the install bundle, or issued separately by the server):', True);

  { install-defaults.txt line order: server_url, host_id, persona, token --
    see server/app.py's bundle-generation endpoint. The token field is
    masked (Password:=True above) since it's a credential -- the server
    only ever ships it embedded in this sidecar file, never shown back
    to a human, so masking it here is just defense in depth against
    shoulder-surfing during an interactive install. }
  ConnectionPage.Values[0] := GetDefault(0, 'http://SERVER-ADDRESS:8000');
  ConnectionPage.Values[1] := GetDefault(1, GetEnv('COMPUTERNAME'));
  ConnectionPage.Values[2] := GetDefault(2, 'default');
  ConnectionPage.Values[3] := GetDefault(3, '');
end;

function YamlEscape(const S: String): String;
begin
  { Escapes for embedding in a double-quoted YAML scalar. Defense in
    depth: server/app.py already restricts host_id/persona to a safe
    charset before they ever reach install-defaults.txt, but this file
    could in principle be hand-edited or the wizard filled in by hand,
    so this doesn't assume that validation already happened. Order
    matters -- backslashes first, or a quote's own escaping backslash
    would itself get re-escaped. }
  Result := S;
  StringChangeEx(Result, '\', '\\', True);
  StringChangeEx(Result, '"', '\"', True);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ConfigLines: TArrayOfString;
begin
  if CurStep = ssPostInstall then
  begin
    SetArrayLength(ConfigLines, 6);
    ConfigLines[0] := 'server_url: "' + YamlEscape(ConnectionPage.Values[0]) + '"';
    ConfigLines[1] := 'host_id: "' + YamlEscape(ConnectionPage.Values[1]) + '"';
    ConfigLines[2] := 'os: "windows"';
    ConfigLines[3] := 'persona: "' + YamlEscape(ConnectionPage.Values[2]) + '"';
    ConfigLines[4] := 'poll_interval_seconds: 10';
    ConfigLines[5] := 'token: "' + YamlEscape(ConnectionPage.Values[3]) + '"';
    SaveStringsToFile(ExpandConstant('{app}\config.yaml'), ConfigLines, False);
  end;
end;

[Run]
; No --config argument needed: agent.py resolves config.yaml next to its
; own executable by default when frozen (sys.frozen) -- see agent.py's
; _default_config_path(). That also sidesteps a real nested-quoting bug
; found while testing this: schtasks' /tr value needs its own embedded
; escaped quotes when it carries extra arguments after the path, and
; getting that right through Inno's *own* "" escaping on top was
; error-prone (silently produced a malformed /tr value -- exit code 1,
; task never created). No extra arguments, no nesting, no problem.
Filename: "schtasks.exe"; Parameters: "/create /tn ""cybersim-agent"" /tr ""{app}\{#MyAppExeName}"" /sc onlogon /rl highest /f"; Flags: runhidden; StatusMsg: "Registering startup task..."
Filename: "{app}\{#MyAppExeName}"; Description: "Launch CyberSim Agent now"; Flags: nowait postinstall skipifsilent unchecked

[UninstallRun]
Filename: "schtasks.exe"; Parameters: "/delete /tn ""cybersim-agent"" /f"; Flags: runhidden; RunOnceId: "RemoveSchTask"
