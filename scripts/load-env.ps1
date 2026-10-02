# Carga en ESTA sesión de PowerShell la configuración guardada en tu perfil de usuario
# de Windows (HKCU\Environment). Útil cuando la consola se abrió desde un programa
# (VS Code, Windows Terminal...) iniciado antes de guardar las variables.
# No muestra ningún valor.
#
#   . .\scripts\load-env.ps1      (con el punto delante: afecta a la sesión actual)

$names = "DATABASE_URL", "INVENTORY_ENCRYPTION_KEYS", "INVENTORY_ADMIN_API", "INVENTORY_ADMIN_TOKEN",
         "INVENTORY_SCAN_EXECUTOR", "INVENTORY_PRICE_TABLE"
foreach ($name in $names) {
    $value = [Environment]::GetEnvironmentVariable($name, "User")
    if ($value) { Set-Item -Path "Env:$name" -Value $value }
}
foreach ($name in "DATABASE_URL", "INVENTORY_ENCRYPTION_KEYS") {
    $status = if ([Environment]::GetEnvironmentVariable($name, "Process")) { "OK" } else { "FALTA" }
    Write-Host ("{0}: {1}" -f $name, $status)
}
$listening = [bool](Get-NetTCPConnection -State Listen -LocalPort 5432 -ErrorAction SilentlyContinue)
Write-Host ("PostgreSQL: {0}" -f $(if ($listening) { "OK" } else { "FALTA  ->  & `"$env:LOCALAPPDATA\HuaweiInventory\start-postgres.ps1`"" }))
