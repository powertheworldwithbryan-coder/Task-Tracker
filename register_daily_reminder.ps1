# register_daily_reminder.ps1
# Registers a Windows Task Scheduler job to run daily_tasks.py every morning at 8:00 AM.
# Run this ONCE as Administrator.

$taskName   = "TaskTracker_DailyReminder"
$scriptPath = (Resolve-Path (Join-Path $PSScriptRoot "daily_tasks.py")).Path
$pythonPath = (Get-Command python).Source

$action  = New-ScheduledTaskAction -Execute $pythonPath -Argument "`"$scriptPath`"" -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Daily -At "08:00AM"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -StartWhenAvailable

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -RunLevel Limited -Force

Write-Host "Scheduled task '$taskName' registered — runs daily at 8:00 AM." -ForegroundColor Green
