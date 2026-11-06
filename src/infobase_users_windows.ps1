param($job)
$ErrorActionPreference='Stop'

# A private process loads the exact DLL; COM registration on the PC is unchanged.
function NewConnector([string]$path) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class KaOneCFactory {
    [DllImport("kernel32", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern IntPtr LoadLibraryEx(string name, IntPtr file, uint flags);
    [DllImport("kernel32", CharSet=CharSet.Ansi, ExactSpelling=true)]
    static extern IntPtr GetProcAddress(IntPtr module, string name);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    delegate int GetFactory(ref Guid clsid, ref Guid iid, out IntPtr result);
    [ComImport, Guid("00000001-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IClassFactory {
        [PreserveSig] int CreateInstance(IntPtr outer, ref Guid iid, [MarshalAs(UnmanagedType.Interface)] out object result);
        [PreserveSig] int LockServer(bool locked);
    }
    public static object Open(string path) {
        IntPtr module=LoadLibraryEx(path,IntPtr.Zero,8);
        if(module==IntPtr.Zero)throw new System.ComponentModel.Win32Exception();
        IntPtr address=GetProcAddress(module,"DllGetClassObject");
        if(address==IntPtr.Zero)throw new Exception("COM DLL has no DllGetClassObject");
        GetFactory get=(GetFactory)Marshal.GetDelegateForFunctionPointer(address,typeof(GetFactory));
        Guid clsid=new Guid("181E893D-73A4-4722-B61D-D604B3D67D47");
        Guid iid=new Guid("00000001-0000-0000-C000-000000000046");
        IntPtr pointer;Marshal.ThrowExceptionForHR(get(ref clsid,ref iid,out pointer));
        object factory=Marshal.GetObjectForIUnknown(pointer);Marshal.Release(pointer);
        try {
            Guid dispatch=new Guid("00020400-0000-0000-C000-000000000046");object result;
            Marshal.ThrowExceptionForHR(((IClassFactory)factory).CreateInstance(IntPtr.Zero,ref dispatch,out result));
            return result;
        } finally {Marshal.ReleaseComObject(factory);}
    }
}
'@
    return ,([KaOneCFactory]::Open($path))
}
function M($object,[string]$name,[object[]]$arguments=@()) {
    if([Runtime.InteropServices.Marshal]::IsComObject($object)) {
        return ,($object.GetType().InvokeMember($name,[Reflection.BindingFlags]::InvokeMethod,$null,$object,$arguments))
    }
    return ,($object.PSObject.Methods[$name].Invoke($arguments))
}
function G($object,[string]$name) {
    if([Runtime.InteropServices.Marshal]::IsComObject($object)) {
        return ,($object.GetType().InvokeMember($name,[Reflection.BindingFlags]::GetProperty,$null,$object,$null))
    }
    return ,($object.$name)
}
function S($object,[string]$name,$value) {
    if([Runtime.InteropServices.Marshal]::IsComObject($object)) {
        [void]$object.GetType().InvokeMember($name,[Reflection.BindingFlags]::SetProperty,$null,$object,@($value))
    } else {$object.$name=$value}
}
function Items($collection) {
    $count=[int](M $collection 'Количество')
    for($i=0;$i -lt $count;$i++){Write-Output -NoEnumerate (M $collection 'Получить' @($i))}
}
function Uid($object){return [string](M $connection 'String' @($object))}
function Hash($value) {
    $raw=[Text.Encoding]::UTF8.GetBytes(($value|ConvertTo-Json -Depth 16 -Compress))
    $sha=[Security.Cryptography.SHA256]::Create()
    try{return ([BitConverter]::ToString($sha.ComputeHash($raw))).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}
}
function Emit($kind,$text){[Console]::Out.WriteLine((@{kind=$kind;text=$text}|ConvertTo-Json -Compress))}
function Structure($values) {
    $result=M $connection 'NewObject' @('Структура')
    foreach($key in $values.Keys){[void](M $result 'Вставить' @($key,$values[$key]))}
    return ,$result
}
function FindUser($id) {
    $uid=M $connection 'NewObject' @('УникальныйИдентификатор',$id)
    return ,(M $users 'НайтиПоУникальномуИдентификатору' @($uid))
}
function UserInfo($user) {
    $roles=@(Items (G $user 'Роли')|ForEach-Object{[string](G $_ 'Имя')}|Sort-Object)
    $id=Uid (G $user 'УникальныйИдентификатор')
    $groups=@();$catalogId=''
    if($mode -eq 'bsp') {
        $reference=M $userCatalog 'НайтиПоРеквизиту' @('ИдентификаторПользователяИБ',(G $user 'УникальныйИдентификатор'))
        if(-not (M $reference 'Пустая')) {
            $catalogId=Uid (M $reference 'УникальныйИдентификатор')
            $groups=@($groupRows|Where-Object{$_.members -contains $catalogId}|ForEach-Object{$_.id}|Sort-Object)
        }
    }
    $result=[ordered]@{id=$id;name=[string](G $user 'Имя');full_name=[string](G $user 'ПолноеИмя');roles=$roles;
        standard_auth=[bool](G $user 'АутентификацияСтандартная');os_auth=[bool](G $user 'АутентификацияОС');
        os_user=[string](G $user 'ПользовательОС');show_in_list=[bool](G $user 'ПоказыватьВСпискеВыбора');
        groups=$groups;catalog_id=$catalogId}
    $result['etag']=Hash $result
    return $result
}
function GroupRows {
    $rows=@()
    if($mode -ne 'bsp'){return $rows}
    $selection=M $groupCatalog 'Выбрать'
    while(M $selection 'Следующий') {
        if((G $selection 'ЭтоГруппа') -or (G $selection 'ПометкаУдаления')){continue}
        $ref=G $selection 'Ссылка';$members=@()
        foreach($member in (Items (G $ref 'Пользователи'))) {
            $userRef=G $member 'Пользователь'
            if($null -ne $userRef){$members+=Uid (M $userRef 'УникальныйИдентификатор')}
        }
        $rows+=@{id=Uid (M $ref 'УникальныйИдентификатор');name=[string](G $ref 'Наименование');members=$members}
    }
    return $rows
}
function Inventory {
    $listing=@(Items (M $users 'ПолучитьПользователей')|ForEach-Object{UserInfo $_})
    $roles=@(Items (G $metadata 'Роли')|ForEach-Object{@{name=[string](G $_ 'Имя');title=[string](G $_ 'Синоним')}})
    return @{mode=$mode;reason=$reason;configuration=[string](G $metadata 'Синоним');version=[string](G $metadata 'Версия');
        users=$listing;roles=$roles;groups=@($groupRows|ForEach-Object{@{id=$_.id;name=$_.name}})}
}
function SetGroups($reference,$selected) {
    $catalogId=Uid (M $reference 'УникальныйИдентификатор')
    foreach($group in $groupRows) {
        $wanted=$selected -contains $group.id;$present=$group.members -contains $catalogId
        if($wanted -eq $present){continue}
        $uid=M $connection 'NewObject' @('УникальныйИдентификатор',$group.id)
        $ref=M $groupCatalog 'ПолучитьСсылку' @($uid)
        $obj=M $ref 'ПолучитьОбъект';$table=G $obj 'Пользователи'
        # Re-read current members, preserving changes by other administrators.
        $member=M $table 'Найти' @($reference,'Пользователь')
        if($wanted -and $null -eq $member){$member=M $table 'Добавить';S $member 'Пользователь' $reference}
        elseif(-not $wanted -and $null -ne $member){[void](M $table 'Удалить' @($member))}
        else{continue}
        [void](M $obj 'Записать')
        Emit 'message' ('Группа доступа: '+$group.name+' · '+$(if($wanted){'добавлен'}else{'исключён'}))
    }
}
function ApplyChange($change) {
    $creating=$change.action -eq 'create';$deleting=$change.action -eq 'delete'
    $user=$null
    if($creating) {
        if($null -ne (M $users 'НайтиПоИмени' @([string]$change.name))){throw 'Пользователь с таким логином уже существует.'}
    } else {
        $user=FindUser ([string]$change.id)
        if($null -eq $user){throw 'Пользователь уже удалён. Обновите список.'}
        $before=UserInfo $user
        if($before.etag -ne $change.etag){throw 'Свойства пользователя изменились. Обновите список перед сохранением.'}
        $current=M $users 'ТекущийПользователь'
        if((Uid (G $current 'УникальныйИдентификатор')) -eq $before.id){throw 'Изменение текущей учётной записи выполняйте в самой 1С.'}
        if(@(Items (M $users 'ПолучитьПользователей')).Count -le 1){throw 'Нельзя изменить или удалить последнюю учётную запись.'}
    }
    if(-not $deleting) {
        $available=if($mode -eq 'bsp'){@($groupRows|ForEach-Object{$_.id})}else{@(Items (G $metadata 'Роли')|ForEach-Object{[string](G $_ 'Имя')})}
        $requested=if($mode -eq 'bsp'){@($change.groups)}else{@($change.roles)}
        foreach($item in $requested){if($available -notcontains $item){throw ('Не найдено выбранное право: '+$item)}}
        $duplicate=M $users 'НайтиПоИмени' @([string]$change.name)
        if($null -ne $duplicate -and ($creating -or (Uid (G $duplicate 'УникальныйИдентификатор')) -ne $change.id)) {
            throw 'Такой логин уже используется другим пользователем.'
        }
    }
    $values=@{Действие=$(if($deleting){'Удалить'}else{'Записать'})}
    if(-not $deleting) {
        $values['Имя']=[string]$change.name;$values['ПолноеИмя']=[string]$change.full_name
        $values['АутентификацияСтандартная']=[bool]$change.standard_auth;$values['АутентификацияОС']=[bool]$change.os_auth
        $values['ПользовательОС']=[string]$change.os_user;$values['ПоказыватьВСпискеВыбора']=[bool]$change.show_in_list
        if($change.set_password){$values['Пароль']=[string]$change.password}
    }
    $stage='до записи пользователя'
    try {
        if($mode -eq 'bsp') {
            if(-not $creating -and -not $before.catalog_id){throw 'Карточка пользователя не сопоставлена с ИБ. Выполните сопоставление в 1С.'}
            $obj=if($creating){M $userCatalog 'СоздатьЭлемент'}else{
                $uid=M $connection 'NewObject' @('УникальныйИдентификатор',$before.catalog_id)
                M (M $userCatalog 'ПолучитьСсылку' @($uid)) 'ПолучитьОбъект'
            }
            if(-not $deleting){S $obj 'Наименование' ([string]$change.full_name)}
            $description=Structure $values
            [void](M (G $obj 'ДополнительныеСвойства') 'Вставить' @('ОписаниеПользователяИБ',$description))
            $stage='запись карточки и пользователя ИБ'
            if($deleting){[void](M $obj 'УстановитьПометкуУдаления' @($true))}else{[void](M $obj 'Записать')}
            $reference=G $obj 'Ссылка';$stage='назначение групп доступа'
            SetGroups $reference $(if($deleting){@()}else{@($change.groups)})
        } else {
            if($creating){$user=M $users 'СоздатьПользователя'}
            $stage='запись пользователя ИБ'
            if($deleting){[void](M $user 'Удалить')}else{
                foreach($key in $values.Keys){if($key -ne 'Действие'){S $user $key $values[$key]}}
                $assigned=G $user 'Роли';[void](M $assigned 'Очистить')
                foreach($role in $change.roles){[void](M $assigned 'Добавить' @((M (G $metadata 'Роли') 'Найти' @($role))))}
                [void](M $user 'Записать')
            }
        }
        $stage='проверка результата'
        $groupRows=GroupRows
        if($deleting) {
            if($null -ne (FindUser ([string]$change.id))){throw 'Пользователь ИБ всё ещё существует.'}
            return @{deleted=$true;id=$change.id}
        }
        $written=M $users 'НайтиПоИмени' @([string]$change.name)
        if($null -eq $written){throw 'Записанный пользователь не найден.'}
        $result=UserInfo $written
        if($mode -eq 'bsp' -and (@($result.groups|Sort-Object) -join '|') -ne (@($change.groups|Sort-Object) -join '|')) {
            throw 'Состав групп доступа не подтверждён.'
        }
        return @{user=$result}
    } catch {
        throw ('Этап: '+$stage+'. Изменения могли быть применены частично; обновите список перед повтором. '+$_.Exception.Message)
    }
}

$connector=$null;$connection=$null
try {
    $connector=NewConnector ([string]$job.connector)
    $login=([string]$job.credentials.user).Replace('"','""');$password=([string]$job.credentials.password).Replace('"','""')
    $address=[string]$job.target.address+'Usr="'+$login+'";Pwd="'+$password+'";'
    $connection=M $connector 'Connect' @($address)
    $users=G $connection 'ПользователиИнформационнойБазы';$metadata=G $connection 'Метаданные'
    $mode='platform';$reason='Механизм пользователей БСП не обнаружен. Доступны пользователи и роли платформы; можно открыть Конфигуратор.'
    $userCatalog=$null;$groupCatalog=$null
    try {
        $catalogs=G $metadata 'Справочники';$userMeta=M $catalogs 'Найти' @('Пользователи');$groupMeta=M $catalogs 'Найти' @('ГруппыДоступа')
        if($null -ne $userMeta -and $null -ne $groupMeta) {
            $idAttribute=M (G $userMeta 'Реквизиты') 'Найти' @('ИдентификаторПользователяИБ')
            $members=M (G $groupMeta 'ТабличныеЧасти') 'Найти' @('Пользователи')
            if($null -ne $idAttribute -and $null -ne $members -and $null -ne (M (G $members 'Реквизиты') 'Найти' @('Пользователь'))) {
                # Read-only capability probe, no calls to guessed mutating procedures.
                $module=G $connection 'Пользователи';[void](M $module 'НовоеОписаниеПользователяИБ')
                $userCatalog=G (G $connection 'Справочники') 'Пользователи'
                $groupCatalog=G (G $connection 'Справочники') 'ГруппыДоступа'
                $mode='bsp';$reason='БСП: карточки пользователей и группы доступа. Запись выполняется штатными обработчиками конфигурации.'
            }
        }
    } catch {$reason='Прикладной механизм БСП недоступен во внешнем соединении. Доступны роли платформы либо Конфигуратор.'}
    $groupRows=@(GroupRows)
    if($job.action -eq 'inspect'){$result=Inventory}
    elseif($job.action -eq 'change') {
        if($job.expected_mode -ne $mode -or $job.change.mode -ne $mode){throw 'Механизм пользователей изменился. Обновите подключение.'}
        $result=ApplyChange $job.change
    } else {throw 'Неизвестная операция.'}
    [Console]::Out.WriteLine((@{kind='done';result=$result}|ConvertTo-Json -Depth 20 -Compress))
} finally {
    foreach($object in @($connection,$connector)) {
        if($null -ne $object -and [Runtime.InteropServices.Marshal]::IsComObject($object)) {
            [void][Runtime.InteropServices.Marshal]::ReleaseComObject($object)
        }
    }
}
