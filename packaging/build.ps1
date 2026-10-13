<#
Construye el ejecutable (PyInstaller onedir), corre el smoke test y arma el instalador.

    pwsh packaging/build.ps1 [-Version 0.1.0] [-SinInstalador]

Requiere `uv` y, para el instalador, Inno Setup 6 (iscc).
#>
param(
    [string]$Version = "",
    [switch]$SinInstalador
)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not $Version) {
    $Version = (uv run python -c "import factumono; print(factumono.__version__)").Trim()
}
Write-Host "== Factumono $Version =="

Remove-Item -Recurse -Force build/dist, build/work -ErrorAction SilentlyContinue
uv run pyinstaller packaging/factumono.spec --noconfirm --distpath build/dist --workpath build/work
if ($LASTEXITCODE -ne 0) { throw "PyInstaller falló" }

$exe = (Resolve-Path "build/dist/factumono/factumono.exe").Path

# --- smoke test (con un directorio de datos vacío y sin tocar la red) ---
$env:FACTUMONO_HOME = Join-Path $env:TEMP "factumono-smoke-$([guid]::NewGuid())"
$salida = & $exe --version
if ($LASTEXITCODE -ne 0 -or $salida -notmatch [regex]::Escape($Version)) {
    throw "--version devolvió '$salida' (se esperaba $Version)"
}
Write-Host "OK: $salida"

$salida = & $exe estado --offline 2>&1 | Out-String
if ($LASTEXITCODE -ne 1 -or $salida -notmatch "factumono init") {
    throw "estado --offline sin configurar debería pedir `factumono init`; devolvió ($LASTEXITCODE): $salida"
}
Write-Host "OK: estado --offline (sin configurar) explica cómo empezar"

$salida = & $exe mcp instalar --solo-mostrar | Out-String
if ($salida -notmatch [regex]::Escape($exe.Replace("\", "\\")) -and $salida -notmatch [regex]::Escape($exe)) {
    throw "mcp instalar no apunta al ejecutable empaquetado: $salida"
}
Write-Host "OK: mcp instalar apunta al .exe"

$salida = & $exe app --probar 2>&1 | Out-String
if ($LASTEXITCODE -ne 0 -or $salida -notmatch "OK: la interfaz") {
    throw "app --probar falló ($LASTEXITCODE): $salida"
}
Write-Host "OK: la interfaz gráfica empaquetada responde (plantillas, estáticos, seguridad)"

# La ventana propia (sin consola) tiene que abrir y quedarse viva unos segundos.
$gui = (Resolve-Path "build/dist/factumono/factumono-gui.exe").Path
$proc = Start-Process -FilePath $gui -PassThru
Start-Sleep -Seconds 10
$vivo = -not $proc.HasExited
if ($vivo) { Stop-Process -Id $proc.Id -Force }
if (-not $vivo) { throw "factumono-gui.exe se cerró solo (código $($proc.ExitCode))" }
Write-Host "OK: factumono-gui.exe abre y se mantiene"

uv run python packaging/smoke_mcp.py $exe
if ($LASTEXITCODE -ne 0) { throw "El MCP empaquetado no responde" }
Remove-Item Env:FACTUMONO_HOME

if ($SinInstalador) { return }

$iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
if (-not $iscc) {
    foreach ($p in "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") {
        if (Test-Path $p) { $iscc = $p; break }
    }
}
if (-not $iscc) { throw "No encuentro Inno Setup (iscc). Instalalo: choco install innosetup" }
& $iscc "/DAppVersion=$Version" packaging/factumono.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup falló" }

$setup = "build/installer/Factumono-Setup-$Version.exe"
$hash = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
"$hash  Factumono-Setup-$Version.exe" | Set-Content -Encoding ascii "$setup.sha256"
Write-Host "OK: $setup"
Write-Host "SHA256: $hash"
