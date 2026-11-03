# 창고 PC 서버 4개를 Windows 서비스로 등록 (관리자 PowerShell에서 실행)
#   warehouse-redis         Redis (6379)
#   warehouse-board-flask   보드 Flask (5000)
#   warehouse-keycap-flask  키캡 Flask (5002, waitress)
#   warehouse-status-page   상태판 (5080, 읽기 전용)
# - PC 켜면 자동 시작, 죽으면 3초 뒤 자동 재시작 (로그인 안 해도 뜸)
# - 로봇 프로그램(보드 main, 키캡·트레이 게이트웨이)은 서비스로 안 만듦 - 사람이 없을 때 팔이 움직이면 안 됨
# - 이미 등록된 서비스는 건너뜀. 다시 등록하려면 uninstall_services.ps1 먼저
# 준비: NSSM(https://nssm.cc/download) zip에서 win64\nssm.exe를 저장소 폴더 옆 nssm\ 에 둘 것 (예: C:\warehouse\nssm\nssm.exe)
$ErrorActionPreference = 'Stop'
$Repo      = Split-Path $PSScriptRoot -Parent                 # 저장소 루트
$Nssm      = Join-Path (Split-Path $Repo -Parent) 'nssm\nssm.exe'   # 저장소 폴더 옆 (예: C:\warehouse\nssm)
$LogDir    = Join-Path $Repo 'logs'
$Python    = Join-Path $Repo 'venv\Scripts\python.exe'        # 공용 가상환경
$Waitress  = Join-Path $Repo 'venv\Scripts\waitress-serve.exe'
$RedisDir  = Join-Path (Split-Path $Repo -Parent) 'redis'    # Windows용 Redis - 저장소 폴더 옆 (예: C:\warehouse\redis)

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw '관리자 PowerShell에서 실행해야 함 (시작 메뉴 > PowerShell 우클릭 > 관리자 권한으로 실행)' }
if (-not (Test-Path $Nssm)) { throw "nssm.exe 없음: $Nssm" }
New-Item -ItemType Directory -Force $LogDir | Out-Null

$services = @(
    @{ Name = 'warehouse-redis'; Display = '창고 Redis (6379)'; Port = 6379; Depends = $null
       App = Join-Path $RedisDir 'redis-server.exe'; Args = '"' + (Join-Path $RedisDir 'redis.warehouse.conf') + '"'
       Dir = $RedisDir },
    @{ Name = 'warehouse-board-flask'; Display = '창고 보드 Flask (5000)'; Port = 5000; Depends = 'warehouse-redis'
       App = $Python; Args = 'app.py'
       Dir = Join-Path $Repo 'board' },
    @{ Name = 'warehouse-keycap-flask'; Display = '창고 키캡 Flask (5002)'; Port = 5002; Depends = 'warehouse-redis'
       App = $Waitress; Args = '--listen=0.0.0.0:5002 app:app'
       Dir = Join-Path $Repo 'keycap_flask' },
    @{ Name = 'warehouse-status-page'; Display = '창고 상태판 (5080)'; Port = 5080; Depends = $null
       App = $Python; Args = 'status_page.py'
       Dir = $Repo }
)

foreach ($s in $services) {
    if (Get-Service $s.Name -ErrorAction SilentlyContinue) { Write-Host "[skip] $($s.Name) 이미 등록됨"; continue }
    if (-not (Test-Path $s.App)) { throw "실행 파일 없음: $($s.App)" }
    # 같은 포트를 창으로 띄운 프로그램이 떠 있으면 서비스가 못 뜨거나(Redis) 둘이 같이 잡음(waitress)
    if (Get-NetTCPConnection -LocalPort $s.Port -State Listen -ErrorAction SilentlyContinue) {
        throw "$($s.Port) 포트를 이미 다른 프로그램이 쓰는 중 - 창으로 띄운 것부터 Ctrl+C로 끄고 다시 실행"
    }
    $log = Join-Path $LogDir "svc_$($s.Name).log"
    & $Nssm install $s.Name $s.App $s.Args | Out-Null
    & $Nssm set $s.Name DisplayName $s.Display | Out-Null
    & $Nssm set $s.Name AppDirectory $s.Dir | Out-Null
    & $Nssm set $s.Name Start SERVICE_AUTO_START | Out-Null
    & $Nssm set $s.Name AppExit Default Restart | Out-Null       # 죽으면 재시작
    & $Nssm set $s.Name AppRestartDelay 3000 | Out-Null           # 3초 뒤
    & $Nssm set $s.Name AppStdout $log | Out-Null                 # 화면 출력 + 에러 -> 파일
    & $Nssm set $s.Name AppStderr $log | Out-Null
    & $Nssm set $s.Name AppRotateFiles 1 | Out-Null               # 10MB 넘으면 새 파일
    & $Nssm set $s.Name AppRotateBytes 10485760 | Out-Null
    & $Nssm set $s.Name AppEnvironmentExtra PYTHONIOENCODING=utf-8 PYTHONUNBUFFERED=1 | Out-Null
    if ($s.Depends) { & $Nssm set $s.Name DependOnService $s.Depends | Out-Null }
    Write-Host "[ok] $($s.Name) 등록 (로그: $log)"
}

foreach ($s in $services) {
    Start-Service $s.Name
    $up = $false
    for ($i = 0; $i -lt 30; $i++) {
        if (Get-NetTCPConnection -LocalPort $s.Port -State Listen -ErrorAction SilentlyContinue) { $up = $true; break }
        Start-Sleep -Milliseconds 500
    }
    if ($up) { Write-Host "[ok] $($s.Name) 실행 중 - $($s.Port) 응답" }
    else { Write-Host "[!!] $($s.Name) $($s.Port) 응답 없음 - $LogDir\svc_$($s.Name).log 확인" -ForegroundColor Red }
}
