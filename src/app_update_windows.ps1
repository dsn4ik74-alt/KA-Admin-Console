param([Parameter(Mandatory=$true)][string]$Config)
$ErrorActionPreference = 'Stop'
$p = Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
$stage = [IO.Path]::GetFullPath($PSScriptRoot)
if ([IO.Path]::GetFullPath([string]$p.folder) -ne $stage -or
    (Split-Path $stage -Leaf) -notmatch '^ka-update-[a-f0-9]{32}-[a-zA-Z0-9_-]+$' -or
    (Get-Content -LiteralPath (Join-Path $stage 'ka-update.marker') -Raw) -ne 'KA_UPDATE_1') {
    throw 'Invalid update staging directory'
}
$target = [IO.Path]::GetFullPath([string]$p.target)
$replacement = [IO.Path]::GetFullPath([string]$p.executable)
if ([IO.Path]::GetDirectoryName($replacement) -ne $stage -or
    [IO.Path]::GetExtension($target) -ne '.exe' -or
    [string]$p.startup_token -notmatch '^[a-f0-9]{32}$') { throw 'Invalid update executable or startup token' }
$backup = $target + '.previous-' + [Guid]::NewGuid().ToString('N')
$candidate = $target + '.incoming-' + [Guid]::NewGuid().ToString('N')
$ready = Join-Path $stage 'ready.json'
$newProcess = $null
$startupPending = $false
try {
    if ((Get-FileHash -LiteralPath $replacement -Algorithm SHA256).Hash -ne [string]$p.sha256) {
        throw 'The downloaded executable checksum does not match'
    }
    # A onefile application has a Python process and a bootloader parent. Both
    # must finish before replacing the executable or reusing its file name.
    foreach ($processId in (@($p.pid, $p.parent_pid) | Where-Object { $_ } | Select-Object -Unique)) {
        $old = Get-Process -Id ([int]$processId) -ErrorAction SilentlyContinue
        if ($old -and $old.Path -eq $target -and -not $old.WaitForExit(180000)) {
            throw 'The console did not close in time'
        }
    }
    if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne [string]$p.source_sha256) {
        throw 'The original executable changed during update'
    }
    # ReplaceFile requires the same volume and preserves the original DACL.
    # Moving a file from TEMP would instead retain TEMP's permissions.
    [IO.File]::Copy($replacement, $candidate, $false)
    if ((Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash -ne [string]$p.sha256) {
        throw 'The adjacent executable checksum does not match'
    }
    for ($attempt=0; $attempt -lt 120; $attempt++) {
        try { [IO.File]::Replace($candidate, $target, $backup, $false); break }
        catch {
            if ($attempt -eq 119 -or (Test-Path -LiteralPath $backup)) { throw }
            Start-Sleep -Milliseconds 500
        }
    }
    if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne [string]$p.sha256) {
        throw 'The installed executable checksum does not match'
    }
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName=$target
    $start.Arguments='--ka-updated --ka-update-config "' + [IO.Path]::GetFullPath($Config) + '"'
    if ($p.resume_scheduler) { $start.Arguments += ' --resume-scheduler' }
    $start.WorkingDirectory=[IO.Path]::GetDirectoryName($target)
    # The helper already has the console's elevation. A direct launch gives
    # the new onefile instance its own extraction and parent process.
    $start.UseShellExecute=$false
    $start.CreateNoWindow=$true
    $start.EnvironmentVariables['PYINSTALLER_RESET_ENVIRONMENT']='1'
    $newProcess=[Diagnostics.Process]::Start($start)
    $timer=[Diagnostics.Stopwatch]::StartNew()
    $confirmed=$false
    while ($timer.Elapsed.TotalSeconds -lt 120) {
        if (Test-Path -LiteralPath $ready) {
            try {
                $reply=Get-Content -LiteralPath $ready -Raw -Encoding UTF8 | ConvertFrom-Json
                if ($reply.token -eq $p.startup_token -and $reply.version -eq $p.version -and
                    [IO.Path]::GetFullPath([string]$reply.target) -eq $target) {
                    $confirmed=$true; break
                }
            } catch { }
        }
        if ($newProcess.HasExited) { break }
        Start-Sleep -Milliseconds 200
    }
    if (-not $confirmed) {
        $startupPending=-not $newProcess.HasExited
        if ($startupPending) { throw "Startup is not confirmed yet. The new process may still be starting. Backup: $backup" }
        throw "The updated console exited before confirming startup (code $($newProcess.ExitCode))"
    }
    try { Remove-Item -LiteralPath $backup -Force } catch { }
    # Only the marker-checked directory containing this very script is removed.
    try { Remove-Item -LiteralPath $stage -Recurse -Force } catch { }
} catch {
    $failure=$_.Exception.Message
    if (-not $startupPending -and (Test-Path -LiteralPath $backup)) {
        try {
            if (Test-Path -LiteralPath $target) { [IO.File]::Replace($backup, $target, $candidate, $false) }
            else { [IO.File]::Move($backup, $target) }
            $failure += "`r`nThe previous executable was restored: $target"
        } catch { $failure += "`r`nAutomatic rollback failed. Keep the previous executable: $backup`r`n$($_.Exception.Message)" }
    }
    if (Test-Path -LiteralPath $candidate) { Remove-Item -LiteralPath $candidate -Force }
    if (Test-Path -LiteralPath $replacement) { Remove-Item -LiteralPath $replacement -Force }
    $log=Join-Path $stage 'update-error.txt'
    [IO.File]::WriteAllText($log,$failure,[Text.Encoding]::UTF8)
    Add-Type -AssemblyName System.Windows.Forms
    $null=[Windows.Forms.MessageBox]::Show("Update was not confirmed.`r`n$failure`r`n$log",'KA Console update')
}
