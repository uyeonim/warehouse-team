# warehouse-team

Agentic Mini Keyboard Keyring Factory — 스마트 창고팀

## 구조 (v2, 2026-09-05 확정 기준)

- `firmware/Uno_Storage/` — 선반 아두이노(R4 WiFi) 5대, HiveMQ MQTT 직접 구독
- `robot_arm/` — PC 로봇팔 제어 (Dobot Magician Lite ×3 + 컨베이어 ×3, Mobius HTTP 폴링)

ESP32 총괄 구조는 9/5부로 폐기됨. 이전 코드는 태그
`archive/pre-cleanup-2026-09` 에서 확인 가능.

## 브랜치 규칙

- 팀원 각자 자기 이름으로 브랜치 생성 후 작업
- 작업 완료 후 main으로 PR
- 예: noyeonwoo, kimXX, leeXX
