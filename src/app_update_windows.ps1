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
    [IO.Path]::GetExtension($target) -ne '.exe') { throw 'Invalid update executable' }
$backup = $target + '.previous-' + [Guid]::NewGuid().ToString('N')
$moved = $false
$installed = $false
try {
    if ((Get-FileHash -LiteralPath $replacement -Algorithm SHA256).Hash -ne [string]$p.sha256) {
        throw 'The downloaded executable checksum does not match'
    }
    $old = Get-Process -Id ([int]$p.pid) -ErrorAction SilentlyContinue
    if ($old -and -not $old.WaitForExit(180000)) { throw 'The console did not close in time' }
    if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne [string]$p.source_sha256) {
        throw 'The original executable changed during update'
    }
    for ($attempt=0; $attempt -lt 120; $attempt++) {
        try { Move-Item -LiteralPath $target -Destination $backup; $moved=$true; break }
        catch { if ($attempt -eq 119) { throw }; Start-Sleep -Milliseconds 500 }
    }
    Move-Item -LiteralPath $replacement -Destination $target
    $installed=$true
    if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne [string]$p.sha256) {
        throw 'The installed executable checksum does not match'
    }
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName=$target
    $start.Arguments='--ka-updated'
    if ($p.resume_scheduler) { $start.Arguments += ' --resume-scheduler' }
    $start.WorkingDirectory=[IO.Path]::GetDirectoryName($target)
    $start.UseShellExecute=$true
    $null=[Diagnostics.Process]::Start($start)
    try { Remove-Item -LiteralPath $backup -Force } catch { }
    # Only the marker-checked directory containing this very script is removed.
    try { Remove-Item -LiteralPath $stage -Recurse -Force } catch { }
} catch {
    $failure=$_.Exception.Message
    if ($moved -and (Test-Path -LiteralPath $backup)) {
        if ($installed -and (Test-Path -LiteralPath $target)) { Remove-Item -LiteralPath $target -Force }
        Move-Item -LiteralPath $backup -Destination $target
    }
    if (Test-Path -LiteralPath $replacement) { Remove-Item -LiteralPath $replacement -Force }
    $log=Join-Path $stage 'update-error.txt'
    [IO.File]::WriteAllText($log,$failure,[Text.Encoding]::UTF8)
    Add-Type -AssemblyName System.Windows.Forms
    $null=[Windows.Forms.MessageBox]::Show("Update failed. The previous executable was kept or restored.`r`n$failure`r`n$log",'KA Console update')
}
