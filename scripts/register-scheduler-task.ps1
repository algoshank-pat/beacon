# Registers the Beacon scheduler (app.scheduler) as a Windows Scheduled Task
# that starts at logon and restarts automatically on failure. Run this
# yourself from a normal PowerShell window (no admin needed for a per-user
# logon trigger) -- the Claude Code session that would otherwise run this
# doesn't have permission to register scheduled tasks.
#
# Usage: powershell -File C:\AI\beacon\scripts\register-scheduler-task.ps1

$action = New-ScheduledTaskAction -Execute "C:\AI\beacon\.venv\Scripts\python.exe" -Argument "-m app.scheduler" -WorkingDirectory "C:\AI\beacon"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 0) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask -TaskName "BeaconScheduler" -Action $action -Trigger $trigger -Settings $settings -Description "Runs the Beacon job-search app's persistent scheduler process (app.scheduler) at logon, restarting automatically on failure." -Force

Write-Host "Registered. Starting it now..."
Start-ScheduledTask -TaskName "BeaconScheduler"
Start-Sleep -Seconds 3
Get-ScheduledTask -TaskName "BeaconScheduler" | Select-Object TaskName, State
Write-Host ""
Write-Host "Check C:\AI\beacon\scheduler.log to confirm it's running."
Write-Host "To stop it:    Stop-ScheduledTask -TaskName BeaconScheduler; Stop-Process -Name python -Force"
Write-Host "To unregister: Unregister-ScheduledTask -TaskName BeaconScheduler -Confirm:`$false"
