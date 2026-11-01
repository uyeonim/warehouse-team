# 창고 PC 실행 스크립트 - Redis -> Flask -> DobotLink -> main 순서로 띄움 (start_all.bat 더블클릭)
# - 이미 켜진 건 건너뜀 (포트/프로세스로 판단)
# - 앞 단계가 안 뜨면 거기서 멈춤 (Redis 없이 Flask를 띄우면 재고가 0으로 보임)
# - main이 이미 떠 있으면(5001 사용 중) 또 띄우지 않음 - 두 개 뜨면 COM3 충돌
# 끌 때는 각 창에서 Ctrl+C (main -> Flask -> Redis 순서)
$ErrorActionPreference = 'Stop'
$Root      = $PSScriptRoot
$Python    = Join-Path $Root 'venv-dobot\Scripts\python.exe'
$RedisDir  = 'C:\Users\dusdn\redis'
$RedisConf = Join-Path $RedisDir 'redis.warehouse.conf'
$DobotLink = Join-Path $env:LOCALAPPDATA 'Programs\DobotLink\DobotLink.exe'

function Step($msg) { Write-Host "[start_all] $msg" }
function Fail($msg) {
    Write-Host "[start_all] 실패: $msg" -ForegroundColor Red
    Read-Host '엔터를 누르면 닫힘' | Out-Null
    exit 1
}
function Test-Port($port) { [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) }
function Wait-Port($port, $sec) {
    for ($i = 0; $i -lt $sec * 2; $i++) { if (Test-Port $port) { return $true }; Start-Sleep -Milliseconds 500 }
    return $false
}
function Start-Window($title, $script) {
    # 새 PowerShell 창에서 실행 - 창 제목으로 구분, 에러가 나도 창이 안 닫혀서 로그를 볼 수 있음
    Start-Process powershell -WorkingDirectory $Root -ArgumentList '-NoExit', '-Command',
        "`$host.UI.RawUI.WindowTitle = '$title'; & '$Python' $script"
}

# 1) Redis
if (Test-Port 6379) {
    Step 'Redis 이미 실행 중 - 건너뜀'
} else {
    if (-not (Test-Path $RedisConf)) { Fail "Redis 설정 파일 없음: $RedisConf" }
    Start-Process (Join-Path $RedisDir 'redis-server.exe') -ArgumentList "`"$RedisConf`"" `
        -WorkingDirectory $RedisDir -WindowStyle Minimized
    if (-not (Wait-Port 6379 10)) { Fail "Redis가 안 뜸 - $RedisDir\data\redis.log 확인" }
    Step 'Redis 실행됨 (최소화 창)'
}

# 2) Flask
if (Test-Port 5000) {
    Step 'Flask 이미 실행 중 - 건너뜀'
} else {
    Start-Window 'Flask' 'app.py'
    if (-not (Wait-Port 5000 15)) { Fail 'Flask가 안 뜸 - Flask 창의 에러 확인' }
    Step 'Flask 실행됨'
}

# 3) DobotLink
if (Get-Process DobotLink -ErrorAction SilentlyContinue) {
    Step 'DobotLink 이미 실행 중 - 건너뜀'
} else {
    if (-not (Test-Path $DobotLink)) { Fail "DobotLink 없음: $DobotLink" }
    Start-Process $DobotLink
    Step 'DobotLink 실행 - 8초 대기'
    Start-Sleep -Seconds 8
}
Step '주의: DobotLab 창에서 로봇을 연결하지 말 것 (COM3 충돌)'

# 4) main
if (Test-Port 5001) {
    Fail 'main이 이미 실행 중 (5001 사용 중) - 두 개 띄우면 COM3 충돌. 기존 main 창 확인'
}
Start-Window 'main' 'main_board_robot_arm.py'
Step 'main 실행됨 - 팔이 HOME으로 이동함. 로그는 main 창 / logs 폴더'
Start-Sleep -Seconds 5
