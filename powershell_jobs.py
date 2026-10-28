"""Stream background PowerShell work through stdin/stdout, without input files."""
import base64
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

from postgres_runner import OperationCancelled


BOOTSTRAP = r'''
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$utf8=New-Object Text.UTF8Encoding($false)
[Console]::InputEncoding=$utf8; [Console]::OutputEncoding=$utf8; $OutputEncoding=$utf8
try {
    $job=[Console]::In.ReadLine() | ConvertFrom-Json
    & ([scriptblock]::Create([string]$job.code)) $job.data
} catch {
    [Console]::Out.WriteLine((@{kind='fatal';text=$_.Exception.Message} | ConvertTo-Json -Compress))
    exit 1
}
'''


def run_script(code, data, on_event, *, environment=None, require_done=True, secrets=()):
    if os.name != "nt":
        raise RuntimeError("Для этой операции нужен Windows PowerShell")
    cancel_event = getattr(on_event, '__dict__', {}).get('cancel_event')
    if cancel_event is not None and cancel_event.is_set():
        raise OperationCancelled('Операция прервана пользователем')
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    encoded = base64.b64encode(BOOTSTRAP.encode("utf-16-le")).decode("ascii")
    process = subprocess.Popen([str(executable), "-NoLogo", "-NoProfile", "-NonInteractive",
                                "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               creationflags=subprocess.CREATE_NO_WINDOW, env=environment)
    lines = queue.Queue()

    def read():
        try:
            for line in iter(process.stdout.readline, b""):
                lines.put(line.decode("utf-8-sig", errors="replace").rstrip())
        finally:
            lines.put(None)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()

    def redact(text):
        for secret in secrets:
            if secret:
                text = text.replace(secret, "[скрыто]")
        return text

    done, fatal, tail = None, "", []
    try:
        payload = (json.dumps({"code": code, "data": data}, ensure_ascii=False) + '\n').encode("utf-8")
        process.stdin.write(payload)
        process.stdin.flush()
        if cancel_event is None:
            process.stdin.close()
        cancel_sent = None
        while True:
            if cancel_event is not None and cancel_event.is_set() and cancel_sent is None:
                cancel_sent = time.monotonic()
                try:
                    process.stdin.write(b'cancel\n')
                    process.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
            if cancel_sent is not None and time.monotonic() - cancel_sent > 5 and process.poll() is None:
                process.terminate()
            try:
                line = lines.get(timeout=0.1)
            except queue.Empty:
                continue
            if line is None:
                break
            if not line:
                continue
            try:
                event = json.loads(line)
                if not isinstance(event, dict) or "kind" not in event:
                    raise ValueError()
            except ValueError:
                event = {"kind": "message", "text": line}
            if "text" in event:
                event["text"] = redact(str(event["text"]))
                tail.append(event["text"])
                tail = tail[-12:]
            if event["kind"] == "fatal":
                fatal = event.get("text", "Ошибка PowerShell")
            elif event["kind"] == "done":
                done = event
            else:
                on_event(event)
        returncode = process.wait()
        if cancel_sent is not None and done is None:
            raise OperationCancelled('Запрос прерван пользователем. SQL Server может продолжать откат; '
                                     'неполный бэкап нельзя использовать для восстановления.')
        if fatal or returncode or (require_done and done is None):
            reason = f"PowerShell завершился с кодом {returncode}" if returncode else "PowerShell не вернул результат"
            raise RuntimeError(fatal or (reason + '.\n' + '\n'.join(tail)))
        return done or {"kind": "done"}
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        reader.join(timeout=2)
        if process.stdout:
            process.stdout.close()
