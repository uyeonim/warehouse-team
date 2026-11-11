# 창고 끄기 (stop_all.bat, 바탕화면 '창고 끄기')
#   끄는 것: 키캡·트레이 게이트웨이 -> 보드 main -> 가짜 Mobius(fake_order.py, 켜져 있으면)
#   안 끄는 것: Redis·보드 Flask·키캡 Flask·상태판 (Windows 서비스라 계속 떠 있어야 재고·상태판이 보임), DobotLink
# - 먼저 Ctrl+C 를 보내 정상 종료시킴 (로봇 연결 해제·컨베이어 정지 마무리가 돎). 15초 안에 안 꺼지면 강제 종료
#   -> 강제 종료한 경우 컨베이어가 돌고 있을 수 있으니 확인할 것
$ErrorActionPreference = 'Continue'
$Root   = $PSScriptRoot
$Python = Join-Path $Root 'venv\Scripts\python.exe'
$Helper = Join-Path $Root 'stop_proc.py'

function Step($msg) { Write-Host "[stop_all] $msg" }

function Stop-Program($label, $pattern) {
    $procs = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.CommandLine -match $pattern })
    if ($procs.Count -eq 0) { Step "$label 꺼져 있음"; return }
    foreach ($p in $procs) {
        Step "$label 끄는 중 (PID $($p.ProcessId)) - Ctrl+C 보냄"
        & $Python $Helper $p.ProcessId | Out-Null
        $gone = $false
        for ($i = 0; $i -lt 30; $i++) {
            if (-not (Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue)) { $gone = $true; break }
            Start-Sleep -Milliseconds 500
        }
        if ($gone) { Step "$label 정상 종료" }
        else {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "[stop_all] $label 이(가) 15초 안에 안 꺼져 강제 종료함 - 컨베이어·로봇 상태 확인할 것" -ForegroundColor Yellow
        }
    }
}

Stop-Program '키캡·트레이 게이트웨이' 'local_gateway\.py'
Stop-Program '보드 main'              'main_board_robot_arm\.py'
Stop-Program '가짜 Mobius'            'fake_order\.py'
# 보드 Flask: 창으로 띄운 것만 끔 (서비스면 그대로). 가짜 Mobius 모드로 띄웠을 수 있어서 - 다음 시작에서 모드에 맞게 다시 뜸. 재고는 Redis 에 남음
# 키캡 Flask(5002)도 같음 (가짜 모드에선 주문을 MQTT 로 아두이노에 넘기는 곳이라 같이 가짜 Mobius 를 봄)
foreach ($f in @(@{ Port = 5000; Svc = 'warehouse-board-flask'; Label = '보드 Flask' },
                 @{ Port = 5002; Svc = 'warehouse-keycap-flask'; Label = '키캡 Flask' })) {
    if (Get-Service $f.Svc -ErrorAction SilentlyContinue) { continue }
    $owners = @(Get-NetTCPConnection -LocalPort $f.Port -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
    if ($owners.Count -gt 0) {
        Step "$($f.Label) 끄는 중 (PID $($owners -join ',')) - 다음 시작에서 모드에 맞게 다시 뜸"
        foreach ($o in $owners) { Stop-Process -Id $o -Force -ErrorAction SilentlyContinue }
    }
}
Step '끝. Redis·Flask·상태판은 그대로 켜 둠. 다시 시작은 바탕화면 "창고 시작" / "창고 테스트"'
Start-Sleep -Seconds 4
