"""Windows PowerShell locally or WinRM; credentials are sent only on stdin."""
import re
from powershell_jobs import run_script
from postgres_runner import OperationCancelled


WRAPPER = r'''
param($request)
$session=$null
try {
  if ($request.connection.remote) {
    $c=$request.connection
    $secure=ConvertTo-SecureString ([string]$c.password) -AsPlainText -Force
    $credential=New-Object System.Management.Automation.PSCredential([string]$c.user,$secure)
    $options=New-PSSessionOption -OpenTimeout 15000 -OperationTimeout 60000
    $session=New-PSSession -ComputerName ([string]$c.host) -Port ([int]$c.port) -UseSSL:([bool]$c.ssl) -Credential $credential -Authentication Negotiate -SessionOption $options
    Invoke-Command -Session $session -ScriptBlock {param($code,$data) & ([scriptblock]::Create($code)) $data} -ArgumentList ([string]$request.code),$request.payload | ForEach-Object {[Console]::Out.WriteLine([string]$_)}
  } else {
    & ([scriptblock]::Create([string]$request.code)) $request.payload | ForEach-Object {[Console]::Out.WriteLine([string]$_)}
  }
} finally {if ($session) {Remove-PSSession $session -ErrorAction SilentlyContinue}}
'''


def run_windows(code, connection, payload, event):
    if connection.get('remote'):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]*', connection.get('host', '')):
            raise ValueError('Укажите корректный адрес Windows / WinRM')
        if not connection.get('user') or not connection.get('password'):
            raise ValueError('Введите логин и пароль Windows / WinRM')
        port = int(connection.get('port', 5985))
        if not 1 <= port <= 65535:
            raise ValueError('Некорректный порт WinRM')
    try:
        result = run_script(WRAPPER, dict(code=code, connection=connection, payload=payload), event,
                            secrets=(connection.get('password', ''),))
        return result.get('data', {})
    except OperationCancelled:
        raise OperationCancelled('Управление веб-сервером прервано. Проверьте состояние публикации на сервере перед повтором.') from None
