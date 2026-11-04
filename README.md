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
| Redis | 저장소 옆 `redis\` (예: `C:\warehouse\redis`) | 6379 (노트북 내부만) | 서버 | 재고 · 대기열 · 게이트 (키캡+보드) |
| 보드 Flask | `board/app.py` | 5000 (노트북 내부만) | 서버 | 보드 main·게이트웨이 보고 → Mobius CIN, 보드 재고 |
| 키캡 Flask | `keycap_flask/app.py` | 5002 | 서버 | Mobius 알림 → MQTT로 아두이노 전달, 아두이노 보고 → Mobius CIN |
| 보드 main | `board/main_board_robot_arm.py` | 5001 (알림 수신) | 로봇 | `tray_reached`/`board_mismatched` 보드 픽업, 트레이 벨트 section0/1/2 |
| 키캡·트레이 게이트웨이 | `keycap_tray/local_gateway.py` | 5003 (알림 수신) | 로봇 | `assigned` 트레이 투입, `keycap_dispensed` 키캡 벨트, `keycap_reached` 키캡 4개 적재 |
| 키캡 아두이노 ×4 | `firmware/` | – | 장치 | 카트리지에서 키캡 배출 (EI · NS · TF · PJ) |

- 주문 받기: 로봇 프로그램 둘 다 **Mobius 알림 직접 수신 + 5초마다 `cnt_process/la` 조회**(알림 누락 보충). 알림은 "직전 seq와 다르면", 조회는 "클 때만" 받음.
- Mobius 구독 (로컬 Mobius `cnt_process`): `sub_warehouse_robotarm`(→5001) · `sub_warehouse_keycap_robotarm`(→5003) · `sub_warehouse_keycap_cartridge`(→5002)
- 로봇 팔은 **USB 시리얼로 찾음** (보드: `board_config.DOBOT_SERIAL`, 키캡·트레이: `.env.local`의 `SET1_SERIAL`·`SET2_SERIAL`·`TRAY_SERIAL`) —
  USB를 다른 구멍에 꽂아 COM 번호가 바뀌어도 팔이 뒤바뀌지 않음. 시리얼이 안 보이면 그 팔은 연결하지 않음 (보드 main은 5초마다 다시 찾음, 게이트웨이는 켜지지 않음).

## 폴더

```
├─ start_all.bat / start_all.ps1   실행 스크립트 (서버 확인 → DobotLink → 로봇 프로그램 2개)
├─ status.bat                      상태판을 브라우저로 (바탕화면 '창고 점검')
├─ start_test.bat                  창고 시작 + 테스트 페이지(/test) 탭 (바탕화면 '창고 테스트')
├─ test_page.py                    테스트 페이지 - 주문 스냅샷을 손으로 cnt_process에 올림 (같은 공유기에서만)
├─ reset_stock.bat                 재고·창고 풀 전부 초기화 (전시 총량표, 재부팅 필요 없음) - 평소엔 테스트 페이지 '우리 재고만 초기화' 버튼
├─ setup_laptop.bat / setup/      새 노트북 한 번 설정 (관리자) + Redis 설정 원본 - 노트북_이전_가이드.md
├─ service/                        서버 3개 Windows 서비스 등록/해제 (NSSM, 관리자)
├─ requirements.txt                공용 가상환경 (Python 3.9)
├─ board/                          보드 팔 + 트레이 벨트 + 보드 Flask   (.env.example)
│  └─ tools/                       좌표·R 튜닝, 카메라, 픽업/컨베이어 점검, 구독 등록, 통신 테스트
├─ keycap_flask/                   키캡 Flask + 구독 등록             (.env.example)
├─ keycap_tray/                    키캡 팔 2 · 트레이 팔 · 키캡 벨트   (.env.local.example, README, 참고사항.md)
└─ firmware/                       키캡 아두이노 4대 EI · NS · TF · PJ (11/04 키캡팀 버전 - 원격 복구·재접속, config.h.example 복사해 값 채움)
```

## 처음 설치 (창고 노트북 한 번)

> **새 노트북으로 옮길 때는 `노트북_이전_가이드.md`** — 챙길 파일 3종 + `setup_laptop.bat`(아래 1·3·4번과 전원·바로가기를 자동으로) + 확인 순서.
> 폴더는 저장소 옆에 `redis\`(필수)·`nssm\`(선택)을 두는 구조 — 사용자 이름과 무관.

1. Python 3.9 가상환경
   ```powershell
   py -3.9 -m venv venv
   .\venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
2. 설정 파일: `board/.env.example` → `board/.env`, `keycap_flask/.env.example` → `keycap_flask/.env`,
   `keycap_tray/.env.local.example` → `keycap_tray/.env.local` 로 복사해 값 채우기 (AE 값은 팀 담당자). **절대 커밋·공유 금지**
3. Redis: Windows용 Redis 5.0을 저장소 옆 `redis\`에 두고 `setup\redis.warehouse.conf`를 그 폴더로 복사 (127.0.0.1만, AOF 켬, 데이터는 `redis\data\`)
4. 방화벽 인바운드 허용 (관리자): 5001 · 5002 · 5003 · 5080(상태판) — 규칙 이름은 `warehouse-`로 시작 (점검 스크립트가 이 이름으로 확인)
5. 창고 노트북 IP 고정(공유기 주소 예약) — Mobius 구독과 아두이노 `config.h`가 이 주소를 가리킴
6. Mobius 구독 등록 (각 프로그램을 켠 상태에서, 한 번만)
   ```powershell
   cd board;        ..\venv\Scripts\python.exe tools\sub_robotarm.py
   cd keycap_flask; ..\venv\Scripts\python.exe make_sub.py
   cd keycap_tray;  ..\venv\Scripts\python.exe make_sub.py
   ```
7. (선택) 서버 3개를 서비스로: NSSM `nssm.exe`를 저장소 옆 `nssm\`에 두고 관리자 PowerShell에서 `service\install_services.ps1`

## 실행 · 종료

> 전시 당일 운영은 **`운영_매뉴얼.md`** (1장). 켜기 전/문제 생겼을 때 **`check_all.bat`**(읽기 전용 점검: 설정·네트워크·Mobius 구독·서버·로봇 USB·전원 설정,
> 로봇 프로그램 상태 - 보드 main·게이트웨이가 5초마다 Redis `hb:*`에 남기는 heartbeat로 "떠 있는데 멈춤/로봇 끊김/보고 대기"까지 표시)
> **상태판**: 같은 점검을 웹으로 - iPad·휴대폰에서 `http://192.168.0.12:5080` (읽기 전용, 10초마다 갱신, `start_all`이 같이 띄움).
> 다른 기기에서 보려면 방화벽 5080 인바운드 허용 (관리자): `netsh advfirewall firewall add rule name="warehouse-status-5080" dir=in action=allow protocol=TCP localport=5080`

- 실행: 충전기 연결(절전 방지) → 로봇·매직박스 전원/USB → DobotLab 연결 끊기 → **`start_all.bat` 더블클릭**
  - 서버 확인(서비스가 없으면 띄움) → DobotLink → 보드 main → 키캡·트레이 게이트웨이 → 상태판을 브라우저로
  - 띄우는 프로그램은 전부 Windows Terminal 창 하나(`warehouse`)의 탭으로 (탭 이름 = 프로그램). wt가 없으면 창 하나씩
  - 상태판만 열기: `status.bat` (바탕화면 '창고 점검') - 안 떠 있으면 띄우고 브라우저로. 명령창 점검은 `check_all.bat`
  - 테스트 주문: `start_test.bat` (바탕화면 '창고 테스트') - 창고 시작 + `http://<노트북 IP>:5080/test` 탭. 올리면 로봇이 실제로 움직임
  - 보드 main은 켜자마자 팔이 HOME으로 감. 키캡 팔은 전원을 새로 켰으면 `keycap_tray`에서 `demo.py home` 먼저
- **리허설 (로봇 없이 통신 전체 시험)**: `start_rehearsal.bat` — 보드 main·게이트웨이가 로봇·컨베이어·카메라를 "움직인 척"하고
  보고는 실제로 보냄(Mobius·보드 Flask·Redis까지 실전과 같음). 창 제목에 `[리허설]`. 주의: 보드 재고가 실제로 차감되고
  관리자 페이지에도 보고가 보임 → 끝나면 재고 원복. 키캡 아두이노는 리허설과 무관하게 실제 배출(원치 않으면 아두이노 전원 끄기)
- 종료: 로봇 창 Ctrl+C (게이트웨이 → 보드 main). 창으로 띄운 서버는 키캡 Flask → 보드 Flask → Redis 순서
- 로그: 각 탭, `board/logs/`(보드 main·Flask 날짜별), `keycap_tray/logs/`(게이트웨이 날짜별), `logs/`(서비스)

## 문제가 생겼을 때

| 증상 | 조치 |
|---|---|
| 팔이 안 움직임 / 도착 실패 | 관절 한계 알람 — 팔을 손으로 가운데로 옮기고 해당 로봇 프로그램 재시작 (`keycap_tray/참고사항.md` 1·7장) |
| `device is not connected` / `USB 시리얼 … 안 보임` | USB·전원 확인 → `check_all.bat`의 "로봇 USB" 확인 → 해당 로봇 프로그램 재시작 |
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
