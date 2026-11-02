# 스마트 창고 — 창고팀 로봇 제어 (CO-SHOW 키링 스마트팩토리)

주문이 들어오면 **빈 트레이를 벨트에 올리고, 키캡 4개를 트레이에 담는** 창고 구간의 로봇 제어 코드예요.
2026-11-02 기준: 도면 배치(높이 단차 제외) + 실물에서 찍은 좌표로 키캡 4/4 반복 성공, 키캡 단계 32.9초.

## 담당 범위

| 주문 status | 우리 장비 | 하는 일 | 보고 (Mobius 컨테이너) |
|---|---|---|---|
| `assigned` | 트레이 팔 R4 (COM8) | 빈 트레이를 집어 트레이 벨트 투입 자리(C2)에 놓기 | `cnt_robot_arm/cnt_tray_robot_arm` |
| `keycap_dispensed` | 키캡 벨트 2대 (매직박스) | 60mm/s로 10초 → 키캡이 정지부(끝막이)에 멈춤 | `cnt_conveyor/cnt_keycap_conveyor` |
| `keycap_reached` | 키캡 팔 R1·R2 (COM6·COM7) | 키캡 4개를 트레이에 담고, 하나 놓을 때마다 바로 보고 | `cnt_robot_arm/cnt_keycap_robot_arm` |

- 세트1(R1): keycap1(EI)·keycap2(NS) / 세트2(R2): keycap3(TF)·keycap4(PJ)
- 트레이 벨트(section0/1/2), 보드 팔(R3), 키캡 카트리지(아두이노), AGV는 다른 담당이에요.

## 구조

```
[전시장]  플랫폼 서버 192.168.0.100 (Mobius :7579)
             │  주문 상태 cnt_process (구독 알림 → 이 PC :5003/notify/process + 5초 조회)
             ▼
          local_gateway.py ── warehouse_server.py (단계 판단·보고) ── dobot_control.py (로봇)
             │  결과 보고
             ▼
          창고 PC 보드 Flask 127.0.0.1:5000 /report/...  →  Mobius

```

로봇 API(DobotRPC)는 한 스레드·한 이벤트 루프에서만 호출해요. 게이트웨이는 PC 하나에서 **하나만** 실행해요(잠금 파일로 중복 실행 방지).

## 파일

| 파일 | 역할 |
|---|---|
| `dobot_control.py` | 로봇·벨트 제어 (좌표, 이동, 알람 처리, 두 팔 교대) |
| `warehouse_server.py` | status별 처리, 보고, fault 복구 |
| `local_gateway.py` · `local_feed.py` · `local_transport.py` | 전시장 로컬망: 주문 받기(Mobius 알림 직접 수신+조회), 보드 Flask로 보고 |
| `make_sub.py` | 전시장 로컬 Mobius에 구독 등록 (`sub_warehouse_keycap_robotarm` → `NOTIFY_BASE_URL`) |
| `runtime_config.py` | `.env.local`(전시장) 읽기 |
| `worker_lock.py` | 같은 로봇을 두 프로그램이 동시에 못 움직이게 |
| `demo.py` | 통신 없는 실물 시연·점검·홈 |
| `read_pose.py` | 좌표 찍기, 한 팔 홈·벨트·그리퍼 단독 동작 |
| `pose_set1.json` · `pose_set2.json` | 실물에서 찍은 키캡 팔 좌표 (최우선) |
| `pose_tray_design.json` | 트레이 팔 좌표 (도면 추정값, `LAYOUT=design`일 때 사용. 실물 트레이가 오면 찍어서 `pose_tray.json`으로) |
| `.env.local.example` | 설정 예시 (실제 `.env.local`은 저장소에 올리지 않음) |
| `참고사항.md` | **운영 주의사항·알려진 문제·결정 사항** — 꼭 읽기 |

## 준비

- Python: 저장소 루트의 공용 가상환경 `venv` (Python 3.9, 루트 `requirements.txt`)
- DobotLink 실행. DobotLab은 포트를 잡으므로 **연결을 끊고** 실행
- 로봇 USB: 세트1 COM6, 세트2 COM7, 트레이 COM8 (`.env`의 `SET1_PORT` 등으로 변경 가능)
- 설정: `.env.local.example` → `.env.local`. AE 값은 팀 담당자에게 받아요

## 실행

```powershell
# 1) 로봇 점검 (안 움직임) — 전원을 새로 켰으면 다음 줄의 home
& '..\venv\Scripts\python.exe' demo.py check
& '..\venv\Scripts\python.exe' demo.py home     # 한 대씩 홈 → 집기 자리 위로 비킴

# 2-a) 통신 없이 시연: 트레이 → 벨트 → 키캡 4개 (tray / belts / arms 로 단계별도 가능)
& '..\venv\Scripts\python.exe' demo.py run

# 2-b) 전시장 실전
& '..\venv\Scripts\python.exe' local_gateway.py --env .env.local --check   # 설정 검사만
& '..\venv\Scripts\python.exe' local_gateway.py --env .env.local


```

## 핵심 동작 규칙

- **좌표 우선순위**: 코드 기본값 → `pose_tray_design.json`(`LAYOUT=design`) → 실물에서 찍은 `pose_*.json`. 켤 때 반경 230~330mm, 관절 각도 ±125°를 벗어나면 경고해요.
- **이동**: 빈 이동·운반은 JUMP(올라감 → 이동 → 내려감), 두 번째 키캡은 바로 위에서 수직(MOVL)으로 내려가요. 팔이 완전히 멈춘 걸 확인한 뒤 다음 명령을 보내요. 명령이 무시되면 다시 보내요. 키캡을 들고 갈 때는 MOVJ로 바꾸지 않아요.
- **놓은 뒤**: 바로 JUMP로 빠져요(`PLACE_LIFT=0`). 놓는 자리가 330mm 가까이 가서 알람이 반복되면 `PLACE_LIFT=1`(살짝 들기 + 몸 쪽 당기기)로 바꿔요.
- **두 팔 충돌 방지**: 트레이 자리는 한 번에 한 팔만 들어가요. 먼저 집은 팔이 먼저 들어가요(1·3·2·4 또는 3·1·4·2). 못 빠진 팔이 있으면 다른 팔은 최대 15초 기다려요.
- **알람(빨간불)**: 움직이기 전, 명령이 무시됐을 때, 도착 실패 뒤, 단계 끝에 자동 해제해요. 관절 한계에 걸려 있거나 탈조(충돌)면 해제만으로 안 풀려요 → 손으로 옮기거나 홈. 자세한 표는 `참고사항.md` 7장에 있어요.
- **성공 판단**: 그리퍼 센서가 없어요. 좌표 도착 + 그리퍼 명령 응답 + 트레이 자리에서 빠짐을 성공으로 봐요.
- **보고·fault**: 장비는 단계당 한 번만 움직여요. 보고만 실패하면 보고만 다시 보내요. fault가 풀리면 트레이 팔·벨트는 다시 실행하고, 키캡 팔은 4개 모두 `done`으로 다시 보고해요(`MANUAL_FIX_DONE=1`, 사람이 조치했다는 뜻).

## 시간 (2026-11-02 실측)

| 단계 | 시간 | 플랫폼 제한 |
|---|---|---|
| 트레이 팔 | 약 10초 | 60초 |
| 키캡 벨트 | 약 11초 | 60초 |
| 키캡 팔 4개 | 약 33초 | 60초 |

## 남은 일

- 실물 트레이가 오면 트레이 팔 좌표 찍기, 여러 개 쌓였을 때 높이 바뀌는 처리
- 디스펜서·카메라·높이 단차를 설치한 뒤 좌표를 다시 확인하고, 필요한 구간에만 중간 좌표 넣기
- 전시장: `.env.local`의 AE 값, 보드팀 최신 서버와 보고 형식 대조, 서버 재시작 시 seq 초기화 대응 (참고사항 2장)
- 알람 분류 알림(화면·소리·기록 파일, 플랫폼 `reason`) — 참고사항 7·9장
