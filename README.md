# warehouse-team

Agentic Mini Keyboard Keyring Factory — 스마트 창고팀 (CO-SHOW 2026)

전시장 내부망(192.168.0.x)에서 **인터넷 없이** 창고 구간 전체(키캡 카트리지 · 키캡 팔 · 트레이 팔 · 트레이 벨트 · 보드 팔)를
창고 노트북 한 대로 운영하는 코드. 2026-11-03 통합 기준.

## 전체 구성

```
플랫폼 서버 192.168.0.100 (플랫폼팀)
  Mobius :7579  ── cnt_process 구독 알림 ──┬──> 보드 main      :5001   (보드 팔 + 트레이 벨트)
                                            ├──> 키캡·트레이 GW :5003   (키캡 팔 2 + 트레이 팔 + 키캡 벨트)
                                            └──> 키캡 Flask     :5002 ──MQTT──> Mosquitto :1883 ──> 키캡 아두이노 ×4
  Mobius <── CIN 보고 ── 보드 Flask :5000 <── 보드 main / 키캡·트레이 GW
  Mobius <── CIN 보고 ── 키캡 Flask :5002 <── 키캡 아두이노 (HTTP)

창고 노트북 192.168.0.12 : 위의 5000/5001/5002/5003 + Redis 127.0.0.1:6379 (DB 0)
```

| 프로그램 | 폴더 | 포트 | 종류 | 맡는 일 |
|---|---|---|---|---|
| Redis | 저장소 밖 `C:\Users\dusdn\redis` | 6379 (노트북 내부만) | 서버 | 재고 · 대기열 · 게이트 (키캡+보드) |
| 보드 Flask | `board/app.py` | 5000 (노트북 내부만) | 서버 | 보드 main·게이트웨이 보고 → Mobius CIN, 보드 재고 |
| 키캡 Flask | `keycap_flask/app.py` | 5002 | 서버 | Mobius 알림 → MQTT로 아두이노 전달, 아두이노 보고 → Mobius CIN |
| 보드 main | `board/main_board_robot_arm.py` | 5001 (알림 수신) | 로봇 | `tray_reached`/`board_mismatched` 보드 픽업, 트레이 벨트 section0/1/2 |
| 키캡·트레이 게이트웨이 | `keycap_tray/local_gateway.py` | 5003 (알림 수신) | 로봇 | `assigned` 트레이 투입, `keycap_dispensed` 키캡 벨트, `keycap_reached` 키캡 4개 적재 |
| 키캡 아두이노 ×4 | `firmware/` | – | 장치 | 카트리지에서 키캡 배출 (EI · NS · TF · PJ) |

- 주문 받기: 로봇 프로그램 둘 다 **Mobius 알림 직접 수신 + 5초마다 `cnt_process/la` 조회**(알림 누락 보충). 알림은 "직전 seq와 다르면", 조회는 "클 때만" 받음.
- Mobius 구독 (로컬 Mobius `cnt_process`): `sub_warehouse_robotarm`(→5001) · `sub_warehouse_keycap_robotarm`(→5003) · `sub_warehouse_keycap_cartridge`(→5002)
- 로봇 프로그램은 서로 다른 COM 포트를 씀 — **USB 꽂는 구멍을 고정하고 라벨**을 붙일 것 (바뀌면 다른 팔 좌표로 움직여 충돌).

## 폴더

```
├─ start_all.bat / start_all.ps1   실행 스크립트 (서버 확인 → DobotLink → 로봇 프로그램 2개)
├─ service/                        서버 3개 Windows 서비스 등록/해제 (NSSM, 관리자)
├─ requirements.txt                공용 가상환경 (Python 3.9)
├─ board/                          보드 팔 + 트레이 벨트 + 보드 Flask   (.env.example)
│  └─ tools/                       좌표·R 튜닝, 카메라, 픽업/컨베이어 점검, 구독 등록, 통신 테스트
├─ keycap_flask/                   키캡 Flask + 구독 등록             (.env.example)
├─ keycap_tray/                    키캡 팔 2 · 트레이 팔 · 키캡 벨트   (.env.local.example, README, 참고사항.md)
└─ firmware/                       키캡 아두이노 PJ · TF (EI · NS는 키캡팀 최신 로컬 버전 반영 대기)
```

## 처음 설치 (창고 노트북 한 번)

1. Python 3.9 가상환경
   ```powershell
   py -3.9 -m venv venv
   .\venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
2. 설정 파일: `board/.env.example` → `board/.env`, `keycap_flask/.env.example` → `keycap_flask/.env`,
   `keycap_tray/.env.local.example` → `keycap_tray/.env.local` 로 복사해 값 채우기 (AE 값은 팀 담당자). **절대 커밋·공유 금지**
3. Redis: Windows용 Redis 5.0을 `C:\Users\dusdn\redis`에 두고 `redis.warehouse.conf` 사용 (127.0.0.1만, AOF 켬)
4. 방화벽 인바운드 허용 (관리자): 5001 · 5002 · 5003
5. 창고 노트북 IP 고정(공유기 주소 예약) — Mobius 구독과 아두이노 `config.h`가 이 주소를 가리킴
6. Mobius 구독 등록 (각 프로그램을 켠 상태에서, 한 번만)
   ```powershell
   cd board;        ..\venv\Scripts\python.exe tools\sub_robotarm.py
   cd keycap_flask; ..\venv\Scripts\python.exe make_sub.py
   cd keycap_tray;  ..\venv\Scripts\python.exe make_sub.py
   ```
7. (선택) 서버 3개를 서비스로: NSSM `nssm.exe`를 `C:\Users\dusdn\nssm\`에 두고 관리자 PowerShell에서 `service\install_services.ps1`

## 실행 · 종료

> 전시 당일 운영은 **`운영_매뉴얼.md`** (1장). 켜기 전/문제 생겼을 때 **`check_all.bat`**(읽기 전용 점검: 설정·네트워크·Mobius 구독·서버·로봇 USB·전원 설정)

- 실행: 충전기 연결(절전 방지) → 로봇·매직박스 전원/USB → DobotLab 연결 끊기 → **`start_all.bat` 더블클릭**
  - 서버 3개 확인(서비스가 없으면 창으로 띄움) → DobotLink → 보드 main → 키캡·트레이 게이트웨이
  - 보드 main은 켜자마자 팔이 HOME으로 감. 키캡 팔은 전원을 새로 켰으면 `keycap_tray`에서 `demo.py home` 먼저
- 종료: 로봇 창 Ctrl+C (게이트웨이 → 보드 main). 창으로 띄운 서버는 키캡 Flask → 보드 Flask → Redis 순서
- 로그: 각 창, `board/logs/`(보드 main·Flask 날짜별), `keycap_tray/logs/`(게이트웨이 날짜별), `logs/`(서비스)

## 문제가 생겼을 때

| 증상 | 조치 |
|---|---|
| 팔이 안 움직임 / 도착 실패 | 관절 한계 알람 — 팔을 손으로 가운데로 옮기고 해당 로봇 프로그램 재시작 (`keycap_tray/참고사항.md` 1·7장) |
| `device is not connected` | USB 확인 → 장치 관리자에서 COM 번호 확인 → 해당 로봇 프로그램 재시작 |
| 보드 main 로그 `주기 조회 실패` | 노트북 ↔ 플랫폼 서버 네트워크 확인 (Wi-Fi 자동 전환 주의, 유선 권장) |
| 노트북 IP가 192.168.0.12가 아님 | 각 `.env`의 `NOTIFY_BASE_URL` 수정 → 구독 3개 재등록 → 아두이노 `config.h`의 `FLASK_HOST` 수정 |
| 같은 프로그램이 두 개 뜸 | 하나 끄기. 키캡 Flask(waitress)는 같은 포트에 두 개 떠도 에러가 안 남 |

## 브랜치 규칙

- `main` = 전시장에서 실제로 돌리는 통합본. 팀원 각자 이름 브랜치에서 작업 후 PR로 main에 반영
- 통합 전 작업 기록은 각자 브랜치(`nohyeonwoo` · `leehyunjung` · `kojunghyun` 등)에 그대로 남아 있음

```bash
git checkout main && git pull           # 최신 받기
git checkout <본인이름> && git merge main
git add . && git commit -m "무엇을 왜 바꿨는지" && git push origin <본인이름>
```
