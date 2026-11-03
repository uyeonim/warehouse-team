# 창고 서버 서비스 4개를 멈추고 등록 해제 (관리자 PowerShell에서 실행)
# 해제 후에는 start_all이 예전처럼 창으로 띄움 - 서비스 방식에 문제가 있을 때 되돌리는 용도
$ErrorActionPreference = 'Stop'
$Nssm = Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'nssm\nssm.exe'   # 저장소 폴더 옆

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw '관리자 PowerShell에서 실행해야 함' }
if (-not (Test-Path $Nssm)) { throw "nssm.exe 없음: $Nssm" }

# Redis에 기대는 Flask부터 내림
foreach ($name in 'warehouse-status-page', 'warehouse-keycap-flask', 'warehouse-board-flask', 'warehouse-redis') {
    if (-not (Get-Service $name -ErrorAction SilentlyContinue)) { Write-Host "[skip] $name 등록 안 돼 있음"; continue }
    & $Nssm stop $name | Out-Null
    & $Nssm remove $name confirm | Out-Null
    Write-Host "[ok] $name 해제"
}
