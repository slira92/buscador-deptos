# Programa el buscador en el Programador de tareas de Windows.
# Corre de lunes a viernes cada 30 minutos entre 08:00 y 20:00, solo con tu sesion iniciada
# (asi las notificaciones de Windows aparecen en pantalla). Sin ventana: usa pyw.
#
#   Instalar:     powershell -ExecutionPolicy Bypass -File instalar_tarea.ps1
#   Desinstalar:  powershell -ExecutionPolicy Bypass -File instalar_tarea.ps1 -Desinstalar
param(
    [int]$CadaMinutos = 30,
    [string]$Desde = "08:00",
    [int]$Horas = 12,
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
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $Desde
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At $Desde -RepetitionInterval (New-TimeSpan -Minutes $CadaMinutos) -RepetitionDuration (New-TimeSpan -Hours $Horas)).Repetition
$opciones = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $nombre -Action $accion -Trigger $trigger -Settings $opciones -Description "Scrapea Portal Inmobiliario y avisa deptos nuevos" -Force | Out-Null
Write-Host "Tarea '$nombre' instalada: L-V cada $CadaMinutos min desde $Desde por $Horas h."
