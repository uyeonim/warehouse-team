# 창고 노트북 한 번 설정 (새 노트북으로 옮길 때) - 저장소 루트의 setup_laptop.bat 더블클릭 (관리자 권한 창이 뜸)
# 하는 일 (이미 된 건 건너뜀 - 여러 번 돌려도 됨):
#   1) 폴더 확인: 저장소 옆 redis\ (redis-server.exe), .env 3개, DobotLink, Windows Terminal
#   2) Python 3.9 가상환경(venv) + requirements.txt
#   3) Redis 설정 파일(setup\redis.warehouse.conf -> redis\, 없을 때만) + data 폴더
#   4) 방화벽 인바운드 5001·5002·5003·5080 (규칙 이름 warehouse-<포트>, 점검이 warehouse-* 로 찾음)
#   5) 전원: 충전 중·배터리 모두 절전·최대 절전 안 함, 덮개를 닫아도 아무것도 안 함
#   6) 바탕화면 바로가기: 창고 시작 / 창고 점검 / 창고 테스트 / 재고 초기화
# 사람이 할 일(공유기 IP 예약, .env·Redis 데이터 복사, DobotLink 설치, 카메라 번호 확인)은 노트북_이전_가이드.md
$ErrorActionPreference = 'Stop'
$Repo     = Split-Path $PSScriptRoot -Parent                  # 저장소 루트 (예: C:\warehouse\warehouse-team)
$Base     = Split-Path $Repo -Parent                          # 그 옆에 redis\, nssm\ (예: C:\warehouse)
$RedisDir = Join-Path $Base 'redis'
$Venv     = Join-Path $Repo 'venv'
$Python   = Join-Path $Venv 'Scripts\python.exe'
$todo     = New-Object System.Collections.Generic.List[string]

function Step($msg) { Write-Host "`n[setup] $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "  [OK] $msg" }
function Todo($msg) { Write-Host "  [할 일] $msg" -ForegroundColor Yellow; $todo.Add($msg) }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw '관리자 권한으로 실행해야 함 - setup_laptop.bat 을 더블클릭하면 관리자 권한 창이 뜸' }
Write-Host "저장소: $Repo`nRedis : $RedisDir"

# ---------------- 1) 준비물 확인 ----------------
Step '준비물 확인'
foreach ($f in 'board\.env', 'keycap_flask\.env', 'keycap_tray\.env.local') {
    if (Test-Path (Join-Path $Repo $f)) { Ok $f } else { Todo "$f 없음 - 예전 노트북에서 복사 (가이드 1장)" }
}
if (Test-Path (Join-Path $RedisDir 'redis-server.exe')) { Ok 'Redis 프로그램' }
else { Todo "Redis 없음 - Windows용 Redis 5.0 폴더를 $RedisDir 로 복사 (가이드 1장)" }
$dobotLink = Join-Path $env:LOCALAPPDATA 'Programs\DobotLink\DobotLink.exe'
if (Test-Path $dobotLink) { Ok 'DobotLink' } else { Todo 'DobotLink 설치 (DobotLab 설치하면 같이 깔림) - 이 로그인 사용자로 설치' }
if (Get-Command wt.exe -ErrorAction SilentlyContinue) { Ok 'Windows Terminal (탭으로 띄우기)' }
else { Todo 'Windows Terminal 없음 - Microsoft Store에서 설치 (없어도 동작은 함, 창이 여러 개 뜸)' }

# ---------------- 2) Python 가상환경 ----------------
Step 'Python 3.9 가상환경'
if (-not (Test-Path $Python)) {
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if (-not $py) { Todo 'Python 3.9 설치 후 다시 실행 (python.org, "py launcher" 포함)' }
    else {
        & py.exe -3.9 -m venv $Venv
        if ($LASTEXITCODE -ne 0) { Todo 'Python 3.9가 없음 - 3.9 설치 후 다시 실행 (DobotRPC가 3.9 기준)' }
        else { Ok "venv 생성: $Venv" }
    }
}
if (Test-Path $Python) {
    & $Python -m pip install --disable-pip-version-check -q -r (Join-Path $Repo 'requirements.txt')
    if ($LASTEXITCODE -eq 0) { Ok 'requirements.txt 설치' } else { Todo 'pip 설치 실패 - 인터넷 연결 확인 후 다시 실행' }
}

# ---------------- 3) Redis 설정 ----------------
Step 'Redis 설정'
if (Test-Path $RedisDir) {
    $conf = Join-Path $RedisDir 'redis.warehouse.conf'
    if (Test-Path $conf) { Ok '설정 파일 있음 (덮어쓰지 않음)' }
    else { Copy-Item (Join-Path $PSScriptRoot 'redis.warehouse.conf') $conf; Ok "설정 파일 복사: $conf" }
    New-Item -ItemType Directory -Force (Join-Path $RedisDir 'data') | Out-Null
    if (Test-Path (Join-Path $RedisDir 'data\appendonly.aof')) { Ok 'Redis 데이터(재고·큐) 있음' }
    else { Todo 'Redis 데이터 없음 - 예전 노트북 redis\data\appendonly.aof 복사, 없으면 재고를 새로 입력 (가이드 1장)' }
}

# ---------------- 4) 방화벽 ----------------
Step '방화벽 인바운드'
$open = @(Get-NetFirewallRule -DisplayName 'warehouse-*' -ErrorAction SilentlyContinue |
    Where-Object { $_.Direction -eq 'Inbound' -and $_.Enabled -eq 'True' -and $_.Action -eq 'Allow' } |
    Get-NetFirewallPortFilter | ForEach-Object { "$($_.LocalPort)" })
foreach ($port in 5001, 5002, 5003, 5080) {
    if ($open -contains "$port") { Ok "$port 이미 허용"; continue }
    New-NetFirewallRule -DisplayName "warehouse-$port" -Direction Inbound -Action Allow -Protocol TCP `
        -LocalPort $port -Profile Any | Out-Null
    Ok "$port 허용 (warehouse-$port)"
}

# ---------------- 5) 전원 ----------------
Step '전원 (충전 중 + 배터리)'
# 배터리일 때도 끔 - 2026-11-04 충전기 빠진 채 19:26 절전 -> 깬 뒤 DobotLink 무응답으로 컨베이어 failed
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change standby-timeout-dc 0
powercfg /change hibernate-timeout-dc 0
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setdcvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setactive SCHEME_CURRENT
Ok '충전 중·배터리 모두 절전 안 함, 최대 절전 안 함, 덮개 닫아도 아무것도 안 함 (배터리는 금방 닳으니 충전기는 꽂아 둘 것)'

# ---------------- 6) 바탕화면 바로가기 ----------------
Step '바탕화면 바로가기'
$shell = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
foreach ($s in @(@{ Name = '창고 시작'; Bat = 'start_all.bat' },
                 @{ Name = '창고 점검'; Bat = 'status.bat' },
                 @{ Name = '창고 테스트'; Bat = 'start_test.bat' },
                 @{ Name = '재고 초기화'; Bat = 'reset_stock.bat' })) {   # 리허설(start_rehearsal.bat)은 바로가기 안 만듦
    $lnk = $shell.CreateShortcut((Join-Path $desktop "$($s.Name).lnk"))
    $lnk.TargetPath = Join-Path $Repo $s.Bat
    $lnk.WorkingDirectory = $Repo
    if ($s.Bat -eq 'status.bat') { $lnk.WindowStyle = 7 }   # 브라우저만 열면 되니 창은 최소화
    $lnk.Save()
    Ok "$($s.Name) -> $($s.Bat)"
}
if ($desktop -notlike "$env:USERPROFILE*") { Todo "바로가기가 $desktop 에 생김 - 관리자 계정이 따로면 평소 쓰는 계정 바탕화면으로 옮길 것" }

# ---------------- 정리 ----------------
Step '끝'
if ($todo.Count -eq 0) { Write-Host '  자동 설정 끝. 노트북_이전_가이드.md 3장(확인)으로' -ForegroundColor Green }
else {
    Write-Host "  남은 할 일 $($todo.Count)건 - 처리하고 이 스크립트를 다시 돌리면 됨:" -ForegroundColor Yellow
    $todo | ForEach-Object { Write-Host "   - $_" -ForegroundColor Yellow }
}
