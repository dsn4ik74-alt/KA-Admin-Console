$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$script:runLog = $null
$script:failures = 0
$script:created = 0
$script:existing = 0
$script:archivedFiles = 0
$script:deletedFiles = 0
$culture = [Globalization.CultureInfo]::InvariantCulture
$deleteAfterArchive = $env:JR_DELETE_AFTER_ARCHIVE -eq '1'

if ($deleteAfterArchive) {
    # Проверяем и удаляем через один дескриптор: файл нельзя дописать или
    # подменить между проверкой SHA-256 и удалением.
    Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using Microsoft.Win32.SafeHandles;

public static class ArchiveJrDeletion {
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateFileW(string path, uint access,
        uint share, IntPtr security, uint creation, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetFileInformationByHandle(SafeFileHandle handle,
        int infoClass, ref byte disposition, uint size);

    public static void DeleteVerified(string path, string expectedHash,
        long expectedLength, long expectedModifiedTicks) {
        string extension = Path.GetExtension(path);
        if (!extension.Equals(".lgp", StringComparison.OrdinalIgnoreCase) &&
            !extension.Equals(".lgx", StringComparison.OrdinalIgnoreCase))
            throw new IOException("Удаление разрешено только для LGP/LGX.");
        // GENERIC_READ | DELETE, FILE_SHARE_READ, OPEN_EXISTING.
        using (SafeFileHandle handle = CreateFileW(path, 0x80010000, 1,
            IntPtr.Zero, 3, 0x80, IntPtr.Zero)) {
            if (handle.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error());
            using (FileStream stream = new FileStream(handle, FileAccess.Read)) {
                if (stream.Length != expectedLength ||
                    File.GetLastWriteTimeUtc(path).Ticks != expectedModifiedTicks)
                    throw new IOException("Файл изменился после архивации: " + path);
                using (SHA256 sha = SHA256.Create()) {
                    if (BitConverter.ToString(sha.ComputeHash(stream)) != expectedHash)
                        throw new IOException("Содержимое файла отличается от ZIP: " + path);
                }
                byte disposition = 1;
                if (!SetFileInformationByHandle(handle, 4, ref disposition, 1))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
            }
        }
    }
}
'@
}

function Write-Log([string]$Message, [string]$Level = 'INFO') {
    $line = '{0:yyyy-MM-dd HH:mm:ss} [{1}] {2}' -f (Get-Date), $Level, $Message
    Write-Host $line
    if ($script:runLog) {
        Add-Content -LiteralPath $script:runLog -Value $line -Encoding UTF8
    }
}

function Get-SafeName([string]$Name) {
    $safe = [regex]::Replace($Name, '[<>:"/\\|?*\x00-\x1f]', '_').Trim().TrimEnd('.')
    if (-not $safe) { $safe = 'Base' }
    if ($safe -match '^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)') { $safe = '_' + $safe }
    if ($safe.Length -gt 80) { $safe = $safe.Substring(0, 80).TrimEnd('.') }
    return $safe
}

function Get-BaseLabel([IO.DirectoryInfo]$Folder) {
    $parent = $Folder.Parent
    $id = $parent.Name
    $name = $id
    if ($env:JR_BASE_NAME) {
        $name = $env:JR_BASE_NAME
    } else {
        # Как в Python: сопоставить GUID папки с именем в 1CV8Clst.lst.
        $current = $parent
        for ($i = 0; $i -lt 3 -and $current; $i++) {
            $list = Join-Path $current.FullName '1CV8Clst.lst'
            if (Test-Path -LiteralPath $list -PathType Leaf) {
                $text = [IO.File]::ReadAllText($list, [Text.Encoding]::UTF8)
                $pattern = '(?i)' + [regex]::Escape($id) + '\s*,\s*"((?:[^"]|"")*)"\s*,'
                $match = [regex]::Match($text, $pattern)
                if ($match.Success) { $name = $match.Groups[1].Value.Replace('""', '"'); break }
            }
            $current = $current.Parent
        }
    }
    if (-not $env:JR_BASE_NAME -and $name -eq $id -and $id -match '^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$') {
        throw ('Не удалось определить имя базы для ' + $id + '. Проверьте 1CV8Clst.lst или запустите для одной базы с JR_BASE_NAME.')
    }
    return (Get-SafeName $name)
}

function Get-StreamHash([IO.Stream]$Stream) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($sha.ComputeHash($Stream)) }
    finally { $sha.Dispose() }
}

function Open-SourceFile([IO.FileInfo]$File) {
    $share = [IO.FileShare]::Read
    if ($File.Name -ieq '1Cv8.lgf') {
        # 1С держит словарь открытым на запись даже при чтении старых фрагментов.
        # Разрешаем совместную запись, но сами открываем только для чтения.
        $share = [IO.FileShare]::ReadWrite
    }
    try {
        return [IO.File]::Open($File.FullName, [IO.FileMode]::Open, [IO.FileAccess]::Read, $share)
    } catch [IO.IOException] {
        $nativeCode = $_.Exception.HResult -band 0xffff
        if ($nativeCode -eq 32 -or $nativeCode -eq 33) {
            throw ('Не удалось прочитать файл из-за блокировки: {0}. Если процесс запрещает совместное чтение, повторите запуск в окно обслуживания. Исходный файл не изменен.' -f $File.FullName)
        }
        throw
    }
}

function Test-Zip([string]$Path, [object[]]$Files) {
    $zip = [IO.Compression.ZipFile]::OpenRead($Path)
    try {
        if ($zip.Entries.Count -ne $Files.Count) { throw 'Набор файлов в ZIP отличается от ожидаемого.' }
        foreach ($file in $Files) {
            $entry = $zip.GetEntry($file.Name)
            if (-not $entry -or $entry.Length -ne $file.Length) {
                throw ('В ZIP отсутствует файл или отличается размер: ' + $file.Name)
            }
            $sourceStream = Open-SourceFile $file
            $entryStream = $null
            try {
                $entryStream = $entry.Open()
                if ((Get-StreamHash $sourceStream) -ne (Get-StreamHash $entryStream)) {
                    throw ('Содержимое ZIP отличается от исходного файла: ' + $file.Name)
                }
            } finally {
                if ($entryStream) { $entryStream.Dispose() }
                $sourceStream.Dispose()
            }
            $actual = Get-Item -LiteralPath $file.FullName
            if ($actual.Length -ne $file.Length -or $actual.LastWriteTimeUtc -ne $file.LastWriteTimeUtc) {
                throw ('Исходный файл изменился во время архивации: ' + $file.Name)
            }
        }
    } finally { $zip.Dispose() }
}

function Save-Zip([string]$Target, [object[]]$Files) {
    if (Test-Path -LiteralPath $Target) {
        Test-Zip $Target $Files
        Write-Log ('Уже существует, содержимое проверено: ' + $Target)
        $script:existing++
        return
    }
    $temporary = $Target + '.' + [guid]::NewGuid().ToString('N') + '.partial'
    $output = $null
    $zip = $null
    try {
        $output = [IO.File]::Open($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
        $zip = New-Object IO.Compression.ZipArchive($output, [IO.Compression.ZipArchiveMode]::Create, $true)
        foreach ($file in $Files) {
            $inputStream = Open-SourceFile $file
            $entryStream = $null
            try {
                $entry = $zip.CreateEntry($file.Name, [IO.Compression.CompressionLevel]::Optimal)
                $entryStream = $entry.Open()
                $inputStream.CopyTo($entryStream)
            } finally {
                if ($entryStream) { $entryStream.Dispose() }
                $inputStream.Dispose()
            }
        }
        $zip.Dispose(); $zip = $null
        $output.Dispose(); $output = $null
        Test-Zip $temporary $Files
        # Публикуем ZIP только после проверки, существующие ZIP не заменяем.
        [IO.File]::Move($temporary, $Target)
        $script:created++
        $script:archivedFiles += $Files.Count
        Write-Log ('Создан и проверен: {0}; файлов: {1}' -f $Target, $Files.Count)
    } finally {
        if ($zip) { $zip.Dispose() }
        if ($output) { $output.Dispose() }
        # Удаляется только собственный незавершенный ZIP в папке архивов.
        if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
    }
}

function Remove-ArchivedFiles([string]$Archive, [object[]]$Files, [string]$SourceFolder) {
    # Держим проверенный ZIP открытым для чтения на все время удаления.
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        $sourcePath = [IO.Path]::GetFullPath($SourceFolder).TrimEnd('\')
        foreach ($file in $Files) {
            if ($file.Name -ieq '1Cv8.lgf' -or $file.Extension -notin @('.lgp', '.lgx')) { continue }
            try {
                $filePath = [IO.Path]::GetFullPath($file.FullName)
                if ([IO.Path]::GetDirectoryName($filePath).TrimEnd('\') -ine $sourcePath) {
                    throw ('Файл находится вне папки журнала: ' + $filePath)
                }
                $entry = $zip.GetEntry($file.Name)
                if (-not $entry -or $entry.Length -ne $file.Length) {
                    throw ('В ZIP отсутствует проверенная копия: ' + $file.Name)
                }
                $stream = $entry.Open()
                try { $hash = Get-StreamHash $stream }
                finally { $stream.Dispose() }
                [ArchiveJrDeletion]::DeleteVerified($filePath, $hash, $file.Length, $file.LastWriteTimeUtc.Ticks)
                $script:deletedFiles++
                Write-Log ('Удален после сверки с ZIP: ' + $filePath)
            } catch {
                $script:failures++
                Write-Log ('Не удален {0}: {1}' -f $file.FullName, $_.Exception.Message) 'ERROR'
            }
        }
    } finally { $zip.Dispose() }
}

function Archive-Folder([IO.DirectoryInfo]$Folder, [string]$Label, [datetime]$Cutoff, [string]$Destination) {
    Write-Log ('База: {0}; журнал: {1}' -f $Label, $Folder.FullName)
    $all = @(Get-ChildItem -LiteralPath $Folder.FullName -File -Force)
    if (@($all | Where-Object { $_.Extension -ieq '.lgd' }).Count -gt 0) {
        throw 'Обнаружен журнал .lgd (SQLite): выборочное архивирование этого формата не поддерживается.'
    }
    $dictionary = @($all | Where-Object { $_.Name -ieq '1Cv8.lgf' })
    $indexes = @{}
    foreach ($file in $all) {
        if ($file.Extension -ieq '.lgx') { $indexes[$file.BaseName] = $file }
    }
    $eligible = @(
        foreach ($file in $all) {
            if ($file.Extension -ine '.lgp') { continue }
            if ($file.BaseName -notmatch '^\d{14}$') {
                Write-Log ('Пропущено нестандартное имя: ' + $file.Name) 'WARN'
                continue
            }
            $date = [datetime]::MinValue
            if (-not [datetime]::TryParseExact($file.BaseName, 'yyyyMMddHHmmss', $culture, [Globalization.DateTimeStyles]::None, [ref]$date)) {
                Write-Log ('Пропущена некорректная дата: ' + $file.Name) 'WARN'
                continue
            }
            # Имя — начало периода. Проверка изменения исключает дописанные недавно файлы.
            if ($date -ge $Cutoff -or $file.LastWriteTime -ge $Cutoff) { continue }
            [pscustomobject]@{ File = $file; Date = $date }
        }
    )
    if ($eligible.Count -eq 0) { Write-Log 'Файлов старше установленного срока нет.'; return }
    if ($dictionary.Count -ne 1) { throw 'Не найден 1Cv8.lgf: без словаря архив журнала будет неполным.' }
    $baseDestination = Join-Path $Destination $Label
    [IO.Directory]::CreateDirectory($baseDestination) | Out-Null
    # Один ZIP на базу за весь отобранный период, без разделения по месяцам.
    $ordered = @($eligible | Sort-Object Date)
    $first = $ordered[0].Date.ToString('yyyy-MM-dd')
    $last = $ordered[-1].Date.ToString('yyyy-MM-dd')
    $target = Join-Path $baseDestination ($Label + '_' + $first + '_' + $last + '.zip')
    $files = @(
        foreach ($fragment in $ordered) {
            $fragment.File
            # Включаем только индекс отобранного LGP, если он тоже старше границы.
            if ($indexes.ContainsKey($fragment.File.BaseName)) {
                $index = $indexes[$fragment.File.BaseName]
                if ($index.LastWriteTime -lt $Cutoff) {
                    $index
                } else {
                    Write-Log ('Индекс недавно изменен и пропущен: ' + $index.Name) 'WARN'
                }
            }
        }
    ) + $dictionary
    Write-Log ('Общий период: {0} — {1}; фрагментов LGP: {2}' -f $first, $last, $ordered.Count)
    Save-Zip $target $files
    if ($deleteAfterArchive) { Remove-ArchivedFiles $target $files $Folder.FullName }
}

try {
    if ($env:JR_DELETE_AFTER_ARCHIVE -notin @('0', '1')) {
        throw 'JR_DELETE_AFTER_ARCHIVE должен быть 0 или 1.'
    }
    $months = 0
    if (-not [int]::TryParse($env:JR_MONTHS, [ref]$months) -or $months -lt 1 -or $months -gt 1200) {
        throw 'JR_MONTHS должен быть целым числом от 1 до 1200.'
    }
    if (-not $env:JR_SOURCE -or -not $env:JR_ARCHIVE) { throw 'Укажите источник ЖР и папку архивов.' }
    $sourceItem = Get-Item -LiteralPath $env:JR_SOURCE -Force
    if (-not $sourceItem.PSIsContainer) { throw 'JR_SOURCE должен указывать на папку.' }
    $source = $sourceItem.FullName.TrimEnd('\')
    $destination = [IO.Path]::GetFullPath($env:JR_ARCHIVE).TrimEnd('\')
    if ($destination -ieq $source -or $destination.StartsWith($source + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Папка архивов должна находиться вне исходной папки журнала/кластера.'
    }
    $cutoff = (Get-Date).Date.AddMonths(-$months)
    [IO.Directory]::CreateDirectory($destination) | Out-Null
    $script:runLog = Join-Path $destination ('ArchiveJR_' + (Get-Date -Format 'yyyyMMdd_HHmmss_fff') + '_' + $PID + '.log')
    Write-Log ('Начало. Архивировать файлы строго до {0:yyyy-MM-dd}; срок: {1} мес.' -f $cutoff, $months)
    Write-Log ('Источник: {0}; архивы: {1}' -f $source, $destination)
    Write-Log ('Удаление архивированных LGP/LGX: {0}. 1Cv8.lgf остается на месте.' -f $deleteAfterArchive)
    $directFiles = @(Get-ChildItem -LiteralPath $sourceItem.FullName -File -Force | Where-Object { $_.Extension -in @('.lgp', '.lgx', '.lgf', '.lgd') })
    if ($sourceItem.Name -ieq '1Cv8Log' -or $directFiles.Count -gt 0) {
        $folders = @($sourceItem)
    } else {
        $folders = @(Get-ChildItem -LiteralPath $sourceItem.FullName -Directory -Recurse -Force | Where-Object { $_.Name -ieq '1Cv8Log' })
    }
    if ($folders.Count -eq 0) { throw 'Не найдены папки 1Cv8Log. Проверьте JR_SOURCE.' }
    if ($env:JR_BASE_NAME -and $folders.Count -gt 1) { throw 'JR_BASE_NAME используйте только для одной базы; для кластера оставьте пустым.' }
    $usedLabels = @{}
    foreach ($folder in $folders) {
        try {
            $label = Get-BaseLabel $folder
            if ($usedLabels.ContainsKey($label)) { throw ('Повтор имени базы: ' + $label + '. Запустите отдельно с JR_BASE_NAME.') }
            $usedLabels[$label] = $true
            Archive-Folder $folder $label $cutoff $destination
        } catch {
            $script:failures++
            Write-Log ($folder.FullName + ': ' + $_.Exception.Message) 'ERROR'
        }
    }
    Write-Log ('Завершено. Новых ZIP: {0}; уже существующих: {1}; файлов в новых ZIP: {2}; удалено LGP/LGX: {3}; ошибок: {4}. 1Cv8.lgf сохранен.' -f $script:created, $script:existing, $script:archivedFiles, $script:deletedFiles, $script:failures)
    if ($script:failures -gt 0) { exit 1 }
    exit 0
} catch {
    Write-Log $_.Exception.Message 'ERROR'
    exit 1
}
