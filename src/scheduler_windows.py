"""Register only our headless task, using Windows' own credential storage."""
import base64
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import subprocess
import sys

from powershell_jobs import run_script


TASK_SCRIPT = r'''
param($p)
$service=New-Object -ComObject Schedule.Service
$service.Connect()
$folder=$service.GetFolder('\')
$name=[string]$p.name
$task=$null
try { $task=$folder.GetTask($name) } catch {
    if ($_.Exception.HResult -notin @(-2147024894,-2147024893)) { throw }
}
if ($p.action -eq 'register') {
    $definition=$service.NewTask(0)
    $definition.RegistrationInfo.Description='Консоль администратора КА: фоновый исполнитель сохранённых последовательностей'
    $definition.Principal.UserId=[string]$p.user
    $definition.Principal.LogonType=1
    $definition.Principal.RunLevel=1
    $definition.Settings.Enabled=$true
    $definition.Settings.Hidden=$true
    $definition.Settings.StartWhenAvailable=$true
    $definition.Settings.DisallowStartIfOnBatteries=$false
    $definition.Settings.StopIfGoingOnBatteries=$false
    $definition.Settings.ExecutionTimeLimit='PT0S'
    $definition.Settings.MultipleInstances=2
    $definition.Settings.RestartInterval='PT1M'
    $definition.Settings.RestartCount=3
    $definition.Settings.WakeToRun=$true
    $boot=$definition.Triggers.Create(8)
    $boot.Delay='PT30S'
    $timer=$definition.Triggers.Create(1)
    $timer.StartBoundary=(Get-Date).AddSeconds(10).ToString('yyyy-MM-ddTHH:mm:ss')
    $timer.Repetition.Interval='PT1M'
    $action=$definition.Actions.Create(0)
    $action.Path=[string]$p.executable
    $action.Arguments=[string]$p.arguments
    $action.WorkingDirectory=[string]$p.directory
    $task=$folder.RegisterTaskDefinition($name,$definition,6,[string]$p.user,[string]$p.password,1,$null)
    $null=$task.Run($null)
} elseif ($p.action -eq 'start') {
    if (-not $task) { throw 'Сначала включите фоновый запуск планировщика' }
    $null=$task.Run($null)
} elseif ($p.action -eq 'remove') {
    if ($task) { $folder.DeleteTask($name,0); $task=$null }
}
$result=@{kind='done';installed=[bool]$task;name=$name}
if ($task) { $result.state=[int]$task.State; $result.user=[string]$task.Definition.Principal.UserId }
[Console]::Out.WriteLine(($result | ConvertTo-Json -Compress))
'''


def current_user():
    if os.name != 'nt':
        return os.environ.get('USER', '')
    size = wintypes.ULONG(0)
    function = ctypes.WinDLL('secur32', use_last_error=True).GetUserNameExW
    function.argtypes = [ctypes.c_int, wintypes.LPWSTR, ctypes.POINTER(wintypes.ULONG)]
    function.restype = wintypes.BOOL
    function(2, None, ctypes.byref(size))
    buffer = ctypes.create_unicode_buffer(size.value or 512)
    size.value = len(buffer)
    if not function(2, buffer, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    return buffer.value


def worker_command(path):
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--scheduler-worker', str(Path(path).resolve())]
    return [sys.executable, str(Path(__file__).with_name('1C Console.py').resolve()),
            '--scheduler-worker', str(Path(path).resolve())]


def task_name(path):
    return 'KAConsole Scheduler ' + hashlib.sha256(str(Path(path).resolve()).casefold().encode()).hexdigest()[:16]


def control(action, path, password='', execute=run_script):
    if action not in ('register', 'start', 'query', 'remove'):
        raise ValueError('Неизвестная команда фонового запуска')
    if action == 'register' and not password:
        raise ValueError('Для запуска после выхода из сеанса нужен пароль текущей учётной записи Windows, не PIN')
    command = worker_command(path)
    # DPAPI belongs to the current user, so selecting another account is prohibited.
    payload = dict(action=action, name=task_name(path), user=current_user(), password=password,
                   executable=command[0], arguments=subprocess.list2cmdline(command[1:]),
                   directory=str(Path(command[0]).resolve().parent))
    return execute(TASK_SCRIPT, payload, lambda event: None, secrets=(password,))
