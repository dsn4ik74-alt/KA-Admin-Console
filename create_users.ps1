#Requires -RunAsAdministrator

param(
    # Без BOM: сначала строгий UTF-8, затем Windows-1251.
    # Для файлов из DOS можно явно указать -UsersFileEncoding OEM866.
    [ValidateSet('Auto', 'UTF8', 'Windows1251', 'OEM866')]
    [string]$UsersFileEncoding = 'Auto',

    [ValidatePattern('^(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}$')]
    [string]$EmailDomain = 'corpsoft24.ru'
)

# --- ПРОВЕРКА ПРАВ АДМИНИСТРАТОРА ---
$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($currentUser)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Скрипт должен быть запущен с правами администратора!"
    exit 1
}

# --- ОПРЕДЕЛЕНИЕ ВЕРСИИ POWERSHELL ---
$useModernCmdlets = $PSVersionTable.PSVersion -ge [version]'5.1'

Write-Host "PowerShell версия: $($PSVersionTable.PSVersion)" -ForegroundColor DarkGray
if ($useModernCmdlets) {
    Write-Host "Режим: современные командлеты (LocalUser)" -ForegroundColor DarkGray
} else {
    Write-Host "Режим: совместимость (ADSI)" -ForegroundColor DarkGray
}

# --- ЧТЕНИЕ ФАЙЛА ПОЛЬЗОВАТЕЛЕЙ ---
function Read-UsersText {
    param(
        [string]$Path,
        [string]$FileEncoding = 'Auto'
    )

    $bytes = [IO.File]::ReadAllBytes($Path)
    $offset = 0
    $encoding = $null
    $strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)

    if ($FileEncoding -eq 'Auto') {
        # Проверяем UTF-32 раньше UTF-16: их BOM имеют общий префикс.
        $bomEncodings = @(
            @{ BOM = [byte[]](0xFF, 0xFE, 0x00, 0x00); CodePage = 12000 },
            @{ BOM = [byte[]](0x00, 0x00, 0xFE, 0xFF); CodePage = 12001 },
            @{ BOM = [byte[]](0xEF, 0xBB, 0xBF); CodePage = 65001 },
            @{ BOM = [byte[]](0xFF, 0xFE); CodePage = 1200 },
            @{ BOM = [byte[]](0xFE, 0xFF); CodePage = 1201 }
        )
        foreach ($candidate in $bomEncodings) {
            if ($bytes.Length -lt $candidate.BOM.Length) { continue }
            $matchesBom = $true
            for ($i = 0; $i -lt $candidate.BOM.Length; $i++) {
                if ($bytes[$i] -ne $candidate.BOM[$i]) {
                    $matchesBom = $false
                    break
                }
            }
            if ($matchesBom) {
                $encoding = [Text.Encoding]::GetEncoding(
                    $candidate.CodePage,
                    [Text.EncoderFallback]::ExceptionFallback,
                    [Text.DecoderFallback]::ExceptionFallback
                )
                $offset = $candidate.BOM.Length
                break
            }
        }

        if ($null -eq $encoding) {
            try {
                $null = $strictUtf8.GetString($bytes)
                $encoding = $strictUtf8
            }
            catch [Text.DecoderFallbackException] {
                $encoding = [Text.Encoding]::GetEncoding(1251)
            }
        }
    }
    else {
        $encoding = switch ($FileEncoding) {
            'UTF8'        { $strictUtf8 }
            'Windows1251' { [Text.Encoding]::GetEncoding(1251) }
            'OEM866'      { [Text.Encoding]::GetEncoding(866) }
        }
    }

    $text = $encoding.GetString($bytes, $offset, $bytes.Length - $offset)
    $text = $text.TrimStart([char]0xFEFF)
    if ($text.IndexOf([char]0xFFFD) -ge 0 -or $text.IndexOf([char]0) -ge 0) {
        throw 'В users.txt обнаружен повреждённый текст или неподдерживаемая кодировка. Восстановите русские буквы из исходного списка и сохраните файл в UTF-8 с BOM.'
    }

    Write-Host "Кодировка users.txt: $($encoding.WebName)" -ForegroundColor DarkGray
    return $text
}

# --- ГЕНЕРАЦИЯ ЛОГИНА И АДРЕСА ПО ФИО ---
function ConvertTo-LatinName {
    param([string]$Name)

    $letters = @{
        'а' = 'a'; 'б' = 'b'; 'в' = 'v'; 'г' = 'g'; 'д' = 'd'
        'е' = 'e'; 'ё' = 'e'; 'ж' = 'zh'; 'з' = 'z'; 'и' = 'i'
        'й' = 'y'; 'к' = 'k'; 'л' = 'l'; 'м' = 'm'; 'н' = 'n'
        'о' = 'o'; 'п' = 'p'; 'р' = 'r'; 'с' = 's'; 'т' = 't'
        'у' = 'u'; 'ф' = 'f'; 'х' = 'kh'; 'ц' = 'ts'; 'ч' = 'ch'
        'ш' = 'sh'; 'щ' = 'shch'; 'ъ' = ''; 'ы' = 'y'; 'ь' = ''
        'э' = 'e'; 'ю' = 'yu'; 'я' = 'ya'
    }
    $result = New-Object Text.StringBuilder
    foreach ($character in $Name.ToLowerInvariant().ToCharArray()) {
        $letter = [string]$character
        if ($letters.ContainsKey($letter)) {
            $null = $result.Append($letters[$letter])
        }
        elseif ($letter -cmatch '^[a-z-]$') {
            $null = $result.Append($letter)
        }
        else {
            throw "Недопустимый символ в имени или фамилии: $character"
        }
    }
    return $result.ToString()
}

function Get-UserIdentity {
    param([string]$FullName, [string]$Domain)

    # Порядок: фамилия, имя, необязательное отчество.
    $nameParts = $FullName.Trim() -split '\s+'
    if ($nameParts.Count -lt 2 -or $nameParts.Count -gt 3) {
        throw 'Укажите фамилию и имя, при необходимости отчество.'
    }
    if ($nameParts[0] -notmatch '^[\p{L}]+(?:-[\p{L}]+)*$' -or
        $nameParts[1] -notmatch '^[\p{L}]+(?:-[\p{L}]+)*$') {
        throw 'Фамилия и имя должны содержать буквы; допускается дефис.'
    }
    $surname = ConvertTo-LatinName -Name $nameParts[0]
    $firstName = ConvertTo-LatinName -Name $nameParts[1]
    if ([string]::IsNullOrEmpty($surname) -or [string]::IsNullOrEmpty($firstName)) {
        throw 'Не удалось получить латинскую фамилию или имя.'
    }
    # Берём одну первую букву латинского имени: Ivan -> i, Yuri -> y.
    $username = "$($firstName.Substring(0, 1)).$surname"
    if ($username.Length -gt 20) {
        throw "Логин '$username' длиннее разрешённых Windows 20 символов."
    }
    $email = "$username@$($Domain.ToLowerInvariant())"
    if ($email.Length -gt 48) {
        throw "Адрес '$email' не помещается в описание из 48 символов."
    }
    return @{
        User = $username
        Email = $email
    }
}

$usersFile = Join-Path $PSScriptRoot "users.txt"
if (-not (Test-Path $usersFile)) {
    Write-Error "Файл не найден: $usersFile"
    exit 1
}

$UsersData = @()
$lineNumber = 0
$seenUsernames = @{}

try {
    $usersText = Read-UsersText -Path $usersFile -FileEncoding $UsersFileEncoding
}
catch {
    Write-Error "Не удалось прочитать файл пользователей: $($_.Exception.Message)"
    exit 1
}

foreach ($line in ($usersText -split '\r\n|\n|\r')) {
    $lineNumber++
    $line = $line.Trim()
    if ([string]::IsNullOrWhiteSpace($line)) { continue }

    # Новый формат: Фамилия Имя Отчество "Компания".
    # Старый формат с email тоже читается, но адрес генерируется заново по ФИО.
    $record = [regex]::Match($line, '^(?<FullName>[^"@]+?)(?:\s+[^\s"@]+@[^\s"@]+)?(?:\s+"(?<Company>[^"]*)")?\s*$')
    if (-not $record.Success) {
        Write-Warning "Строка $lineNumber пропущена: ожидается Фамилия Имя Отчество и необязательная компания в двойных кавычках."
        continue
    }
    $nameText = $record.Groups['FullName'].Value.Trim()
    $company = $record.Groups['Company'].Value.Trim()

    try {
        $identity = Get-UserIdentity -FullName $nameText -Domain $EmailDomain
        if ($seenUsernames.ContainsKey($identity.User)) {
            Write-Warning "Строка $lineNumber пропущена: логин $($identity.User) уже получен из строки $($seenUsernames[$identity.User])."
            continue
        }
        $seenUsernames[$identity.User] = $lineNumber
        $UsersData += @{
            FullName = $nameText
            User     = $identity.User
            Email    = $identity.Email
            Company  = $company
        }
    }
    catch {
        Write-Warning "Строка $lineNumber пропущена: $($_.Exception.Message)"
    }
}

if ($UsersData.Count -eq 0) {
    Write-Error "Не удалось прочитать ни одного пользователя из файла."
    exit 1
}

Write-Host "Прочитано пользователей из файла: $($UsersData.Count)" -ForegroundColor Cyan

# --- ФУНКЦИЯ ГЕНЕРАЦИИ СЛОЖНОГО ПАРОЛЯ ---
function Generate-Password {
    $length  = 14
    $upper   = 'ABCDEFGHJKLMNPQRSTUVWXYZ'.ToCharArray()
    $lower   = 'abcdefghijkmnopqrstuvwxyz'.ToCharArray()
    $digits  = '23456789'.ToCharArray()
    $special = ('!', '@', '#', '$', '%', '^', '&', '*')

    $password = @(
        ($upper   | Get-Random -Count 1)
        ($lower   | Get-Random -Count 1)
        ($digits  | Get-Random -Count 1)
        ($special | Get-Random -Count 1)
    )
    $allChars  = $upper + $lower + $digits + $special
    $password += ($allChars | Get-Random -Count ($length - 4))
    return -join ($password | Sort-Object { Get-Random })
}

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (ADSI-совместимость) ---

function User-Exists($username) {
    if ($useModernCmdlets) {
        return [bool](Get-LocalUser -Name $username -ErrorAction SilentlyContinue)
    } else {
        $computer = [ADSI]"WinNT://$env:COMPUTERNAME"
        $user = $computer.Children | Where-Object { $_.SchemaClassName -eq 'User' -and $_.Name -eq $username }
        return [bool]$user
    }
}

function Create-User($username, $passwordPlain, $fullName, $description) {
    if ($useModernCmdlets) {
        $passwordSecure = ConvertTo-SecureString $passwordPlain -AsPlainText -Force
        New-LocalUser -Name $username `
                      -Password $passwordSecure `
                      -FullName $fullName `
                      -Description $description `
                      -PasswordNeverExpires:$true `
                      -ErrorAction Stop
    } else {
        $computer = [ADSI]"WinNT://$env:COMPUTERNAME"
        $user = $computer.Create("User", $username)
        $user.SetPassword($passwordPlain)
        $user.Put("FullName", $fullName)
        $user.Put("Description", $description)
        $user.Put("UserFlags", 65600)
        $user.SetInfo()
    }
}

function Add-UserToRDP($username) {
    if ($useModernCmdlets) {
        $rdpGroup = (Get-LocalGroup -SID "S-1-5-32-555").Name
        Add-LocalGroupMember -Group $rdpGroup -Member $username -ErrorAction Stop
    } else {
        $rdpGroup = (Get-WmiObject Win32_Group -Filter "SID='S-1-5-32-555' AND LocalAccount=True").Name
        $group = [ADSI]"WinNT://$env:COMPUTERNAME/$rdpGroup,group"
        $group.Add("WinNT://$env:COMPUTERNAME/$username,user")
    }
}

# --- ОПРЕДЕЛЕНИЕ ПУТИ ДЛЯ ЛОГА ---
$logDir  = if ($PSScriptRoot) { $PSScriptRoot } else { $env:USERPROFILE }
$logFile = Join-Path $logDir "created_users_$(Get-Date -Format 'yyyyMMdd_HHmmss').txt"

Write-Host "Начинаю создание пользователей..." -ForegroundColor Cyan
$Results = @()

foreach ($item in $UsersData) {
    $username    = $item.User
    # Адрес в начале описания, чтобы он сохранился при обрезке длинной компании.
    $descFull    = if ($item.Company) { "$($item.Email) ($($item.Company))" } else { $item.Email }
    $description = if ($descFull.Length -gt 48) { $descFull.Substring(0, 48) } else { $descFull }

    if (User-Exists $username) {
        Write-Warning "Пользователь $username уже существует. Пропускаю."
        continue
    }

    $passwordPlain = Generate-Password

    try {
        Create-User -username $username -passwordPlain $passwordPlain -fullName $item.FullName -description $description
        Add-UserToRDP -username $username

        Write-Host "Добавлен: $username" -ForegroundColor Green

        $Results += New-Object PSObject -Property @{
            Username = $username
            Email    = $item.Email
            Password = $passwordPlain
            FullName = $item.FullName
            Company  = $item.Company
        }
    }
    catch {
        Write-Error "Ошибка при создании $username : $($_.Exception.Message)"
    }
}

# --- ВЫВОД РЕЗУЛЬТАТОВ ---
if ($Results.Count -gt 0) {
    Write-Host "`nСПИСОК СОЗДАННЫХ УЧЕТНЫХ ЗАПИСЕЙ И ПАРОЛЕЙ:" -ForegroundColor Yellow
    $Results | Format-Table Username, Email, Password, FullName, Company -AutoSize

    $Results | Format-Table Username, Password, FullName -AutoSize | Out-File -FilePath $logFile -Encoding UTF8 -Width 4096
    Write-Host "Данные сохранены в файл: $logFile" -ForegroundColor Cyan
    Write-Host "ВНИМАНИЕ: удалите файл после передачи паролей пользователям!" -ForegroundColor Red
}
else {
    Write-Host "`nНовых пользователей не создано." -ForegroundColor Gray
}
