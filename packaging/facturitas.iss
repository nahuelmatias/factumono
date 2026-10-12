; Instalador de Facturitas para Windows (Inno Setup 6).
;   iscc /DAppVersion=0.1.0 packaging\facturitas.iss
; Instala por usuario (sin admin) en %LOCALAPPDATA%\Programs\Facturitas, agrega la carpeta al PATH
; del usuario y NO toca los datos (historial, config, certificados) al desinstalar.

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{6F1B7C52-3D0A-4E58-9A6B-FAC7021A0001}
AppName=Facturitas
AppVersion={#AppVersion}
AppPublisher=Facturitas (código abierto, MIT)
AppPublisherURL=https://github.com/nahuelmatias/facturitas
DefaultDirName={autopf}\Facturitas
DefaultGroupName=Facturitas
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\build\installer
OutputBaseFilename=Facturitas-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
ChangesEnvironment=yes
UninstallDisplayIcon={app}\facturitas.exe
LicenseFile=..\LICENSE

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Files]
Source: "..\build\dist\facturitas\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
; Abre una consola que se queda abierta (/k) con el asistente de configuración.
Name: "{autoprograms}\Facturitas – Configurar"; Filename: "{cmd}"; Parameters: "/k """"{app}\facturitas.exe"" init"""; WorkingDir: "{userdocs}"; Comment: "Asistente de configuración de Facturitas"

[Registry]
; Agrega la carpeta de instalación al PATH del usuario (si no estaba).
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Check: NeedsAddPath(ExpandConstant('{app}'))

[Run]
; Casillas finales. «Conectar con Claude Desktop» usa el .exe instalado (no uv).
Filename: "{cmd}"; Parameters: "/c """"{app}\facturitas.exe"" mcp instalar & pause"""; Description: "Conectar con Claude Desktop"; Flags: postinstall skipifsilent
Filename: "{cmd}"; Parameters: "/k """"{app}\facturitas.exe"" init"""; Description: "Abrir el asistente de configuración"; Flags: postinstall skipifsilent

[Code]
function NeedsAddPath(Param: string): Boolean;
var
  Actual: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Actual) then
  begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + Uppercase(Param) + ';', ';' + Uppercase(Actual) + ';') = 0;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Actual, Carpeta: string;
  P: Integer;
begin
  if CurUninstallStep <> usPostUninstall then exit;
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Actual) then exit;
  Carpeta := ExpandConstant('{app}');
  P := Pos(';' + Uppercase(Carpeta), ';' + Uppercase(Actual));
  if P > 0 then
  begin
    Delete(Actual, P, Length(Carpeta) + 1);
    RegWriteExpandStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Actual);
  end;
  { Los datos (historial, config, certificados) NO se borran a propósito. }
end;
