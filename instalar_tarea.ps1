# Programa el buscador en el Programador de tareas de Windows.
# Corre todos los dias cada 30 minutos entre 08:00 y 22:00, con tu sesion iniciada
# (asi las notificaciones de Windows aparecen en pantalla). Sin ventana: usa pyw.
# Si el PC esta en suspension, lo despierta para correr (no funciona con el PC apagado).
#
#   Instalar:     powershell -ExecutionPolicy Bypass -File instalar_tarea.ps1
#   Solo L-V:     powershell -ExecutionPolicy Bypass -File instalar_tarea.ps1 -SoloSemana
#   Desinstalar:  powershell -ExecutionPolicy Bypass -File instalar_tarea.ps1 -Desinstalar
param(
    [int]$CadaMinutos = 30,
    [string]$Desde = "08:00",
    [int]$Horas = 14,
    [switch]$SoloSemana,
    [switch]$Desinstalar
)
$nombre = "Buscador Deptos Arriendo"
if ($Desinstalar) {
    Unregister-ScheduledTask -TaskName $nombre -Confirm:$false
    Write-Host "Tarea '$nombre' eliminada."
    return
}
$dir = $PSScriptRoot
$pyw = (Get-Command pyw -ErrorAction Stop).Source
$accion = New-ScheduledTaskAction -Execute $pyw -Argument "`"$dir\buscador.py`"" -WorkingDirectory $dir
if ($SoloSemana) {
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $Desde
} else {
    $trigger = New-ScheduledTaskTrigger -Daily -At $Desde
}
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At $Desde -RepetitionInterval (New-TimeSpan -Minutes $CadaMinutos) -RepetitionDuration (New-TimeSpan -Hours $Horas)).Repetition
$opciones = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $nombre -Action $accion -Trigger $trigger -Settings $opciones -Description "Scrapea Portal Inmobiliario, avisa deptos nuevos y publica el dashboard" -Force | Out-Null
$dias = if ($SoloSemana) { "L-V" } else { "todos los dias" }
Write-Host "Tarea '$nombre' instalada: $dias cada $CadaMinutos min desde $Desde por $Horas h (despierta el PC si esta suspendido)."
