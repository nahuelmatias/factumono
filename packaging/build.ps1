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
    $Version = (uv run python -c "import facturitas; print(facturitas.__version__)").Trim()
}
Write-Host "== Facturitas $Version =="

Remove-Item -Recurse -Force build/dist, build/work -ErrorAction SilentlyContinue
uv run pyinstaller packaging/facturitas.spec --noconfirm --distpath build/dist --workpath build/work
if ($LASTEXITCODE -ne 0) { throw "PyInstaller falló" }

$exe = (Resolve-Path "build/dist/facturitas/facturitas.exe").Path

# --- smoke test (con un directorio de datos vacío y sin tocar la red) ---
$env:FACTURITAS_HOME = Join-Path $env:TEMP "facturitas-smoke-$([guid]::NewGuid())"
$salida = & $exe --version
if ($LASTEXITCODE -ne 0 -or $salida -notmatch [regex]::Escape($Version)) {
    throw "--version devolvió '$salida' (se esperaba $Version)"
}
Write-Host "OK: $salida"

$salida = & $exe estado --offline 2>&1 | Out-String
if ($LASTEXITCODE -ne 1 -or $salida -notmatch "facturitas init") {
    throw "estado --offline sin configurar debería pedir `facturitas init`; devolvió ($LASTEXITCODE): $salida"
}
Write-Host "OK: estado --offline (sin configurar) explica cómo empezar"

$salida = & $exe mcp instalar --solo-mostrar | Out-String
if ($salida -notmatch [regex]::Escape($exe.Replace("\", "\\")) -and $salida -notmatch [regex]::Escape($exe)) {
    throw "mcp instalar no apunta al ejecutable empaquetado: $salida"
}
Write-Host "OK: mcp instalar apunta al .exe"

uv run python packaging/smoke_mcp.py $exe
if ($LASTEXITCODE -ne 0) { throw "El MCP empaquetado no responde" }
Remove-Item Env:FACTURITAS_HOME

if ($SinInstalador) { return }

$iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
if (-not $iscc) {
    foreach ($p in "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") {
        if (Test-Path $p) { $iscc = $p; break }
    }
}
if (-not $iscc) { throw "No encuentro Inno Setup (iscc). Instalalo: choco install innosetup" }
& $iscc "/DAppVersion=$Version" packaging/facturitas.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup falló" }

$setup = "build/installer/Facturitas-Setup-$Version.exe"
$hash = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
"$hash  Facturitas-Setup-$Version.exe" | Set-Content -Encoding ascii "$setup.sha256"
Write-Host "OK: $setup"
Write-Host "SHA256: $hash"
