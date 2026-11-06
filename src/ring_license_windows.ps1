param($p)
$ErrorActionPreference='Stop'
function Emit($kind,$text) { @{kind=$kind;text=$text}|ConvertTo-Json -Compress }
function Done($value) { @{kind='done';data=$value}|ConvertTo-Json -Compress -Depth 12 }
function OwnedRoot([bool]$allowMissing=$false) {
    if ([string]$p.token -notmatch '^[0-9a-f]{32}$') {throw 'Некорректная метка задания ring'}
    $expected=Join-Path ([IO.Path]::GetTempPath()) ('ka-ring-'+$p.token)
    if ([IO.Path]::GetFullPath([string]$p.root) -ne [IO.Path]::GetFullPath($expected)) {throw 'Каталог ring не принадлежит этому заданию'}
    if ($allowMissing -and -not (Test-Path -LiteralPath $expected)) {return $null}
    $item=Get-Item -LiteralPath $expected
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {throw 'Каталог ring является ссылкой'}
    $marker=Join-Path $expected '.ka-owned'
    if ((Get-Item -LiteralPath $marker).Attributes -band [IO.FileAttributes]::ReparsePoint) {throw 'Метка ring является ссылкой'}
    if ([IO.File]::ReadAllText($marker) -ne [string]$p.token) {throw 'Метка каталога ring не совпадает'}
    return $expected
}
if ($p.action -eq 'probe') {
    Done @{computer=$env:COMPUTERNAME;user=[Security.Principal.WindowsIdentity]::GetCurrent().Name;description=[Environment]::OSVersion.VersionString}
    return
}
if ($p.action -eq 'prepare') {
    if ([string]$p.token -notmatch '^[0-9a-f]{32}$') {throw 'Некорректная метка ring'}
    $root=Join-Path ([IO.Path]::GetTempPath()) ('ka-ring-'+$p.token)
    if (Test-Path -LiteralPath $root) {throw 'Временный каталог ring уже существует'}
    New-Item -ItemType Directory -Path $root | Out-Null
    [IO.File]::WriteAllText((Join-Path $root '.ka-owned'),[string]$p.token)
    Done @{root=$root};return
}
if ($p.action -eq 'cleanup') {
    $owned=OwnedRoot $true
    if ($owned) {Remove-Item -LiteralPath $owned -Recurse -Force}
    Done @{};return
}
$root=OwnedRoot
if ($p.action -notin @('list','check')) {throw 'Разрешены только чтение и проверка лицензий'}
function Native([string[]]$arguments) {
    $quoted=@(foreach ($value in $arguments) {
        $escaped=[regex]::Replace([string]$value,'(\\*)"','$1$1\"')
        $escaped=[regex]::Replace($escaped,'(\\+)$','$1$1')
        '"'+$escaped+'"'
    })
    $start=New-Object Diagnostics.ProcessStartInfo
    $start.FileName=$script:java;$start.Arguments=$quoted -join ' '
    $start.UseShellExecute=$false;$start.CreateNoWindow=$true
    $start.RedirectStandardOutput=$true;$start.RedirectStandardError=$true
    $start.StandardOutputEncoding=New-Object Text.UTF8Encoding($false)
    $start.StandardErrorEncoding=New-Object Text.UTF8Encoding($false)
    foreach ($key in @('JAVA_TOOL_OPTIONS','_JAVA_OPTIONS','JDK_JAVA_OPTIONS','CLASSPATH')) {$start.EnvironmentVariables.Remove($key)}
    $start.EnvironmentVariables['E1C_RING_COMMANDS']=$script:registry
    $start.EnvironmentVariables['E1C_RING_COMMANDS_USER']=$script:userRegistry
    $process=New-Object Diagnostics.Process
    $process.StartInfo=$start
    try {
        $null=$process.Start()
        $stdout=$process.StandardOutput.ReadToEndAsync();$stderr=$process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(90000)) {$process.Kill();throw 'ring не завершил команду за 90 с'}
        $text=($stdout.GetAwaiter().GetResult()+$stderr.GetAwaiter().GetResult()).Trim()
        if ($text.Length -gt 2097152) {throw 'Вывод ring превышает 2 МБ'}
        return @{code=$process.ExitCode;text=$text}
    } finally {
        try {if (-not $process.HasExited) {$process.Kill();$process.WaitForExit()}} catch {}
        $process.Dispose()
    }
}
try {
    if ($p.path -and -not (Test-Path -LiteralPath ([string]$p.path) -PathType Container)) {throw 'Каталог лицензий на Windows-сервере не существует или недоступен'}
    $archive=Join-Path $root 'ring.zip'
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne [string]$p.sha256) {throw 'Повреждён переданный комплект ring'}
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $z=[IO.Compression.ZipFile]::OpenRead($archive)
    try {
        foreach ($entry in $z.Entries) {
            $destination=[IO.Path]::GetFullPath((Join-Path $root ('bundle\'+$entry.FullName)))
            $prefix=[IO.Path]::GetFullPath((Join-Path $root 'bundle'))+'\'
            if (-not $destination.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)) {throw 'Путь вне комплекта ring'}
        }
    } finally {$z.Dispose()}
    [IO.Compression.ZipFile]::ExtractToDirectory($archive,(Join-Path $root 'bundle'))
    $bundle=Join-Path $root 'bundle'
    $script:java=Join-Path $bundle 'java\bin\java.exe'
    $ring=Join-Path $bundle 'ring'
    $module=@(Get-ChildItem -LiteralPath (Join-Path $bundle 'license-tools') -Filter 'com._1c.license.activator.ring-*.jar' -File)[0].FullName
    $script:registry=Join-Path $root 'ring-commands.cfg'
    $script:userRegistry=Join-Path $root 'user-commands.cfg'
    $yaml="license:`n- file: "+($module|ConvertTo-Json -Compress)+"`n  arch: x86_64`n  version: 0.15.2`n"
    [IO.File]::WriteAllText($registry,$yaml,(New-Object Text.UTF8Encoding($false)))
    [IO.File]::WriteAllText($userRegistry,"{}`n",(New-Object Text.UTF8Encoding($false)))
    $script:prefix=@('-Dfile.encoding=UTF-8','-Dsun.stdout.encoding=UTF-8','-Dsun.stderr.encoding=UTF-8','-Duser.language=ru','-Duser.country=RU',('-De1c.ring.location='+$ring),
        '-p',(Join-Path $ring 'lib\boot'),'--add-modules=ALL-MODULE-PATH','--add-opens','java.base/java.lang=com.e1c.ring.cli.boot.jpms',
        'com.e1c.ring.cli.boot.RingBootstrap')
    function License([string]$action,[string]$name='') {
        if ($action -notin @('list','info','validate')) {throw 'Недопустимая команда ring'}
        $arguments=@($script:prefix)+@('license',$action,'--send-statistics','false')
        if ($action -ne 'list') {
            if ($name -notmatch '^\d{5,30}-\d{5,30}$') {throw 'Некорректное имя лицензии'}
            $arguments+=@('--name',$name)
        }
        if ($p.path) {$arguments+=@('--path',[string]$p.path)}
        Emit 'stage' ('ring license '+$action+' '+$name)
        $result=Native $arguments
        Emit $(if ($result.code -eq 0) {'message'} else {'warning'}) $result.text
        $script:lastLicenseResult=$result
    }
    $names=@($p.names);$listing=''
    if ($names.Count -eq 0) {
        License 'list';$list=$script:lastLicenseResult;$listing=$list.text
        if ($list.code -ne 0) {throw ('ring license list: код '+$list.code+'. '+$list.text)}
        $names=@([regex]::Matches($listing,'(?<![\w-])(\d{5,30}-\d{5,30})(?:\.lic)?(?![\w-])')|ForEach-Object {$_.Groups[1].Value}|Select-Object -Unique)
    }
    if ($names.Count -gt 1000) {throw 'Более 1000 лицензий; укажите отдельный каталог'}
    if (@($p.names).Count -gt 0) {Emit 'stage' ('Проверка по загруженному списку: '+$names.Count)}
    $rows=@();$index=0
    foreach ($name in $names) {
        $info=$null;$check=$null
        if ($p.action -eq 'check' -and $p.info_cache) {
            $property=$p.info_cache.PSObject.Properties[[string]$name]
            if ($property -and $property.Value.code -eq 0 -and $property.Value.text -is [string]) {
                $info=@{code=0;text=[string]$property.Value.text}
            }
        }
        if ($null -eq $info) {License 'info' $name;$info=$script:lastLicenseResult}
        if ($p.action -eq 'check') {License 'validate' $name;$check=$script:lastLicenseResult}
        $row=@{name=$name;info=$info;check=$check};$rows+=@($row)
        @{kind='license_raw_row';row=$row}|ConvertTo-Json -Compress -Depth 8
        $index++;@{kind='progress';percent=100.0*$index/$names.Count;phase='Проверка лицензий';unit='лицензий'}|ConvertTo-Json -Compress
    }
    Done @{rows=@($rows);listing=$listing;checked=($p.action -eq 'check')}
} finally {
    $owned=OwnedRoot
    Remove-Item -LiteralPath $owned -Recurse -Force
}
