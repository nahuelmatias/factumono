; Instalador de Factumono para Windows (Inno Setup 6).
;   iscc /DAppVersion=0.2.0 packaging\factumono.iss
; Instala por usuario (sin admin) en %LOCALAPPDATA%\Programs\Factumono, agrega la carpeta al PATH
; del usuario y NO toca los datos (historial, config, certificados) al desinstalar.

#ifndef AppVersion
  #define AppVersion "0.2.0"
#endif

[Setup]
AppId={{B2E4D9A1-7C35-4F68-8E1D-FAC70270A002}
AppName=Factumono
AppVersion={#AppVersion}
AppPublisher=Factumono (código abierto, MIT)
AppPublisherURL=https://github.com/nahuelmatias/factumono
DefaultDirName={autopf}\Factumono
DefaultGroupName=Factumono
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\build\installer
OutputBaseFilename=Factumono-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
ChangesEnvironment=yes
SetupIconFile=factumono.ico
UninstallDisplayIcon={app}\factumono-gui.exe
LicenseFile=..\LICENSE

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "desktopicon"; Description: "Crear un acceso directo en el escritorio"; GroupDescription: "Accesos directos:"

[Files]
Source: "..\build\dist\factumono\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
; La interfaz gráfica (sin consola) es lo principal. La consola sigue disponible.
Name: "{autoprograms}\Factumono"; Filename: "{app}\factumono-gui.exe"; WorkingDir: "{userdocs}"; Comment: "Facturación electrónica de ARCA"
Name: "{autodesktop}\Factumono"; Filename: "{app}\factumono-gui.exe"; WorkingDir: "{userdocs}"; Tasks: desktopicon
; Consola que se queda abierta (/k) con el asistente de configuración en texto.
Name: "{autoprograms}\Factumono – Configurar (consola)"; Filename: "{cmd}"; Parameters: "/k """"{app}\factumono.exe"" init"""; WorkingDir: "{userdocs}"; IconFilename: "{app}\factumono.exe"; Comment: "Asistente de configuración en la terminal"

[Registry]
; Agrega la carpeta de instalación al PATH del usuario (si no estaba).
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Check: NeedsAddPath(ExpandConstant('{app}'))

[Run]
; Casillas finales. «Conectar con Claude Desktop» usa el .exe instalado (no uv).
Filename: "{cmd}"; Parameters: "/c """"{app}\factumono.exe"" mcp instalar & pause"""; Description: "Conectar con Claude Desktop"; Flags: postinstall skipifsilent
Filename: "{app}\factumono-gui.exe"; Description: "Abrir Factumono"; Flags: postinstall nowait skipifsilent
Filename: "{cmd}"; Parameters: "/k """"{app}\factumono.exe"" init"""; Description: "Configurar desde la terminal (en vez de la ventana)"; Flags: postinstall skipifsilent unchecked

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
