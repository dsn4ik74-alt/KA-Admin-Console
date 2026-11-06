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
        if ([IO.File]::Exists($path)) {[IO.File]::Replace($temp,$path,[NullString]::Value)} else {[IO.File]::Move($temp,$path)}
    } finally {if ([IO.File]::Exists($temp)) {[IO.File]::Delete($temp)}}
}
function HashFile([string]$path) {(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()}
function ReadFile([string]$path) {
    if ((Get-Item -LiteralPath $path).Length -gt 2097152) {throw 'Конфигурация превышает 2 МБ'}
    [IO.File]::ReadAllText($path)
}
function XmlFile([string]$path) {
    $text=ReadFile $path
    if ($text -match '<!DOCTYPE|<!ENTITY') {throw 'DTD и внешние сущности не поддерживаются'}
    $xml=New-Object Xml.XmlDocument;$xml.XmlResolver=$null;$xml.PreserveWhitespace=$true
    $xml.LoadXml($text)
    return ,$xml
}
function XmlFlag($node,[string]$name,[bool]$default) {
    if ($null -eq $node -or -not $node.HasAttribute($name)) {return $default}
    return $node.GetAttribute($name) -in @('true','1')
}
function FormattedXml($xml) {
    $settings=New-Object Xml.XmlWriterSettings
    $settings.Encoding=New-Object Text.UTF8Encoding($false)
    $settings.Indent=$true;$settings.IndentChars='    ';$settings.NewLineOnAttributes=$true
    $settings.NewLineChars="`n";$settings.NewLineHandling=[Xml.NewLineHandling]::None
    $memory=New-Object IO.MemoryStream
    $writer=[Xml.XmlWriter]::Create($memory,$settings)
    try {$xml.Save($writer);$writer.Flush();return [Text.Encoding]::UTF8.GetString($memory.ToArray())+"`n"}
    finally {$writer.Dispose();$memory.Dispose()}
}
function VrdDetails([string]$path) {
    if ((Get-Item -LiteralPath $path).Length -gt 2097152) {throw 'Конфигурация превышает 2 МБ'}
    $raw=[IO.File]::ReadAllBytes($path)
    if ($raw.Length -gt 2097152) {throw 'Конфигурация превышает 2 МБ'}
    $memory=[IO.MemoryStream]::new($raw,$false);$reader=[IO.StreamReader]::new($memory,[Text.Encoding]::UTF8,$true)
    try {$text=$reader.ReadToEnd()} finally {$reader.Dispose()}
    if ($text -match '<!DOCTYPE|<!ENTITY') {throw 'DTD и внешние сущности не поддерживаются'}
    $xml=New-Object Xml.XmlDocument;$xml.XmlResolver=$null;$xml.PreserveWhitespace=$true;$xml.LoadXml($text);$root=$xml.DocumentElement
    $sha=[Security.Cryptography.SHA256]::Create()
    try {$snapshotHash=[BitConverter]::ToString($sha.ComputeHash($raw)).Replace('-','').ToLowerInvariant()} finally {$sha.Dispose()}
    if ($root.LocalName -ne 'point' -or $root.NamespaceURI -ne 'http://v8.1c.ru/8.2/virtual-resource-system') {throw 'Файл не является описанием публикации 1С'}
    $ns=New-Object Xml.XmlNamespaceManager($xml.NameTable);$ns.AddNamespace('v',$root.NamespaceURI)
    $ws=$root.SelectSingleNode('v:ws',$ns);$hs=$root.SelectSingleNode('v:httpServices',$ns)
    $rules=New-Object 'Collections.Generic.List[object]'
    function AddRule($id,$kind,$name,$node,$attr,$default,$parent,$address) {
        $enabled=XmlFlag $node $attr $default
        [void]$rules.Add(@{id=$id;kind=$kind;name=$name;address=$address;enabled=[bool]$enabled;effective=([bool]$enabled -and [bool]$parent);explicit=($null -ne $node -and $node.HasAttribute($attr))})
    }
    $wsEnabled=$null -ne $ws -and (XmlFlag $ws 'enable' $true)
    AddRule 'ws/enabled' 'WS' 'Разрешить Web-сервисы' $ws 'enable' ($null -ne $ws) $true ''
    AddRule 'ws/default' 'WS' 'Сервисы без явного разрешения' $ws 'pointEnableCommon' $true $wsEnabled ''
    AddRule 'ws/extensions' 'WS' 'Сервисы расширений по умолчанию' $ws 'publishExtensionsByDefault' $false $wsEnabled ''
    if ($null -ne $ws) {
        $index=0
        foreach ($node in $ws.SelectNodes('v:point',$ns)) {
            $alias=$node.GetAttribute('alias');if (-not $alias) {$alias=$node.GetAttribute('name')}
            AddRule ('ws/point/'+$index) 'WS' ($node.GetAttribute('name')) $node 'enable' (XmlFlag $ws 'pointEnableCommon' $true) $wsEnabled ('ws/'+[Uri]::EscapeDataString($alias))
            $index++
        }
    }
    AddRule 'hs/default' 'HS' 'HTTP-сервисы по умолчанию' $hs 'publishByDefault' ($null -ne $hs) $true ''
    AddRule 'hs/extensions' 'HS' 'HTTP-сервисы расширений по умолчанию' $hs 'publishExtensionsByDefault' $false $true ''
    if ($null -ne $hs) {
        $index=0
        foreach ($node in $hs.SelectNodes('v:service',$ns)) {
            $url=$node.GetAttribute('rootUrl');$address=if ($url) {'hs/'+[Uri]::EscapeDataString($url)} else {''}
            AddRule ('hs/service/'+$index) 'HS' ($node.GetAttribute('name')) $node 'enable' $false $true $address
            $index++
        }
    }
    $info=@{base_url=$root.GetAttribute('base');web_enabled=(XmlFlag $root 'enable' $true);services=@($rules.ToArray());etag=$snapshotHash}
    $pairs=@{}
    foreach ($match in [regex]::Matches($root.GetAttribute('ib'),'(?:^|;)\s*([\w-]+)\s*=\s*(?:"((?:[^"]|"")*)"|([^;]*))')) {
        $value=$match.Groups[3].Value.Trim()
        if ($match.Groups[2].Success) {$value=$match.Groups[2].Value.Replace('""','"')}
        $pairs[$match.Groups[1].Value]=$value
    }
    $info.cluster='';$info.base='';$info.file_path='';$info.connection='';$info.base_type='Не определён'
    if ($pairs.ContainsKey('file')) {$info.file_path=$pairs.file;$info.base_type='Файловая';$info.connection='File="'+$pairs.file.Replace('"','""')+'";'}
    elseif ($pairs.ContainsKey('srvr')) {
        $info.cluster=$pairs.srvr;$info.base=[string]$pairs.ref;$info.base_type='Серверная'
        $info.connection='Srvr="'+$pairs.srvr.Replace('"','""')+'";Ref="'+([string]$pairs.ref).Replace('"','""')+'";'
    }
    return @{xml=$xml;ns=$ns;info=$info}
}
function WebModule([string]$directory) {
    $path=Join-Path $directory 'web.config'
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        $xml=XmlFile $path
        foreach ($node in $xml.SelectNodes('/configuration/system.webServer/handlers/add')) {
            $module=$node.GetAttribute('scriptProcessor')
            if ($module -match '(?i)wsisapi\.dll') {return ($module -split '\|')[0]}
        }
    }
    return ''
}
$script:ModuleVersions=@{}
$script:ModuleVersionSources=@{}
function PublicationInfo($row,[bool]$details=$false) {
    $copy=@{};foreach ($key in $row.Keys) {$copy[$key]=$row[$key]}
    $vrd=[string]$copy.vrd_path;if (-not $vrd) {$vrd=Join-Path ([string]$copy.path) 'default.vrd'}
    $copy.vrd_path=$vrd;$copy.is_1c=Test-Path -LiteralPath $vrd -PathType Leaf
    $module=[string]$copy.module_path
    if (-not $module -and $p.server -eq 'iis') {
        try {$module=WebModule ([string]$copy.path)} catch {$copy.metadata_error=$_.Exception.Message}
    }
    if (-not $module -and $copy.is_1c -and $row.site -and $p.server -eq 'iis') {
        try {
            $location=([string]$row.site)+'/'+([string]$row.name)
            $handler=@(Get-WebConfiguration -PSPath 'MACHINE/WEBROOT/APPHOST' -Location $location -Filter 'system.webServer/handlers/add' | Where-Object {$_.scriptProcessor -match '(?i)wsisapi\.dll'} | Select-Object -First 1)
            if ($handler.Count) {$module=([string]$handler[0].scriptProcessor -split '\|')[0]}
        } catch {$copy.metadata_error=$_.Exception.Message}
    }
    if ($module) {$module=[Environment]::ExpandEnvironmentVariables($module.Trim().Trim('"'))}
    $copy.module_path=$module;$copy.version='';$copy.version_source=''
    if ($module) {
        if (-not $script:ModuleVersions.ContainsKey($module)) {
            $version=''
            if (Test-Path -LiteralPath $module -PathType Leaf) {$version=[Diagnostics.FileVersionInfo]::GetVersionInfo($module).FileVersion}
            $match=[regex]::Match($version,'(?<!\d)\d+\.\d+\.\d+\.\d+(?!\d)')
            $script:ModuleVersionSources[$module]='file'
            if (-not $match.Success) {$match=[regex]::Match($module,'(?<!\d)\d+\.\d+\.\d+\.\d+(?!\d)');$script:ModuleVersionSources[$module]='path'}
            $script:ModuleVersions[$module]=$match.Value
        }
        $copy.version=$script:ModuleVersions[$module]
        $copy.version_source=$script:ModuleVersionSources[$module]
    }
    if ($copy.is_1c) {
        try {
            $data=VrdDetails $vrd
            foreach ($key in $data.info.Keys) {if ($details -or $key -ne 'services') {$copy[$key]=$data.info[$key]}}
        } catch {$copy.metadata_error=$_.Exception.Message}
    }
    return $copy
}
function IisEntries($site) {
    $known=@{};$poolStates=@{};$entries=New-Object 'Collections.Generic.List[object]'
    $contexts=New-Object 'Collections.Generic.List[object]'
    $rootPool=[string](Get-Website -Name $site.id).applicationPool
    [void]$contexts.Add(@{alias='';path=[Environment]::ExpandEnvironmentVariables([string]$site.path);pool=$rootPool})
    function AddEntry($alias,$path,$pool,$type,$rootEntry=$false) {
        $alias=([string]$alias).Trim('/')
        if ($known.ContainsKey($alias)) {return}
        $known[$alias]=$true
        $path=[Environment]::ExpandEnvironmentVariables([string]$path)
        $state=[string]$site.state
        if ($state -eq 'Started' -and $pool) {
            if (-not $poolStates.ContainsKey($pool)) {
                try {$poolStates[$pool]=[string](Get-WebAppPoolState -Name $pool).Value} catch {$poolStates[$pool]='Не определено'}
            }
            $state=$poolStates[$pool]
        }
        [void]$entries.Add(@{name=$alias;site=$site.id;path=$path;pool=$pool;state=$state;entry_type=$type;root=$rootEntry})
    }
    if (Test-Path -LiteralPath (Join-Path $contexts[0].path 'default.vrd') -PathType Leaf) {AddEntry '' $contexts[0].path $rootPool 'application' $true}
    foreach ($app in @(Get-WebApplication -Site $site.id)) {
        $alias=([string]$app.Path).Trim('/');AddEntry $alias $app.PhysicalPath $app.applicationPool 'application'
        [void]$contexts.Add(@{alias=$alias;path=[Environment]::ExpandEnvironmentVariables([string]$app.PhysicalPath);pool=[string]$app.applicationPool})
    }
    foreach ($context in @($contexts.ToArray())) {
        $application='/'+$context.alias
        foreach ($vdir in @(Get-WebVirtualDirectory -Site $site.id -Application $application)) {
            if ([string]$vdir.path -eq '/') {continue}
            $alias=($context.alias+'/'+([string]$vdir.path).Trim('/')).Trim('/')
            AddEntry $alias $vdir.physicalPath $context.pool 'virtual_directory'
            $entry=@($entries | Where-Object {$_.name -eq $alias})[0]
            $entry.parent_application=$application;$entry.vdir_name=([string]$vdir.path).Trim('/')
            [void]$contexts.Add(@{alias=$alias;path=[Environment]::ExpandEnvironmentVariables([string]$vdir.physicalPath);pool=$context.pool})
        }
    }
    foreach ($context in @($contexts.ToArray())) {
        foreach ($folder in @(Get-ChildItem -LiteralPath $context.path -Directory -ErrorAction SilentlyContinue | Select-Object -First 5000)) {
            AddEntry (($context.alias+'/'+$folder.Name).Trim('/')) $folder.FullName $context.pool 'folder'
        }
    }
    return @($entries.ToArray())
}
function SelectedPublication {
    $site=Get-Website -Name ([string]$p.site)
    if (-not $site) {throw 'Сайт не найден. Обновите список.'}
    $row=@(IisEntries @{id=$site.Name;path=$site.PhysicalPath;state=[string]$site.State} | Where-Object {$_.name -eq [string]$p.alias})
    if ($row.Count -ne 1 -or [IO.Path]::GetFullPath([string]$row[0].path).TrimEnd('\') -ne [IO.Path]::GetFullPath([string]$p.directory).TrimEnd('\')) {throw 'Публикация изменилась. Обновите список.'}
    return $row[0]
}
function PublicationBrowse($row) {
    $root=[IO.Path]::GetFullPath([string]$row.path).TrimEnd('\')
    $target=[string]$p.browse_path;if (-not $target) {$target=$root}
    $target=[IO.Path]::GetFullPath($target).TrimEnd('\')
    if ($target -ne $root -and -not $target.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase)) {throw 'Каталог находится вне выбранной публикации'}
    $item=Get-Item -LiteralPath $target -Force
    if (-not $item.PSIsContainer) {throw 'Это не каталог'}
    for ($cursor=$item;$cursor.FullName.TrimEnd('\') -ne $root;$cursor=$cursor.Parent) {
        if ($null -eq $cursor -or ($cursor.Attributes -band [IO.FileAttributes]::ReparsePoint)) {throw 'Переход по ссылкам за пределы каталога недоступен'}
    }
    $all=@(Get-ChildItem -LiteralPath $target -Force | Select-Object -First 10001)
    $rows=@($all | Select-Object -First 10000 | ForEach-Object {@{name=$_.Name;path=$_.FullName;directory=[bool]$_.PSIsContainer;link=[bool]($_.Attributes -band [IO.FileAttributes]::ReparsePoint);size=if ($_.PSIsContainer) {0} else {$_.Length};modified=$_.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')}})
    return @{path=$target;root=$root;entries=$rows;truncated=($all.Count -gt 10000)}
}
function ServiceUpdate($row) {
    $vrd=[string]$row.vrd_path;if (-not $vrd) {$vrd=Join-Path ([string]$row.path) 'default.vrd'}
    if ((HashFile $vrd) -ne [string]$p.etag) {throw 'default.vrd изменился. Обновите свойства перед применением.'}
    $data=VrdDetails $vrd;$xml=$data.xml;$root=$xml.DocumentElement;$ns=$data.ns;$known=@{}
    if ($data.info.etag -ne [string]$p.etag) {throw 'default.vrd изменился. Обновите свойства перед применением.'}
    foreach ($rule in $data.info.services) {$known[$rule.id]=$true}
    foreach ($change in @($p.changes)) {
        if (-not $known.ContainsKey([string]$change.id)) {throw 'Настройки сервиса изменились. Обновите свойства.'}
        $known.Remove([string]$change.id)
        $parts=([string]$change.id).Split('/');$tag=if ($parts[0] -eq 'ws') {'ws'} else {'httpServices'}
        $parent=$root.SelectSingleNode('v:'+$tag,$ns)
        if ($null -eq $parent) {$parent=$xml.CreateElement($tag,$root.NamespaceURI);[void]$root.AppendChild($parent)}
        $node=$parent;$attr=''
        switch ($parts[1]) {
            enabled {$attr='enable'}
            'default' {$attr=if ($parts[0] -eq 'ws') {'pointEnableCommon'} else {'publishByDefault'}}
            extensions {$attr='publishExtensionsByDefault'}
            point {$node=$parent.SelectNodes('v:point',$ns)[[int]$parts[2]];$attr='enable'}
            service {$node=$parent.SelectNodes('v:service',$ns)[[int]$parts[2]];$attr='enable'}
            default {throw 'Неизвестная настройка сервиса'}
        }
        $node.SetAttribute($attr,([bool]$change.enabled).ToString().ToLowerInvariant())
    }
    if ($xml.FirstChild -is [Xml.XmlDeclaration]) {$xml.FirstChild.Encoding='utf-8'}
    if ((HashFile $vrd) -ne $data.info.etag) {throw 'default.vrd изменился во время подготовки. Обновите свойства.'}
    BackupFile $vrd
    $previous=ReadFile $vrd
    try {
        WriteFile $vrd (FormattedXml $xml)
        [void](VrdDetails $vrd)
        if ($p.server -eq 'apache') {ApacheTest | ForEach-Object {$_};ApacheReload | ForEach-Object {$_}}
    } catch {WriteFile $vrd $previous;if ($p.server -eq 'apache') {try {ApacheReload | ForEach-Object {$_}} catch {}};throw}
    $info=PublicationInfo $row $true;$info.text='Настройки WS/HS сохранены.';Done $info
}
function ApacheEntries([string]$body,[string]$siteId,[string]$state,[string[]]$modules) {
    foreach ($match in [regex]::Matches($body,'(?im)^[ \t]*Alias[ \t]+"?(/[^"\s]+)"?[ \t]+"?([^"\r\n]+?)"?[ \t]*$')) {
        $name=$match.Groups[1].Value.Trim('/');$directory=$match.Groups[2].Value.Trim().TrimEnd('/','\')
        $vrd='';$module=''
        foreach ($block in [regex]::Matches($body,'(?is)<Directory\s+"?([^">]+)"?\s*>(.*?)</Directory\s*>')) {
            if ($block.Groups[1].Value.Trim().Trim('"').TrimEnd('/','\') -eq $directory) {
                $found=[regex]::Match($block.Groups[2].Value,'(?i)ManagedApplicationDescriptor\s+"?([^"\r\n]+)')
                if ($found.Success) {$vrd=$found.Groups[1].Value.Trim()}
            }
        }
        if ($modules.Count -eq 1) {$module=$modules[0]}
        $type=if ($vrd) {'application'} else {'virtual_directory'}
        if (-not $vrd) {$vrd=Join-Path $directory 'default.vrd'}
        @{name=$name;site=$siteId;path=$directory;pool='';state=$state;entry_type=$type;vrd_path=$vrd;module_path=$module}
    }
}
function OpenPoolManager {
    if (-not ('Microsoft.Web.Administration.ServerManager' -as [type])) {
        Add-Type -Path (Join-Path $env:windir 'System32\inetsrv\Microsoft.Web.Administration.dll')
    }
    return New-Object Microsoft.Web.Administration.ServerManager
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
    if ($p.action -notin @('inspect','read_config','publication_properties','publication_browse')) {
        $backupName='KA-'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'-'+[Guid]::NewGuid().ToString('N').Substring(0,6)
        Backup-WebConfiguration -Name $backupName
        Emit 'message' ('Резервная копия конфигурации IIS: '+$backupName)
    }
    if ($p.action -eq 'inspect') {
        $sites=@(Get-Website | ForEach-Object {
            $endpoints=@(foreach ($binding in $_.Bindings.Collection) {
                if ([string]$binding.bindingInformation -match '^(.*):(\d+):(.*)$') {@{protocol=[string]$binding.protocol;ip=$Matches[1];port=[int]$Matches[2];host=$Matches[3]}}
            })
            @{id=$_.Name;number=$_.Id;name=$_.Name;state=[string]$_.State;path=$_.PhysicalPath;endpoints=$endpoints;bindings=($_.Bindings.Collection | ForEach-Object {$_.protocol+' '+$_.bindingInformation}) -join '; '}
        })
        $apps=@(foreach ($site in $sites) {foreach ($entry in @(IisEntries $site)) {PublicationInfo $entry}})
        $pools=@(Get-ChildItem IIS:\AppPools | ForEach-Object {@{name=$_.Name;state=[string](Get-WebAppPoolState -Name $_.Name).Value;runtime=$_.managedRuntimeVersion;pipeline=[string]$_.managedPipelineMode;bit32=$_.enable32BitAppOnWin64}})
        $bindings=@(foreach ($site in $sites) {Get-WebBinding -Name $site.id | ForEach-Object {@{site=$site.id;protocol=$_.protocol;binding=$_.bindingInformation}}})
        $certificates=@(Get-ChildItem Cert:\LocalMachine\My | ForEach-Object {@{thumbprint=$_.Thumbprint;subject=$_.Subject;expires=$_.NotAfter.ToString('yyyy-MM-dd')}})
        Done @{hostname=[Net.Dns]::GetHostName();sites=$sites;publications=$apps;pools=$pools;bindings=$bindings;certificates=$certificates;platforms=@(Platforms)};return
    }
    if ($p.action -eq 'server_restart') {Restart-Service W3SVC;Done @{text='Служба IIS перезапущена.'};return}
    if ($null -ne $p.site_number -and $p.site) {
        $current=Get-Website -Name ([string]$p.site)
        if (-not $current -or $current.Id -ne $p.site_number) {throw 'Сайт IIS изменился. Обновите список перед действием.'}
    }
    if ($p.action -in @('publication_properties','publication_services','publication_browse','publication_convert','unpublish')) {
        $row=SelectedPublication
        if ($p.action -eq 'publication_browse') {Done (PublicationBrowse $row);return}
        if ($p.action -eq 'publication_properties') {
            Done (PublicationInfo $row $true);return
        }
        if ($p.action -eq 'publication_services') {ServiceUpdate $row;return}
        if ($row.root) {throw 'Корень сайта нельзя снять или преобразовать как отдельную публикацию'}
        if ($p.action -eq 'publication_convert') {
            if ($row.entry_type -eq 'application') {throw 'Уже является приложением IIS'}
            if (-not (Test-Path -LiteralPath ('IIS:\AppPools\'+[string]$p.pool))) {throw 'Выберите существующий пул IIS'}
            if ($row.entry_type -eq 'virtual_directory') {
                ConvertTo-WebApplication -PSPath ('IIS:\Sites\'+[string]$p.site+'\'+([string]$row.name).Replace('/','\')) -ApplicationPool ([string]$p.pool) | Out-Null
            } else {New-WebApplication -Site ([string]$p.site) -Name ([string]$row.name) -PhysicalPath ([string]$row.path) -ApplicationPool ([string]$p.pool) | Out-Null}
            $created=@(Get-WebApplication -Site ([string]$p.site) | Where-Object {$_.Path.Trim('/') -eq [string]$row.name})
            if ($created.Count -ne 1) {throw 'Преобразование не подтверждено. Обновите список.'}
            Done @{text='Каталог преобразован в приложение IIS.'};return
        }
        $vrd=Join-Path ([string]$row.path) 'default.vrd';$saved='';$shared=$false
        foreach ($otherSite in @(Get-Website)) {
            foreach ($other in @(IisEntries @{id=$otherSite.Name;path=$otherSite.PhysicalPath;state=[string]$otherSite.State})) {
                if ($other.site -eq $row.site -and $other.name -eq $row.name) {continue}
                if ([IO.Path]::GetFullPath([string]$other.path).TrimEnd('\') -eq [IO.Path]::GetFullPath([string]$row.path).TrimEnd('\')) {$shared=$true}
            }
        }
        if ($shared -and $row.entry_type -eq 'folder') {throw 'Каталог используется другой публикацией. Измените сопоставление отдельно, чтобы не отключить соседнюю публикацию.'}
        try {
            if (-not $shared -and (Test-Path -LiteralPath $vrd -PathType Leaf)) {
                $saved=$vrd+'.ka-unpublished-'+[Guid]::NewGuid().ToString('N')+'.bak'
                Move-Item -LiteralPath $vrd -Destination $saved
            } elseif ($row.entry_type -eq 'folder') {throw 'Это обычная папка. Публикации 1С в ней нет.'}
            if ($row.entry_type -eq 'application') {Remove-WebApplication -Site ([string]$p.site) -Name ([string]$row.name)}
            elseif ($row.entry_type -eq 'virtual_directory') {Remove-WebVirtualDirectory -Site ([string]$p.site) -Application ([string]$row.parent_application) -Name ([string]$row.vdir_name)}
        } catch {if ($saved -and (Test-Path -LiteralPath $saved)) {Move-Item -LiteralPath $saved -Destination $vrd};throw}
        $message=if ($shared) {'Публикация снята. Общий default.vrd оставлен без изменений для других публикаций.'} else {'Публикация снята. База и файлы сохранены; default.vrd сохранён резервной копией.'}
        Done @{text=$message};return
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
            pool_start {
                if ([string](Get-WebAppPoolState -Name $pool).Value -notin @('Started','Starting')) {
                    $manager=OpenPoolManager
                    try {$item=$manager.ApplicationPools[$pool];if ($null -eq $item) {throw 'Пул не найден'};[void]$item.Start()} finally {$manager.Dispose()}
                }
            }
            pool_stop {
                if ([string](Get-WebAppPoolState -Name $pool).Value -notin @('Stopped','Stopping')) {
                    $manager=OpenPoolManager
                    try {$item=$manager.ApplicationPools[$pool];if ($null -eq $item) {throw 'Пул не найден'};[void]$item.Stop()} finally {$manager.Dispose()}
                }
            }
            pool_recycle {if ([string](Get-WebAppPoolState -Name $pool).Value -ne 'Started') {throw 'Пул остановлен. Сначала запустите его.'};Restart-WebAppPool -Name $pool}
            pool_delete {
                $used=@(Get-Website | Where-Object {$_.applicationPool -eq $pool})
                foreach ($s in Get-Website) {$used+=@(Get-WebApplication -Site $s.Name | Where-Object {$_.applicationPool -eq $pool})}
                if ($used.Count) {throw 'Пул используется сайтом или приложениями; сначала смените их пул'}
                Remove-WebAppPool -Name $pool
            }
        }
        if ($p.action -in @('pool_start','pool_stop')) {
            $expected=if ($p.action -eq 'pool_start') {'Started'} else {'Stopped'}
            $until=[DateTime]::UtcNow.AddSeconds(40)
            do {$state=[string](Get-WebAppPoolState -Name $pool).Value;if ($state -eq $expected) {break};Start-Sleep -Milliseconds 300} while ([DateTime]::UtcNow -lt $until)
            if ($state -ne $expected) {throw ('Команда передана пулу '+$pool+', текущее состояние: '+$state+'. Обновите список.')}
        }
        Done @{text=('Действие с пулом '+$pool+' выполнено.');pool=$pool;state=$state};return
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
    $sites=@();$apps=@();$files=@{};$modules=@()
    foreach ($siteFile in @(ApacheFiles)) {
        $files[$siteFile]=ReadFile $siteFile
        foreach ($loaded in [regex]::Matches($files[$siteFile],'(?im)^\s*LoadModule\s+\S+\s+"?([^"\r\n]*wsap(?:24|22)\.dll)')) {
            $module=$loaded.Groups[1].Value.Trim().Replace('${SRVROOT}',(Split-Path (Split-Path ([string]$p.apache_tool))))
            if (-not [IO.Path]::IsPathRooted($module)) {$module=Join-Path (Split-Path (Split-Path ([string]$p.apache_tool))) $module}
            if ($modules -notcontains $module) {$modules+=$module}
        }
    }
    $apacheState=try {[string](Get-Service -Name ([string]$p.apache_service)).Status} catch {'Не определено'}
    foreach ($siteFile in $files.Keys) {
      $fileText=$files[$siteFile]
      $virtualHosts=[regex]::Matches($fileText,$pattern)
      for ($i=0;$i -lt $virtualHosts.Count;$i++) {
        $body=$virtualHosts[$i].Groups[2].Value
        $name=[regex]::Match($body,'(?im)^\s*ServerName\s+([^\s#]+)').Groups[1].Value
        if (-not $name) {$name='VirtualHost '+$i}
        $siteId=$siteFile+'#'+$i
        $endpoints=@(foreach ($address in ($virtualHosts[$i].Groups[1].Value -split '\s+')) {
            if ($address -match '^(.*):(\d+)$') {
                $ip=$Matches[1];$port=[int]$Matches[2]
                $protocol=if ($port -eq 443 -or $body -match '(?im)^\s*SSLEngine\s+on') {'https'} else {'http'}
                @{protocol=$protocol;ip=$ip;port=$port;host=($name -split ':')[0]}
            }
        })
        $folder=[regex]::Match($body,'(?im)^\s*DocumentRoot\s+"?([^"\r\n]+)').Groups[1].Value.Trim()
        $sites+=@{id=$siteId;name=$name;bindings=$virtualHosts[$i].Groups[1].Value;state=$apacheState;path=$folder;endpoints=$endpoints;config_path=$siteFile;index=$i;etag=(HashFile $siteFile)}
        foreach ($entry in @(ApacheEntries $body $siteId $apacheState $modules)) {
            $apps+=PublicationInfo $entry
        }
      }
    }
    Done @{hostname=[Net.Dns]::GetHostName();sites=$sites;publications=$apps;pools=@();certificates=@();platforms=@(Platforms)};return
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
if ($p.action -in @('publication_properties','publication_services','publication_browse')) {
    $modules=@()
    foreach ($file in @(ApacheFiles)) {
        foreach ($loaded in [regex]::Matches((ReadFile $file),'(?im)^\s*LoadModule\s+\S+\s+"?([^"\r\n]*wsap(?:24|22)\.dll)')) {
            $module=$loaded.Groups[1].Value.Trim()
            if ($modules -notcontains $module) {$modules+=$module}
        }
    }
    $rows=@(ApacheEntries $match.Groups[2].Value ([string]$p.site) '' $modules | Where-Object {$_.name -eq [string]$p.alias})
    if ($rows.Count -ne 1 -or [IO.Path]::GetFullPath($rows[0].path).TrimEnd('\','/') -ne [IO.Path]::GetFullPath([string]$p.directory).TrimEnd('\','/')) {throw 'Публикация изменилась. Обновите список.'}
    if ($p.action -eq 'publication_browse') {Done (PublicationBrowse $rows[0]);return}
    if ($p.action -eq 'publication_properties') {Done (PublicationInfo $rows[0] $true);return}
    ServiceUpdate $rows[0];return
}
if ($p.action -notin @('publish','unpublish')) {throw 'Для Apache Windows измените VirtualHost через редактор конфигурации. Запуск отдельных сайтов не поддерживается.'}
$alias=[string]$p.alias
$marker='(?s)\r?\n?# KA-1C '+[regex]::Escape($alias)+' BEGIN\r?\n.*?# KA-1C '+[regex]::Escape($alias)+' END\r?\n?'
$found=[regex]::IsMatch($match.Groups[2].Value,$marker)
if ($p.action -eq 'unpublish' -and -not $found) {
    $body=$match.Groups[2].Value
    $aliases=@([regex]::Matches($body,'(?im)^[ \t]*Alias[ \t]+"?(/[^"\s]+)"?[ \t]+"?([^"\r\n]+?)"?[ \t]*$'))
    $target=@($aliases | Where-Object {$_.Groups[1].Value.Trim('/') -eq $alias})
    if ($target.Count -ne 1 -or $target[0].Groups[2].Value.Trim().TrimEnd('/','\') -ne ([string]$p.directory).TrimEnd('/','\')) {throw 'Alias изменился. Обновите список.'}
    if (@($aliases | Where-Object {$_.Groups[1].Value.Trim('/') -ne $alias -and $_.Groups[2].Value.Trim().TrimEnd('/','\') -eq ([string]$p.directory).TrimEnd('/','\')}).Count) {throw 'Каталог используется другим Alias. Используйте редактор конфигурации.'}
    $spans=@($target[0])
    foreach ($block in [regex]::Matches($body,'(?is)<Directory\s+"?([^">]+)"?\s*>(.*?)</Directory\s*>')) {
        if ($block.Groups[1].Value.Trim().Trim('"').TrimEnd('/','\') -eq ([string]$p.directory).TrimEnd('/','\') -and $block.Groups[2].Value -match '(?i)ManagedApplicationDescriptor\s') {$spans+=$block}
    }
    foreach ($span in @($spans | Sort-Object Index -Descending)) {$body=$body.Remove($span.Index,$span.Length)}
    BackupFile $conf
    $updated=$text.Substring(0,$match.Index)+'<VirtualHost '+$match.Groups[1].Value+'>'+$body+'</VirtualHost>'+$text.Substring($match.Index+$match.Length)
    try {WriteFile $conf $updated;ApacheTest | ForEach-Object {$_};ApacheReload | ForEach-Object {$_}} catch {WriteFile $conf $text;throw}
    Done @{text='Публикация снята. База и файлы сохранены.'};return
}
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
