@echo off
rem Reset all stock and pools to the exhibition totals (keycap_flask /reset/hard) - no reboot needed
chcp 65001 >nul
echo 재고와 창고 풀을 전시 총량표로 전부 초기화합니다.
echo   키캡 32칸 카트리지 30개, 보드 4색 20개, 창고 풀은 총량에서 카트리지분을 뺀 값
echo   진행 중 주문의 아두이노 큐와 게이트도 비웁니다. 고장(disable) 칸은 그대로 둡니다.
echo.
set /p ok=계속하려면 reset 입력: 
if /i not "%ok%"=="reset" (
  echo 취소했습니다.
  pause
  exit /b
)
curl -s -X POST http://127.0.0.1:5002/reset/hard
echo.
echo 아두이노는 바로, 보드 main은 30초 안에 새 재고를 따라갑니다. 상태판 카트리지 칸에서 확인하세요.
pause
