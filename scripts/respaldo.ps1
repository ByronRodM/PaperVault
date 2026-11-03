#Requires -Version 7.3
<#
.SYNOPSIS
    Respaldo de PaperVault: base de datos PostgreSQL y carpeta uploads/ (Mejora de auditoria H-13).

.DESCRIPTION
    Crea en -Destino una carpeta papervault_AAAAMMDD_HHMMSS con:
      - papervault.dump  pg_dump en formato custom (se restaura con pg_restore)
      - uploads.zip      documentos originales e imagenes de plantillas (sin uploads/tmp)
      - SHA256SUMS.txt   huellas para comprobar la integridad de la copia
    Elimina los respaldos con mas de -DiasRetencion dias.

    Con -ProbarRestauracion restaura el dump en una base temporal dentro del
    contenedor, compara los conteos de filas con la base de origen y borra la
    base temporal. El script termina con error si la prueba no coincide.

    Las credenciales se toman de las variables POSTGRES_USER / POSTGRES_DB del
    propio contenedor, asi que no hace falta leer el archivo .env.

.EXAMPLE
    pwsh -File scripts\respaldo.ps1 -Destino D:\Respaldos\PaperVault -ProbarRestauracion
#>
param(
    [string]$Destino = (Join-Path $PSScriptRoot "..\respaldos"),
    [string]$Uploads = (Join-Path $PSScriptRoot "..\uploads"),
    [string]$Contenedor = "papervault_db",
    [string]$Base = "",
    [int]$DiasRetencion = 30,
    [switch]$ProbarRestauracion
)

$ErrorActionPreference = "Stop"
$Tablas = @("usuarios", "documentos", "plantillas_ocr", "zonas_ocr")
$BasePrueba = "papervault_restauracion_prueba"

function Invoke-Contenedor([string]$Comando) {
    $salida = docker exec $Contenedor sh -c $Comando
    if ($LASTEXITCODE -ne 0) { throw "Fallo en el contenedor ${Contenedor}: $Comando" }
    return $salida
}

function Get-Conteos([string]$NombreBase) {
    $conteos = [ordered]@{}
    foreach ($t in $Tablas) {
        $conteos[$t] = [int](Invoke-Contenedor ('psql -U "$POSTGRES_USER" -d {0} -tAc "SELECT count(*) FROM {1}"' -f $NombreBase, $t))
    }
    return $conteos
}

# ── Comprobaciones previas ──────────────────────────────────
$estado = docker inspect -f "{{.State.Running}}" $Contenedor 2>$null
if ($LASTEXITCODE -ne 0 -or $estado -ne "true") {
    throw "El contenedor '$Contenedor' no esta en ejecucion. Inicielo con: docker compose up -d"
}
if (-not $Base) { $Base = Invoke-Contenedor 'printf %s "$POSTGRES_DB"' }
if ($Base -notmatch '^[A-Za-z0-9_]+$') { throw "Nombre de base invalido: $Base" }

$marca = Get-Date -Format "yyyyMMdd_HHmmss"
$carpeta = Join-Path $Destino "papervault_$marca"
New-Item -ItemType Directory -Force -Path $carpeta | Out-Null
$carpeta = (Resolve-Path $carpeta).Path
Write-Host "Respaldo en $carpeta"

# ── Base de datos ───────────────────────────────────────────
$dumpTmp = "/tmp/papervault_$marca.dump"
$dumpLocal = Join-Path $carpeta "papervault.dump"
try {
    Invoke-Contenedor ('pg_dump -U "$POSTGRES_USER" -d {0} -Fc -f {1}' -f $Base, $dumpTmp) | Out-Null
    docker cp "${Contenedor}:$dumpTmp" $dumpLocal | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "No se pudo copiar el dump desde el contenedor." }
} finally {
    docker exec $Contenedor rm -f $dumpTmp | Out-Null
}
Write-Host ("  Base '{0}': {1:N0} bytes" -f $Base, (Get-Item $dumpLocal).Length)

# ── Archivos subidos ────────────────────────────────────────
if (Test-Path $Uploads) {
    $elementos = Get-ChildItem -LiteralPath $Uploads | Where-Object Name -ne "tmp"
    if ($elementos) {
        $zip = Join-Path $carpeta "uploads.zip"
        $elementos | Compress-Archive -DestinationPath $zip -CompressionLevel Optimal
        Write-Host ("  uploads/: {0:N0} bytes comprimidos" -f (Get-Item $zip).Length)
    } else {
        Write-Host "  uploads/ esta vacia; no se genera uploads.zip"
    }
} else {
    Write-Warning "No existe la carpeta de archivos subidos: $Uploads"
}

# ── Huellas de integridad ───────────────────────────────────
Get-ChildItem -LiteralPath $carpeta -File | Where-Object Name -ne "SHA256SUMS.txt" |
    ForEach-Object { "{0}  {1}" -f (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower(), $_.Name } |
    Set-Content -Path (Join-Path $carpeta "SHA256SUMS.txt") -Encoding ascii

# ── Prueba de restauracion ──────────────────────────────────
if ($ProbarRestauracion) {
    Write-Host "Probando la restauracion en la base temporal '$BasePrueba'..."
    $restTmp = "/tmp/restauracion_$marca.dump"
    docker cp $dumpLocal "${Contenedor}:$restTmp" | Out-Null
    try {
        Invoke-Contenedor ('dropdb -U "$POSTGRES_USER" --if-exists {0} && createdb -U "$POSTGRES_USER" {0}' -f $BasePrueba) | Out-Null
        Invoke-Contenedor ('pg_restore -U "$POSTGRES_USER" -d {0} --no-owner {1}' -f $BasePrueba, $restTmp) | Out-Null
        $origen = Get-Conteos $Base
        $restaurada = Get-Conteos $BasePrueba
        $diferencias = 0
        foreach ($t in $Tablas) {
            $ok = $origen[$t] -eq $restaurada[$t]
            if (-not $ok) { $diferencias++ }
            Write-Host ("  {0,-15} origen={1,-6} restaurada={2,-6} {3}" -f $t, $origen[$t], $restaurada[$t], $(if ($ok) { "OK" } else { "DIFERENTE" }))
        }
        if ($diferencias) {
            throw "La restauracion no coincide con el origen en $diferencias tabla(s). Si la aplicacion estaba en uso durante el respaldo, repita la prueba."
        }
        Write-Host "  Restauracion verificada."
    } finally {
        docker exec $Contenedor sh -c ('dropdb -U "$POSTGRES_USER" --if-exists {0}; rm -f {1}' -f $BasePrueba, $restTmp) | Out-Null
    }
}

# ── Retencion ───────────────────────────────────────────────
if ($DiasRetencion -gt 0) {
    $limite = (Get-Date).AddDays(-$DiasRetencion)
    Get-ChildItem -Path $Destino -Directory -Filter "papervault_*" |
        Where-Object { $_.LastWriteTime -lt $limite -and $_.FullName -ne $carpeta } |
        ForEach-Object {
            Write-Host "  Eliminando respaldo antiguo: $($_.Name)"
            Remove-Item -Recurse -Force $_.FullName
        }
}

Write-Host "Respaldo completado."
