"""In-memory adapter for the functions in create_users.ps1.

The original script is a bundled code resource. Its file-reading, file-writing
and top-level execution are never invoked by the GUI.
"""
import base64
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

DEFAULT_EMAIL_DOMAIN = "corpsoft24.ru"
ACCOUNT_TIMEOUT = 300

POWERSHELL_ADAPTER = r'''
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

function Send-Event($value) {
    [Console]::Out.WriteLine(($value | ConvertTo-Json -Compress -Depth 5))
    [Console]::Out.Flush()
}

try {
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $preview = [bool]$request.preview
    $domain = ([string]$request.email_domain).Trim().ToLowerInvariant()
    if ($domain -notmatch '^(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?[.])+[a-zA-Z]{2,63}$') {
        throw 'Укажите корректный почтовый домен, например corpsoft24.ru.'
    }
    if (-not $preview) {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            throw 'Для создания локальных учётных записей нужны права администратора.'
        }
    }

    # Import only named functions; do not execute the script body or #Requires.
    $tokens = $null
    $parseErrors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseInput(
        [string]$request.script, [ref]$tokens, [ref]$parseErrors)
    if ($parseErrors.Count -gt 0) { throw 'Ошибка синтаксиса в create_users.ps1.' }
    $required = @('ConvertTo-LatinName', 'Get-UserIdentity', 'Generate-Password',
                  'User-Exists', 'Create-User', 'Add-UserToRDP')
    $functions = @($ast.FindAll({ param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
    }, $false))
    foreach ($name in $required) {
        $matches = @($functions | Where-Object { $_.Name -eq $name })
        if ($matches.Count -ne 1) { throw "Не найдена однозначная функция $name в create_users.ps1." }
        . ([scriptblock]::Create($matches[0].Extent.Text))
    }
    $commands = @('Get-LocalUser', 'New-LocalUser', 'Get-LocalGroup', 'Add-LocalGroupMember')
    $useModernCmdlets = @(Get-Command -Name $commands -ErrorAction SilentlyContinue).Count -eq $commands.Count
    $seen = @{}
    $lineNumber = 0
    $count = 0
    foreach ($line in (([string]$request.names) -split '\r\n|\n|\r')) {
        $lineNumber++
        $line = $line.Trim()
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $count++
        $row = [ordered]@{
            line = $lineNumber; full_name = $line; username = ''; email = '';
            password = ''; company = ''; status = 'error'; message = ''
        }
        try {
            $record = [regex]::Match($line, '^(?<FullName>[^"@]+?)(?:\s+[^\s"@]+@[^\s"@]+)?(?:\s+"(?<Company>[^"]*)")?\s*$')
            if (-not $record.Success) {
                throw 'Ожидается Фамилия Имя Отчество и необязательная компания в двойных кавычках.'
            }
            $row.full_name = $record.Groups['FullName'].Value.Trim()
            $row.company = $record.Groups['Company'].Value.Trim()
            $userIdentity = Get-UserIdentity -FullName $row.full_name -Domain $domain
            $row.username = $userIdentity.User
            $row.email = $userIdentity.Email
            if ($seen.ContainsKey($row.username)) {
                $row.status = 'duplicate'
                $row.message = "Логин уже получен из строки $($seen[$row.username])."
            } else {
                $seen[$row.username] = $lineNumber
                if ($preview) {
                    $row.status = 'ready'
                    $row.message = 'Формат проверен. Учётная запись ещё не создана.'
                } elseif (User-Exists $row.username) {
                    $row.status = 'exists'
                    $row.message = 'Учётная запись уже существует; пароль и группы не изменены.'
                } else {
                    $description = if ($row.company) { "$($row.email) ($($row.company))" } else { $row.email }
                    if ($description.Length -gt 48) { $description = $description.Substring(0, 48) }
                    $password = Generate-Password
                    $null = Create-User -username $row.username -passwordPlain $password `
                                        -fullName $row.full_name -description $description
                    # Return the password immediately after successful creation.
                    # A later RDP failure must not lose an already created account.
                    $row.password = $password
                    $row.status = 'created'
                    $row.message = 'Учётная запись создана; добавление в группу RDP выполняется.'
                    Send-Event @{ kind = 'row'; row = $row }
                    try {
                        $null = Add-UserToRDP -username $row.username
                        $row.status = 'created_rdp'
                        $row.message = 'Создана и добавлена в группу удалённого рабочего стола.'
                    } catch {
                        $row.status = 'created_no_rdp'
                        $row.message = "Создана, но не добавлена в группу RDP: $($_.Exception.Message)"
                    }
                }
            }
        } catch {
            $row.status = 'error'
            $row.message = $_.Exception.Message
        }
        Send-Event @{ kind = 'row'; row = $row }
    }
    Send-Event @{ kind = 'done'; count = $count; preview = $preview }
} catch {
    Send-Event @{ kind = 'fatal'; message = $_.Exception.Message }
    exit 1
}
'''


def script_source():
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return (root / "create_users.ps1").read_text(encoding="utf-8-sig")


def powershell_path():
    windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    # Sysnative avoids the 32-bit LocalAccounts limitation for 32-bit Python.
    for directory in ("Sysnative", "System32"):
        path = windows / directory / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if path.is_file():
            return str(path)
    path = shutil.which("powershell.exe")
    if not path:
        raise RuntimeError("Windows PowerShell не найден.")
    return path


def run_account_batch(names, email_domain, on_row, preview=False, timeout=ACCOUNT_TIMEOUT):
    """Send JSON through stdin and consume JSON events; never save user data."""
    if not names.strip():
        raise ValueError("Введите хотя бы одно ФИО.")
    payload = json.dumps({"script": script_source(), "names": names,
                          "email_domain": email_domain, "preview": preview}, ensure_ascii=False)
    encoded = base64.b64encode(POWERSHELL_ADAPTER.encode("utf-16le")).decode("ascii")
    cmd = [powershell_path(), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    events = queue.Queue()
    done = False
    failure = ""
    protocol_error = False
    deadline = time.monotonic() + timeout

    def read_output():
        try:
            for line in process.stdout:
                events.put(line)
        finally:
            events.put(None)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    try:
        process.stdin.write(payload)
        process.stdin.close()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Превышено время выполнения. Полученные результаты сохранены в таблице; "
                                   "состояние остальных учётных записей нужно проверить в Windows.")
            try:
                line = events.get(timeout=min(0.2, remaining))
            except queue.Empty:
                continue
            if line is None:
                break
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                if event.get("kind") == "row":
                    row = event["row"]
                    if not isinstance(row, dict) or not isinstance(row.get("line"), int):
                        raise ValueError("Invalid row")
                    on_row(row)
                elif event.get("kind") == "done":
                    done = True
                elif event.get("kind") == "fatal":
                    failure = str(event.get("message", "Ошибка PowerShell"))
                else:
                    protocol_error = True
            except (ValueError, AttributeError, KeyError, TypeError):
                # Do not echo arbitrary console output which could contain passwords.
                protocol_error = True
        code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        if failure:
            raise RuntimeError(failure)
        if code or not done or protocol_error:
            raise RuntimeError(f"PowerShell завершился с неполным ответом (код {code}). "
                               "Полученные результаты сохранены; проверьте состояние остальных учётных записей.")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        reader.join(timeout=2)
        if process.stdout:
            process.stdout.close()
