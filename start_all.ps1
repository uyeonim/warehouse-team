# 창고 PC 실행 스크립트 (start_all.bat 더블클릭)
#   서버:  Redis(6379) -> 보드 Flask(5000) -> 키캡 Flask(5002)
#   로봇:  DobotLink -> 보드 main -> 키캡·트레이 게이트웨이(고정현, 로봇 3대)
# - 서버는 Windows 서비스(service\install_services.ps1)로 등록돼 있으면 서비스가 띄움 - 여기선 떠 있는지만 확인
#   서비스가 없으면 예전처럼 창으로 띄움
# - 이미 켜진 건 건너뜀 (포트/프로세스로 판단)
#   키캡 Flask(waitress)는 Windows에서 같은 포트에 두 개가 떠도 에러가 안 나서 특히 중복 확인 필요
# - 서버가 안 뜨면 거기서 멈춤 (Redis 없이 Flask를 띄우면 재고가 0으로 보임)
# - 로봇 프로그램은 두 개 뜨면 같은 COM 포트를 두고 충돌 -> 떠 있으면 다시 안 띄움
# 끌 때는 로봇 창에서 Ctrl+C (게이트웨이 -> main), 창으로 띄운 서버는 키캡 Flask -> 보드 Flask -> Redis 순서
# -Rehearsal (start_rehearsal.bat): 로봇 프로그램 2개를 리허설 모드로 - 로봇·컨베이어·카메라는 움직인 척,
#   보고는 실제로 나감. 실물 없이 전시장 통신 전체 시험용. DobotLink 안 켬
# -StatusOnly (status.bat, 바탕화면 '창고 점검'): 상태판만 띄우고 브라우저로 열기
# -Test (start_test.bat, 바탕화면 '창고 테스트'): 창고 시작과 같고, 브라우저에 테스트 페이지(/test) 탭도 같이 열기
param([switch]$Rehearsal, [switch]$StatusOnly, [switch]$Test)
$ErrorActionPreference = 'Stop'
$Root       = $PSScriptRoot                                  # 저장소 루트
$Python     = Join-Path $Root 'venv\Scripts\python.exe'       # 공용 가상환경 (Python 3.9, requirements.txt)
$Waitress   = Join-Path $Root 'venv\Scripts\waitress-serve.exe'
$BoardDir   = Join-Path $Root 'board'                         # 보드 main + 보드 Flask
$KeycapDir  = Join-Path $Root 'keycap_flask'
$GatewayDir = Join-Path $Root 'keycap_tray'                   # 키캡 팔 2대 + 트레이 팔 + 키캡 벨트
$GatewayEnv = Join-Path $GatewayDir '.env.local'
$RedisDir   = Join-Path (Split-Path $Root -Parent) 'redis'     # Windows용 Redis - 저장소 폴더 옆 (예: C:\warehouse\redis)
$RedisConf  = Join-Path $RedisDir 'redis.warehouse.conf'
$DobotLink  = Join-Path $env:LOCALAPPDATA 'Programs\DobotLink\DobotLink.exe'
# 리허설 스위치는 환경 변수로 로봇 프로그램 창에 물려줌 (새 창은 이 창의 환경 변수를 이어받음)
if ($Rehearsal) { $env:WAREHOUSE_REHEARSAL = '1'; $Tag = '[리허설] ' }
else { Remove-Item Env:WAREHOUSE_REHEARSAL -ErrorAction SilentlyContinue; $Tag = '' }

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
$UseWt = [bool](Get-Command wt.exe -ErrorAction SilentlyContinue)
function Start-Window($title, $dir, $command) {
    # Windows Terminal 창 하나("warehouse")에 탭으로 - 탭 이름으로 구분 (wt가 없으면 예전처럼 PowerShell 창 하나씩)
    # 프로그램이 끝나면(Ctrl+C·에러) 'exit 1'로 끝내서 탭이 남음 -> 로그를 볼 수 있고, 그 탭에서 Enter = 같은 명령으로
    # 다시 시작, Ctrl+D = 탭 닫기 (wt 1.18+ 기능). 코드 고친 뒤 창고 시작을 다시 눌러 탭이 쌓이지 않게 (11/05)
    # 명령은 인코딩해서 넘김 - wt가 ';'를 자기 명령 구분자로 먹어서
    $script = "`$host.UI.RawUI.WindowTitle = '$title'; $command"
    if ($UseWt) {
        $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes(
            "$script; Write-Host ''; Write-Host '[$title] 꺼짐 - Enter = 다시 시작, Ctrl+D = 탭 닫기' -ForegroundColor Yellow; exit 1"))
        Start-Process wt.exe -ArgumentList ("-w warehouse new-tab --title `"$title`" --suppressApplicationTitle " +
            "-d `"$dir`" powershell -EncodedCommand $enc")
        Start-Sleep -Milliseconds 800   # 연달아 띄우면 창이 따로 생길 수 있어서 앞 탭이 뜰 때까지
    } else {
        Start-Process powershell -WorkingDirectory $dir -ArgumentList '-NoExit', '-Command', $script
    }
}
function Ensure-Server($label, $port, $service, [scriptblock]$startWindow) {
    if (Test-Port $port) { Step "$label 실행 중 ($port)"; return }
    $svc = Get-Service $service -ErrorAction SilentlyContinue
    if ($svc) {
        # 서비스는 죽어도 3초 뒤 자동 재시작 - 잠깐 기다려 봄. 서비스 시작/중지는 관리자 권한이라 여기선 안 함
        Step "$label 서비스($service) 상태 $($svc.Status) - 응답 대기"
        if (-not (Wait-Port $port 15)) {
            Fail "$label 서비스가 응답 없음 - 관리자 PowerShell에서 'Start-Service $service' 후 logs\svc_$service.log 확인"
        }
        Step "$label 실행 중 ($port, 서비스)"
        return
    }
    & $startWindow
    if (-not (Wait-Port $port 15)) { Fail "$label 이(가) 안 뜸 - $label 창의 에러 확인" }
    Step "$label 실행됨 ($port, 창)"
}

$StatusUrl = 'http://127.0.0.1:5080'
if ($StatusOnly) {
    # 창고 점검(status.bat): 상태판만 띄우고(없으면) 브라우저로 열기 - 로봇·서버는 안 건드림
    Ensure-Server '상태판' 5080 'warehouse-status-page' {
        Start-Window '상태판' $Root "& '$Python' status_page.py"
    }
    Start-Process $StatusUrl
    exit 0
}

# ---------------- 서버 ----------------
Ensure-Server 'Redis' 6379 'warehouse-redis' {
    if (-not (Test-Path $RedisConf)) { Fail "Redis 설정 파일 없음: $RedisConf" }
    Start-Window 'Redis' $RedisDir "& '$(Join-Path $RedisDir 'redis-server.exe')' '$RedisConf'"
}
Ensure-Server '보드 Flask' 5000 'warehouse-board-flask' {
    Start-Window 'Flask' $BoardDir "& '$Python' app.py"
}
Ensure-Server '키캡 Flask' 5002 'warehouse-keycap-flask' {
    if (-not (Test-Path $Waitress)) { Fail "키캡 Flask 실행 파일 없음: $Waitress" }
    Start-Window '키캡 Flask' $KeycapDir "& '$Waitress' --listen=0.0.0.0:5002 app:app"
}
# 상태판 (읽기 전용 웹, http://<노트북 IP>:5080) - 로봇 안 움직임
Ensure-Server '상태판' 5080 'warehouse-status-page' {
    Start-Window '상태판' $Root "& '$Python' status_page.py"
}

# ---------------- 로봇 ----------------
if ($Rehearsal) {
    Write-Host '[start_all] 리허설 모드 - 로봇은 안 움직이지만 Mobius 보고·보드 재고 차감은 실제로 일어남. 끝나면 재고 원복' -ForegroundColor Yellow
    Write-Host '[start_all] 키캡 아두이노는 리허설과 무관하게 실제로 배출함 - 원치 않으면 아두이노 전원을 끌 것' -ForegroundColor Yellow
} elseif (Get-Process DobotLink -ErrorAction SilentlyContinue) {
    Step 'DobotLink 실행 중'
} else {
    if (-not (Test-Path $DobotLink)) { Fail "DobotLink 없음: $DobotLink" }
    Start-Process $DobotLink
    Step 'DobotLink 실행 - 8초 대기'
    Start-Sleep -Seconds 8
}
if (-not $Rehearsal) { Step '주의: DobotLab 창에서 로봇을 연결하지 말 것 (COM 포트 충돌)' }

# 보드 main (보드 팔 + 트레이 벨트)
if (Test-Port 5001) {
    Step '보드 main 이미 실행 중 (5001) - 건너뜀 (리허설/실제 모드는 그 창 제목으로 확인)'
} else {
    Start-Window "${Tag}main" $BoardDir "& '$Python' main_board_robot_arm.py"
    if ($Rehearsal) { Step "${Tag}보드 main 실행됨" } else { Step '보드 main 실행됨 - 팔이 HOME으로 이동함' }
}

# 키캡·트레이 게이트웨이 (키캡 팔 2대 + 트레이 팔 + 키캡 벨트) - 준비 안 됐으면 건너뜀
$gatewayRunning = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -match 'local_gateway\.py' }
if ($gatewayRunning) {
    Step '키캡·트레이 게이트웨이 이미 실행 중 - 건너뜀'
} elseif (-not (Test-Path $GatewayEnv)) {
    Step "키캡·트레이 게이트웨이 준비 안 됨(.env.local 없음: $GatewayDir) - 건너뜀"
} else {
    Start-Window "${Tag}키캡·트레이 게이트웨이" $GatewayDir "& '$Python' local_gateway.py --env .env.local"
    Step "${Tag}키캡·트레이 게이트웨이 실행됨"
}
Step '로그: warehouse 터미널 창의 각 탭 / board\logs, keycap_tray\logs 폴더 / logs 폴더(서비스)'
Start-Process $StatusUrl   # 상태판을 브라우저로
if ($Test) { Start-Process "$StatusUrl/test" }   # 테스트 페이지 - 주문 스냅샷을 손으로 올림 (로봇이 실제로 움직임)
Start-Sleep -Seconds 5
