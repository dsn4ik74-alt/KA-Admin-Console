param($p)
$ErrorActionPreference='Stop'
function Emit($kind,$text) { @{kind=$kind;text=$text} | ConvertTo-Json -Compress -Depth 12 }
function Done($value) { @{kind='done';data=$value} | ConvertTo-Json -Compress -Depth 16 }
function BackupFile([string]$path) {
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        $backup=$path+'.ka-'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'-'+[Guid]::NewGuid().ToString('N').Substring(0,6)+'.bak'
        Copy-Item -LiteralPath $path -Destination $backup
        Emit 'message' ('Резервная копия: '+$backup)
    }
}
function WriteFile([string]$path,[string]$text) {
    if (-not [IO.Path]::IsPathRooted($path)) {throw 'Требуется абсолютный путь на веб-сервере'}
    $temp=Join-Path ([IO.Path]::GetDirectoryName($path)) ('.ka-config-'+[Guid]::NewGuid().ToString('N'))
    try {
        [IO.File]::WriteAllText($temp,$text,(New-Object Text.UTF8Encoding($false)))
        if ([IO.File]::Exists($path)) {[IO.File]::Replace($temp,$path,$null)} else {[IO.File]::Move($temp,$path)}
    } finally {if ([IO.File]::Exists($temp)) {[IO.File]::Delete($temp)}}
}
function HashFile([string]$path) {(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()}
function ReadFile([string]$path) {
    if ((Get-Item -LiteralPath $path).Length -gt 2097152) {throw 'Конфигурация превышает 2 МБ'}
    [IO.File]::ReadAllText($path)
}
function Platforms {
    $roots=@($env:ProgramFiles,${env:ProgramFiles(x86)}) | Where-Object {$_} | Select-Object -Unique
    @($roots | ForEach-Object {
        Get-ChildItem -LiteralPath (Join-Path $_ '1cv8') -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            $bin=Join-Path $_.FullName 'bin'
            if (Test-Path -LiteralPath (Join-Path $bin 'webinst.exe')) {@{version=$_.Name;bin=$bin}}
        }
    })
}
function Native([string]$tool,[string[]]$arguments) {
    # PowerShell 5 removes embedded quotes when invoking native programs with &.
    # Build the Windows argv explicitly, preserving connection strings and spaces.
    $quoted=@(foreach ($value in $arguments) {
        $escaped=[regex]::Replace([string]$value,'(\\*)"','$1$1\"')
        $escaped=[regex]::Replace($escaped,'(\\+)$','$1$1')
        '"'+$escaped+'"'
    })
    $start=New-Object Diagnostics.ProcessStartInfo
    $start.FileName=$tool;$start.Arguments=$quoted -join ' '
    $start.UseShellExecute=$false;$start.CreateNoWindow=$true
    $start.RedirectStandardOutput=$true;$start.RedirectStandardError=$true
    $process=New-Object Diagnostics.Process
    $process.StartInfo=$start
    try {
        if (-not $process.Start()) {throw 'Не удалось запустить утилиту веб-сервера'}
        $stdout=$process.StandardOutput.ReadToEndAsync()
        $stderr=$process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        foreach ($output in @($stdout.Result,$stderr.Result)) {
            foreach ($line in ($output -split '\r?\n')) {if ($line) {Emit 'message' $line}}
        }
        if ($process.ExitCode -ne 0) {throw (([IO.Path]::GetFileName($tool))+': код '+$process.ExitCode)}
    } finally {$process.Dispose()}
}
function ApacheTest {Native ([string]$p.apache_tool) @('-t','-f',[string]$p.main_conf)}
function ApacheReload {Native ([string]$p.apache_tool) @('-k','restart','-n',[string]$p.apache_service)}
function ApacheFiles {
    $root=Split-Path (Split-Path ([string]$p.apache_tool))
    $queue=New-Object 'Collections.Generic.Queue[string]';$queue.Enqueue([string]$p.main_conf)
    $seen=New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    while ($queue.Count) {
        $file=[IO.Path]::GetFullPath($queue.Dequeue())
        if (-not $seen.Add($file)) {continue}
        if ($seen.Count -gt 500) {throw 'В Apache слишком много включаемых конфигураций'}
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {continue}
        $file
        $content=ReadFile $file
        foreach ($inc in [regex]::Matches($content,'(?im)^\s*Include(?:Optional)?\s+"?([^"\r\n]+)')) {
            $name=$inc.Groups[1].Value.Trim().Replace('${SRVROOT}',$root)
            if (-not [IO.Path]::IsPathRooted($name)) {$name=Join-Path $root $name}
            foreach ($child in @(Get-ChildItem -Path $name -File -ErrorAction SilentlyContinue)) {$queue.Enqueue($child.FullName)}
        }
    }
}

if ($p.action -eq 'install') {
    if ($p.server -ne 'iis') {throw 'Установите Apache для Windows отдельно и укажите путь к httpd.exe'}
    if (Get-Command Install-WindowsFeature -ErrorAction SilentlyContinue) {
        $result=Install-WindowsFeature Web-Server,Web-ISAPI-Ext,Web-ISAPI-Filter,Web-Windows-Auth,Web-Scripting-Tools -IncludeManagementTools
        if (-not $result.Success) {throw 'Установка IIS не подтверждена'}
    } else {
        Enable-WindowsOptionalFeature -Online -FeatureName IIS-WebServerRole,IIS-WebServer,IIS-CommonHttpFeatures,IIS-DefaultDocument,IIS-StaticContent,IIS-HttpErrors,IIS-ApplicationDevelopment,IIS-ISAPIExtensions,IIS-ISAPIFilter,IIS-WindowsAuthentication,IIS-ManagementConsole,IIS-ManagementScriptingTools -All -NoRestart | Out-Null
    }
    Done @{text='IIS установлен. Может потребоваться перезагрузка Windows. Обновите список сайтов.'};return
}
if ($p.server -eq 'iis') {
    Import-Module WebAdministration -ErrorAction Stop
    if ($p.action -notin @('inspect','read_config')) {
        $backupName='KA-'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'-'+[Guid]::NewGuid().ToString('N').Substring(0,6)
        Backup-WebConfiguration -Name $backupName
        Emit 'message' ('Резервная копия конфигурации IIS: '+$backupName)
    }
    if ($p.action -eq 'inspect') {
        $sites=@(Get-Website | ForEach-Object {@{id=$_.Name;number=$_.Id;name=$_.Name;state=[string]$_.State;path=$_.PhysicalPath;bindings=($_.Bindings.Collection | ForEach-Object {$_.protocol+' '+$_.bindingInformation}) -join '; '}})
        $apps=@(foreach ($site in $sites) {Get-WebApplication -Site $site.id | ForEach-Object {@{site=$site.id;name=$_.Path.Trim('/');path=$_.PhysicalPath;pool=$_.applicationPool}}})
        $pools=@(Get-ChildItem IIS:\AppPools | ForEach-Object {@{name=$_.Name;state=[string]$_.state;runtime=$_.managedRuntimeVersion;pipeline=[string]$_.managedPipelineMode;bit32=$_.enable32BitAppOnWin64}})
        $bindings=@(foreach ($site in $sites) {Get-WebBinding -Name $site.id | ForEach-Object {@{site=$site.id;protocol=$_.protocol;binding=$_.bindingInformation}}})
        $certificates=@(Get-ChildItem Cert:\LocalMachine\My | ForEach-Object {@{thumbprint=$_.Thumbprint;subject=$_.Subject;expires=$_.NotAfter.ToString('yyyy-MM-dd')}})
        Done @{sites=$sites;publications=$apps;pools=$pools;bindings=$bindings;certificates=$certificates;platforms=@(Platforms)};return
    }
    if ($p.action -eq 'server_restart') {Restart-Service W3SVC;Done @{text='Служба IIS перезапущена.'};return}
    if ($null -ne $p.site_number -and $p.site) {
        $current=Get-Website -Name ([string]$p.site)
        if (-not $current -or $current.Id -ne $p.site_number) {throw 'Сайт IIS изменился. Обновите список перед действием.'}
    }
    if ($p.action -eq 'site_create') {
        if (Get-Website -Name ([string]$p.site_name) -ErrorAction SilentlyContinue) {throw 'Сайт уже существует'}
        if (-not [IO.Path]::IsPathRooted([string]$p.site_path)) {throw 'Укажите полный путь сайта'}
        [IO.Directory]::CreateDirectory([string]$p.site_path) | Out-Null
        New-Website -Name ([string]$p.site_name) -PhysicalPath ([string]$p.site_path) -Port ([int]$p.port) -HostHeader ([string]$p.hostname) | Out-Null
        Done @{text='Сайт создан. HTTPS можно добавить в привязках.'};return
    }
    if ($p.action -in @('site_start','site_stop','site_delete')) {
        $site=Get-Website -Name ([string]$p.site)
        if (-not $site) {throw 'Сайт не найден'}
        switch ($p.action) {
            site_start {Start-Website -Name $site.Name}
            site_stop {Stop-Website -Name $site.Name}
            site_delete {Remove-Website -Name $site.Name}
        }
        Done @{text='Действие с сайтом завершено. Файлы сайта не удалялись.'};return
    }
    if ($p.action -like 'pool_*') {
        $pool=[string]$p.pool
        if (-not $pool -or $pool -match '[\\/:*?"<>|]') {throw 'Некорректное имя пула'}
        switch ($p.action) {
            pool_create {New-WebAppPool -Name $pool | Out-Null;Set-ItemProperty -LiteralPath ('IIS:\AppPools\'+$pool) -Name managedRuntimeVersion -Value ''}
            pool_start {Start-WebAppPool -Name $pool}
            pool_stop {Stop-WebAppPool -Name $pool}
            pool_recycle {Restart-WebAppPool -Name $pool}
            pool_delete {
                $used=@(Get-Website | Where-Object {$_.applicationPool -eq $pool})
                foreach ($s in Get-Website) {$used+=@(Get-WebApplication -Site $s.Name | Where-Object {$_.applicationPool -eq $pool})}
                if ($used.Count) {throw 'Пул используется сайтом или приложениями; сначала смените их пул'}
                Remove-WebAppPool -Name $pool
            }
        }
        Done @{text='Действие с пулом выполнено.'};return
    }
    if ($p.action -in @('binding_add','binding_delete')) {
        $protocol=[string]$p.protocol;$port=[int]$p.port;$ip=[string]$p.ip
        if (-not $ip) {$ip='*'}
        if ($protocol -notin @('http','https') -or $port -lt 1 -or $port -gt 65535) {throw 'Некорректная привязка'}
        if ($p.action -eq 'binding_delete') {Remove-WebBinding -Name ([string]$p.site) -Protocol $protocol -Port $port -IPAddress $ip -HostHeader ([string]$p.hostname)}
        else {
            if ($protocol -eq 'https' -and -not (Test-Path -LiteralPath ('Cert:\LocalMachine\My\'+[string]$p.certificate))) {throw 'Выберите существующий сертификат LocalMachine\My'}
            $sni=0;if ($protocol -eq 'https' -and $p.hostname) {$sni=1}
            New-WebBinding -Name ([string]$p.site) -Protocol $protocol -Port $port -IPAddress $ip -HostHeader ([string]$p.hostname) -SslFlags $sni
            if ($protocol -eq 'https') {
                try {(Get-WebBinding -Name ([string]$p.site) -Protocol https -Port $port -IPAddress $ip -HostHeader ([string]$p.hostname)).AddSslCertificate([string]$p.certificate,'My')}
                catch {Remove-WebBinding -Name ([string]$p.site) -Protocol https -Port $port -IPAddress $ip -HostHeader ([string]$p.hostname);throw}
            }
        }
        Done @{text='Привязка сайта изменена.'};return
    }
}

if ($p.action -in @('read_config','save_config')) {
    $path=[string]$p.config_path
    if ($p.config_kind -eq 'iis') {$path=Join-Path $env:windir 'System32\inetsrv\config\applicationHost.config'}
    if (-not [IO.Path]::IsPathRooted($path)) {throw 'Требуется абсолютный путь конфигурации'}
    $text=ReadFile $path;$hash=HashFile $path
    if ($p.action -eq 'read_config') {Done @{text=$text;etag=$hash;path=$path};return}
    if ($hash -ne $p.etag) {throw 'Файл изменился на сервере. Прочитайте его заново.'}
    if ($p.config_kind -in @('vrd','web','iis')) {
        if ([string]$p.text -match '<!DOCTYPE|<!ENTITY') {throw 'DTD не поддерживается'}
        try {$xml=New-Object Xml.XmlDocument;$xml.XmlResolver=$null;$xml.LoadXml([string]$p.text)} catch {throw 'Некорректный XML'}
    }
    BackupFile $path
    try {
        WriteFile $path ([string]$p.text)
        if ($p.server -eq 'iis') {Get-Website | Out-Null}
        else {ApacheTest | ForEach-Object {$_}}
    } catch {WriteFile $path $text;throw}
    if ($p.server -eq 'apache') {ApacheReload | ForEach-Object {$_}}
    Done @{text='Файл сохранён и проверен.'};return
}

if ($p.server -eq 'iis') {
    $site=Get-Website -Name ([string]$p.site)
    if (-not $site) {throw 'Выберите существующий сайт IIS'}
    $alias=[string]$p.alias
    if ($alias -notmatch '^[\w-]{1,80}$') {throw 'Некорректное имя публикации'}
    $existing=Get-WebApplication -Site $site.Name | Where-Object {$_.Path -eq '/'+$alias}
    if ($p.action -eq 'unpublish') {
        if (-not $existing) {throw 'Публикация не найдена'}
        Remove-WebApplication -Site $site.Name -Name $alias
        Done @{text='Публикация снята. Файлы и база данных не удалялись.'};return
    }
    if ($p.action -ne 'publish') {throw 'Неизвестная операция IIS'}
    if ($existing -and -not $p.replace) {throw 'Публикация уже существует. Включите замену.'}
    $module=Join-Path ([string]$p.platform) 'wsisapi.dll'
    if (-not (Test-Path -LiteralPath $module -PathType Leaf)) {throw 'В выбранной платформе нет wsisapi.dll. Установите модуль расширения веб-сервера этой версии.'}
    $directory=[string]$p.directory
    if (-not [IO.Path]::IsPathRooted($directory) -or [IO.Path]::GetPathRoot($directory).TrimEnd('\') -eq $directory.TrimEnd('\')) {throw 'Укажите отдельный каталог публикации'}
    if ($existing -and [IO.Path]::GetFullPath($existing.PhysicalPath).TrimEnd('\') -ne [IO.Path]::GetFullPath($directory).TrimEnd('\')) {throw 'Для обновления используйте каталог существующей публикации или сначала снимите её'}
    [IO.Directory]::CreateDirectory($directory) | Out-Null
    $vrd=Join-Path $directory 'default.vrd';$web=Join-Path $directory 'web.config'
    if ((Test-Path -LiteralPath $vrd) -and -not $p.replace) {throw 'В каталоге уже есть default.vrd. Включите замену.'}
    $oldVrd=$null;$oldWeb=$null
    if (Test-Path -LiteralPath $vrd) {$oldVrd=ReadFile $vrd;BackupFile $vrd}
    if (Test-Path -LiteralPath $web) {$oldWeb=ReadFile $web;BackupFile $web}
    $pool=[string]$p.pool
    if (-not $pool) {$pool='KA-1C-'+([IO.Path]::GetFileName((Split-Path ([string]$p.platform)))).Replace('.','_')}
    $reader=[IO.BinaryReader]::new([IO.File]::OpenRead($module))
    try {$reader.BaseStream.Position=60;$pe=$reader.ReadInt32();$reader.BaseStream.Position=$pe+4;$x86=($reader.ReadUInt16() -eq 332)} finally {$reader.Close()}
    if (-not (Test-Path -LiteralPath ('IIS:\AppPools\'+$pool))) {
        New-WebAppPool -Name $pool | Out-Null
        Set-ItemProperty -LiteralPath ('IIS:\AppPools\'+$pool) -Name managedRuntimeVersion -Value ''
        Set-ItemProperty -LiteralPath ('IIS:\AppPools\'+$pool) -Name enable32BitAppOnWin64 -Value $x86
    } elseif ([bool](Get-Item -LiteralPath ('IIS:\AppPools\'+$pool)).enable32BitAppOnWin64 -ne $x86) {throw 'Разрядность выбранного пула не совпадает с wsisapi.dll. Выберите отдельный совместимый пул.'}
    $created=$false
    try {
        $xml=New-Object Xml.XmlDocument;$xml.XmlResolver=$null
        if ($oldWeb) {$xml.LoadXml($oldWeb)} else {$xml.LoadXml('<configuration><system.webServer><handlers /></system.webServer></configuration>')}
        $system=$xml.SelectSingleNode('/configuration/system.webServer');if (-not $system) {$system=$xml.CreateElement('system.webServer');[void]$xml.DocumentElement.AppendChild($system)}
        $handlers=$system.SelectSingleNode('handlers');if (-not $handlers) {$handlers=$xml.CreateElement('handlers');[void]$system.AppendChild($handlers)}
        foreach ($node in @($handlers.SelectNodes("add[@name='KA-1C']"))) {[void]$handlers.RemoveChild($node)}
        $handler=$xml.CreateElement('add')
        foreach ($entry in @{name='KA-1C';path='*';verb='*';modules='IsapiModule';scriptProcessor=$module;resourceType='Unspecified';requireAccess='None'}.GetEnumerator()) {$handler.SetAttribute($entry.Key,$entry.Value)}
        [void]$handlers.AppendChild($handler)
        WriteFile $vrd ([string]$p.vrd);WriteFile $web $xml.OuterXml
        $restriction=@(Get-WebConfiguration -PSPath 'MACHINE/WEBROOT/APPHOST' -Filter 'system.webServer/security/isapiCgiRestriction/add' | Where-Object {$_.path -eq $module})
        if (-not $restriction.Count) {Add-WebConfigurationProperty -PSPath 'MACHINE/WEBROOT/APPHOST' -Filter 'system.webServer/security/isapiCgiRestriction' -Name '.' -Value @{path=$module;allowed=$true;description='1C '+$p.platform}}
        if (-not $existing) {New-WebApplication -Site $site.Name -Name $alias -PhysicalPath $directory -ApplicationPool $pool | Out-Null;$created=$true}
        else {Set-ItemProperty -LiteralPath ('IIS:\Sites\'+$site.Name+'\'+$alias) -Name applicationPool -Value $pool}
        $location=$site.Name+'/'+$alias
        Set-WebConfigurationProperty -PSPath 'MACHINE/WEBROOT/APPHOST' -Location $location -Filter 'system.webServer/security/authentication/anonymousAuthentication' -Name enabled -Value (-not [bool]$p.os_auth)
        if ($p.os_auth) {Set-WebConfigurationProperty -PSPath 'MACHINE/WEBROOT/APPHOST' -Location $location -Filter 'system.webServer/security/authentication/windowsAuthentication' -Name enabled -Value $true}
        $acl=Get-Acl -LiteralPath $directory
        $rule=New-Object Security.AccessControl.FileSystemAccessRule(('IIS AppPool\'+$pool),'ReadAndExecute','ContainerInherit,ObjectInherit','None','Allow')
        $acl.AddAccessRule($rule);Set-Acl -LiteralPath $directory -AclObject $acl
        Get-WebConfiguration -PSPath ('IIS:\Sites\'+$site.Name+'\'+$alias) -Filter 'system.webServer/handlers' | Out-Null
    } catch {
        if ($created) {Remove-WebApplication -Site $site.Name -Name $alias -ErrorAction SilentlyContinue}
        if ($null -ne $oldVrd) {WriteFile $vrd $oldVrd} elseif (Test-Path -LiteralPath $vrd) {[IO.File]::Delete($vrd)}
        if ($null -ne $oldWeb) {WriteFile $web $oldWeb} elseif (Test-Path -LiteralPath $web) {[IO.File]::Delete($web)}
        throw
    }
    Done @{text=('Опубликовано на сайте '+$site.Name+': /'+$alias)};return
}

# Apache for Windows: enumerate explicitly selected configuration and VirtualHost blocks.
$conf=[string]$p.main_conf
if (-not (Test-Path -LiteralPath $conf -PathType Leaf)) {throw 'Укажите существующий конфигурационный файл Apache на веб-сервере'}
$text=ReadFile $conf
if ($p.action -eq 'server_restart') {ApacheTest | ForEach-Object {$_};ApacheReload | ForEach-Object {$_};Done @{text='Apache перезапущен.'};return}
$pattern='(?is)<VirtualHost\s+([^>]+)>(.*?)</VirtualHost\s*>'
$virtualHosts=[regex]::Matches($text,$pattern)
if ($p.action -eq 'inspect') {
    $sites=@();$apps=@()
    foreach ($siteFile in @(ApacheFiles)) {
      $fileText=ReadFile $siteFile
      $virtualHosts=[regex]::Matches($fileText,$pattern)
      for ($i=0;$i -lt $virtualHosts.Count;$i++) {
        $body=$virtualHosts[$i].Groups[2].Value
        $name=[regex]::Match($body,'(?im)^\s*ServerName\s+([^\s#]+)').Groups[1].Value
        if (-not $name) {$name='VirtualHost '+$i}
        $siteId=$siteFile+'#'+$i
        $sites+=@{id=$siteId;name=$name;bindings=$virtualHosts[$i].Groups[1].Value;state='Конфигурация';path='';config_path=$siteFile;index=$i;etag=(HashFile $siteFile)}
        foreach ($m in [regex]::Matches($body,'(?s)# KA-1C ([\w-]+) BEGIN\r?\n(.*?)# KA-1C \1 END')) {
            $dir=[regex]::Match($m.Groups[2].Value,'(?i)ManagedApplicationDescriptor\s+"([^"]+)/default\.vrd"').Groups[1].Value
            $apps+=@{site=$siteId;name=$m.Groups[1].Value;path=$dir;pool=''}
        }
        foreach ($m in [regex]::Matches($body,'(?im)^\s*Alias\s+"?(/[^"\s]+)"?\s+"([^"\r\n]+)"')) {
            $name=$m.Groups[1].Value.Trim('/')
            if (-not @($apps | Where-Object {$_.site -eq $siteId -and $_.name -eq $name}).Count) {$apps+=@{site=$siteId;name=$name;path=$m.Groups[2].Value.TrimEnd('/');pool=''}}
        }
      }
    }
    Done @{sites=$sites;publications=$apps;pools=@();certificates=@();platforms=@(Platforms)};return
}
if ($p.action -eq 'site_create') {
    if ([string]$p.hostname -notmatch '^[A-Za-z0-9_.-]+$') {throw 'Укажите доменное имя сайта'}
    [IO.Directory]::CreateDirectory([string]$p.site_path) | Out-Null
    BackupFile $conf
    $extra="`r`n<VirtualHost *:$([int]$p.port)>`r`n ServerName $($p.hostname)`r`n DocumentRoot `"$(([string]$p.site_path).Replace('\','/'))`"`r`n</VirtualHost>`r`n"
    try {WriteFile $conf ($text+$extra);ApacheTest | ForEach-Object {$_}} catch {WriteFile $conf $text;throw}
    ApacheReload | ForEach-Object {$_};Done @{text='VirtualHost добавлен.'};return
}
$index=0
$hashIndex=([string]$p.site).LastIndexOf('#')
if ($hashIndex -lt 1) {throw 'Выберите VirtualHost из списка'}
$conf=([string]$p.site).Substring(0,$hashIndex)
if (@(ApacheFiles) -notcontains $conf) {throw 'Файл сайта отсутствует среди включённых конфигураций Apache'}
$text=ReadFile $conf;$virtualHosts=[regex]::Matches($text,$pattern)
if ($p.site_etag -and (HashFile $conf) -ne $p.site_etag) {throw 'Конфигурация сайта изменилась. Обновите список.'}
if (-not [int]::TryParse(([string]$p.site).Substring($hashIndex+1),[ref]$index) -or $index -lt 0 -or $index -ge $virtualHosts.Count) {throw 'Выберите VirtualHost из списка'}
$match=$virtualHosts[$index]
if ($p.action -notin @('publish','unpublish')) {throw 'Для Apache Windows измените VirtualHost через редактор конфигурации. Запуск отдельных сайтов не поддерживается.'}
$alias=[string]$p.alias
$marker='(?s)\r?\n?# KA-1C '+[regex]::Escape($alias)+' BEGIN\r?\n.*?# KA-1C '+[regex]::Escape($alias)+' END\r?\n?'
$found=[regex]::IsMatch($match.Groups[2].Value,$marker)
if ($p.action -eq 'unpublish' -and -not $found) {throw 'Публикация не создана консолью; используйте редактор конфигурации'}
if ($p.action -eq 'publish' -and $found -and -not $p.replace) {throw 'Публикация существует. Включите замену.'}
if ($p.action -eq 'publish') {
    $expectedModule=[IO.Path]::GetFullPath((Join-Path ([string]$p.platform) 'wsap24.dll')).Replace('/','\')
    foreach ($file in @(ApacheFiles)) {
        $content=ReadFile $file
        if ($file -eq $conf) {$content=[regex]::Replace($content,$marker,'')}
        foreach ($loaded in [regex]::Matches($content,'(?im)^\s*LoadModule\s+\S+\s+"?([^"\r\n]*wsap24\.dll)"?')) {
            $modulePath=$loaded.Groups[1].Value.Trim().Replace('/','\')
            if (-not [IO.Path]::IsPathRooted($modulePath)) {$modulePath=Join-Path (Split-Path (Split-Path ([string]$p.apache_tool))) $modulePath}
            if ([IO.Path]::GetFullPath($modulePath) -ne $expectedModule) {throw 'В Apache загружен модуль другой версии 1С. Используйте совместимую платформу или отдельный экземпляр Apache.'}
        }
    }
}
$fragment='';$root=$null;$vrdPath=$null;$previous=$null
BackupFile $conf
try {
    if ($p.action -eq 'publish') {
        $directory=[string]$p.directory
        [IO.Directory]::CreateDirectory($directory) | Out-Null
        $vrdPath=Join-Path $directory 'default.vrd'
        if (Test-Path -LiteralPath $vrdPath) {if (-not $p.replace) {throw 'default.vrd уже существует'};$previous=ReadFile $vrdPath;BackupFile $vrdPath}
        $root=Join-Path $env:TEMP ('ka-publish-'+[Guid]::NewGuid().ToString('N'))
        [IO.Directory]::CreateDirectory($root) | Out-Null
        $stage=Join-Path $root 'publication.conf';$vrd=Join-Path $root 'template.vrd'
        WriteFile $stage '';WriteFile $vrd ([string]$p.vrd)
        Native (Join-Path ([string]$p.platform) 'webinst.exe') @('-publish','-apache24','-wsdir',$alias,'-dir',$directory,'-connstr',[string]$p.connection,'-descriptor',$vrd,'-confpath',$stage) | ForEach-Object {$_}
        $fragment=ReadFile $stage
        if ($fragment -notmatch 'ManagedApplicationDescriptor') {throw 'webinst не создал описание Apache'}
        $fragment="`r`n# KA-1C $alias BEGIN`r`n"+$fragment+"`r`n# KA-1C $alias END`r`n"
    }
    $body=[regex]::Replace($match.Groups[2].Value,$marker,'')+$fragment
    $updated=$text.Substring(0,$match.Index)+'<VirtualHost '+$match.Groups[1].Value+'>'+$body+'</VirtualHost>'+$text.Substring($match.Index+$match.Length)
    try {WriteFile $conf $updated;ApacheTest | ForEach-Object {$_}} catch {WriteFile $conf $text;throw}
    ApacheReload | ForEach-Object {$_}
    Done @{text=('Действие с публикацией завершено: /'+$alias)}
} catch {
    if ($null -ne $previous) {WriteFile $vrdPath $previous}
    throw
} finally {
    if ($root) {
        $resolved=[IO.Path]::GetFullPath($root)
        $tempRoot=[IO.Path]::GetFullPath($env:TEMP).TrimEnd('\')+'\'
        if ($resolved.StartsWith($tempRoot,[StringComparison]::OrdinalIgnoreCase) -and [IO.Path]::GetFileName($resolved) -match '^ka-publish-[0-9a-f]{32}$') {Remove-Item -LiteralPath $resolved -Recurse -Force}
    }
}
