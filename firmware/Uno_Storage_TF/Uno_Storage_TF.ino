/*
  ================================================================================
  키캡 배출 아두이노 - T/F 축 담당 (Arduino Uno R4 WiFi)
  ================================================================================
  역할
   1) 로컬 Mosquitto(MQTT, 평문)에 직접 연결해서 "sjuiot/warehouse/process" 구독
      (주문/재고보충 push는 MQTT 한 방향으로만 옴: Mobius -> Flask 웹훅 -> MQTT publish.
       Flask(/notify/process)는 con 내용을 파싱하지 않고 그대로 relay만 하므로,
       orders/restock 파싱과 그에 따른 후속 HTTP 호출은 전부 이 아두이노가 직접 함)
   2) status == "assigned" 인 주문에서, 내 축(keycap 문자열의 AXIS_INDEX 위치)
      글자와 그 위치의 색상을 뽑아서 로컬 큐 + 서버 큐(Redis) 양쪽에 적재
   3) 배출은 반드시 "직전에 우리가 배출한 주문이 pickup_reached 이후 상태가 될 때까지"
      대기했다가 순서대로 진행 (로봇암/컨베이어가 1대뿐이라 동시 진행 불가)
   4) 배출 성공/실패는 IR센서로 판정(미장착 보드는 항상 성공 처리), 성공 시에만
      dispense 이벤트 보고
   5) 재고는 Redis가 원본(source of truth). 아두이노 로컬 count는
      "즉각 empty 판단용 캐시"일 뿐 - 부팅 시 서버에서 조회해서 채움
   6) restock(카트리지 재고보충)도 cnt_process의 keycap_restock 배열에서 직접 감지해서
      서버에 알리고, 서버가 계산한 채워진 개수를 응답받아 로컬 count 동기화
   7) 게이트(픽업 대기) 상태도 Redis에 영속화해서, 재부팅되면 서버에서 복원함
      (로컬 RAM에만 있으면 재부팅 시 유실되어 큐에 남은 주문을 게이트 없이
       바로 배출해버리는 문제가 있었음)

  ============================== 통신 대상 (Flask) ==============================
   GET  /stock/keycap?board=NS         부팅 시 재고 8칸 초기 조회
   GET  /queue/NS/next                 부팅 시 미완료 큐 복원
   POST /queue/NS/enqueue              새 주문 감지 시 서버 큐에도 적재
   POST /queue/NS/complete             배출 성공 후(또는 유령 주문 정리 시) 서버 큐에서 제거
   POST /restock/{letter}/{color}                restock 감지 시 호출, 채워진 개수 응답받음
   POST /report/keycap-status              기존 그대로 (상태 보고, 성공/실패 무관 항상)
   POST /report/keycap-dispense            기존 그대로 (배출 성공했을 때만)
   GET  /gate/NS                       부팅 시 게이트(픽업 대기) 상태 복원
   POST /gate/NS                       게이트 잠글 때 서버에 기록 (body: order_id)
   POST /gate/NS/clear                 게이트 열릴 때 서버 기록 해제

  ※ 위 신규 라우트(/stock, /queue/*, /restock/*, /gate/*)는 이번에 새로 정의한
    스펙이라 Flask 쪽 실제 응답 형식과 어긋나면 즉시 팀원과 맞춰서 파싱부만 고칠 것.
    이 코드가 가정하는 응답 형식은 각 함수 주석에 명시해둠.

  [2026-09-06 변경 사항 - 서보 배출 동작을 0~90도 점프 -> 0~180도 풀 스윕으로 교체]
   - 실물 테스트 결과 0~90도 점프 방식은 디스펜서가 부품을 끝까지 밀어내지 못함
     (각도 부족) -> 구버전(ESP32/Uno_Storage.ino 시절) 검증된 "0->180도, 1도씩
     천천히 스윕" 방식으로 교체. delay() 없이 millis() 기준 non-blocking 처리.

  [2026-09-07 스티션(정지마찰) 대응 - 스윕 시작 시 breakaway kick 추가]
   - 1도씩 미세하게 명령을 올리면 정지마찰을 넘길 토크가 안 나와서 제자리에서
     떨다가 누적된 힘이 한꺼번에 터지는 현상 -> 스윕 시작 시 BREAKAWAY_KICK_DEG
     만큼 한 번에 점프시켜 마찰을 먼저 넘긴 뒤 1도씩 스윕 이어감.

  [2026-09-07 seq 비교를 정수 -> 문자열로 변경]
   - doc["seq"]를 long/long long 정수로 파싱 시 32비트 long 범위(10자리)를 넘는
     값에서 ArduinoJson v6 정수 처리가 깨지는 문제 확인 -> String으로 받아서 비교,
     자릿수 제한 없앰.

  [2026-09-09 재고 관리를 Preferences(로컬 영속화) -> Redis(서버) 기반으로 전환]
   - 배경: R4 WiFi의 Preferences 라이브러리가 실기에서 최초 접근 시 수 분 단위로
     블로킹되는 문제가 발생, 원인 특정이 어려워 근본적으로 다른 방식으로 전환.
   - 재고의 원본(source of truth)은 이제 Redis. 아두이노는 부팅 시 GET /stock/keycap
     으로 값을 받아와 로컬 count에 채우고, 이후엔 배출/restock마다 서버와 동기화.
   - 배출(차감)은 아두이노가 계산해서 서버에 보고(SET), restock(충전)은 서버가
     계산해서 아두이노에 알려주는(응답값 반영) 반대 방향 흐름임에 주의.
   - 큐(대기 주문 목록)도 서버(Redis)에 같이 적재해서, 재부팅 시 GET /queue/{board}/next
     로 미완료 목록을 복원함 (재부팅 시점에 진행 중이던 배출 자체는 유실되지만,
     아직 시작 안 한 대기 주문은 복원됨).

  [2026-09-09 pickup_reached 게이트 추가]
   - 배경: 로봇암/컨베이어가 1대뿐이라, 우리가 배출을 마쳐도 그 결과물이
     "pickup_reached" 상태(트레이에 실제로 담겨 다음 단계로 넘어감)가 되기 전에
     다음 배출을 시작하면 안 됨.
   - 방식: 배출 성공/실패 처리 직후 gateOrderId에 그 주문의 order_id를 기록하고
     게이트를 잠금. 이후 수신하는 모든 cnt_process 스냅샷에서 그 order_id의
     status가 pickup_reached(이후 단계 포함)이거나 orders 목록에서 아예 사라졌으면
     게이트를 열어 다음 큐 항목을 진행함. 큐잉 순서 자체는 스냅샷에 등장한 순서를
     그대로 따름.

  [2026-09-09 restock/orders 중복 호출 방지]
   - 같은 스냅샷이 재전송되는 경우는 seq로 이미 막혀있지만, "플랫폼이 처리 완료
     표시를 아직 안 지운 상태로 여러 스냅샷에 걸쳐 같은 항목이 계속 남아있는 경우"
     까지 막아야 서버에 같은 restock을 중복 호출하지 않음.
   - orders는 기존 alreadyProcessed(order_id+letter+color 조합) 로직 그대로 재사용.
   - restock은 별도로 "지금 이 스냅샷 restock 배열에 내 레일이 있는가"를 보고,
     이전 스냅샷에서 이미 있었으면 스킵, 이번에 사라졌으면 다음 등장 시 다시
     호출 가능하도록 in-progress 플래그로 관리함.

  [2026-09-09 HTTPS 요청 재시도 로직 추가]
   - 배경: WiFi 연결 직후 첫 HTTPS(TLS) 요청만 err=-1로 실패하고 바로 다음
     요청은 정상 성공하는 패턴을 실기에서 반복 확인 (R4 WiFi TLS 핸드셰이크
     타이밍 이슈로 추정).
   - getJsonFromFlask / postJsonToFlask 모두 실패 시 300ms 대기 후 1회 재시도.
     재시도까지 실패하면 기존과 동일하게 실패 처리(재고 0/empty, 재고보충 재시도 등).
   - (11/01 참고: 로컬 평문 HTTP 전환 후엔 TLS 핸드셰이크 이슈가 사라질 가능성이
     크지만, 일시적 연결 실패 대비용으로 재시도 로직은 그대로 유지함)

  [2026-09-09 restock 필드명 확정 + order_id 명시]
   - Mobius cnt_process의 실제 재고보충 필드명은 "keycap_restock"임 ->
     doc["keycap_restock"]로 파싱 (플랫폼팀 확인 완료).
   - reportRailStatus 호출 시 order_id를 빈 문자열("")로 보내던 지점(시리얼 명령
     SETEMPTY/REPAIR, restock 처리) 전부 식별 가능한 문자열로 변경:
     시리얼 명령 -> "manual", restock -> "restock". Mobius 기록만 봐도 상태 변화
     원인을 구분할 수 있게 됨.

  [2026-09-09 pool을 글자 단위에서 색상별로 세분화]
   - 기존엔 pool:{글자}(예: pool:E) 하나로 4색 재고를 합쳐서 관리했는데,
     실제 창고 재고 수량표가 색상별로 다 다르게 나와서(예: E그린 158 vs E블루 157)
     글자 단위로 합쳐두면 실제 재고와 어긋남 -> pool도 stock과 동일하게
     색상별(pool:{글자}_{색상}, 예: pool:E_r)로 분리해서 관리하도록 변경.
     아두이노 코드 자체는 letter/color를 그대로 넘겨 /restock/{letter}/{color}를
     호출하는 방식이라 이 변경은 Flask/Redis 쪽 키 구조만 바뀐 것이고
     아두이노 코드에는 영향 없음.

  [2026-09-11 report 계열 HTTP 타임아웃을 Flask -> Mobius 재시도 시간에 맞춰 상향]
   - 배경: Flask의 post_cin_to_mobius()에 재시도(최대 1회, 총 2회 시도, 요청당
     3초 타임아웃 + 재시도 사이 0.5초 대기)가 추가되어, Flask 응답이 워스트케이스로
     최대 약 6.5초까지 걸릴 수 있게 됨. 기존 아두이노 HttpClient 타임아웃(5000ms)이
     이보다 짧으면, Flask가 아직 Mobius와 재시도 중인데 아두이노가 먼저 포기해서
     결국 성공했을 요청을 실패로 잘못 처리하는 역효과가 생김.
   - 대응: Mobius CIN 생성을 실제로 거치는 두 엔드포인트(REPORT_STATUS_PATH,
     REPORT_DISPENSE_PATH)에 대해서만 postJsonToFlask 호출 시 타임아웃을
     FLASK_MOBIUS_TIMEOUT_MS(8000ms, 6.5초 워스트케이스 + 여유 1.5초)로 늘림.
   - 나머지(재고/큐/restock/게이트 조회·조작)는 Redis만 쓰고 Mobius를 거치지 않아
     원래도 빠르므로 기본 타임아웃(5000ms) 그대로 둠 - 전부 다 늘리면 loop()
     블로킹 시간이 불필요하게 길어져 MQTT keepalive(PubSubClient 기본 15초)에
     영향 줄 수 있어 이 두 곳만 선별 적용함.
   - 주의: 이 두 호출이 8초 가까이 블로킹될 수 있는 만큼, 배출 사이클마다
     빈번하게 겹쳐 발생하면 MQTT 핑 타이밍에 영향 줄 가능성이 있음. 실기에서
     MQTT 재연결이 잦아지면 MQTT_KEEPALIVE를 늘리거나 Flask 쪽 재시도를
     줄이는 방향(MOBIUS_MAX_RETRIES ↓ 또는 MOBIUS_REQUEST_TIMEOUT ↓)을 검토할 것.

  [2026-09-?? 유령 주문(orders에서 사라진 큐 항목) 자동 정리 추가]
   - 배경: cnt_process 스펙상 진행 중인 주문은 매 스냅샷마다 전부 실리므로,
     로컬 큐(jobQueue)에 대기 중인 job이 한 번이라도 orders 목록에서 빠지면
     더 이상 진행 중이 아니라고(취소/오류 등으로 종료됐다고) 판단해도 됨.
   - pruneDeadJobsFromQueue()를 매 스냅샷마다 checkGateAgainstOrders() 다음,
     handleOrdersField() 이전에 실행. 큐를 한 번 비웠다가 orders에 남아있는
     것만 순서 그대로 다시 채워넣고, 제거된 job은 completeOnServer()로 서버
     큐에서도 함께 제거함. currentJob(배출 진행 중인 것)은 이미 큐 밖으로
     나온 상태라 영향 없음.

  [2026-09-?? IR 센서 미장착 보드 대응 토글 추가]
   - 배경: IR 센서 수량이 부족해 일부 보드는 물리적으로 배선이 안 된 상태로
     먼저 테스트/운영해야 하는 상황 발생.
   - IR_SENSOR_INSTALLED 매크로로 제어: 1이면 기존처럼 IR_PIN을 실제로 읽어서
     성공/실패 판정, 0이면 DS_WAIT_IR 단계에서 핀을 읽지 않고 무조건 성공(idle)
     처리함. 센서 장착/배선 완료되는 대로 이 값만 1로 바꾸면 원래 동작으로
     복귀함 - 각 보드의 현재 기본값은 아래 "IR 센서 장착 여부" 블록에서
     확인/조정할 것.

  [2026-09-13 게이트 상태를 Redis에 영속화 (재부팅 복원)]
   - 배경: gateOpen/gateOrderId가 로컬 RAM 변수라서 재부팅하면 무조건
     gateOpen=true로 초기화됨. 큐는 Redis에서 복원되는데 게이트 상태는
     복원이 안 되니, 재부팅 직후 "직전 배출이 아직 픽업 안 됐을 수도 있는데"
     큐에 남은 다음 주문을 게이트 확인 없이 바로 배출해버리는 문제가 있었음.
   - 대응: Redis에 gate:{board} 키를 하나 더 두고, 게이트를 잠글 때(배출
     직후) 그 주문 id를 SET, 게이트가 열릴 때(pickup_reached 이후 확인 또는
     목록에서 사라짐) DELETE. 부팅 시 GET해서 값이 있으면 "잠금+해당 order_id
     대기중" 상태로, 없으면 "열림" 상태로 시작.
   - 트레이드오프: 부팅 시점엔 실제 상태를 재확인하지 않고 Redis 값 그대로
     잠금부터 걸고 시작함. 다음 MQTT 스냅샷이 들어오면 기존
     checkGateAgainstOrders()가 즉시 재평가해서 필요하면 바로 열어주므로,
     부팅 직후 스냅샷 수신 전까지의 아주 짧은 구간만 실제보다 보수적으로
     닫혀있을 수 있음 (안전한 방향의 오차라 허용).

  [2026-09-13 게이트 오픈 조건을 pickup_reached 단일 비교 -> 이후 상태 화이트리스트로 확장]
   - 배경: 플랫폼팀 상태표 확인 결과 pickup_reached 이후로도
     loaded -> arrived -> (unclaimed) -> verified -> received -> completed
     등 여러 단계가 더 있음이 확인됨. 기존 코드는 status가 정확히
     "pickup_reached" 문자열일 때만 게이트를 열었는데, 스냅샷 수신 유실로
     pickup_reached 단계를 건너뛰고 바로 그 다음 단계(loaded 등)로 넘어간
     스냅샷을 받으면, orders 목록엔 여전히 주문이 남아있고(containsKey=true)
     status도 "pickup_reached"가 아니라서 게이트가 영원히 안 열리는
     문제가 있었음.
   - 대응: GATE_OPEN_STATUSES 화이트리스트를 두고, pickup_reached 및 그
     이후 모든 단계를 게이트 오픈 조건으로 인정하도록 변경.
   - unclaimed(노쇼)도 일단 화이트리스트에 포함함 - 픽업 지점 자체는 우리
     입장에서 비워진 것으로 간주. 단, 노쇼 시 실제로 물건이 픽업 지점에서
     치워지는지는 로봇암/컨베이어 운영 방식에 달려있어 재확인 필요
     (치워지지 않는 운영이라면 이 상태는 화이트리스트에서 빼야 함).
   - 화이트리스트 문자열은 플랫폼팀 상태표 기준으로 채워둔 것이라, 실제
     Mobius orders[].status 값과 대소문자/철자가 정확히 일치하는지 반드시
     실측 확인 필요. 그 앞단계(assigned, dispensing 등 우리가 아직 픽업
     지점을 안 비운 상태)는 절대 포함하면 안 됨.

  [2026-09-13 브레이크빔(송신부/수신부 분리형) IR 센서용 판정 방식을
   단순 1회 레벨 체크 -> 폴링 + 최소 지속시간 디바운스로 교체]
   - 배경: 이 보드에 실제로 장착되는 IR 센서는 반사형이 아니라 브레이크빔
     (송신부/수신부 분리형)이고, 설치 위치는 슈트 구조상 서보 암 궤적이
     절대 닿을 수 없는 지점이라 "서보 암이 빔을 스치는" 노이즈는 걱정할
     필요가 없음이 확인됨. 다만 키캡이 그 지점을 순간적으로 "통과"하는
     방식이라, 기존처럼 RETURN_DELAY_MS 이후 IR_CHECK_DELAY_MS만큼 대기했다가
     "그 시점에" 딱 한 번 레벨을 읽는 방식으로는 이미 지나가버린 키캡을
     놓쳐서 항상 실패로 오판정할 위험이 컸음(false negative). 반대로 순간
     감지 즉시 확정하는 방식은 전기적 노이즈(서보 PWM 스위칭 등)에 취약함.
   - 대응: DS_RETURNING의 IR_CHECK_DELAY_MS 대기가 끝나면 곧바로 판정하지
     않고, DS_WAIT_IR에서 IR_POLL_WINDOW_MS 동안 매 loop()마다 계속 핀을
     폴링함. 빔이 끊긴 상태가 IR_MIN_BREAK_MS 이상 연속되면 그 순간
     "유효 통과"로 래치(확정)하고, 그 뒤 빔이 복구돼도(순간 통과였으니
     당연히 복구됨) 래치 상태는 유지함. 윈도우가 끝난 시점에 래치가 됐으면
     성공, 안 됐으면 실패로 최종 판정.
   - IR_SENSOR_INSTALLED는 실제 장착 완료됨에 따라 0 -> 1로 변경. IR_PIN,
     IR_DETECT_LEVEL, IR_POLL_WINDOW_MS(폴링 전체 윈도우), IR_MIN_BREAK_MS
     (노이즈 필터용 최소 연속 끊김 시간)는 전부 실측 전 잠정값이므로 실기
     테스트하며 조정할 것.

  [2026-09-13 IR 센서 배선 여부를 보드 단위 -> 레일 단위로 세분화]
   - 배경: 센서 수량 부족으로 이 보드(NS)도 8개 레일 중 N_r 1개에만 먼저
     배선 완료됨. 기존 IR_SENSOR_INSTALLED는 보드 전체를 한 번에 켜고 끄는
     매크로라, 이 값을 1로 켜면 아직 미배선인 나머지 7개 레일까지 플로팅
     핀을 실제로 읽어 판정하려고 시도해서 오동작(랜덤 성공/실패)할 위험이 있었음.
   - 대응: IR_INSTALLED[2][4] 배열을 추가해 레일 단위로 배선 여부를 관리.
     true인 레일만 DS_WAIT_IR에서 실제 폴링+디바운스 판정을 하고, false인
     레일은 핀을 읽지 않고 즉시 성공 처리함. 미배선 레일의 pinMode도
     INPUT -> INPUT_PULLUP으로 바꿔서, 혹시 모를 실수 참조에도 안전하게
     HIGH로 고정되도록 방어함. IR_SENSOR_INSTALLED 매크로는 "이 보드에
     센서가 하나라도 있는가"를 나타내는 최상위 스위치로 의미를 유지.
   - 배선이 늘어날 때마다 IR_INSTALLED 표만 갱신하면 되고, 8개 레일이 전부
     완료되면 이 배열을 전부 true로 채우면 됨.

  [2026-09-13 IR 감시를 "복귀 이후에만" -> "모터가 움직이기 시작하는 순간부터
   배출 사이클 내내" 계속 폴링하는 구조로 변경]
   - 배경: 기존엔 서보가 복귀를 마친 뒤(DS_RETURNING 종료 후) 그 시점부터만
     IR_POLL_WINDOW_MS 동안 감시했음. 그런데 키캡은 서보가 180도까지 도는
     스윕 도중이나 홀드 중에도 떨어질 수 있어서, "복귀 후"로 한정하면 그
     이전에 떨어진 통과를 원천적으로 놓칠 위험이 있었음.
   - 대응: pollIrForCurrentJob()을 새로 추가해 runDispenseStateMachine() 맨
     위에서 dsState != DS_IDLE인 동안(즉 배출 사이클 진행 중 내내) 매
     loop()마다 무조건 호출하도록 변경. 감시 윈도우 시작 시각(irWindowStart)도
     DS_RETURNING이 아니라 DS_IDLE -> DS_HOMING 전환 시점(=모터가 실제로
     움직이기 시작하는 순간)에 기록함. DS_WAIT_IR은 더 이상 새로 폴링을
     시작하지 않고, 이미 쭉 진행돼온 감시가 IR_POLL_WINDOW_MS만큼 지났는지만
     확인해서 최종 판정함.
   - IR_POLL_WINDOW_MS는 250(구 IR_CHECK_DELAY_MS)+400(구 폴링) 같은 분리된
     구간이 아니라 "모터 시작 시각 기준 전체 감시 시간"으로 의미가 바뀌었고,
     4500ms(약 4.5초)로 넉넉하게 잡음 - 실제 서보 동작 시퀀스(복귀대기 400 +
     킥 정착 60 + 스윕 2700 + 홀드 400 ≈ 3.5초)를 전부 덮고도 여유가 남음.
   - IR_CHECK_DELAY_MS(200ms)는 이제 판정 타이밍과 무관하게, DS_RETURNING에서
     DS_WAIT_IR로 넘어가기 전 서보가 홈 위치에 물리적으로 도달할 최소 시간만
     보장하는 순수 정착 대기로 역할이 축소됨.

  [2026-09-18 disable 사유(reason) 필드 + PCA 드라이버 헬스체크 + 플랫폼 fault
   기반 disable/재배출 추가]
   - disable 보고에 reason 필드 추가: IR 판정 실패(끼임/서보모터 고장)는
     REASON_CARTRIDGE_FAILED("keycap_cartridge_failed"), PCA9685 I2C 응답
     없음은 REASON_DRIVER_FAILED("keycap_driver_failed")로 구분해서 보냄.
   - PCA9685는 이 보드의 8개 레일을 전부 구동하는 단일 드라이버라, 고장나면
     8개 레일이 다 같이 영향받음. checkPcaHealthy()로 1초 간격 헬스체크,
     고장 감지 시 8개 레일 전부 disable 보고하되, 그 순간 처리하려던 주문과
     관련된 레일 1개에만 order_id를 채우고 나머지 7개는 order_id를 null로 보냄
     (reportRailStatus에 빈 문자열을 넘기면 JSON null로 직렬화되도록 처리).
     복구되면 8개 레일 전부 재고 기준 상태(idle/empty)로 원복 보고.
   - 플랫폼(Mobius)이 주문 객체에 "fault": "..." 문자열을 올리면(예:
     tray_01_belt_jammed), handleOrderFaults()가 매 스냅샷마다 우리 축 담당
     주문인지 확인해서: fault가 새로 생기면 해당 레일을 disable(reason=fault
     문자열 그대로)로 보고, 이후 같은 order_id에서 fault가 사라지면 그 레일만
     재고 기준 상태로 되돌리고 같은 order_id로 재배출 job을 큐에 다시 넣음
     (재고 0이면 job은 넣지 않고 상태만 원복). fault와 무관한 레일은
     이 로직이 아예 건드리지 않음.

  [2026-09-19 fault 처리 방식 재설계 - disable 기반 -> 추적/조건부 재배출 기반]
   - 위 09-18 항목에서 설명한 "fault 등장 시 disable 보고" 방식은 폐기함.
     실제로는 fault가 두 종류로, 성격이 다름이 확인됨:
       - tray_jam류: 우리가 이미 실패/disable을 보고했기 때문에 플랫폼이
         그걸 보고 채워주는 것(우리 상태의 메아리) - 우리가 또 disable을
         보고할 이유가 없음(이미 아는 정보를 되돌려받는 것뿐).
       - stage_timeout류: 같은 status가 60초 유지되면 플랫폼이 자동으로
         붙임 - 정상적으로 배출 대기 중(큐에서 순서 기다리는 중)이어도
         뜰 수 있어서, 이것만 보고 disable로 판단할 근거가 없음.
   - 대응: fault 등장 시엔 disable/보고를 전혀 하지 않고, "이 주문에 fault가
     있다"는 사실만 faultActive[]/faultOrderId[]로 추적. fault가 사라지는
     시점에만 그 주문의 현재 status로 갈라서 처리:
       - status가 아직 "assigned"면(stage_timeout이 대기 중 뜬 경우 등)
         아무것도 안 함 - handleOrdersField()가 정상 경로로 알아서 주워감.
       - status가 assigned를 이미 지났으면(배출 끝난 뒤 걸린 tray_jam류)
         정상 경로가 다시 안 주워가므로 재배출 job을 직접 큐에 넣음.
   - 참고: 보드 로봇암 쪽 파이썬 코드(팀원 작성)도 같은 원리로 "fault 등장
     시 무보고, 해제 시에만 재시도"로 짜여있음을 확인 - 다만 그쪽은 큐/게이트
     없이 함수를 직접 재호출하는 방식(게이트가 재시도 대상 주문 자신에게
     걸려있어 큐를 거치면 데드락 위험이 있어서)이고, 여기 키캡 아두이노는
     배출 자체가 상태머신 기반이라 여전히 큐에 넣는 방식을 씀 - 이 경우
     게이트가 재배출 대상 주문 자신에게 걸려있는 상황이 실제로 나올 수
     있는지는 미확인이라, 만약 그런 경우가 생기면 재배출 job이 게이트가
     열릴 때까지 큐에서 대기만 하다가 못 나갈 위험이 있음 (실기 확인 필요).

  [2026-09-19 fault 재배출 시 실기에서 확인된 문제 2건 수정]
   - (자기 자신 게이트 데드락) 위에서 "미확인"이라 적었던 우려가 실기에서
     그대로 발생함 확인: 재배출 대상 주문이 마침 게이트를 걸고 있는 그
     주문 자신인 경우, 큐에 넣어도 게이트가 영원히 안 열려 실행이 안 됨.
     대응: 재배출 큐잉 직전에 gateOrderId == orderId면 게이트를 먼저 풀어줌
     (이 주문이 pickup_reached 되길 기다릴 이유가 없음 - 배출 자체를 다시
     하는 거라서).
   - (disable 상태 방치) 재배출 job을 큐에 넣어도, 그 레일이 여전히
     RAIL_DISABLE 상태로 남아있으면 DS_IDLE이 꺼내자마자 "disable이라
     건너뜀"으로 또 조용히 버려버리는 문제 확인. 대응: 재배출 큐잉 직전에
     레일이 disable이면 REPAIR 명령과 동일하게 재고 기준 상태(idle/empty)로
     원복시킴 - fault가 풀렸다는 신호 자체를 "사람이 고쳤다"는 신뢰로 보고
     엶. PCA 드라이버 고장이었을 경우 다음 배출 시도 직전 PCA 헬스체크가
     다시 확인하므로, 진짜 안 고쳐졌으면 즉시 재차 disable되는 안전망이 있음.
   - [이스케이프 버그 확인] 팀원분이 지적한 '\\0'/'\\n' 더블 백슬래시 버그를
     확인한 결과, 지금 올라온 원본 기준으로는 NS가 아니라 EI 쪽에 있었음
     (EI에서 수정함). 이 NS 파일은 원래도 '\0'/'\n'로 정상이라 이 부분은
     그대로 둠 - 팀원이 본 버전과 다를 수 있으니 서로 확인 필요.

  [2026-09-19 stage_timeout fault는 등장 즉시 큐에서 제거 + 영구 재큐잉 차단]
   - 배경: stage_timeout은 "우리 레일의 실제 성공/실패"와 무관하게, 어떤
     단계든 정해진 시간(60초) 안에 다음 단계로 못 넘어가면 플랫폼이 자동으로
     붙이는 타임아웃 표시일 뿐임. 이제 이 경우는 사람이 수동으로 폐기 처리
     하기로 결정됨 - 즉 stage_timeout이 뜬 주문은 우리가 다시 배출을 시도할
     이유가 없음.
   - 최종 방식(해제 시점 판단 -> 등장 시점 즉시 처리로 단순화): 처음엔 fault
     해제 시점에 rail 상태를 보고 재배출 여부를 가르려 했으나, stage_timeout은
     우리 rail 상태와 무관하게 뜰 수 있어 판단 기준이 되지 못함이 확인됨. 더
     단순하게, stage_timeout이 **등장하는 즉시** 그 주문을 처리해버리는 쪽으로
     방향을 바꿈 - 해제 시점엔 아무 것도 할 필요가 없어짐(할 일을 이미 등장
     시점에 다 끝내버렸으므로).
   - 대응: stage_timeout이 뜨면(handleOrderFaults) 그 자리에서 바로
     ① 이 주문의 jobKey를 markProcessed()로 등록해 handleOrdersField()가
     앞으로 절대 이 주문을 다시 큐에 넣지 않도록 영구 차단하고,
     ② removeOrderFromQueue()로 로컬 큐(jobQueue)에 이 주문이 대기 중이면
     즉시 꺼내서 completeOnServer()로 서버 큐에서도 같이 제거함.
     지금 한창 배출 중인 주문(currentJob)은 이미 큐 밖으로 나와 진행 중이라
     이 처리와 무관 - 물리적으로 멈출 수 없으니 그대로 끝까지 진행되고,
     끝난 뒤엔 원래 흐름대로 completeOnServer()가 불려서 서버 큐에서도
     알아서 빠짐.
   - keycap_cartridge_failed/keycap_driver_failed는 기존 방식(등장 시엔
     추적만, 해제 시 rail이 disable이었으면 재배출) 그대로 유지 - 이 둘은
     우리가 이미 disable을 보고한 것의 메아리라 등장 시점에 즉시 처리할
     필요가 없고, 오히려 해제 시점에 rail 상태를 보고 판단하는 게 맞음.
   - 게이트: 별도 처리 안 함 - 폐기된 주문이 마침 게이트를 잡고 있었다면
     checkGateAgainstOrders()가 기존 기준(orders에서 사라짐 또는
     GATE_OPEN_STATUSES) 그대로 알아서 열어줌. 단, 플랫폼이 폐기 처리 시
     실제로 orders에서 그 주문을 지우는지, 아니면 별도 status만 남기고
     목록엔 계속 두는지는 미확인 - 후자라면 그 status가 GATE_OPEN_STATUSES에
     없을 경우 게이트가 영원히 안 열리는 위험이 있으므로 플랫폼팀 확인 후
     필요 시 화이트리스트에 추가할 것.
   - [알려진 채로 남겨두는 구멍, 지금은 손 안 댐] 배출 도중(서보 동작 중)
     아두이노 자체가 멈추거나 재부팅되면, 그 job은 completeOnServer가 안
     불려서 서버 큐엔 "대기 중"으로 남음. 그 사이 stage_timeout -> 폐기가
     되면, 재부팅 후 restoreQueueFromServer()가 이 죽은 job을 다시 로컬
     큐에 채우는데, 그걸 걸러낼 다음 MQTT 스냅샷이 오기 전까지의 아주 짧은
     시간 동안은 이 job이 살아있는 것처럼 배출을 시작해버릴 수 있음(반대로
     살아있는 주문은 이 시간차와 무관하게 정상적으로 바로 재배출돼야 함).
     지금은 이 경우를 다루지 않기로 함 - 나중에 필요해지면 부팅 시 Flask가
     들고 있는 최신 cnt_process 스냅샷을 한 번 조회해서 큐를 미리 거르는
     방식을 검토할 것.

  [2026-09-19 서보 각도/방향 실측값 반영 - 대기 90도->170도, 배출 방향 반전]
   - 배경: 별도 캘리브레이션용 테스트 스케치(Servo_Push_Cycle_Test)로 실측한
     결과, 기존 SERVO_HOME_ANGLE=90(대기)이 실제로는 디스펜서가 카트리지
     출구 아래까지 나와있는 위치였음이 확인됨. 대기 각도는 170도, 배출
     (푸시) 각도는 70도가 적절함이 확인됨 - 즉 각도가 "증가"가 아니라
     "감소"하면서 배출되는 방향으로 뒤바뀜.
   - 대응: SERVO_HOME_ANGLE을 170으로 변경, 배출 목표 각도를 상수화한
     PUSH_ANGLE(70)을 새로 추가. DS_HOMING의 브레이크어웨이 킥 방향을
     `+BREAKAWAY_KICK_DEG`에서 `-BREAKAWAY_KICK_DEG`로, DS_SWEEPING의
     스텝 방향을 `sweepAngle++`에서 `sweepAngle--`로, 종료 조건을
     `sweepAngle >= 180`에서 `sweepAngle <= PUSH_ANGLE`로 각각 반전.
     SWEEP_STEP_DELAY_MS도 실측값 기준 15 -> 10으로 조정.
   - SERVO_MIN_PULSE/SERVO_MAX_PULSE, RETURN_DELAY_MS, DISPENSE_HOLD_MS,
     BREAKAWAY_SETTLE_MS 등 나머지 서보 타이밍 값은 이번 실측 대상이
     아니었으므로 그대로 둠 - 배출이 실기에서 이상하면 이 값들도 순서대로
     재검토할 것.

  [2026-09-19 IR_MIN_BREAK_MS 실측값 반영 - 20ms -> 5ms]
   - 배경: 별도 IR 센서 테스트 스케치(IR_Sensor_Test)로 N_r 레일에서 키캡
     낙하 시 실제 빔 끊김 지속시간을 반복 측정한 결과 7~19ms 범위로 나왔고,
     추가 측정에서 최소 7ms까지 확인됨. 기존 IR_MIN_BREAK_MS=20ms는 이 값들
     보다 높아서, 짧게 끊기는 정상적인 통과까지 디바운스에서 걸러져 실패로
     오판정하고 있었을 가능성이 확인됨(이게 "센서가 감지를 잘 못 한다"고
     느꼈던 원인으로 추정).
   - 대응: 실측 최소값(7ms)보다 여유를 두고 IR_MIN_BREAK_MS를 5로 낮춤.
     0에 가깝게 낮추지 않은 이유는, 이 디바운스가 원래 서보 PWM 스위칭 등
     전기적 노이즈를 걸러내기 위한 목적도 겸하고 있어서 - 너무 낮추면 반대로
     노이즈를 진짜 통과로 오판정할 위험이 생김. 다만 이번 실측에서는 노이즈
     자체의 지속시간을 직접 재보지는 못했음(서보 구동 중 IR 핀 노이즈 동시
     관찰은 별도 통합 테스트가 필요해 보류함) - 실기에서 오탐지(가만히
     있는데 배출 성공으로 잘못 판정되는 경우)가 관찰되면 이 값을 다시 올릴 것.
   - IR_POLL_WINDOW_MS(4500ms)는 이번 실측 대상이 아니었으므로 그대로 둠.
   - 편차가 큰 원인(7~19ms로 거의 3배 차이)이 디바운스만으로는 완전히
     해결 안 될 수 있음 - 낙하 경로가 매번 빔을 정확히 지나가지 않아서
     아예 감지 자체가 안 되는 경우도 관찰됨(디바운스 값과 무관한 별개
     문제). 슈트 내 빔 위치/가이드 등 기구적 정렬 확인이 필요할 수 있음.

  [2026-09-19 IR_SENSOR_INSTALLED 0 -> 1로 활성화]
   - N_r 레일 실측(디바운스 값 확정)이 끝나 최상위 스위치를 켬. 이제
     `#if IR_SENSOR_INSTALLED` 블록이 실제로 컴파일에 포함되고,
     IR_INSTALLED[0][0]=true인 N_r 레일만 실제 IR 판정 로직이 동작함.
     나머지 7개 레일은 IR_INSTALLED가 false라 여전히 무조건 성공 처리됨.

  [2026-09-19 IR_INSTALLED[0][0] false로 되돌아가 있던 것 수정]
   - 위에서 IR_SENSOR_INSTALLED(최상위 스위치)만 1로 켜고, 정작 레일별
     스위치인 IR_INSTALLED[0][0](N_r)은 true로 바꾸는 걸 누락함. 그 결과
     실제 배출 후 `if (IR_INSTALLED[l][c])` 조건을 항상 통과 못 해서
     `#if IR_SENSOR_INSTALLED` 블록 안 판정 로직 자체가 매번 스킵되고,
     success 기본값(true)이 그대로 채택돼 키캡이 실제로 안 떨어졌어도
     계속 idle(성공)로 보고되는 증상이 발생함 - IR_MIN_BREAK_MS를 아무리
     바꿔도 이 줄이 안 읽히니 전혀 반영이 안 됐던 것.
   - 대응: IR_INSTALLED[0][0]을 true로 수정. 이제 센서 관련 스위치는
     IR_SENSOR_INSTALLED(보드 전체) / IR_INSTALLED[l][c](레일별) /
     IR_MIN_BREAK_MS(디바운스) 세 곳 모두 맞춰야 실제 판정이 동작함 -
     다음에 새 레일 배선 추가할 때도 이 세 곳을 다 확인할 것.

  [2026-09-30 PCA9685 채널 매핑을 짝수 채널(0,2,4,...,14)로 변경 + 4개 보드 공통 코드 정리]
   - 배경: 실제 배선이 4개 보드 모두 PCA9685의 0,2,4,6,8,10,12,14번 채널에
     연결되어 있음 (기존 코드는 0~7번 연속 채널을 가정). 전압원에 전 보드를
     연결해 E/I, N/S, T/F, P/J 전부 동작시켜야 해서 SERVO_CHANNEL을 수정함.
   - 매핑: LETTER_0 r,y,b,g = 0,2,4,6 / LETTER_1 r,y,b,g = 8,10,12,14
   - IR 센서는 현재 E_r 1개만 배선됨 -> EI 보드만 IR_SENSOR_INSTALLED=1,
     IR_INSTALLED[0][0]=true. NS/TF/PJ는 센서가 없으므로 IR_SENSOR_INSTALLED=0,
     IR_INSTALLED 전부 false (배출 후 무조건 성공 처리). 센서가 추가 배선될 때마다
     IR_SENSOR_INSTALLED / IR_INSTALLED[l][c] / IR_MIN_BREAK_MS 세 곳을 함께 확인할 것.

  [2026-09-30 부팅 시 서보 홈 이동을 동시 -> 순차로 변경]
   - 8개 서보가 동시에 움직이며 전압이 순간 떨어지는 문제 완화를 위해 setup()에서
     채널마다 SERVO_STARTUP_STAGGER_MS(200ms) 간격으로 한 개씩 홈(170도)으로 보냄.
     배출 사이클은 원래 한 번에 한 레일만 움직이므로 영향 없음.

  [2026-09-30 색상 순서 변경 + 대기 각도 170 -> 165]
   - 색상 순서를 r,y,b,g -> r,g,b,y로 변경 (COLOR_LIST). 채널 매핑은 그대로
     LETTER_0 = 0,2,4,6 / LETTER_1 = 8,10,12,14 이고, 각 자리의 색이 바뀜:
     r=0/8, g=2/10, b=4/12, y=6/14. 색 인덱스 자리가 바뀌므로 IR_PIN 표도
     같은 자리에 대응함(현재 센서는 E_r=슬롯 0번뿐이라 영향 없음).
   - SERVO_HOME_ANGLE 170 -> 165: 170도에서 일부 서보가 끝단에 걸려 계속 떨림
     (Servo_Jitter_Test로 확인, 5도 낮추니 해소). PUSH_ANGLE(70)은 그대로.

  [2026-10-06 IR 판정 진단 출력 추가 + IR 설정 정리]
   - 배경: N_r만 연결해 테스트했는데 키캡이 정상 통과했는데도 disable로 판정됨.
     원인 후보: (1) IR_MIN_BREAK_MS(7)가 실측 최소 끊김(7ms)과 같아서 짧은 통과가
     걸러짐 (2) 감시 중 MQTT 콜백의 HTTP 호출로 loop()가 막혀 폴링을 놓침
     (3) 빔이 안 읽힘(IR_DETECT_LEVEL 반대/센서 전원·GND).
   - 대응: 배출 판정 직후 시리얼에 "[IR] 최대 끊김(ms) / 최대 폴링 간격(ms) / 판정"을
     출력. 이 값으로 위 셋 중 어느 쪽인지 가를 것 (최대 끊김 1~6 -> IR_MIN_BREAK_MS
     낮춤, 0 + 간격 큼 -> loop 블로킹, 0 + 간격 정상 -> 레벨/배선 점검).
   - IR 설정: EI는 E_r, NS는 N_r에만 센서 켬(IR_SENSOR_INSTALLED=1,
     IR_INSTALLED[0][0]=true), TF/PJ는 센서 없음(0 / 전부 false). 이전에 어긋나 있던
     IR 관련 주석도 현재 값에 맞게 정리함. IR_MIN_BREAK_MS 값(7) 자체는 그대로 둠.

  [2026-10-06 IR 끊김 측정을 폴링 -> 핀 변화 인터럽트 병행으로 보강]
   - 배경: N_r 테스트에서 "[IR] 최대 끊김=0, 최대 폴링 간격=39ms, 판정=실패". 핀/레벨/전원은
     이상 없음이 확인됨. 최대 끊김=0은 "한 번도 안 끊김"이 아니라 "끊김이 폴링 샘플
     1개로만 잡힘"일 때도 0이라, loop() 폴링 간격(WiFi/MQTT 호출 포함 수십 ms)이
     키캡 끊김(7~19ms)보다 길어서 놓쳤을 가능성이 큼.
   - 대응: 배선된 레일 핀에 attachInterrupt(CHANGE)를 걸어 끊김 시간을 마이크로초로
     직접 측정(irIsr). 판정은 폴링 래치 OR 인터럽트 래치. 시리얼 진단에 폴링 횟수,
     끊김 샘플 수, 인터럽트 최대 끊김(us)을 같이 출력. 레일을 늘릴 때 핀별 인터럽트 지원 여부를 확인할 것.

  [2026-10-07 서보 각도 재실측 + IR 감시 시간/디바운스 조정 + 4개 보드 코드 동기화]
   - 서보: 혼을 0도 기준으로 다시 조립한 뒤 배출(PUSH_ANGLE)=20도로 변경. 대기
     (SERVO_HOME_ANGLE)는 170도로 시험해봤으나 165도일 때 더 안정적으로 작동해서
     165도 유지(09-30 값 그대로).
   - IR_POLL_WINDOW_MS 4500 -> 7000 (모터 시작 시각 기준 전체 감시 시간 7초).
   - IR_MIN_BREAK_MS 5 -> 10: 반복 테스트에서 끊김이 대체로 20ms 이상으로 관찰되어 상향.
     (인터럽트 측정값 "최대끊김(us)"를 시리얼 [IR] 진단에서 계속 확인하며 조정할 것)
   - 시리얼 [IR] 진단 출력 + 핀 변화 인터럽트 측정을 NS에만 있던 상태에서 4개 보드
     전부로 통일 (센서 미배선 보드는 IR_SENSOR_INSTALLED=0이라 해당 블록이 컴파일에서 빠짐).

  [2026-10-08 안정성 보강 (기존 동작 흐름은 그대로)]
   - restock 플래그 초기값: presentNow 배열이 전부 false로 시작하는지 4개 보드 모두
     확인/통일 (EI 쪽에 [0][0]=true 오타가 있어서 E_r restock이 부팅 후 1회만 동작했음).
   - 주문 큐잉 순서: markProcessed()를 queuePush() 성공 "이후"로 이동. 큐가 가득 차서
     push가 실패해도 중복방지 기록이 남지 않으므로, 다음 스냅샷에서 다시 주워짐
     (이전엔 기록만 남고 큐에는 없어서 그 주문이 영구 유실됐음).
   - fault 해제 후 재배출 분기: 게이트 해제와 레일 원복을 재고(count)와 무관하게 수행하고,
     원복 상태를 reportRailStatus로 서버(Mobius)에도 보고. 재배출 job 적재만 count>0일 때.
     (이전엔 전부 count>0 안에 있어서, 재고 0이면 그 주문이 잡고 있던 게이트가 영원히
     안 풀려 보드 전체 배출이 멈출 수 있었고, Mobius엔 계속 disable로 남았음)
   - 중복방지 기록 ORDER_HISTORY_SIZE 8 -> 32: 큐 크기(8)와 같아서 기록이 금방 밀려나
     같은 주문이 다시 큐에 들어갈(이중 배출) 위험이 있었음.
   - MQTT 수신 버퍼/JSON 문서 2048 -> 4096, JSON 문서는 스택 대신 힙(DynamicJsonDocument).
     주문이 몰려 스냅샷이 커지면 PubSubClient가 메시지를 조용히 버리거나 NoMemory로
     파싱 실패하던 위험 완화. 파싱 실패 시 수신 길이도 같이 출력.
   - seq 필드가 없는 스냅샷은 중복 비교를 건너뜀 (없으면 "null" 문자열끼리 같다고
     판단해 이후 스냅샷을 전부 무시할 수 있었음).
   - IR 센서: 현재 E/I 보드(E_r)만 사용. N/S, T/F, P/J는 IR_SENSOR_INSTALLED=0.

  [2026-11-01 전시장 로컬 서버 전환 - HiveMQ Cloud/Cloud Run(TLS) -> 로컬 Mosquitto/Flask(평문)]
   - 배경: 전시장은 인터넷이 없다고 가정하고, 클라우드에서 돌던 Mobius/MQTT/Redis를
     플랫폼 서버(192.168.0.100, 고정 IP)로 옮김. 키캡 Flask(릴레이 + /stock /queue
     /gate /restock /report)도 같은 로컬망에서 돌리기로 함(포트 5000, HTTP).
     Redis는 플랫폼팀이 DB 0번, 보드 로봇암이 1번을 쓰므로 키캡 Flask는 겹치지 않는
     번호를 써야 함(Flask 쪽 설정이며 아두이노 코드에는 영향 없음).
   - TLS 제거: MQTT용 WiFiSSLClient -> WiFiClient, Flask용 httpSSLClient -> httpClient
     (WiFiClient). 로컬 Mosquitto(1883)/Flask(5000)는 인증서 없는 평문이라 TLS를 쓰면
     접속이 안 됨. "SSL 루트 인증서 업로드"도 더 이상 필요 없음(예전에 구워둔
     인증서가 모듈에 남아 있어도 무해).
   - MQTT 계정 미정 대응: tryConnectMQTT()가 MQTT_USER가 빈 문자열이면 익명으로
     connect(clientId), 아니면 계정으로 connect(clientId, user, pass)를 부름.
     Mosquitto가 익명 허용이면 config.h의 MQTT_USER/MQTT_PASS를 ""로 두면 되고,
     계정이 생기면 그 두 줄만 채워서 4개 보드에 다시 업로드하면 됨.
     접속 실패 시 시리얼 state 값: -2 = TCP 연결 불가(Mosquitto 외부 접속 미허용/
     방화벽/IP 오류), 4 = 계정/비번 틀림, 5 = 인증 필요(익명 불가).
   - 시리얼 로그의 "[HTTPS]" 접두어를 "[HTTP]"로 변경(실제로 TLS를 안 쓰므로).
   - 아직 확인 필요(플랫폼팀 답변 대기): Mosquitto 외부 접속 허용(listener 1883
     0.0.0.0) + 계정 여부, sjuiot/warehouse/process publish 주체(우리 Flask 웹훅 릴레이
     그대로 쓰는지), Mobius 구독 notification URL을 새 Flask 주소로 바꿨는지.
   - config.h에서 쓰는 이름은 WIFI_SSID/WIFI_PASS/MQTT_HOST/MQTT_PORT/MQTT_USER/
     MQTT_PASS/FLASK_HOST/FLASK_PORT 8개뿐(인증서 관련 정의 삭제).

  [2026-11-01 IR 센서를 4개 보드 8개 레일 전부 활성화]
   - 4개 보드(EI/NS/TF/PJ) 모두 8개 레일 전부에 브레이크빔 센서를 배선함 ->
     IR_SENSOR_INSTALLED=1, IR_INSTALLED 8칸 전부 true. 이제 IR 관련 설정은
     4개 보드가 완전히 동일하고, 보드마다 다른 건 맨 위 "보드별 설정" 블록뿐임.
   - 배선은 IR_PIN 표 그대로: LETTER_0 r,g,b,y = D2,D3,D4,D5 /
     LETTER_1 r,g,b,y = D6,D7,D8,D9 (수신부 신호선 기준).
   - 주의 1) 이제 모든 레일이 실제 IR 판정을 함 - 센서가 빠졌거나 배선이 안 된
     레일은 핀이 플로팅이라 판정이 랜덤해지고, 감지가 안 되면 배출할 때마다
     disable(keycap_cartridge_failed)로 보고됨. 8개 다 배선한 뒤 업로드할 것.
   - 주의 2) UNO R4 WiFi는 모든 디지털 핀이 외부 인터럽트를 지원하지 않음.
     인터럽트가 안 붙는 핀의 레일은 폴링 판정만 동작함(판정 = 폴링 OR 인터럽트).
     시리얼 [IR] 진단에서 "끊김샘플>0인데 인터럽트 최대끊김(us)=0"이 반복되는
     레일은 인터럽트가 안 걸리는 핀이라는 뜻 - 그 레일에서 통과를 자주 놓치면
     인터럽트 지원 핀으로 옮기는 걸 검토할 것.
   - 주의 3) 판정은 IR_POLL_WINDOW_MS(7초)가 끝나야 내려지므로, 이제 모든 레일이
     배출 1건당 최소 7초씩 걸림(기존엔 센서 레일만).

  [2026-11-01 PCA 드라이버 고장 복구를 "PCA 응답 회복 시 자동" -> "사람이 fault 내릴 때만"으로 변경]
   - 원칙: failed가 뜨면 무조건 사람이 fault를 내린 뒤에만 복구/재배출함.
   - 기존: PCA가 다시 I2C 응답하면 주문과 무관한 7개 레일을 자동 원복함. 그런데
     I2C 응답만 보고 판단해서 서보 전원선이 빠져있어도 "복구"로 보고, 재부팅되면
     pcaDriverFailed(RAM)가 지워져서 8개가 Redis의 disable 그대로 남는 문제가 실기에서 발생함
     (fault를 내려도 주문 걸린 레일 1개만 풀림).
   - 변경: PCA 응답이 돌아와도 아무것도 자동으로 안 풀고 배출 금지(pcaDriverFailed) 유지.
     keycap_driver_failed fault가 내려가는 순간 이 보드의 disable 레일을 전부 원복+보고하고
     배출 금지 해제, 주문 걸린 레일은 기존처럼 재배출. 단 다른 fault가 따로 걸려있는
     레일은 그 fault가 풀릴 때 따로 처리되도록 건드리지 않음.
   - 재부팅 대응: 스냅샷에 keycap_driver_failed가 보이면 배출 금지를 다시 걸고, 부팅 시
     8개 레일이 전부 disable로 복원되면 driver 고장 중 재부팅된 것으로 보고 배출 금지로 시작함.
   - 배출 금지 중엔 큐의 job을 꺼내지 않고 그대로 둠 -> fault 해제 시 같은 job이 큐에
     이미 있으면 재적재하지 않음(이중 배출 방지, queueContains()).
   - 고장 당시 대기 주문이 없었으면 모든 레일이 order_id 없이 보고돼서 플랫폼에 내릴
     fault 자체가 없음 -> 이 경우는 시리얼 "REPAIR ALL"로 사람이 직접 풂(새로 추가한 명령).

  [2026-11-01 MQTT 연결 상태 LED 표시 추가]
   - 시리얼 모니터 없이도 연결 여부를 알 수 있게 보드 기본 LED(LED_BUILTIN, "L" 표시, D13)를 씀.
   - MQTT 연결 성공 순간 5번 깜빡이고, 이후 연결이 유지되는 동안은 계속 켜짐.
     연결이 끊기면 꺼짐 -> 꺼져 있으면 WiFi 또는 MQTT 연결 안 된 상태.
   - 깜빡임은 millis() 기반 non-blocking이라 배출 동작/IR 감시를 막지 않음.

  [2026-11-01 restock이 disable 레일을 풀어버리던 구멍 수정]
   - 기존 callRestock()은 채워진 개수만 보고 status를 idle/empty로 덮어써서, disable 레일에
     restock이 들어오면 fault를 내리기 전에 풀려버림. 그 뒤 fault를 내려도 "우리 레일은
     disable 아니었음"으로 판단해 실패했던 주문을 재배출하지 않는 문제까지 생김.
   - 변경: 레일이 disable이면 count만 갱신하고 status는 disable 유지. 사람이 fault를
     내리거나 REPAIR 해야만 풀리고, 풀릴 때 갱신된 count 기준으로 idle/empty가 정해짐.

  [2026-11-04 11/03 통합 테스트 대응 - MQTT 구독 확인 + 재부팅 중복 방지 + 복원 재시도]
   - 배경: 11/03 테스트에서 EI/TF/PJ가 MQTT 연결 LED는 켜진 채 주문을 전혀 못 받음
     (subscribe 실패를 확인하지 않아 "연결됨 + 수신 불가" 상태로 굳음). NS는 재부팅 후
     이미 배출한 주문(N_r)을 서버 큐에 다시 넣음. 부팅 시 서버 복원도 1회만 시도했음.
   - 함께 바뀐 것: Flask가 cnt_process를 retain=True로 발행함. 브로커가 마지막 스냅샷을
     들고 있다가 구독하는 순간 바로 보내주므로, 재접속/재부팅 직후 항상 최신 주문 목록을
     받음 (스냅샷은 진행 중 주문 "전체"라서 끊긴 사이 놓친 주문도 다 들어 있음).
   - (1) 구독 실패 시 재연결: tryConnectMQTT()에서 subscribe() 반환값을 확인하고, 실패면
     disconnect() -> ensureMQTT()가 MQTT_RETRY_MS(3초)마다 성공할 때까지 계속 재시도.
   - (2) 자기 에코 확인: PubSubClient의 subscribe()는 SUBSCRIBE 패킷을 "보냈는지"만 알려주고
     브로커가 거부해도 true를 반환하므로 (1)만으로는 부족함. 보드 전용 토픽
     (sjuiot/warehouse/ping/{BOARD_NAME})을 같이 구독하고 PING_INTERVAL_MS(2분)마다 자기에게
     핑을 보내서, PING_TIMEOUT_MS(30초) 안에 안 돌아오면 구독이 죽은 것으로 보고 끊고
     재접속함. 정상일 땐 절대 끊지 않음. 끊는 건 배출 사이클 중이 아닐 때(DS_IDLE)만.
     접속 직후 첫 핑은 PING_FIRST_DELAY_MS(10초) 뒤에 보냄.
     타임아웃을 넉넉히 30초로 잡은 이유: report 호출이 최대 8초 블로킹돼서 핑 수신
     처리가 늦어질 수 있음.
   - (3) 재부팅 후 중복 적재 방지: wasHandled() 추가 - "게이트가 잡고 있는 주문 = 이미
     배출한 주문"으로 취급. 배출은 했는데 아직 assigned인 주문은 게이트에 걸린 주문
     하나뿐이고(게이트가 풀리면 그 주문은 이미 pickup_reached 이후라 다시 주울 일이 없음),
     게이트는 Redis에 영속화돼 재부팅해도 남아 있음. handleAssignedOrder()와
     handleOrderFaults()의 fault 해제 분기 둘 다 이걸로 판단함 - 한쪽만 바꾸면 재부팅 후
     실패 주문의 fault가 풀릴 때 "미시도 -> 정상 경로에 맡김"으로 빠지는데, 정상 경로는
     게이트 때문에 안 주워가서 재배출도 게이트 해제도 안 되는 데드락이 생김.
   - (4) 서버 복원 재시도: 재고/큐/게이트 복원 성공 여부를 각각 플래그로 관리하고, 실패한
     것만 loop()에서 RESTORE_RETRY_MS(5초)마다 재시도. 셋 다 끝나기 전엔 MQTT 접속(주문
     수신)도, 배출도 하지 않음. (재고 조회 실패 시 전부 count 0/empty로 시작해서 들어온
     주문을 "레일 사용 불가"로 버리던 문제, 게이트 조회 실패 시 기본값 open으로 시작하던
     문제 방지) 복원이 끝난 뒤 구독하면 retained 스냅샷이 바로 들어오므로 주문 유실 없음
     -> 플랫폼 서버/Flask/아두이노 켜는 순서와 무관하게 동작.
   - 남겨둔 구멍(기존과 동일): 서보 동작 도중 전원이 나가면 그 job은 서버 큐에 남아
     재부팅 후 한 번 더 배출될 수 있음.

  [2026-11-04 MQTT 원격 복구(REPAIR) 명령 추가 - 창고팀 요청]
   - 배경: disable 칸을 푸는 방법이 시리얼 REPAIR(USB 연결 필요)와 플랫폼 fault 해제뿐이라,
     전시 중 원격으로 disable만 풀 방법이 없었음. 창고팀이 키캡 Flask에 복구 경로와
     상태판 "복구" 버튼을 붙이기로 하고, 펌웨어는 MQTT로 명령을 받게 해 달라고 요청함.
   - 토픽: sjuiot/warehouse/command (4대 공통 구독). 메시지(JSON):
       {"cmd": "REPAIR", "rail": "E_r"}                  -> 시리얼 REPAIR E_r 와 동일
       {"cmd": "REPAIR", "rail": "ALL", "board": "EI"}   -> 시리얼 REPAIR ALL 과 동일
     · board(선택): 있으면 BOARD_NAME과 같을 때만 처리. 없으면 rail로만 판단함.
       rail이 ALL인데 board가 없으면 4대 전부 REPAIR ALL이 되므로, ALL은 board를 같이 보낼 것.
     · rail이 자기 보드 담당이 아니면 조용히 무시 (4대가 같은 토픽을 받으므로 정상 동작).
     · 발행 측은 반드시 retain 없이 보낼 것 - retain이면 재부팅/재접속 때마다 REPAIR가 다시 실행됨.
       (PubSubClient 콜백은 retain 여부를 알려주지 않아 펌웨어에서 걸러낼 수 없음)
   - 시리얼 REPAIR 처리(상태 복구 + 서버 보고)를 repairRail()/repairAllRails()로 빼서
     시리얼과 MQTT가 같은 함수를 씀.
   - 지금 배출 중(busy)인 레일은 REPAIR로 건드리지 않음 (시리얼도 동일하게 적용). 배출 도중
     idle로 바꾸면 배출 결과 처리와 꼬임 - 배출이 끝난 뒤 다시 보내면 됨.
   - 구독 실패 확인(11/04 위 항목)에 명령 토픽도 포함.
   - 참고: REPAIR는 레일 상태만 풂. 플랫폼의 fault는 그대로이고, REPAIR로 먼저 푼 뒤 fault를
     내리면 "우리 레일은 disable 아니었음"으로 판단해 실패 주문을 재배출하지 않음
     (재배출이 필요하면 REPAIR 대신 플랫폼에서 fault를 해제할 것).

  [2026-11-08 서보 발열 대응 - 대기/배출 각도 변경 + 평상시 서보 off + IR 감시 8초]
   - 증상: 대기 중 일부 서보가 계속 미세하게 돌아가는 소리(헌팅)를 내며 뜨거워짐.
     연장선을 빼도 동일, 크랭크를 빼면 발열 없음 -> 홈(165도)에서 크랭크/슬라이더가
     기구에 걸려 서보가 계속 버티는 것이 원인. (노트북 USB로 로직 전원을 줄 때 나던
     소리는 별개 원인으로, 아두이노를 어댑터 전원으로 바꿔 해소됨)
   - SERVO_HOME_ANGLE 165 -> 140, PUSH_ANGLE 20 -> 40 (회전 폭 145도 -> 100도,
     서보 안정 구간 안쪽으로 이동). 각도 테스트 코드로 배출 확인 후 확정.
   - 평상시 서보 off: releaseServo()(PCA full OFF)로 신호를 끊어 힘을 뺌.
     부팅 시 8개를 홈으로 보낸 뒤 RETURN_DELAY_MS 기다렸다가 전부 off,
     배출 후엔 홈 복귀 명령 뒤 RETURN_DELAY_MS가 지나면(DS_WAIT_IR) off.
     다음 배출은 DS_IDLE에서 moveServoAngle(홈)을 다시 보내고 RETURN_DELAY_MS
     기다린 뒤 움직이므로, off 동안 슬라이더가 조금 밀려도 배출 동작은 정확함.
   - IR_POLL_WINDOW_MS 7000 -> 8000 (판정 여유 1초 추가 - 배출 1건당 최소 8초).

  [2026-11-10 상태판에서 레일별 IR 센서 판정 켜기/끄기 (재부팅 없이)]
   - 배경: 일부 레일 센서가 불안정해서, 전시 중에 상태판에서 그 칸만 센서 판정을 끄고 운영할 수
     있어야 함. 꺼진 칸은 IR_INSTALLED=false와 똑같이 "판정 없이 항상 성공" 처리.
   - 토픽: sjuiot/warehouse/ir/{BOARD_NAME} (보드별, retain). 내용 = 이 보드 8칸 전체 상태
       {"N_r": true, "N_g": false, ...}   true = 센서 판정 켬, false = 끔 (없는 칸은 그대로)
     키캡 Flask(/ir/<칸>)가 Redis ir:{칸}에 저장하고 보드 전체 상태를 retain으로 발행함 ->
     재부팅/재접속하면 브로커가 마지막 설정을 바로 다시 보내줘서 설정이 유지됨.
     (REPAIR는 "명령"이라 retain 금지지만, 이건 "현재 설정"이라 retain이 맞음)
   - irEnabled[2][4]: 실행 중 설정. 실제 판정 여부 = IR_INSTALLED(배선) && irEnabled(설정).
     부팅 시 기본값은 전부 켬 -> retained 설정을 받으면 그대로 바뀜.
   - 배출 중인 레일은 지금 사이클엔 시작할 때의 설정(curIrActive)을 그대로 쓰고, 바뀐 설정은
     다음 배출부터 적용 (감시 도중에 켜지면 앞부분을 못 봐서 오판정하므로).
   - 시리얼 "IR" 입력 시 8칸 센서 설정 출력. 구독 실패 확인에 이 토픽도 포함.

  [2026-11-11 상태판에서 서보 영점 맞추기 (CALIB 명령)]
   - 배경: 혼(horn)을 다시 끼울 때 서보를 대기 140도 / 배출 40도에 "힘을 준 채로" 붙잡아 둬야
     각도를 맞출 수 있음. 평상시엔 힘을 빼두므로(11/08) 따로 붙잡는 명령이 필요함.
   - 원격 REPAIR와 같은 토픽(sjuiot/warehouse/command, retain 없이)에 cmd만 다름:
       {"cmd":"CALIB","rail":"N_r","pos":"home"}                 -> N_r를 140도로 보내고 힘 준 채 유지
       {"cmd":"CALIB","rail":"N_r","pos":"push"}                 -> N_r를 40도로 보내고 힘 준 채 유지
       {"cmd":"CALIB","rail":"N_r","pos":"off"}                  -> 영점 작업 끝 (힘 빼고 배출 재개)
       {"cmd":"CALIB","rail":"ALL","board":"NS","pos":"home"}    -> 8개 전부 140도 (0.2초 간격으로 차례로)
     (push/off도 ALL 가능. off는 칸을 지정해도 이 보드에서 붙잡고 있던 칸 전부를 놓고 끝냄)
   - 안전: 배출 중(dsState != DS_IDLE)이면 거절. 영점 작업 중엔 이 보드 배출을 멈춤(주문은 큐에
     그대로 대기). 마지막 명령 후 CALIB_TIMEOUT_MS(60초) 지나면 자동으로 힘 빼고 배출 재개.
   - 재고 숫자/레일 상태(idle/disable)/서버 보고는 건드리지 않음. 모터 각도만 움직임.
   - 시리얼도 같음: CALIB N_r HOME / CALIB N_r PUSH / CALIB N_r OFF / CALIB ALL HOME

  다른 3개 보드는 이 파일을 복사한 뒤 "보드별 설정" 블록만 아래 표대로
  바꿔서 각 보드에 업로드하면 됩니다. (지금 이 파일은 TF 보드용입니다)

  ------------------------------------------------------------------------------
   보드      LETTER_0 / LETTER_1   AXIS_INDEX   MQTT_CLIENT_ID   BOARD_NAME
  ------------------------------------------------------------------------------
   E/I       'E' / 'I'             0            "arduino_EI"     "EI"
   N/S       'N' / 'S'             1            "arduino_NS"     "NS"
   T/F       'T' / 'F'             2            "arduino_TF"     "TF"
   P/J       'P' / 'J'             3            "arduino_PJ"     "PJ"
  ------------------------------------------------------------------------------
  ※ AXIS_NAME도 BOARD_NAME과 같은 값으로 바꿀 것 (핑 토픽은 BOARD_NAME으로 만들어짐)

  필요 라이브러리 (라이브러리 매니저에서 설치)
   - WiFiS3, PubSubClient, ArduinoJson(v6), ArduinoHttpClient,
     Adafruit PWM Servo Driver Library (+ Adafruit BusIO)
   ※ Preferences 라이브러리는 더 이상 사용하지 않음 (위 변경사항 참고)

  하드웨어 관련 주의사항 (실측 후 반드시 조정)
   - SERVO_MIN_PULSE / SERVO_MAX_PULSE, SWEEP_STEP_DELAY_MS,
     BREAKAWAY_KICK_DEG / BREAKAWAY_SETTLE_MS, IR_DETECT_LEVEL, IR_PIN,
     PCA9685 I2C 주소(0x40 아니면 수정), IR_SENSOR_INSTALLED(센서 배선 여부),
     IR_POLL_WINDOW_MS / IR_MIN_BREAK_MS(브레이크빔 폴링 디바운스 파라미터)
  ================================================================================
*/

#include <WiFiS3.h>
#include <ArduinoJson.h>
#include <PubSubClient.h>
#include <ArduinoHttpClient.h>
#include <Wire.h>
#include <Adafruit_PWMServoDriver.h>
#include "config.h"
struct DispenseJob;

// ============================== [보드별 설정] ==============================
// 이 블록만 축마다 바꿔서 4개 보드에 각각 업로드합니다.
#define AXIS_NAME               "TF"
const char  LETTER_0            = 'T';
const char  LETTER_1            = 'F';
const uint8_t AXIS_INDEX        = 2;      // keycap 문자열에서의 내 축 위치 (E/I=0, N/S=1, T/F=2, P/J=3)
const char* MQTT_CLIENT_ID      = "arduino_TF";
const char* BOARD_NAME           = "TF";  // /queue/{board}/*, /stock/keycap?board=*, /gate/{board}, 핑 토픽에 그대로 씀
// ===========================================================================

// ---- 색상 순서 (colors 배열/보드 내부 인덱싱과 동일 순서로 고정) ----
const char COLOR_LIST[4] = {'r', 'g', 'b', 'y'};

// ---- PCA9685 채널 매핑: [글자슬롯(0/1)][색상슬롯(0~3)] ----
const uint8_t SERVO_CHANNEL[2][4] = {
  {0, 2, 4, 6},      // LETTER_0 - r,g,b,y  (실배선: PCA9685 짝수 채널)
  {8, 10, 12, 14}    // LETTER_1 - r,g,b,y
};

// ---- IR 센서 핀 매핑: [글자슬롯(0/1)][색상슬롯(0~3)] (실배선에 맞게 수정) ----
const uint8_t IR_PIN[2][4] = {
  {2, 3, 4, 5},      // LETTER_0 - r,g,b,y
  {6, 7, 8, 9}       // LETTER_1 - r,g,b,y
};

// ---- 서보 동작 파라미터 (실측 후 조정) ----
#define SERVO_MIN_PULSE       150
#define SERVO_MAX_PULSE       600
#define SERVO_HOME_ANGLE      145   // [11/12] 140 -> 145. 구 주석: 대기 각도 (11/08: 165 -> 140, 165에서 크랭크가 기구에 걸려 서보가 버티며 발열)
#define PUSH_ANGLE             40   // 배출(푸시) 각도 (11/08: 20 -> 40) - 대기(140)에서 이 값까지 "감소"하며 이동함에 주의
#define SWEEP_STEP_DELAY_MS     7   // 1도 이동마다 대기 (11/11: 10 -> 7, 푸시 100도 이동 약 1.0초 -> 약 0.7초)
#define RETURN_DELAY_MS       400   // 복귀 명령 후 실제 도달까지 대기
#define DISPENSE_HOLD_MS      400   // 푸시 각도 도달 후 유지 시간
#define IR_CHECK_DELAY_MS     200   // DS_RETURNING에서 DS_WAIT_IR로 넘어가기 전 최소 물리 정착 대기 (IR 감시 자체는 이미 모터 시작 시점부터 계속 진행 중)
#define IR_DETECT_LEVEL       LOW   // 센서가 "감지됨(빔 끊김)"일 때 주는 레벨 (실측 후 조정)
#define BREAKAWAY_KICK_DEG      4   // 스윕 시작 시 정지마찰을 넘기기 위한 초기 킥 각도 (대기->푸시 방향, 즉 감소 방향으로 킥)
#define BREAKAWAY_SETTLE_MS    60   // 킥 이동 후 실제 도달까지 대기
#define CALIB_TIMEOUT_MS   60000UL   // [11/11] 영점 맞추기: 마지막 명령 후 이 시간이 지나면 자동으로 힘 빼고 배출 재개
#define SERVO_STARTUP_STAGGER_MS 200 // 부팅 시 서보를 한 채널씩 순서대로 홈으로 보낼 때 채널 사이 간격 (돌입전류 분산용, 8개 x 200ms = 약 1.6초)

// ---- [브레이크빔(송신부/수신부 분리형) 센서 판정 파라미터] ----
// 키캡이 정확히 언제 떨어질지 알 수 없음(서보가 푸시 각도까지 도는 도중일 수도,
// 홀드 중일 수도, 복귀 중일 수도 있음) -> "복귀가 끝난 뒤에만 잠깐 감시"하는
// 방식 대신, 모터가 움직이기 시작하는 순간(DS_HOMING 진입)부터 배출 사이클이
// 끝날 때까지 매 loop()마다 IR 핀을 계속 폴링함(pollIrForCurrentJob() 참고).
// 빔이 IR_MIN_BREAK_MS 이상 연속으로 끊기면 그 순간 "유효 통과"로 래치(확정)
// 하고, 그 뒤로 빔이 복구돼도 래치는 유지됨.
// IR_POLL_WINDOW_MS는 "모터 시작 시각(irWindowStart)" 기준 전체 감시 윈도우
// 길이 - 실제 서보 동작 시퀀스(복귀대기+킥+스윕+홀드+복귀대기 ≈ 2.6~3초)를
// 전부 덮고도 여유가 남도록 넉넉하게 잡음. 판정은 성공이든 실패든 이 윈도우가
// 다 끝난 뒤에만 내려지므로, 센서가 배선된 레일은 배출 1건당 최소 이 시간만큼 걸림.
#define IR_POLL_WINDOW_MS  8000   // 모터 시작 시각 기준 전체 감시 윈도우 (11/08: 7초 -> 8초, 판정 여유 확보)
#define IR_MIN_BREAK_MS       5   // [11/12] 10 -> 5: loop 개선으로 폴링이 촘촘해져 짧은 정상 통과(실측 최소 7ms)를 놓치지 않게 낮춤. 구 주석: 이 이상 연속으로 끊겨야 유효 통과로 인정(노이즈 필터). 10/07 반복 테스트에서 끊김이 대체로 20ms 이상이라 5 -> 10으로 상향. 시리얼 [IR] 진단의 "최대끊김" 값을 보고 조정할 것

// ---- [IR 센서 장착 여부 토글 - 보드 전체] ----
// 1 = 이 보드에 브레이크빔 IR 센서가 최소 1개 이상 배선됨 (판정 로직 컴파일 포함)
// 0 = 이 보드는 센서가 전혀 없음, DS_WAIT_IR 단계에서 핀을 읽지 않고 무조건 성공(idle) 처리
// 아래 IR_INSTALLED 배열로 레일 단위 세부 제어를 하므로, 이 매크로는
// "이 보드에 센서 배선이 하나라도 있는가"만 나타내는 최상위 스위치임.
// ※ 현재 값: 1 (11/01부터 4개 보드 공통 - 8개 레일 전부 브레이크빔 IR 센서 배선)
#define IR_SENSOR_INSTALLED 1

// ---- [레일별 IR 센서 배선 여부] ----
// 레일(글자슬롯×색상슬롯) 8개가 각자 독립된 센서를 필요로 함. true인 레일만
// 실제로 IR_PIN을 읽어 폴링+디바운스 판정을 하고, false인 레일은 핀을 읽지 않고
// 즉시 성공 처리함(플로팅 핀을 실수로 읽어서 오판정하는 일을 방지).
// IR_PIN 배열과 인덱스([글자슬롯][색상슬롯])가 동일하게 대응.
// ※ 현재 배선: 8개 레일 전부 (11/01부터 4개 보드 공통). 특정 레일 센서를 뺄 때는
//   그 칸만 false로 바꾸면 그 레일은 다시 "판정 없이 항상 성공"으로 돌아감.
const bool IR_INSTALLED[2][4] = {
  {true, true, true, true},   // LETTER_0 - r,g,b,y (핀 2,3,4,5)
  {true, true, true, true}    // LETTER_1 - r,g,b,y (핀 6,7,8,9)
};

// ---- [11/10] 레일별 센서 판정 켜기/끄기 (상태판 -> 키캡 Flask -> MQTT retain, 재부팅 없이 반영) ----
// 실제 판정 여부 = IR_INSTALLED(배선됨) && irEnabled(설정 켬). 부팅 기본값은 전부 켬.
bool irEnabled[2][4] = { {true, true, true, true}, {true, true, true, true} };
// 함수 대신 매크로 - 이 위치(RailStatus 등 타입 선언 전)에 함수를 두면 Arduino IDE 자동 프로토타입 위치가 앞당겨져 컴파일 에러
#define IR_ACTIVE(l, c) (IR_INSTALLED[l][c] && irEnabled[l][c])
// ---- [11/11] 영점 맞추기(CALIB) 상태 - true인 동안 이 보드 배출 멈춤 ----
bool calibActive = false;
unsigned long calibLastMs = 0;   // 마지막 CALIB 명령 시각 (CALIB_TIMEOUT_MS 기준)
bool calibHeld[2][4] = { {false, false, false, false}, {false, false, false, false} };   // 힘 준 채 붙잡고 있는 칸

bool curIrActive = false;   // 지금 배출 중인 job이 시작할 때의 판정 여부 (사이클 도중 설정이 바뀌어도 이 값 유지)

// ---- 카트리지 최대 용량 (restock 시 이 값까지 채움, 전체 풀 부족하면 서버가 그보다 적게 응답) ----
#define MAX_CAPACITY 30

// ---- disable 사유 문자열 (report 시 reason 필드에 실려 나감) ----
const char* REASON_CARTRIDGE_FAILED = "keycap_cartridge_failed"; // 끼임/서보모터 고장 (IR 판정 실패)
const char* REASON_DRIVER_FAILED    = "keycap_driver_failed";    // PCA9685 드라이버 고장 (I2C 통신 실패)

// ---- 우리가 반응하는 fault 화이트리스트 (실제 판정 함수는 RailStatus 등
// 타입 선언 뒤로 옮겨둠 - 함수 정의를 여기 두면 Arduino IDE가 자동 생성하는
// 함수 프로토타입 삽입 위치가 앞당겨져서 RailStatus 등 뒤에 나오는 타입을
// 쓰는 함수들이 "선언 안 됨" 컴파일 에러를 냄. 배열 자체는 상수 데이터라
// 여기 있어도 무방.) ----
// 플랫폼이 orders[].fault에 뭘 채워 올리든 다 반응하면 안 됨 - 예를 들어
// 보드(피지컬 보드 상품) 쪽 fault(board_pack_failed 등)가 같은 order 객체에
// 실려 올 경우 우리가 착각하고 반응할 위험이 있음. 그래서 키캡이 실제로
// 받는 fault 3종류만 화이트리스트로 걸러서 그 외는 아예 무시함.
const char* KNOWN_FAULTS[] = {
  "keycap_cartridge_failed", "keycap_driver_failed", "stage_timeout"
};
const uint8_t KNOWN_FAULTS_COUNT = sizeof(KNOWN_FAULTS) / sizeof(KNOWN_FAULTS[0]);

// ---- 레일 상태 (부팅 시 서버에서 조회해서 채움, 배출/restock마다 서버와 동기화) ----
enum RailStatus : uint8_t { RAIL_IDLE, RAIL_BUSY, RAIL_EMPTY, RAIL_DISABLE };
struct RailState {
  uint8_t count;
  RailStatus status;
  String lastOrderId;
};
RailState rail[2][4]; // [글자슬롯][색상슬롯]

char letterOf(int l) { return (l == 0) ? LETTER_0 : LETTER_1; }
char colorOf(int c)  { return COLOR_LIST[c]; }
String letterColorKey(int l, int c) {
  return String(letterOf(l)) + "_" + String(colorOf(c));
}

bool isKnownFault(const char* fault) {
  for (uint8_t i = 0; i < KNOWN_FAULTS_COUNT; i++) {
    if (strcmp(fault, KNOWN_FAULTS[i]) == 0) return true;
  }
  return false;
}

// ============================== 최근 처리한 주문 기록 (중복 배출 방지) ==============================
#define ORDER_HISTORY_SIZE 32   // 큐(8)보다 넉넉하게 - 작으면 기록이 밀려나 같은 주문이 다시 큐에 들어갈 수 있음
String processedJobKeys[ORDER_HISTORY_SIZE];
uint8_t processedIdx = 0;

bool alreadyProcessed(const String& jobKey) {
  for (uint8_t i = 0; i < ORDER_HISTORY_SIZE; i++) {
    if (processedJobKeys[i] == jobKey) return true;
  }
  return false;
}
void markProcessed(const String& jobKey) {
  processedJobKeys[processedIdx] = jobKey;
  processedIdx = (processedIdx + 1) % ORDER_HISTORY_SIZE;
}

// ============================== restock 중복 호출 방지 ==============================
// 스냅샷마다 restock 배열에 우리 레일이 계속 남아있는 동안은 이미 호출한 것으로 취급.
// 다음 스냅샷에서 그 레일이 배열에서 사라지면(=처리 완료로 플랫폼이 지움) 다시
// 등장할 때 새 restock 이벤트로 인식해서 재호출 가능해짐.
bool restockInProgress[2][4] = { {false, false, false, false}, {false, false, false, false} };

// ============================== PCA9685 드라이버 헬스체크 ==============================
// PCA9685 한 개가 이 보드의 8개 레일(글자 2 × 색상 4) 전부를 구동하므로,
// 드라이버 자체가 죽으면 8개 레일이 전부 영향을 받음. 그래서 고장 감지 시
// 8개 레일 전부에 disable을 보고하되, 지금 처리하려던 주문과 관련된 레일에만
// order_id를 채우고 나머지 7개는 order_id를 null로 보냄.
bool pcaDriverFailed = false;
unsigned long lastPcaCheckMs = 0;
const unsigned long PCA_CHECK_INTERVAL_MS = 1000; // I2C 버스 부담 줄이려고 매 loop()가 아니라 1초 간격으로만 확인

// 고장 감지 시점에 큐 맨 앞에 있던(=order_id가 실제로 붙었던) job을 기억해둠
// (그 레일에만 order_id를 실어 보고하기 위함).
// [11/01] PCA가 다시 응답해도 자동 원복은 하지 않음. 8개 레일 전부 disable +
// 배출 금지(pcaDriverFailed) 상태를 유지하다가, 사람이 keycap_driver_failed
// fault를 내리면(handleOrderFaults -> restoreRailsAfterDriverFault) 한꺼번에 풀림.
// 고장 당시 대기 주문이 없어서 fault가 안 생긴 경우는 시리얼 REPAIR ALL로 풂.
// (DispenseJob 타입 인스턴스는 struct DispenseJob 전체 정의 뒤에서 선언해야
// 해서 pcaRelatedJob 변수는 "배출 대기열" 섹션 아래에 별도로 선언함)
bool pcaHasRelatedOrder = false;

// ============================== 플랫폼(Mobius) fault 추적/재배출 ==============================
// fault 두 종류가 있는데 처리 방식이 다름:
//   - tray_jam류(keycap_cartridge_failed/keycap_driver_failed): 우리가 이미
//     disable/실패를 보고했기 때문에 플랫폼이 그걸 보고 채워주는 것(우리
//     상태의 메아리) - 등장 시엔 추적만 해두고, 해제 시점에 그때 주문 status/
//     rail 상태를 보고 필요하면 재배출함 (기존 방식 그대로 유지).
//   - stage_timeout: 같은 status가 일정 시간(60초) 유지되면 플랫폼이 자동으로
//     붙임 - 우리 rail 상태와 무관하게 뜰 수 있어서 해제 시점 rail 상태로는
//     판단할 수 없음. 이제 사람이 수동으로 폐기 처리하기로 했으므로, 아예
//     "해제 시점 판단" 자체를 안 하고 **등장하는 즉시** 그 주문을 큐에서
//     빼고 영구 재큐잉 차단함 (아래 handleOrderFaults 참고).
bool   faultActive[2][4] = { {false, false, false, false}, {false, false, false, false} };
String faultOrderId[2][4];
String faultType[2][4];   // [11/01] 추적 중인 fault 문자열 (해제 시 driver_failed였는지 구분용)

// ============================== 배출 대기열 (로컬 캐시, 서버 큐와 동기화) ==============================
struct DispenseJob {
  String orderId;
  char letter;
  char color;
};
#define JOB_QUEUE_SIZE 8
DispenseJob jobQueue[JOB_QUEUE_SIZE];
uint8_t jobHead = 0, jobTail = 0;

// PCA 복구 시 "관련 레일 1개만 disable 유지"를 위해 고장 시점 job을 기억해두는
// 변수 - struct DispenseJob 정의 뒤에 선언해야 함 (위 pcaHasRelatedOrder 참고)
DispenseJob pcaRelatedJob;

bool queuePush(const DispenseJob& job) {
  uint8_t next = (jobTail + 1) % JOB_QUEUE_SIZE;
  if (next == jobHead) return false; // 큐 가득참
  jobQueue[jobTail] = job;
  jobTail = next;
  return true;
}
bool queuePop(DispenseJob& job) {
  if (jobHead == jobTail) return false; // 비어있음
  job = jobQueue[jobHead];
  jobHead = (jobHead + 1) % JOB_QUEUE_SIZE;
  return true;
}
// [11/01] 같은 job이 이미 큐에 대기 중인지 확인 (fault 해제 시 중복 적재 방지)
bool queueContains(const String& orderId, char letter, char color) {
  for (uint8_t i = jobHead; i != jobTail; i = (i + 1) % JOB_QUEUE_SIZE) {
    if (jobQueue[i].orderId == orderId && jobQueue[i].letter == letter && jobQueue[i].color == color) return true;
  }
  return false;
}

// ============================== pickup 게이트 ==============================
// gateOpen == true  : 다음 큐 항목을 시작해도 됨
// gateOpen == false : gateOrderId가 pickup_reached(이후) 되기를 기다리는 중, 새 배출 시작 금지
// 이 두 변수는 로컬 RAM 캐시일 뿐이고, 원본은 Redis(gate:{board})임.
// 재부팅 시 restoreGateFromServer()로 복원, 변경될 때마다 lockGateOnServer/
// clearGateOnServer로 서버에도 즉시 동기화함.
bool   gateOpen = true;
String gateOrderId = "";

// ---- [11/04] 이 주문(의 우리 레일 job)을 이미 처리했는가 ----
// alreadyProcessed(RAM 기록)는 재부팅하면 지워지므로, 게이트도 같이 봄.
// "배출은 했는데 아직 assigned인 주문"은 항상 게이트에 걸린 그 주문 하나뿐이고
// (게이트가 풀리면 그 주문은 이미 pickup_reached 이후라 다시 주울 일이 없음),
// 게이트는 Redis에 영속화돼 재부팅 후에도 복원되므로 이것만으로 충분함.
// handleAssignedOrder()와 handleOrderFaults()의 fault 해제 분기 둘 다 이걸로 판단해야 함.
bool wasHandled(const String& orderId, const String& jobKey) {
  if (alreadyProcessed(jobKey)) return true;
  return (!gateOpen && gateOrderId == orderId);
}

// ---- 게이트를 열어도 되는 상태 목록 ----
// pickup_reached 이후 단계는 전부 "우리 픽업 지점은 이미 비워진 상태"이므로
// 다음 배출을 시작해도 됨. 스냅샷 유실로 pickup_reached를 건너뛰고 바로
// 다음 단계로 넘어간 경우까지 커버하기 위해 단일 status 비교 대신 화이트리스트 사용.
// ※ 실측 필요: 플랫폼팀 상태표 기준 문자열이므로 실제 Mobius 값과 철자/대소문자
//   일치 여부 반드시 확인할 것. assigned/dispensing 등 이전 단계는 절대 포함 금지.
// ※ unclaimed(노쇼) 포함 여부는 노쇼 시 실제로 픽업 지점이 비워지는지에 달려있음 -
//   로봇암/컨베이어 운영 방식 확정되면 재검토.
// ※ stage_timeout으로 폐기된 주문이 orders 목록에서 실제로 사라지는지, 아니면
//   별도 status만 남기고 목록엔 남는지 아직 미확인 - 후자라면 그 status를
//   여기 추가해야 게이트가 안 막힘 (플랫폼팀 확인 필요).
const char* GATE_OPEN_STATUSES[] = {
  "pickup_reached", "loaded", "arrived", "unclaimed", "verified", "received", "completed"
};
const uint8_t GATE_OPEN_STATUSES_COUNT = sizeof(GATE_OPEN_STATUSES) / sizeof(GATE_OPEN_STATUSES[0]);

bool isGateOpenStatus(const char* status) {
  for (uint8_t i = 0; i < GATE_OPEN_STATUSES_COUNT; i++) {
    if (strcmp(status, GATE_OPEN_STATUSES[i]) == 0) return true;
  }
  return false;
}

// ============================== [11/04] 서버 복원 상태 ==============================
// 재고/큐/게이트를 서버(Redis)에서 각각 받아왔는지. 셋 다 true가 되기 전엔
// MQTT에 접속하지 않고(=주문을 안 받고) 배출도 시작하지 않음.
// 실패한 항목만 loop()의 tryRestoreFromServer()가 RESTORE_RETRY_MS마다 재시도함.
bool stockRestored = false;
bool queueRestored = false;
bool gateRestored  = false;
bool restoreAttempted = false;
unsigned long lastRestoreAttemptMs = 0;
const unsigned long RESTORE_RETRY_MS = 5000;

bool serverRestored() { return stockRestored && queueRestored && gateRestored; }

// ============================== 배출 상태 머신 ==============================
enum DispenseState { DS_IDLE, DS_HOMING, DS_BREAKAWAY_WAIT, DS_SWEEPING, DS_HOLDING, DS_RETURNING, DS_WAIT_IR };
DispenseState dsState = DS_IDLE;
DispenseJob   currentJob;
unsigned long dsTimer = 0;
int           sweepAngle = 0;

// ---- 브레이크빔 통과 감지용 (모터 시작 시점부터 배출 사이클 내내 계속 폴링) ----
unsigned long irWindowStart = 0;  // 감시 윈도우 시작 시각 (=모터 시작 시각, DS_IDLE->DS_HOMING 전환 시점에 기록)
unsigned long irBreakStart  = 0;  // 지금 이어지고 있는 "빔 끊김" 구간의 시작 시각 (0 = 지금은 안 끊긴 상태)
bool          irLatched     = false; // 윈도우 안에서 유효 통과(디바운스 통과)가 한 번이라도 확정됐는지
unsigned long irMaxBreakMs = 0;  // [진단] 이번 배출에서 관측된 가장 긴 연속 끊김 시간(ms)
unsigned long irLastPollMs = 0;  // [진단] 직전 폴링 시각
unsigned long irMaxGapMs   = 0;  // [진단] 폴링 사이 가장 긴 간격(ms) - 크면 loop()가 막혀서 감시를 놓쳤다는 뜻
unsigned long irPollCount = 0;     // [진단] 이번 배출 동안 폴링한 횟수
unsigned long irBrokenSamples = 0; // [진단] 그 중 "끊김"으로 읽힌 샘플 수
// [11/11] 진단: 시간 기록 (전부 모터 시작 시각 irWindowStart 기준 ms)
unsigned long irPushReachedMs = 0;   // 푸시 각도 도달 시각
unsigned long irReturnCmdMs   = 0;   // 홈 복귀 명령 시각
bool          irPollFirstSet  = false;
unsigned long irPollFirstMs   = 0;   // 폴링이 처음 "끊김"을 본 시각
unsigned long irPollLatchMs   = 0;   // 폴링이 유효 통과를 확정한 시각

#if IR_SENSOR_INSTALLED
// ---- 핀 변화 인터럽트로 끊김 시간 측정 ----
// loop()에서 폴링하면 WiFi/MQTT 호출 때문에 폴링 간격이 길어져서(수십 ms) 짧은
// 끊김을 놓치거나 샘플 1개로만 잡히는 문제가 있었음 -> 핀 변화 시각을 마이크로초로
// 직접 기록해서 loop() 속도와 무관하게 끊김 길이를 잼. 현재 배출 중인 레일의 핀(irIsrPin)만 봄.
// (8개 핀이 같은 ISR을 공유함 - 어느 핀에서 불리든 irIsrPin의 현재 레벨만 읽으므로
//  다른 레일 핀의 변화로 불려도 판정에는 영향 없음)
volatile int8_t        irIsrPin = -1;          // -1 = 감시 안 함
volatile unsigned long irIsrBreakStartUs = 0;  // 지금 이어지는 끊김의 시작 시각(us), 0 = 안 끊김
volatile unsigned long irIsrMaxUs = 0;         // 이번 배출에서 관측된 가장 긴 끊김(us)
volatile bool          irIsrLatched = false;   // IR_MIN_BREAK_MS 이상 끊김이 한 번이라도 있었는지
// [11/11] 끊김 구간별 기록 (시작 시각은 모터 시작 기준 ms, 길이는 us)
#define IR_EV_MAX 8
volatile unsigned long irEvStartMs[IR_EV_MAX];
volatile unsigned long irEvLenUs[IR_EV_MAX];
volatile uint8_t       irEvCount = 0;
volatile unsigned long irIsrBreakStartMs = 0;   // 지금 이어지는 끊김의 시작 시각(모터 시작 기준 ms)
volatile bool          irIsrLatchSet = false;
volatile unsigned long irIsrLatchMs = 0;        // 유효 통과 확정 시각(끊김 시작 + 기준)

void irIsr() {
  if (irIsrPin < 0) return;
  unsigned long now = micros();
  if (now == 0) now = 1;
  bool broken = (digitalRead(irIsrPin) == IR_DETECT_LEVEL);
  if (broken) {
    if (irIsrBreakStartUs == 0) { irIsrBreakStartUs = now; irIsrBreakStartMs = millis() - irWindowStart; }
  } else if (irIsrBreakStartUs != 0) {
    unsigned long len = now - irIsrBreakStartUs;
    irIsrBreakStartUs = 0;
    if (len > irIsrMaxUs) irIsrMaxUs = len;
    if (irEvCount < IR_EV_MAX) { irEvStartMs[irEvCount] = irIsrBreakStartMs; irEvLenUs[irEvCount] = len; irEvCount++; }
    if (len >= (unsigned long)IR_MIN_BREAK_MS * 1000UL) {
      irIsrLatched = true;
      if (!irIsrLatchSet) { irIsrLatchSet = true; irIsrLatchMs = irIsrBreakStartMs + IR_MIN_BREAK_MS; }
    }
  }
}
#endif

// ---- 마지막으로 처리한 cnt_process 스냅샷 seq (문자열 비교, 자릿수 제한 없음) ----
String lastSeqStr = "";

// ---- 네트워크 / 하드웨어 객체 ----
// [11/01] 로컬 서버(평문) 전환: WiFiSSLClient -> WiFiClient
WiFiClient    wifiClient;         // MQTT 전용 - 상시 연결 유지 (주문 수신, 로컬 Mosquitto 평문)
PubSubClient  mqttClient(wifiClient);
WiFiClient    httpClient;         // Flask 보고/조회 전용 - 필요할 때만 짧게 연결 (로컬 HTTP 평문)
Adafruit_PWMServoDriver pwm = Adafruit_PWMServoDriver(0x40);

// ---- MQTT 토픽 ----
const char* TOPIC_CNT_PROCESS = "sjuiot/warehouse/process";
const char* TOPIC_COMMAND     = "sjuiot/warehouse/command";  // [11/04] 원격 REPAIR 명령 (4대 공통, retain 없이 발행할 것)

// ---- [11/04] 구독 생존 확인용 자기 에코(핑) ----
// 보드 전용 토픽(sjuiot/warehouse/ping/{BOARD_NAME})을 주문 토픽과 같이 구독해두고,
// 주기적으로 자기에게 핑을 보내서 돌아오는지 확인함. 안 돌아오면 "연결은 됐는데
// 메시지를 못 받는" 상태로 보고 끊고 재접속함 (재접속 후 retained 스냅샷을 바로 받으므로 유실 없음).
#define TOPIC_PING_PREFIX     "sjuiot/warehouse/ping/"
#define PING_FIRST_DELAY_MS   10000UL   // 접속 직후 첫 핑까지 대기 (11/03 같은 상황을 빨리 잡기 위해 짧게)
#define PING_INTERVAL_MS      120000UL  // 이후 핑 주기 (2분)
#define PING_TIMEOUT_MS       30000UL   // 이 안에 자기 핑이 안 돌아오면 구독이 죽은 것으로 판단
char          topicPing[48];            // setup()에서 BOARD_NAME으로 만듦
#define TOPIC_IR_PREFIX       "sjuiot/warehouse/ir/"   // [11/10] 보드별 센서 설정 (retain)
char          topicIr[48];              // setup()에서 BOARD_NAME으로 만듦
bool          pingPending = false;      // 핑을 보내고 아직 돌아오길 기다리는 중
unsigned long pingSentMs = 0;           // 마지막 핑 보낸 시각
unsigned long nextPingMs = 0;           // 다음 핑 보낼 시각
String        pingToken = "";           // 마지막으로 보낸 핑 내용 (돌아온 게 내 핑인지 비교)

// ---- Flask(로컬 서버) 엔드포인트 ----
const char* REPORT_STATUS_PATH   = "/report/keycap-status";
const char* REPORT_DISPENSE_PATH = "/report/keycap-dispense";
const char* GATE_PATH            = "/gate"; // GET/POST {GATE_PATH}/{BOARD_NAME}, POST {GATE_PATH}/{BOARD_NAME}/clear

// ---- Flask -> Mobius CIN 생성 재시도 시간에 맞춘 타임아웃 ----
// Flask 워스트케이스: 요청당 3초 × 2회 + 재시도 대기 0.5초 × 1회 ≈ 6.5초.
// 그보다 여유 있게 8초로 설정. REPORT_STATUS_PATH / REPORT_DISPENSE_PATH,
// 즉 실제로 Mobius를 거치는 두 호출에만 적용 (나머지는 기본 5000ms 유지).
#define FLASK_MOBIUS_TIMEOUT_MS 8000UL

// ---- [11/01] MQTT 연결 상태 LED (보드 기본 LED, "L" 표시) ----
// 연결 성공 순간 MQTT_OK_BLINKS번 깜빡인 뒤, 연결 유지 중엔 켜짐 / 끊기면 꺼짐
#define STATUS_LED_PIN   LED_BUILTIN
#define MQTT_OK_BLINKS   5
#define LED_BLINK_MS     150
uint8_t       ledTogglesLeft = 0;   // 남은 켜짐/꺼짐 전환 횟수 (깜빡임 1번 = 2회)
unsigned long ledTimer = 0;
bool          ledState = false;

// ============================== 함수 선언 ==============================
void ensureWiFi();
void ensureMQTT();
bool tryConnectMQTT();
void checkMqttEcho();
void mqttCallback(char* topic, byte* payload, unsigned int length);
void handleOrdersField(JsonObject orders);
void handleRestockField(JsonArray restock);
void handleOrderFaults(JsonObject orders);
bool getOrderLetterColor(JsonObject order, char& letter, char& color);
void checkGateAgainstOrders(JsonObject orders);
void pruneDeadJobsFromQueue(JsonObject orders);
void removeOrderFromQueue(const String& orderId);
void restoreRailsAfterDriverFault(int keepL, int keepC);
void handleAssignedOrder(const String& orderId, const char* keycap, JsonArray colors);
void runDispenseStateMachine();
void pollIrForCurrentJob();
bool checkPcaHealthy();
void moveServoAngle(uint8_t channel, uint8_t angle);
void releaseServo(uint8_t channel);
void reportRailStatus(char letter, char color, RailStatus status, const String& orderId, const char* reason = "");
void reportDispenseResult(const String& orderId, char letter, char color);
int  letterSlot(char c);
int  colorSlot(char c);
bool postJsonToFlask(const char* path, const char* payload, String& responseOut, unsigned long timeoutMs = 5000);
bool getJsonFromFlask(const char* path, String& responseOut);
void tryRestoreFromServer();
bool fetchInitialStock();
void checkAllDisabledAfterStockRestore();
bool restoreQueueFromServer();
void enqueueToServer(const DispenseJob& job);
void completeOnServer(const DispenseJob& job);
void callRestock(int l, int c);
void checkSerialCommand();
void handleCommandMessage(byte* payload, unsigned int length);
void handleIrConfig(byte* payload, unsigned int length);
void printIrConfig();
bool repairRail(int l, int c, const char* source);
void repairAllRails(const char* source);
bool restoreGateFromServer();
void lockGateOnServer(const String& orderId);
void clearGateOnServer();
void startConnectBlink();
void updateStatusLed();
void handleCalibCommand(const String& railStr, const String& posStr, bool targeted, const char* source);
void calibEnd(const char* source);
void checkCalibTimeout();

// ============================== setup / loop ==============================
void setup() {
  Serial.begin(115200);
  delay(500);

  pinMode(STATUS_LED_PIN, OUTPUT);   // [11/01] MQTT 연결 상태 LED
  digitalWrite(STATUS_LED_PIN, LOW);

  // IR 센서 핀 설정
  // - 배선 완료된 레일(IR_INSTALLED[l][c]==true): 브레이크빔 수신부 신호선이
  //   실제로 물려있으므로 일반 INPUT (수신부 모듈 자체에 보통 풀업/풀다운이
  //   내장돼 있어 별도 내부 풀업 불필요)
  // - 미배선 레일: 핀에 아무것도 안 물려있어 플로팅 상태라 값이 랜덤하게
  //   튐 -> INPUT_PULLUP으로 묶어서 안정적으로 HIGH 유지 (어차피 이 레일은
  //   DS_WAIT_IR에서 아예 읽지 않지만, 방어적으로 안전한 상태를 유지함)
  for (int l = 0; l < 2; l++) {
    for (int c = 0; c < 4; c++) {
      pinMode(IR_PIN[l][c], IR_INSTALLED[l][c] ? INPUT : INPUT_PULLUP);
      rail[l][c].status = RAIL_IDLE; // 서버 조회 전 임시값, fetchInitialStock()에서 덮어씀
    }
  }

#if IR_SENSOR_INSTALLED
  // 배선된 레일의 IR 핀에 변화 인터럽트 연결 (끊김 시간을 loop() 속도와 무관하게 측정)
  for (int l = 0; l < 2; l++) {
    for (int c = 0; c < 4; c++) {
      if (!IR_INSTALLED[l][c]) continue;
      // UNO R4 코어에는 NOT_AN_INTERRUPT가 없어서 유효성 검사 없이 바로 연결함.
      // UNO R4 WiFi는 모든 핀이 외부 인터럽트를 지원하지는 않으므로, 인터럽트가
      // 안 붙는 핀의 레일은 폴링 판정만 동작함 (판정 = 폴링 OR 인터럽트라 동작 자체는 함).
      // 시리얼 [IR] 진단에서 레일별로 "인터럽트 최대끊김(us)"이 계속 0인지 확인할 것.
      attachInterrupt(digitalPinToInterrupt(IR_PIN[l][c]), irIsr, CHANGE);
    }
  }
#endif

  // PCA9685 초기화 및 전 채널 홈 포지션 이동
  Wire.begin();
  pwm.begin();
  pwm.setPWMFreq(50);
  delay(50);
  // 8개를 동시에 움직이면 돌입전류로 전압이 순간 떨어질 수 있어서,
  // 한 채널씩 SERVO_STARTUP_STAGGER_MS 간격을 두고 순서대로 홈으로 보냄.
  for (int l = 0; l < 2; l++) {
    for (int c = 0; c < 4; c++) {
      moveServoAngle(SERVO_CHANNEL[l][c], SERVO_HOME_ANGLE);
      delay(SERVO_STARTUP_STAGGER_MS);
    }
  }
  // [11/08] 홈 도달을 기다린 뒤 8개 전부 힘 빼기 (평상시 서보 off - 발열 방지)
  delay(RETURN_DELAY_MS);
  for (int l = 0; l < 2; l++) {
    for (int c = 0; c < 4; c++) {
      releaseServo(SERVO_CHANNEL[l][c]);
    }
  }

  // WiFi 연결 (최초 1회, 연결될 때까지 대기)
  Serial.print("[WiFi] 연결 시도: ");
  Serial.println(WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  unsigned long wifiStart = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - wifiStart < 30000) {
    delay(300);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("[WiFi] 연결됨");
  } else {
    Serial.println("[WiFi] 30초 내 연결 실패, loop()에서 계속 재시도함");
  }

  // [11/01] 로컬 Mosquitto(1883)/Flask(5000)는 TLS 없는 평문 통신이라 인증서가
  // 필요 없음 (이전의 "SSL 루트 인증서 업로드"도 더 이상 불필요).

  mqttClient.setServer(MQTT_HOST, MQTT_PORT);
  mqttClient.setBufferSize(4096); // 이보다 큰 메시지는 PubSubClient가 콜백 없이 조용히 버림 - 주문이 몰릴 때 대비해 여유 있게
  mqttClient.setCallback(mqttCallback);

  // [11/04] 보드 전용 핑 토픽 (예: sjuiot/warehouse/ping/EI)
  snprintf(topicPing, sizeof(topicPing), "%s%s", TOPIC_PING_PREFIX, BOARD_NAME);
  snprintf(topicIr, sizeof(topicIr), "%s%s", TOPIC_IR_PREFIX, BOARD_NAME);   // [11/10]

  // [11/04] 서버(Redis)에서 재고/큐/게이트 복원 - 여기서 1차 시도하고, 실패한 항목은
  // loop()의 tryRestoreFromServer()가 RESTORE_RETRY_MS(5초)마다 재시도함.
  // 셋 다 끝나기 전엔 MQTT 접속(주문 수신)과 배출을 하지 않으므로, Flask가 늦게
  // 켜져도 재고 0/게이트 open 같은 잘못된 기본값으로 주문을 처리하는 일이 없음.
  tryRestoreFromServer();

  Serial.print("[");
  Serial.print(AXIS_NAME);
  Serial.println("] setup 완료");
  Serial.print("[설정] 대기=");  Serial.print(SERVO_HOME_ANGLE);
  Serial.print("도, 푸시=");     Serial.print(PUSH_ANGLE);
  Serial.print("도, IR감시=");   Serial.print(IR_POLL_WINDOW_MS);
  Serial.print("ms, 최소끊김="); Serial.print(IR_MIN_BREAK_MS);
  Serial.print("ms, IR센서=");   Serial.println(IR_SENSOR_INSTALLED ? "사용" : "없음(항상 성공 처리)");
  Serial.print("[설정] 핑 토픽="); Serial.println(topicPing);
  Serial.println("[안내] 시리얼 명령: SETEMPTY T_r / REPAIR T_r / REPAIR ALL / IR / CALIB T_r HOME|PUSH|OFF / CALIB ALL HOME (테스트용)");
  Serial.print("[안내] 원격 복구: ");
  Serial.print(TOPIC_COMMAND);
  Serial.print(" 에 {\"cmd\":\"REPAIR\",\"rail\":\"T_r\"} 또는 {\"cmd\":\"REPAIR\",\"rail\":\"ALL\",\"board\":\"");
  Serial.print(BOARD_NAME);
  Serial.println("\"}");
}

// ---- [11/12] loop() 속도 개선 ----
// 배경: 시리얼 진단에서 loop() 한 바퀴가 약 60ms로 측정됨(폴링 131회/8초, 최대간격 72~82ms).
// 스윕이 "loop() 한 바퀴에 1도"씩이라 SWEEP_STEP_DELAY_MS(7ms)가 아무 의미 없이
// 1도당 약 60ms -> 푸시 100도에 약 6초가 걸렸고, IR 폴링도 60ms 간격이라 짧은 끊김을 놓침.
// 원인 추정: R4 WiFi는 WiFi.status() / client.connected() 호출마다 WiFi 모듈과 통신(수 ms~수십 ms)
// 하는데, 이걸 매 바퀴 여러 번 부르고 있었음.
// 대응: (1) WiFi/MQTT "연결됨" 확인 결과를 잠깐 캐시 (끊김은 캐시 안 하고 바로 재확인)
//       (2) mqttClient.loop()는 MQTT_LOOP_MS 간격으로만 호출
//       (3) LOOP_PROFILE=1이면 구간별 소요시간을 LOOP_PROFILE_PRINT_MS마다 시리얼로 출력
#define WIFI_UP_CACHE_MS      1000UL
#define MQTT_UP_CACHE_MS       200UL
#define MQTT_LOOP_MS            30UL
#define LOOP_PROFILE              1
#define LOOP_PROFILE_PRINT_MS 20000UL

unsigned long wifiUpAt = 0;
unsigned long mqttUpAt = 0;

bool wifiUp() {
  unsigned long now = millis();
  if (wifiUpAt != 0 && now - wifiUpAt < WIFI_UP_CACHE_MS) return true;
  if (WiFi.status() == WL_CONNECTED) { wifiUpAt = now ? now : 1; return true; }
  wifiUpAt = 0;
  return false;
}

bool mqttUp() {
  unsigned long now = millis();
  if (mqttUpAt != 0 && now - mqttUpAt < MQTT_UP_CACHE_MS) return true;
  if (mqttClient.connected()) { mqttUpAt = now ? now : 1; return true; }
  mqttUpAt = 0;
  return false;
}

#if LOOP_PROFILE
unsigned long profUs[9] = {0,0,0,0,0,0,0,0,0};
unsigned long profLoops = 0, profMaxLoopUs = 0, profLastPrint = 0, profLastLoopStart = 0;
#define PSEC(i, call) { unsigned long _t = micros(); call; profUs[i] += micros() - _t; }
#else
#define PSEC(i, call) call
#endif

void loop() {
#if LOOP_PROFILE
  unsigned long loopStart = micros();
  if (profLastLoopStart != 0) {
    unsigned long d = loopStart - profLastLoopStart;
    if (d > profMaxLoopUs) profMaxLoopUs = d;
  }
  profLastLoopStart = loopStart;
#endif
  PSEC(0, ensureWiFi());
  PSEC(1, tryRestoreFromServer());   // [11/04] 복원 안 끝난 항목만 5초마다 재시도 (다 끝났으면 바로 return)
  PSEC(2, ensureMQTT());             // [11/04] 복원이 다 끝나야 접속함
  {
    static unsigned long lastMqttLoop = 0;
    unsigned long nowMs = millis();
    if (nowMs - lastMqttLoop >= MQTT_LOOP_MS) {
      lastMqttLoop = nowMs;
      PSEC(3, mqttClient.loop());
    }
  }
  PSEC(4, checkMqttEcho());          // [11/04] 구독이 살아있는지 자기 핑으로 확인
  PSEC(5, runDispenseStateMachine());
  PSEC(6, checkSerialCommand());
  PSEC(7, updateStatusLed());
  PSEC(8, checkCalibTimeout());      // [11/11] 영점 작업 60초 지나면 자동 종료
#if LOOP_PROFILE
  profLoops++;
  if (millis() - profLastPrint >= LOOP_PROFILE_PRINT_MS) {
    if (profLastPrint != 0 && profLoops > 0) {
      Serial.print("[LOOP] 바퀴당 평균(us): wifi="); Serial.print(profUs[0] / profLoops);
      Serial.print(" 복원="); Serial.print(profUs[1] / profLoops);
      Serial.print(" mqttConn="); Serial.print(profUs[2] / profLoops);
      Serial.print(" mqttLoop="); Serial.print(profUs[3] / profLoops);
      Serial.print(" 핑="); Serial.print(profUs[4] / profLoops);
      Serial.print(" 배출="); Serial.print(profUs[5] / profLoops);
      Serial.print(" 시리얼="); Serial.print(profUs[6] / profLoops);
      Serial.print(" LED="); Serial.print(profUs[7] / profLoops);
      Serial.print(" 영점="); Serial.print(profUs[8] / profLoops);
      Serial.print(" | 바퀴수="); Serial.print(profLoops);
      Serial.print(" 최대간격(ms)="); Serial.println(profMaxLoopUs / 1000.0, 1);
    }
    for (uint8_t i = 0; i < 9; i++) profUs[i] = 0;
    profLoops = 0; profMaxLoopUs = 0; profLastPrint = millis();
  }
#endif
}

// ============================== WiFi / MQTT 연결 관리 (논블로킹) ==============================
unsigned long lastWifiAttempt = 0;
const unsigned long WIFI_RETRY_MS = 5000;

void ensureWiFi() {
  if (wifiUp()) return;
  unsigned long now = millis();
  if (now - lastWifiAttempt < WIFI_RETRY_MS) return;
  lastWifiAttempt = now;
  Serial.println("[WiFi] 재연결 시도...");
  WiFi.begin(WIFI_SSID, WIFI_PASS);
}

unsigned long lastMqttAttempt = 0;
const unsigned long MQTT_RETRY_MS = 3000;

void ensureMQTT() {
  if (!wifiUp()) return;
  if (!serverRestored()) return;   // [11/04] 재고/큐/게이트 복원 전엔 주문을 받지 않음
  if (mqttUp()) return;
  unsigned long now = millis();
  if (now - lastMqttAttempt < MQTT_RETRY_MS) return;
  lastMqttAttempt = now;
  tryConnectMQTT();
}

// [11/01] MQTT_USER가 빈 문자열이면 익명 접속, 아니면 계정 접속
// 실패 시 state: -2 = TCP 연결 불가(Mosquitto 외부 접속 미허용/방화벽/IP 오류),
//                 4 = 계정/비번 틀림, 5 = 인증 필요(익명 불가)
// [11/04] 접속 후 구독(주문 토픽 + 핑 토픽)이 하나라도 실패하면 바로 끊음 ->
// ensureMQTT()가 MQTT_RETRY_MS(3초)마다 접속+구독이 다 성공할 때까지 계속 재시도.
// (예전엔 구독 실패를 확인하지 않아서 "연결됨(LED 켜짐) + 수신 불가" 상태로 굳었음 - 11/03)
bool tryConnectMQTT() {
  mqttUpAt = 0;
  Serial.print("[MQTT] 연결 시도... ");
  bool ok;
  if (strlen(MQTT_USER) == 0) {
    ok = mqttClient.connect(MQTT_CLIENT_ID);                        // 익명
  } else {
    ok = mqttClient.connect(MQTT_CLIENT_ID, MQTT_USER, MQTT_PASS);  // 계정
  }
  if (!ok) {
    Serial.print("실패, state=");
    Serial.println(mqttClient.state());
    return false;
  }

  bool subProcess = mqttClient.subscribe(TOPIC_CNT_PROCESS, 1);
  bool subPing    = mqttClient.subscribe(topicPing, 0);
  bool subCommand = mqttClient.subscribe(TOPIC_COMMAND, 1);   // [11/04] 원격 REPAIR
  bool subIr      = mqttClient.subscribe(topicIr, 1);         // [11/10] 센서 설정 (retain - 구독하면 바로 옴)
  if (!subProcess || !subPing || !subCommand || !subIr) {
    Serial.print("연결은 됐지만 구독 실패 (주문=");
    Serial.print(subProcess ? "OK" : "실패");
    Serial.print(", 핑=");
    Serial.print(subPing ? "OK" : "실패");
    Serial.print(", 명령=");
    Serial.print(subCommand ? "OK" : "실패");
    Serial.print(", 센서설정=");
    Serial.print(subIr ? "OK" : "실패");
    Serial.println(") - 끊고 재시도");
    mqttClient.disconnect();
    return false;
  }

  Serial.println("성공 (구독 완료)");
  pingPending = false;
  nextPingMs = millis() + PING_FIRST_DELAY_MS;  // 접속 직후 첫 핑은 조금 빨리
  startConnectBlink();  // [11/01] 연결 성공 표시
  return true;
}

// ---- [11/04] 구독 생존 확인 (자기 에코) ----
// 주기적으로 내 핑 토픽에 핑을 보내고, PING_TIMEOUT_MS 안에 그대로 돌아오는지 확인.
// 돌아오면 브로커 -> 아두이노 수신 경로가 살아있는 것. 안 돌아오면 구독이 죽은
// 것으로 보고 끊음 -> ensureMQTT()가 재접속+재구독하고, retained 스냅샷을 바로 받음.
// 정상일 땐 절대 끊지 않음. 끊는 건 배출 사이클 중이 아닐 때(DS_IDLE)만 -
// 배출 중이면 사이클이 끝난 뒤 다음 loop()에서 판단함.
void checkMqttEcho() {
  if (!mqttUp()) { pingPending = false; return; }
  unsigned long now = millis();

  if (pingPending) {
    if (now - pingSentMs < PING_TIMEOUT_MS) return;  // 아직 기다리는 중
    if (dsState != DS_IDLE) return;                    // 배출 중엔 끊지 않음
    Serial.println("[MQTT] 자기 핑이 " + String(PING_TIMEOUT_MS / 1000) + "초 안에 안 돌아옴 - 구독이 죽은 것으로 보고 재접속");
    pingPending = false;
    mqttClient.disconnect();
    return;
  }

  if ((long)(now - nextPingMs) < 0) return;  // 아직 보낼 때 아님

  pingToken = String(now);
  if (mqttClient.publish(topicPing, pingToken.c_str())) {
    pingPending = true;
    pingSentMs = now;
  } else {
    // 발행 자체가 실패 = 연결이 이미 이상함. 다음 주기에 다시 시도하되,
    // 배출 중이 아니면 바로 끊고 재접속함.
    Serial.println("[MQTT] 핑 발행 실패");
    if (dsState == DS_IDLE) mqttClient.disconnect();
  }
  nextPingMs = now + PING_INTERVAL_MS;
}

// ---- [11/01] MQTT 연결 상태 LED ----
void startConnectBlink() {
  ledTogglesLeft = MQTT_OK_BLINKS * 2;
  ledTimer = millis();
  ledState = false;
  digitalWrite(STATUS_LED_PIN, LOW);
}

// loop()마다 호출: 깜빡임 진행 중이면 LED_BLINK_MS 간격으로 토글,
// 끝났으면 MQTT 연결 상태 그대로 표시 (연결=켜짐, 끊김=꺼짐)
void updateStatusLed() {
  if (ledTogglesLeft > 0) {
    if (millis() - ledTimer >= LED_BLINK_MS) {
      ledTimer = millis();
      ledState = !ledState;
      digitalWrite(STATUS_LED_PIN, ledState ? HIGH : LOW);
      ledTogglesLeft--;
    }
    return;
  }
  bool connected = mqttUp();
  if (connected != ledState) {
    ledState = connected;
    digitalWrite(STATUS_LED_PIN, ledState ? HIGH : LOW);
  }
}

// ============================== MQTT 수신 콜백 ==============================
void mqttCallback(char* topic, byte* payload, unsigned int length) {
  // [11/04] 내 핑이 돌아온 것 - 구독 살아있음 확인
  if (strcmp(topic, topicPing) == 0) {
    if (pingPending && length == pingToken.length() &&
        memcmp(payload, pingToken.c_str(), length) == 0) {
      pingPending = false;
    }
    return;
  }

  // [11/04] 원격 REPAIR 명령
  // [11/10] 센서 판정 설정 (보드별 retain)
  if (strcmp(topic, topicIr) == 0) {
    handleIrConfig(payload, length);
    return;
  }

  if (strcmp(topic, TOPIC_COMMAND) == 0) {
    handleCommandMessage(payload, length);
    return;
  }

  if (strcmp(topic, TOPIC_CNT_PROCESS) != 0) return;

  // 스택(지역 2KB 배열) 대신 힙에 할당 - 주문이 많아지면 2048로는 NoMemory로 파싱 실패함
  DynamicJsonDocument doc(4096);
  DeserializationError err = deserializeJson(doc, payload, length);
  if (err) {
    Serial.print("[MQTT] JSON 파싱 실패: ");
    Serial.print(err.c_str());
    Serial.print(" (수신 길이=");
    Serial.print(length);
    Serial.println("바이트)");
    return;
  }

  // seq가 없는 스냅샷은 중복 비교를 하지 않음 (없을 때 "null" 문자열끼리 같다고
  // 판단해서 이후 스냅샷을 전부 버리는 일을 막음)
  // [11/04] retained 스냅샷: 재접속 시 이미 처리한 스냅샷이 다시 오면 여기서 걸러지고,
  // 끊긴 사이 새 스냅샷이 있었으면 seq가 달라서 정상 처리됨. 재부팅 후엔 lastSeqStr이
  // 비어 있어 처리되지만, wasHandled()/큐 복원 기록이 중복 적재를 막음.
  if (!doc["seq"].isNull()) {
    String seqStr = doc["seq"].as<String>();
    if (seqStr == lastSeqStr) return; // 동일 스냅샷 -> 재전송 무시
    lastSeqStr = seqStr;
  }

  JsonObject orders = doc["orders"];

  // 게이트 확인이 먼저 - 이번 스냅샷에서 우리가 기다리던 주문이 pickup_reached
  // (이후 상태 포함)됐는지 체크
  checkGateAgainstOrders(orders);

  // 큐에 남아있던 job 중 이번 스냅샷 orders에 없는 것들(종료/유령 주문) 정리
  pruneDeadJobsFromQueue(orders);

  // fault 등장/해제 먼저 처리 - 특히 fault가 풀렸는데 주문 status가 여전히
  // "assigned"인 경우(예: stage_timeout), 아래 handleOrdersField()가 같은
  // 주문을 정상 경로로 또 큐에 넣기 전에 먼저 처리해서 markProcessed로
  // 중복 방지 기록을 남겨야 함 (안 그러면 같은 주문이 두 번 큐에 들어감)
  handleOrderFaults(orders);

  handleOrdersField(orders);

  JsonArray restock = doc["keycap_restock"];
  handleRestockField(restock);
}

// ---- 게이트: 우리가 기다리던 주문이 pickup_reached 이후 단계가 됐는지 확인, 맞으면 게이트 열기 ----
void checkGateAgainstOrders(JsonObject orders) {
  if (gateOpen) return; // 기다리는 중이 아니면 확인할 것 없음
  if (gateOrderId.length() == 0) { gateOpen = true; return; }

  if (!orders.containsKey(gateOrderId.c_str())) {
    // 목록에서 아예 사라짐 = 완료 처리된 것으로 간주
    Serial.println("[게이트] " + gateOrderId + " 목록에서 사라짐 -> 게이트 열림");
    gateOpen = true;
    gateOrderId = "";
    clearGateOnServer();
    return;
  }

  JsonObject order = orders[gateOrderId.c_str()].as<JsonObject>();
  const char* status = order["status"] | "";
  if (isGateOpenStatus(status)) {
    Serial.println("[게이트] " + gateOrderId + " status=" + String(status) + " -> 게이트 열림");
    gateOpen = true;
    gateOrderId = "";
    clearGateOnServer();
  }
}

// 로컬 큐(jobQueue)에 대기 중인 job들 중, 이번 스냅샷의 orders에 더 이상
// 존재하지 않는 것들을 제거함. cnt_process 스펙상 "진행 중인 주문은 매
// 스냅샷마다 전부 실린다"고 명시돼 있으므로, 한 번이라도 orders에서 빠지면
// 그 주문은 더 이상 진행 중이 아니라고(종료됐거나 애초에 존재하지 않았다고)
// 판단해도 됨 - 유예 없이 즉시 제거.
// currentJob(지금 배출 진행 중인 것)은 이미 큐에서 빠져나온 상태라 이 함수와
// 무관 - 건드리지 않음.
void pruneDeadJobsFromQueue(JsonObject orders) {
  DispenseJob temp[JOB_QUEUE_SIZE];
  uint8_t tempCount = 0;
  uint8_t removed = 0;

  DispenseJob job;
  while (queuePop(job)) {
    if (orders.containsKey(job.orderId.c_str())) {
      temp[tempCount++] = job;  // 살아있음 - 유지
    } else {
      completeOnServer(job);    // 죽은 job - 서버 큐에서도 제거
      removed++;
    }
  }

  // 살아있는 것만 순서 그대로 다시 채워넣기
  for (uint8_t i = 0; i < tempCount; i++) {
    queuePush(temp[i]);
  }

  if (removed > 0) {
    Serial.println("[큐] orders에서 사라진 job " + String(removed) + "건 제거함");
  }
}

// ---- 특정 order_id의 job이 로컬 큐(jobQueue)에 대기 중이면 즉시 꺼내서
// 서버 큐에서도 같이 제거함. stage_timeout fault가 뜬 주문을 등장 즉시
// 큐에서 빼기 위해 씀(handleOrderFaults 참고). currentJob(이미 큐 밖으로
// 나와 배출 진행 중인 것)은 건드리지 않음 - 물리적으로 멈출 방법이 없어서
// 그대로 끝까지 진행되도록 둠.
void removeOrderFromQueue(const String& orderId) {
  DispenseJob temp[JOB_QUEUE_SIZE];
  uint8_t tempCount = 0;
  bool removed = false;

  DispenseJob job;
  while (queuePop(job)) {
    if (job.orderId == orderId) {
      completeOnServer(job); // 서버 큐에서도 제거
      removed = true;
    } else {
      temp[tempCount++] = job;
    }
  }

  for (uint8_t i = 0; i < tempCount; i++) {
    queuePush(temp[i]);
  }

  if (removed) {
    Serial.println("[큐] " + orderId + " stage_timeout으로 큐에서 즉시 제거함");
  }
}

// ---- orders 필드 처리: 우리 축 담당 주문을 큐에 적재 (로컬 + 서버) ----
void handleOrdersField(JsonObject orders) {
  // 스냅샷에 등장한 순서 그대로 큐잉 순서가 됨 (ArduinoJson은 JSON 원문 순서를 유지)
  for (JsonPair kv : orders) {
    JsonObject order = kv.value().as<JsonObject>();
    const char* status = order["status"] | "";
    if (strcmp(status, "assigned") != 0) continue;
    if (!order["fault"].isNull()) continue;

    String orderId = String(kv.key().c_str());
    const char* keycap = order["keycap"] | "";
    JsonArray colors = order["colors"].as<JsonArray>();

    handleAssignedOrder(orderId, keycap, colors);
  }
}

void handleAssignedOrder(const String& orderId, const char* keycap, JsonArray colors) {
  if ((uint8_t)strlen(keycap) <= AXIS_INDEX || colors.size() <= AXIS_INDEX) return;

  char letter = keycap[AXIS_INDEX];
  if (letterSlot(letter) < 0) return; // 방어코드: 내 축 글자가 아니면 무시

  const char* colorStr = colors[AXIS_INDEX];
  if (colorStr == nullptr || colorStr[0] == '\0') return;
  char color = colorStr[0];
  if (colorSlot(color) < 0) return;

  String jobKey = orderId + "_" + String(letter) + "_" + String(color);
  if (alreadyProcessed(jobKey)) return;

  // [11/04] 게이트가 잡고 있는 주문 = 이미 배출한 주문 (재부팅으로 RAM 기록이 지워져도
  // Redis에서 복원된 게이트로 알 수 있음). 다시 큐에 넣지 않고 기록만 남김.
  if (wasHandled(orderId, jobKey)) {
    markProcessed(jobKey);
    Serial.println("[큐] " + orderId + " 게이트가 잡고 있는 주문(이미 배출함) - 다시 적재하지 않음");
    return;
  }

  DispenseJob job{orderId, letter, color};
  if (!queuePush(job)) {
    // 중복방지 기록을 남기지 않았으므로 다음 스냅샷에서 다시 주워짐 (유실 아님)
    Serial.println("[큐] 가득 참 - " + orderId + " 이번엔 보류, 다음 스냅샷에서 재시도");
    return;
  }
  markProcessed(jobKey); // push 성공 후에만 기록 (실패 시 기록이 남으면 주문이 영구 유실됨)
  enqueueToServer(job); // 서버(Redis) 큐에도 동일하게 적재 - 재부팅 복원용
}

// ---- restock 필드 처리: 우리 축 담당 항목만 골라서 서버에 알림 ----
void handleRestockField(JsonArray restock) {
  // 이번 스냅샷에 등장한 우리 축 restock 항목 표시
  bool presentNow[2][4] = { {false,false,false,false}, {false,false,false,false} };

  for (JsonVariant item : restock) {
    String rc = item.as<String>(); // 예: "E_r"
    if (rc.length() < 3) continue;
    char letter = rc.charAt(0);
    char color  = rc.charAt(2);
    int l = letterSlot(letter);
    int c = colorSlot(color);
    if (l < 0 || c < 0) continue; // 우리 축 담당 아님

    presentNow[l][c] = true;

    if (!restockInProgress[l][c]) {
      // 새로 등장한 restock 이벤트 - 서버 호출
      restockInProgress[l][c] = true;
      callRestock(l, c);
    }
    // 이미 restockInProgress였으면 (이전 스냅샷에서 이미 호출함) 이번엔 스킵
  }

  // 이번 스냅샷에서 사라진 항목은 in-progress 해제 (다음에 다시 등장하면 새 이벤트로 처리)
  for (int l = 0; l < 2; l++) {
    for (int c = 0; c < 4; c++) {
      if (restockInProgress[l][c] && !presentNow[l][c]) {
        restockInProgress[l][c] = false;
      }
    }
  }
}

// ---- orders의 한 항목에서 이 보드(축) 담당 letter/color를 뽑아냄 ----
// status 무관하게(assigned가 아니어도) 동작함 - fault는 tray_reached 등
// 배출 이후 단계에서도 올라올 수 있기 때문. 담당 아니거나 파싱 불가면 false.
bool getOrderLetterColor(JsonObject order, char& letter, char& color) {
  const char* keycap = order["keycap"] | "";
  JsonArray colors = order["colors"].as<JsonArray>();
  if ((uint8_t)strlen(keycap) <= AXIS_INDEX || colors.size() <= AXIS_INDEX) return false;

  letter = keycap[AXIS_INDEX];
  if (letterSlot(letter) < 0) return false;

  const char* colorStr = colors[AXIS_INDEX];
  if (colorStr == nullptr || colorStr[0] == '\0') return false;
  color = colorStr[0];
  if (colorSlot(color) < 0) return false;

  return true;
}

// ---- [11/01] keycap_driver_failed fault가 내려갔을 때 호출 ----
// PCA 고장으로 같이 disable됐던 레일들을 원복하고 배출 금지를 해제함.
// keepL/keepC(fault가 걸려있던 주문의 레일)는 handleOrderFaults의 기존 흐름이
// 원복+재배출까지 처리하므로 여기선 건드리지 않음. 다른 fault가 따로 걸려있는
// 레일도 그 fault가 풀릴 때 처리돼야 하므로 건드리지 않음.
// 만약 PCA가 실제로는 안 고쳐졌으면, 다음 배출 직전 헬스체크가 바로 다시 disable 시킴.
void restoreRailsAfterDriverFault(int keepL, int keepC) {
  for (int l = 0; l < 2; l++) {
    for (int c = 0; c < 4; c++) {
      if (l == keepL && c == keepC) continue;
      if (faultActive[l][c]) continue;
      if (rail[l][c].status != RAIL_DISABLE) continue;
      rail[l][c].status = (rail[l][c].count == 0) ? RAIL_EMPTY : RAIL_IDLE;
      reportRailStatus(letterOf(l), colorOf(c), rail[l][c].status, "");
    }
  }
  pcaDriverFailed = false;
  pcaHasRelatedOrder = false;
  Serial.println("[fault] keycap_driver_failed 해제 - disable 레일 원복, 배출 금지 해제");
}

// ---- fault 필드 처리: 우리 축 담당 주문의 fault 등장/해제를 감지 ----
// stage_timeout: rail 상태와 무관하게 뜰 수 있어서 해제 시점 판단이 불가능함
// (사람이 수동 폐기하기로 결정됨) -> **등장하는 즉시** 큐에서 빼고 영구
// 재큐잉 차단. 해제 시점엔 할 일이 없음(이미 등장 시점에 다 처리했으므로).
// keycap_cartridge_failed/keycap_driver_failed: 등장 시엔 추적만 하고
// disable/보고는 하지 않음(우리가 이미 보고한 것의 메아리라서). 해제 시(같은
// order_id에서 fault가 다시 null이 되면) 그 시점 주문 status로 갈라서 처리 -
// "assigned"면 정상 경로(handleOrdersField)에 맡기고 아무것도 안 함, 아니면
// (이미 assigned를 지났으면) 정상 경로가 다시 안 주워가므로 우리가 직접
// 재배출 job을 큐에 넣음. fault가 없었던 레일은 안 건드림.
void handleOrderFaults(JsonObject orders) {
  for (JsonPair kv : orders) {
    JsonObject order = kv.value().as<JsonObject>();

    char letter, color;
    if (!getOrderLetterColor(order, letter, color)) continue; // 우리 축 담당 아님

    int l = letterSlot(letter);
    int c = colorSlot(color);
    String orderId = String(kv.key().c_str());

    const char* faultStr = order["fault"] | "";
    bool hasFault = !order["fault"].isNull() && isKnownFault(faultStr);

    if (hasFault && strcmp(faultStr, "stage_timeout") == 0) {
      // stage_timeout 등장 - 그 자리에서 바로 폐기 취급: 큐에 있으면 즉시
      // 빼고(있으면 completeOnServer까지 같이), jobKey를 markProcessed로
      // 등록해 앞으로 이 주문이 다시 큐에 안 들어가게 영구 차단함.
      // 지금 배출 진행 중(currentJob)인 경우는 물리적으로 멈출 수 없어서
      // 그대로 두고 건드리지 않음 - 끝나면 원래 흐름대로 알아서 정리됨.
      String jobKey = orderId + "_" + String(letter) + "_" + String(color);
      if (!alreadyProcessed(jobKey)) {
        markProcessed(jobKey);
      }
      removeOrderFromQueue(orderId);
      // 이 주문은 더 이상 fault 추적 대상이 아님(해제 시점에 할 일이 없으므로)
      if (faultActive[l][c] && faultOrderId[l][c] == orderId) {
        faultActive[l][c] = false;
        faultOrderId[l][c] = "";
        faultType[l][c] = "";
      }
      continue;
    }

    if (hasFault) {
      // 등장 시엔 추적만 하고 disable/보고는 하지 않음 (이유는 위 주석 참고)
      faultActive[l][c] = true;
      faultOrderId[l][c] = orderId;
      faultType[l][c] = faultStr;
      // [11/01] driver_failed가 걸려있는 동안은 배출 금지 유지 - 재부팅으로 RAM의
      // pcaDriverFailed가 지워졌어도 스냅샷의 fault를 보고 다시 잠금
      if (strcmp(faultStr, "keycap_driver_failed") == 0 && !pcaDriverFailed) {
        pcaDriverFailed = true;
        Serial.println("[fault] keycap_driver_failed 감지 - 사람이 fault를 내릴 때까지 배출 금지");
      }
    } else {
      if (faultActive[l][c] && faultOrderId[l][c] == orderId) {
        bool wasDriverFault = (faultType[l][c] == "keycap_driver_failed");
        faultActive[l][c] = false;
        faultOrderId[l][c] = "";
        faultType[l][c] = "";

        // [11/01] PCA 드라이버 고장 fault가 사람에 의해 내려감 -> 이 주문 레일을 뺀 나머지
        // disable 레일도 같이 원복하고 배출 금지 해제 (이 주문 레일은 아래 기존 흐름이 처리)
        if (wasDriverFault) {
          restoreRailsAfterDriverFault(l, c);
        }

        // "아직 한 번도 시도 안 된 job인지"는 status가 아니라 처리 기록으로
        // 판단해야 정확함 - status=="assigned"이어도 이미 배출을 한 번 시도했다가
        // (예: IR 실패로 disable됐다가) fault가 걸린 경우엔 jobKey가 이미
        // processedJobKeys에 있어서 handleOrdersField()가 정상적으로 다시
        // 안 주워감. 그러니 "이미 시도됐는가"만 보고 우리가 직접 넣을지 정함.
        // [11/04] alreadyProcessed -> wasHandled: 재부팅으로 RAM 기록이 지워졌어도
        // 게이트가 이 주문을 잡고 있으면 "이미 시도됨"으로 봄. (alreadyProcessed만 보면
        // 재부팅 후 "미시도 -> 정상 경로에 맡김"으로 빠지는데, 정상 경로는 게이트 때문에
        // 안 주워가서 재배출도 게이트 해제도 안 되는 데드락이 생김)
        String jobKey = orderId + "_" + String(letter) + "_" + String(color);

        if (!wasHandled(orderId, jobKey)) {
          // 한 번도 시도된 적 없는 job(진짜 stage_timeout류가 대기 중에 뜬 경우
          // 등) - handleOrdersField()가 이번 스냅샷에서 정상 경로로 알아서
          // 주워가므로 여기선 아무것도 안 함
          Serial.println("[fault] " + letterColorKey(l, c) + " " + orderId + " 해제 (미시도 상태, 정상 경로에 맡김)");
        } else if (rail[l][c].status != RAIL_DISABLE) {
          // 이미 시도는 됐지만 우리 레일 결과 자체는 disable이 아니었음(=우리
          // 쪽은 이미 성공/정상 완료된 상태) - 이 fault는 같은 order의 다른
          // 축(다른 보드)이 실패해서 order 전체에 걸린 것일 뿐, 우리 레일
          // 문제가 아니므로 재배출할 필요 없음. rail[l][c].status는 우리가
          // 직접 겪은 결과를 그대로 들고 있는 값이라 이게 진짜 기준.
          Serial.println("[fault] " + letterColorKey(l, c) + " " + orderId + " 해제 (우리 레일은 disable 아니었음 - 재배출 불필요)");
        } else {
          // 이미 시도됐고 그 결과가 disable이었음 - 진짜 우리 레일이 실패한
          // 경우이므로 재배출
          Serial.println("[fault] " + letterColorKey(l, c) + " " + orderId + " 해제 (우리 레일 disable이었음) -> 재배출 큐 적재");

          // 게이트가 바로 이 주문 자신을 기다리며 잠겨있으면 영원히 안 풀림
          // (이 주문이 pickup_reached 되기를 기다리는 중인데, 그 배출 자체를
          // 다시 하려는 거라 애초에 기다릴 이유가 없음) - 먼저 풀어줌.
          // 재고가 0이라 재배출을 못 하는 경우에도 풀어야 함(안 그러면 이 주문은
          // 우리 키캡 없이 pickup_reached까지 못 가서 보드 전체 배출이 멈춤).
          if (!gateOpen && gateOrderId == orderId) {
            gateOpen = true;
            gateOrderId = "";
            clearGateOnServer();
            Serial.println("[fault] " + orderId + " 이 게이트를 걸고 있었음 -> 게이트 해제");
          }

          // disable -> 사용 가능한 상태로 되돌림 (재고 0이면 empty). fault가
          // 풀렸다는 것 자체가 "사람이 고쳤다"는 신호이므로 REPAIR 명령과
          // 동일하게 신뢰하고 엶 (PCA 드라이버 고장이었을 경우 다음 배출
          // 시도 직전 PCA 헬스체크가 다시 확인하므로, 진짜 안 고쳐졌으면
          // 즉시 재차 disable되는 안전망이 있음). 서버(Mobius)에도 보고해서
          // 플랫폼 쪽에 disable로 남아있지 않게 함.
          rail[l][c].status = (rail[l][c].count == 0) ? RAIL_EMPTY : RAIL_IDLE;
          reportRailStatus(letter, color, rail[l][c].status, orderId);

          if (rail[l][c].count > 0) {
            DispenseJob job{orderId, letter, color};
            if (queueContains(orderId, letter, color)) {
              // driver_failed 동안엔 배출이 멈춰서 원래 job이 아직 큐에 남아있음 - 또 넣으면 이중 배출
              Serial.println("[fault] " + orderId + " 이미 큐에 대기 중 - 재적재 생략");
            } else if (queuePush(job)) {
              enqueueToServer(job);
            } else {
              Serial.println("[fault] 재배출 큐 적재 실패 - 큐 가득 참");
            }
          } else {
            Serial.println("[fault] " + letterColorKey(l, c) + " 재고 0 - 재배출 없이 empty로 원복만 함");
          }
        }
      }
    }
  }
}

// ============================== 배출 상태 머신 (대기->푸시 방향 non-blocking 스윕 + 시작 스티션 킥 + 게이트) ==============================
void runDispenseStateMachine() {
  // 지금 배출 진행 중(DS_IDLE이 아님)이면, 상태 단계와 무관하게 매 loop()마다
  // IR 핀을 계속 폴링함 - 키캡이 스윕 도중/홀드 중/복귀 중 언제 떨어질지
  // 알 수 없으므로, "복귀가 끝난 뒤에만 잠깐 감시"하지 않고 모터가 움직이기
  // 시작한 순간부터 사이클 내내 감시를 이어감. 판정 자체(성공/실패 확정)는
  // 여전히 DS_WAIT_IR에서 감시 윈도우가 다 끝난 시점에만 함.
  if (dsState != DS_IDLE) {
    pollIrForCurrentJob();
  }

  if (dsState == DS_IDLE) {
    // [11/04] 재고/큐/게이트를 서버에서 다 받아오기 전엔 배출 시작 안 함
    // (큐만 복원되고 게이트는 아직이면 기본값 open으로 배출해버릴 수 있음)
    if (!serverRestored()) return;

    // PCA9685 드라이버 헬스체크 - 큐에 job이 있을 때만(=배출을 시도할 상황일 때만)
    // 확인함. job이 없으면 아무도 이 값에 의존하지 않으니 체크 자체를 스킵.
    // job이 있는 동안엔 dsState가 계속 DS_IDLE에 머물면서 loop()가 초당 수백~
    // 수천 번 도는데, 매번 체크하면 disable 리포트 HTTP 요청까지 그만큼
    // 나가버려서(최대 8초 블로킹) 오히려 시스템이 멈출 수 있음 - 그래서
    // "job이 있을 때만" + "1초 간격"을 같이 적용함.
    bool hasJob = (jobHead != jobTail);
    unsigned long nowMs = millis();
    if (hasJob && (nowMs - lastPcaCheckMs >= PCA_CHECK_INTERVAL_MS)) {
      lastPcaCheckMs = nowMs;
      bool pcaOk = checkPcaHealthy();

      if (!pcaOk && !pcaDriverFailed) {
        pcaDriverFailed = true;
        // 대기열 맨 앞 job이 있으면 그 주문과 관련된 레일에만 order_id, 나머지 7개는 null.
        // 이후 PCA가 다시 응답해도 자동으로 풀지 않음 - 사람이 fault를 내릴 때까지
        // 배출 금지 유지 (큐의 job은 꺼내지 않고 그대로 둠).
        pcaHasRelatedOrder = (jobHead != jobTail);
        if (pcaHasRelatedOrder) pcaRelatedJob = jobQueue[jobHead];

        for (int l = 0; l < 2; l++) {
          for (int c = 0; c < 4; c++) {
            rail[l][c].status = RAIL_DISABLE;
            bool isRelated = pcaHasRelatedOrder && letterSlot(pcaRelatedJob.letter) == l && colorSlot(pcaRelatedJob.color) == c;
            reportRailStatus(letterOf(l), colorOf(c), RAIL_DISABLE,
                              isRelated ? pcaRelatedJob.orderId : "", REASON_DRIVER_FAILED);
          }
        }
        Serial.println("[PCA] 드라이버 응답 없음 - 8개 레일 전부 disable 보고 (fault 해제 전까지 배출 금지)");
      }
      // [11/01] PCA가 다시 응답해도 여기서는 아무것도 풀지 않음 (사람이 fault를 내릴 때만 복구)
    }
    if (pcaDriverFailed) return; // 드라이버 고장 중엔 새 배출 시작 안 함 (큐의 job은 그대로 대기)

    if (calibActive) return; // [11/11] 영점 맞추는 중 - 손 다치지 않게 배출 멈춤 (큐의 job은 그대로 대기)

    if (!gateOpen) return; // 직전 주문이 pickup_reached(이후) 되기 전까지 다음 배출 금지

    DispenseJob job;
    if (!queuePop(job)) return;

    int l = letterSlot(job.letter);
    int c = colorSlot(job.color);
    if (l < 0 || c < 0) return;

    if (rail[l][c].status == RAIL_DISABLE || rail[l][c].status == RAIL_EMPTY) {
      Serial.println("[배출] 해당 레일 사용 불가(disable/empty) - 건너뜀");
      // 이 job은 버려지는 것이므로 서버 큐에서도 제거해줘야 함 (안 그러면 다음 부팅 때 또 시도됨)
      completeOnServer(job);
      return;
    }

    currentJob = job;
    rail[l][c].status = RAIL_BUSY;
    rail[l][c].lastOrderId = job.orderId;
    reportRailStatus(job.letter, job.color, RAIL_BUSY, job.orderId);

    // IR 감시 윈도우 시작 - 지금 이 순간(모터가 움직이기 시작하는 시점)부터
    // IR_POLL_WINDOW_MS 동안 계속 폴링함 (위 pollIrForCurrentJob() 호출로 진행됨)
    curIrActive = IR_ACTIVE(l, c);   // [11/10] 이번 배출은 시작 시점 설정으로 끝까지
    irWindowStart = millis();
    irBreakStart = 0;
    irLatched = false;
    irMaxBreakMs = 0;
    irMaxGapMs = 0;
    irLastPollMs = millis();
    irPollCount = 0;
    irBrokenSamples = 0;
    irPushReachedMs = 0; irReturnCmdMs = 0;
    irPollFirstSet = false; irPollFirstMs = 0; irPollLatchMs = 0;
#if IR_SENSOR_INSTALLED
    noInterrupts();
    irIsrBreakStartUs = 0;
    irIsrMaxUs = 0;
    irIsrLatched = false;
    irEvCount = 0; irIsrBreakStartMs = 0; irIsrLatchSet = false; irIsrLatchMs = 0;
    irIsrPin = curIrActive ? (int8_t)IR_PIN[l][c] : (int8_t)-1;
    interrupts();
#endif

    Serial.println("[배출] 시작 " + letterColorKey(l, c) + " order=" + job.orderId);
    Serial.print("[IR] 감시 시작: 부팅 후 "); Serial.print(irWindowStart);
    Serial.print("ms (이후 시각은 모터 시작 기준), 감시창 "); Serial.print(IR_POLL_WINDOW_MS);
    Serial.print("ms, IR "); Serial.println(curIrActive ? "사용" : "꺼짐/미배선");
    moveServoAngle(SERVO_CHANNEL[l][c], SERVO_HOME_ANGLE);
    dsTimer = millis();
    dsState = DS_HOMING;
    return;
  }

  if (dsState == DS_HOMING) {
    if (millis() - dsTimer < RETURN_DELAY_MS) return;

    int l = letterSlot(currentJob.letter);
    int c = colorSlot(currentJob.color);

    sweepAngle = SERVO_HOME_ANGLE - BREAKAWAY_KICK_DEG; // 방향 반전: 대기(140)->푸시(40)는 감소 방향이라 킥도 뺌
    moveServoAngle(SERVO_CHANNEL[l][c], sweepAngle);
    dsTimer = millis();
    dsState = DS_BREAKAWAY_WAIT;
    return;
  }

  if (dsState == DS_BREAKAWAY_WAIT) {
    if (millis() - dsTimer < BREAKAWAY_SETTLE_MS) return;
    dsTimer = millis();
    dsState = DS_SWEEPING;
    return;
  }

  if (dsState == DS_SWEEPING) {
    if (millis() - dsTimer < SWEEP_STEP_DELAY_MS) return;
    dsTimer = millis();

    int l = letterSlot(currentJob.letter);
    int c = colorSlot(currentJob.color);

    sweepAngle--; // 방향 반전: 감소시키며 PUSH_ANGLE까지 이동
    moveServoAngle(SERVO_CHANNEL[l][c], sweepAngle);

    if (sweepAngle <= PUSH_ANGLE) {
      irPushReachedMs = millis() - irWindowStart;
      dsTimer = millis();
      dsState = DS_HOLDING;
    }
    return;
  }

  if (dsState == DS_HOLDING) {
    if (millis() - dsTimer < DISPENSE_HOLD_MS) return;

    int l = letterSlot(currentJob.letter);
    int c = colorSlot(currentJob.color);
    moveServoAngle(SERVO_CHANNEL[l][c], SERVO_HOME_ANGLE);
    irReturnCmdMs = millis() - irWindowStart;
    dsTimer = millis();
    dsState = DS_RETURNING;
    return;
  }

  if (dsState == DS_RETURNING) {
    // 여기선 순수하게 서보가 물리적으로 홈까지 복귀하는 걸 기다리기만 함.
    // IR 감시 자체는 위쪽 pollIrForCurrentJob()이 모터 시작 시점부터 이미
    // 계속 돌고 있으므로 여기서 윈도우를 새로 시작하지 않음.
    if (millis() - dsTimer < IR_CHECK_DELAY_MS) return;
    dsState = DS_WAIT_IR;
    return;
  }

  if (dsState == DS_WAIT_IR) {
    int l = letterSlot(currentJob.letter);
    int c = colorSlot(currentJob.color);
    bool success = true; // 기본값: 센서 미배선 레일은 판정 없이 즉시 성공 처리

    // [11/08] 홈 복귀 명령(DS_HOLDING에서 dsTimer 기록) 후 RETURN_DELAY_MS가 지나
    // 서보가 홈에 도착한 뒤에만 판정/힘 빼기로 넘어감
    if (millis() - dsTimer < RETURN_DELAY_MS) return;

#if IR_SENSOR_INSTALLED
    if (curIrActive) {   // [11/10] IR_INSTALLED -> 시작 시점 설정(배선 && 켜짐)
      // 폴링 자체는 위쪽 pollIrForCurrentJob()이 모터 시작 시점(irWindowStart)
      // 부터 매 loop()마다 계속 해왔음. 여기서는 그 전체 감시 윈도우
      // (IR_POLL_WINDOW_MS)가 다 지났는지만 확인하고, 다 지났으면 그때까지
      // 쌓인 irLatched 값으로 최종 판정함.
      if (millis() - irWindowStart < IR_POLL_WINDOW_MS) return;  // 윈도우 안 끝났으면 다음 loop()에서 계속 대기

      uint8_t evN; unsigned long evS[IR_EV_MAX], evL[IR_EV_MAX];
      bool stillBroken, isrLatchSetCopy; unsigned long stillStartMs, stillLenUs, isrLatchMsCopy;
      noInterrupts();
      bool isrLatchedCopy = irIsrLatched;
      unsigned long isrMaxUsCopy = irIsrMaxUs;
      evN = irEvCount;
      for (uint8_t i = 0; i < evN; i++) { evS[i] = irEvStartMs[i]; evL[i] = irEvLenUs[i]; }
      stillBroken = (irIsrBreakStartUs != 0);
      stillStartMs = irIsrBreakStartMs;
      stillLenUs = stillBroken ? (micros() - irIsrBreakStartUs) : 0;
      isrLatchSetCopy = irIsrLatchSet;
      isrLatchMsCopy = irIsrLatchMs;
      irIsrPin = -1;  // 이 배출의 감시 종료
      interrupts();

      success = irLatched || isrLatchedCopy;  // 폴링 또는 인터럽트 중 하나라도 유효 통과를 잡았으면 성공
      // [진단] 판정 직후 실측값 출력
      //  - 인터럽트 최대 끊김이 수천 us(=수 ms)인데 폴링 최대 끊김은 0 -> 폴링이 느려서 놓친 것
      //  - 인터럽트 최대 끊김도 0 -> 핀 자체가 안 변함(배선/레벨/핀 번호 재확인)
      //    (단, 끊김샘플>0인데 인터럽트만 계속 0이면 그 핀은 인터럽트 미지원)
      Serial.print("[IR] "); Serial.print(letterColorKey(l, c));
      Serial.print(" 폴링: 횟수="); Serial.print(irPollCount);
      Serial.print(" 끊김샘플="); Serial.print(irBrokenSamples);
      Serial.print(" 최대끊김(ms)="); Serial.print(irMaxBreakMs);
      Serial.print(" 최대간격(ms)="); Serial.print(irMaxGapMs);
      Serial.print(" | 인터럽트: 최대끊김(us)="); Serial.print(isrMaxUsCopy);
      Serial.print(" | 기준(ms)="); Serial.print(IR_MIN_BREAK_MS);
      Serial.print(" | 판정="); Serial.println(success ? "통과" : "실패");

      // [11/11] 시간 진단 (전부 모터 시작 기준 ms)
      Serial.print("[IR] 타임라인: 푸시도달 "); Serial.print(irPushReachedMs);
      Serial.print("ms / 홈복귀명령 "); Serial.print(irReturnCmdMs);
      Serial.print("ms / 판정 "); Serial.print(millis() - irWindowStart); Serial.println("ms");
      if (evN == 0 && !stillBroken) {
        Serial.println("[IR] 끊김 없음 (인터럽트 기준) - 핀 변화 자체가 없음: 배선/레벨/핀 번호 확인");
      }
      for (uint8_t i = 0; i < evN; i++) {
        Serial.print("[IR] 끊김 #"); Serial.print(i + 1);
        Serial.print(": 시작 "); Serial.print(evS[i]);
        Serial.print("ms, 길이 "); Serial.print(evL[i] / 1000.0, 1);
        Serial.print("ms -> "); Serial.println(evL[i] >= (unsigned long)IR_MIN_BREAK_MS * 1000UL ? "유효" : "짧아서 무시");
      }
      if (stillBroken) {
        Serial.print("[IR] 감시 끝까지 빔이 끊긴 상태: 시작 "); Serial.print(stillStartMs);
        Serial.print("ms, 지금까지 "); Serial.print(stillLenUs / 1000.0, 1); Serial.println("ms");
      }
      if (irPollFirstSet) {
        Serial.print("[IR] 폴링이 처음 끊김을 본 시각 "); Serial.print(irPollFirstMs); Serial.println("ms");
      }
      {
        bool haveL = false; unsigned long lm = 0;
        if (irLatched) { lm = irPollLatchMs; haveL = true; }
        if (isrLatchSetCopy && (!haveL || isrLatchMsCopy < lm)) { lm = isrLatchMsCopy; haveL = true; }
        if (haveL) { Serial.print("[IR] 통과 확정 시각 "); Serial.print(lm); Serial.println("ms (모터 시작 후)"); }
        else Serial.println("[IR] 통과 확정 없음");
      }
    }
    // 센서 미배선/꺼진 레일: 폴링 없이 위의 기본값(success=true) 그대로 즉시 판정
#endif

    // [11/08] 홈 복귀 끝 -> 평상시 off (서버 보고가 최대 8초씩 걸리므로 그 전에 먼저 뺌)
    releaseServo(SERVO_CHANNEL[l][c]);

    if (success) {
      if (rail[l][c].count > 0) rail[l][c].count--;
      rail[l][c].status = (rail[l][c].count == 0) ? RAIL_EMPTY : RAIL_IDLE;
      reportDispenseResult(currentJob.orderId, currentJob.letter, currentJob.color);
    } else {
      rail[l][c].status = RAIL_DISABLE;
    }

    reportRailStatus(currentJob.letter, currentJob.color, rail[l][c].status, currentJob.orderId,
                      success ? "" : REASON_CARTRIDGE_FAILED);
    completeOnServer(currentJob); // 성공/실패 관계없이 서버 큐에서는 제거 (더 이상 대기열에 있을 이유 없음)

    // 게이트 잠금: 이 주문이 pickup_reached(이후) 될 때까지 다음 배출 시작 안 함
    // 로컬 뿐 아니라 서버(Redis)에도 즉시 기록해서 재부팅 시에도 이 잠금이 복원되게 함.
    gateOrderId = currentJob.orderId;
    gateOpen = false;
    lockGateOnServer(gateOrderId);

    Serial.println("[배출] 종료 " + letterColorKey(l, c) + " -> " + (success ? "성공" : "실패(disable)") + ", 남은 count=" + String(rail[l][c].count));
    dsState = DS_IDLE;
    return;
  }
}

// ---- 지금 진행 중인 job(currentJob)의 IR 핀을 폴링해서 irBreakStart/irLatched
// 갱신 - dsState가 DS_IDLE이 아닌 동안 runDispenseStateMachine() 맨 위에서
// 매 loop()마다 호출됨. 즉 모터가 움직이기 시작한 순간부터 이 job이 끝날
// 때까지(성공/실패 확정 시점까지) 상태 단계와 무관하게 계속 감시가 이어짐.
void pollIrForCurrentJob() {
#if IR_SENSOR_INSTALLED
  int l = letterSlot(currentJob.letter);
  int c = colorSlot(currentJob.color);
  if (l < 0 || c < 0) return;
  if (!curIrActive) return; // 이 레일은 센서 미배선이거나 꺼짐 - 폴링할 것 없음

  unsigned long nowMs = millis();
  if (nowMs - irLastPollMs > irMaxGapMs) irMaxGapMs = nowMs - irLastPollMs;
  irLastPollMs = nowMs;

  bool broken = (digitalRead(IR_PIN[l][c]) == IR_DETECT_LEVEL);
  irPollCount++;
  if (broken) irBrokenSamples++;
  if (broken) {
    if (irBreakStart == 0) {
      irBreakStart = nowMs;
      if (!irPollFirstSet) { irPollFirstSet = true; irPollFirstMs = nowMs - irWindowStart; }
    }
    //               // 끊김 시작
    unsigned long len = nowMs - irBreakStart;
    if (len > irMaxBreakMs) irMaxBreakMs = len;                 // [진단] 최대 끊김 기록
    if (!irLatched && len >= IR_MIN_BREAK_MS) {
      irLatched = true;
      irPollLatchMs = nowMs - irWindowStart;                                         // 디바운스 통과 - 유효 통과 확정
    }
  } else {
    irBreakStart = 0;  // 끊김 해제 - 다음 끊김 구간을 새로 잴 준비 (이미 래치됐으면 irLatched는 안 건드림)
  }
#endif
}

// ============================== 서보 제어 ==============================
void moveServoAngle(uint8_t channel, uint8_t angle) {
  uint16_t pulse = map(angle, 0, 180, SERVO_MIN_PULSE, SERVO_MAX_PULSE);
  pwm.setPWM(channel, 0, pulse);
}

// [11/08] 서보 힘 빼기 - 평상시엔 off, 배출할 때만 신호를 줌 (홈에서 버티며 발열하던 문제 대응)
// 다음 배출 시 DS_IDLE에서 moveServoAngle(홈)을 다시 보내므로 자동으로 다시 잡힘.
void releaseServo(uint8_t channel) {
  pwm.setPWM(channel, 0, 4096);  // full OFF: 펄스 끊김 -> 서보 토크 해제
}

// ---- PCA9685가 I2C로 정상 응답하는지 확인 (헬스체크) ----
// pwm 객체(Adafruit_PWMServoDriver(0x40))와 동일한 I2C 주소로 확인함.
// 주소를 바꾸면(하드웨어 주의사항 참고) 이 함수의 0x40도 같이 바꿔야 함.
bool checkPcaHealthy() {
  Wire.beginTransmission(0x40);
  return Wire.endTransmission() == 0;
}

// ============================== Flask HTTP 공통 통신 (실패 시 1회 재시도) ==============================
// [11/01] 로컬 평문 HTTP - httpClient(WiFiClient) 사용. 로그 접두어도 [HTTP]로 변경.
// timeoutMs: 응답 대기 타임아웃. 기본 5000ms, Mobius를 거치는 report 계열
// 호출(reportRailStatus/reportDispenseResult)에서만 FLASK_MOBIUS_TIMEOUT_MS로
// 늘려서 호출함 (기본값은 함수 선언부에 지정, 정의부에는 반복 지정하지 않음).
bool postJsonToFlask(const char* path, const char* payload, String& responseOut, unsigned long timeoutMs) {
  for (uint8_t attempt = 0; attempt < 2; attempt++) {
    HttpClient http(httpClient, FLASK_HOST, FLASK_PORT);
    http.setTimeout(timeoutMs);

    int err = http.post(path, "application/json", payload);
    if (err != 0) {
      Serial.print("[HTTP] POST 연결/전송 실패, err=");
      Serial.print(err);
      Serial.println(attempt == 0 ? " (재시도)" : "");
      http.stop();
      delay(300);
      continue;
    }

    int statusCode = http.responseStatusCode();
    responseOut = http.responseBody();
    http.stop();

    Serial.print("[HTTP] POST ");
    Serial.print(path);
    Serial.print(" -> ");
    Serial.print(payload);
    Serial.print("  (status=");
    Serial.print(statusCode);
    Serial.println(")");

    if (statusCode < 200 || statusCode >= 300) {
      Serial.print("[HTTP] 응답 실패: ");
      Serial.println(responseOut);
      return false;
    }
    return true;
  }
  return false;
}

bool getJsonFromFlask(const char* path, String& responseOut) {
  for (uint8_t attempt = 0; attempt < 2; attempt++) {
    HttpClient http(httpClient, FLASK_HOST, FLASK_PORT);
    http.setTimeout(5000);

    int err = http.get(path);
    if (err != 0) {
      Serial.print("[HTTP] GET 연결/전송 실패, err=");
      Serial.print(err);
      Serial.println(attempt == 0 ? " (재시도)" : "");
      http.stop();
      delay(300);
      continue;
    }

    int statusCode = http.responseStatusCode();
    responseOut = http.responseBody();
    http.stop();

    Serial.print("[HTTP] GET ");
    Serial.print(path);
    Serial.print("  (status=");
    Serial.print(statusCode);
    Serial.println(")");

    if (statusCode < 200 || statusCode >= 300) {
      Serial.print("[HTTP] 응답 실패: ");
      Serial.println(responseOut);
      return false;
    }
    return true;
  }
  return false;
}

// ============================== [11/04] 서버 복원 (재시도 포함) ==============================
// setup()에서 1차 시도, 이후 loop()에서 실패한 항목만 RESTORE_RETRY_MS(5초)마다 재시도.
// 셋 다 성공해야 ensureMQTT()가 접속을 시작하고, 배출 상태머신도 job을 꺼내기 시작함.
// 복원 중엔 MQTT도 배출도 없어서 HTTP 호출이 길게 블로킹돼도 문제없음.
void tryRestoreFromServer() {
  if (serverRestored()) return;
  if (WiFi.status() != WL_CONNECTED) return;

  unsigned long now = millis();
  if (restoreAttempted && now - lastRestoreAttemptMs < RESTORE_RETRY_MS) return;
  restoreAttempted = true;
  lastRestoreAttemptMs = now;

  // 순서: 재고 -> 큐 -> 게이트 (각각 독립적으로 재시도, 이미 성공한 건 다시 안 함)
  if (!stockRestored) {
    stockRestored = fetchInitialStock();
    if (stockRestored) checkAllDisabledAfterStockRestore();
  }
  if (!queueRestored) queueRestored = restoreQueueFromServer();
  if (!gateRestored)  gateRestored  = restoreGateFromServer();

  if (serverRestored()) {
    Serial.println("[복원] 재고/큐/게이트 복원 완료 - 이제 MQTT 접속(주문 수신) 시작");
    lastMqttAttempt = 0;  // 바로 접속 시도하도록
  } else {
    Serial.print("[복원] 일부 실패 (재고=");
    Serial.print(stockRestored ? "OK" : "실패");
    Serial.print(", 큐=");
    Serial.print(queueRestored ? "OK" : "실패");
    Serial.print(", 게이트=");
    Serial.print(gateRestored ? "OK" : "실패");
    Serial.println(") - 5초 뒤 재시도, 완료 전까지 주문 수신/배출 안 함");
  }
}

// ---- [11/01] 8개 레일이 전부 disable로 복원됐으면 driver 고장 중에 재부팅된 것으로 보고
// 배출 금지 상태로 시작 (사람이 keycap_driver_failed fault를 내리거나 REPAIR ALL 할 때까지)
// [11/04] setup()에 있던 것을 재고 복원 성공 시점에 실행하도록 옮김
void checkAllDisabledAfterStockRestore() {
  bool allDisabled = true;
  for (int l = 0; l < 2; l++) {
    for (int c = 0; c < 4; c++) {
      if (rail[l][c].status != RAIL_DISABLE) allDisabled = false;
    }
  }
  if (allDisabled) {
    pcaDriverFailed = true;
    Serial.println("[PCA] 8개 레일 전부 disable로 복원됨 - fault 해제(또는 REPAIR ALL) 전까지 배출 금지");
  }
}

// ---- 부팅 시 재고 초기 조회 ----
// 가정하는 응답 형식: {"N_r":{"count":8,"status":"idle"}, "N_y":{...}, ...}
// count는 항상 로컬 count에 반영. status가 "disable"이면 재부팅 전 마지막
// 상태를 그대로 이어받아 rail을 disable로 시작함(고장/fault 미해결 상태에서
// 재부팅됐다가 모르고 새 배출을 시도하는 걸 막기 위함) - 그 외 status는
// count 기준(0이면 empty, 아니면 idle)으로 로컬에서 새로 판단함.
// (형식 다르면 Flask 쪽과 맞춰서 이 파싱부만 수정할 것)
// [11/04] 성공 여부를 반환 - 실패하면 tryRestoreFromServer()가 다시 시도함
// (실패한 동안엔 MQTT/배출이 막혀 있어서 임시값 count 0/empty로 주문을 버리는 일 없음)
bool fetchInitialStock() {
  String path = String("/stock/keycap?board=") + BOARD_NAME;
  String resp;
  if (!getJsonFromFlask(path.c_str(), resp)) {
    Serial.println("[재고] 초기 조회 실패 - 재시도 예정 (그동안 임시로 count 0/empty)");
    for (int l = 0; l < 2; l++) {
      for (int c = 0; c < 4; c++) {
        rail[l][c].count = 0;
        rail[l][c].status = RAIL_EMPTY;
      }
    }
    return false;
  }

  StaticJsonDocument<768> doc;
  DeserializationError err = deserializeJson(doc, resp);
  if (err) {
    Serial.print("[재고] 응답 파싱 실패: ");
    Serial.println(err.c_str());
    return false;
  }

  Serial.println("[재고] 서버에서 복원된 값:");
  for (int l = 0; l < 2; l++) {
    for (int c = 0; c < 4; c++) {
      String key = letterColorKey(l, c);
      JsonObject entry = doc[key].as<JsonObject>();
      uint8_t count = entry["count"] | 0;
      const char* serverStatus = entry["status"] | "idle";

      rail[l][c].count = count;
      if (strcmp(serverStatus, "disable") == 0) {
        // 재부팅 전 disable 상태였음 - 원인(고장/fault)이 해결됐는지 여기선 알 수
        // 없으므로 일단 disable로 시작. fault는 다음 스냅샷에서 재평가되고,
        // 카트리지/서보 고장은 fault 해제나 REPAIR 명령 전까지 그대로 disable
        // 유지됨(의도된 동작).
        rail[l][c].status = RAIL_DISABLE;
      } else {
        rail[l][c].status = (count == 0) ? RAIL_EMPTY : RAIL_IDLE;
      }
      Serial.println("  " + key + " = count:" + String(count) + " status:" + String(serverStatus));
    }
  }
  return true;
}

// ---- 부팅 시 미완료 큐 복원 ----
// 가정하는 응답 형식: [{"order_id":"o1","letter":"E","color":"r"}, ...]
// [11/04] 성공 여부를 반환. 조회/파싱이 실패하면 큐에 아무것도 안 넣은 상태라
// 재시도해도 중복으로 들어가지 않음.
bool restoreQueueFromServer() {
  String path = String("/queue/") + BOARD_NAME + "/next";
  String resp;
  if (!getJsonFromFlask(path.c_str(), resp)) {
    Serial.println("[큐] 서버 큐 조회 실패 - 재시도 예정");
    return false;
  }

  StaticJsonDocument<1024> doc;
  DeserializationError err = deserializeJson(doc, resp);
  if (err) {
    Serial.print("[큐] 응답 파싱 실패: ");
    Serial.println(err.c_str());
    return false;
  }

  JsonArray arr = doc.as<JsonArray>();
  int restored = 0;
  for (JsonObject item : arr) {
    String orderId = item["order_id"].as<String>();
    const char* letterStr = item["letter"] | "";
    const char* colorStr  = item["color"] | "";
    if (strlen(letterStr) == 0 || strlen(colorStr) == 0) continue;

    DispenseJob job{orderId, letterStr[0], colorStr[0]};
    if (queuePush(job)) {
      // 이미 서버 큐에 있던 것이므로 다시 enqueue 호출은 안 함, 로컬 중복방지 기록만
      String jobKey = orderId + "_" + String(job.letter) + "_" + String(job.color);
      markProcessed(jobKey);
      restored++;
    }
  }
  Serial.print("[큐] 서버에서 복원된 대기 주문: ");
  Serial.println(restored);
  return true;
}

void enqueueToServer(const DispenseJob& job) {
  String path = String("/queue/") + BOARD_NAME + "/enqueue";
  StaticJsonDocument<192> doc;
  doc["board"] = BOARD_NAME;
  doc["order_id"] = job.orderId;
  doc["letter"] = String(job.letter);
  doc["color"] = String(job.color);
  char payload[192];
  serializeJson(doc, payload, sizeof(payload));

  String resp;
  postJsonToFlask(path.c_str(), payload, resp);
}

void completeOnServer(const DispenseJob& job) {
  String path = String("/queue/") + BOARD_NAME + "/complete";
  StaticJsonDocument<192> doc;
  doc["board"] = BOARD_NAME;
  doc["order_id"] = job.orderId;
  doc["letter"] = String(job.letter);
  doc["color"] = String(job.color);
  char payload[192];
  serializeJson(doc, payload, sizeof(payload));

  String resp;
  postJsonToFlask(path.c_str(), payload, resp);
}

// ---- restock 호출: 서버가 계산한 "채워진 개수"를 응답받아 로컬 count 동기화 ----
// 가정하는 응답 형식: {"filled": 25}
void callRestock(int l, int c) {
  char letter = letterOf(l);
  char color = colorOf(c);
  String path = String("/restock/") + letter + "/" + color;

  String resp;
  if (!postJsonToFlask(path.c_str(), "{}", resp)) {
    Serial.println("[restock] 호출 실패 - 로컬 count 변경 안 함, 다음 스냅샷에서 재시도됨");
    restockInProgress[l][c] = false; // 실패했으니 다음에 다시 시도할 수 있게 플래그 해제
    return;
  }

  StaticJsonDocument<128> doc;
  DeserializationError err = deserializeJson(doc, resp);
  if (err) {
    Serial.print("[restock] 응답 파싱 실패: ");
    Serial.println(err.c_str());
    return;
  }

  uint8_t filled = doc["filled"] | 0;
  rail[l][c].count = filled;
  if (rail[l][c].status == RAIL_DISABLE) {
    // [11/01] disable 레일은 restock으로 풀지 않음 - count만 갱신하고 disable 유지.
    // 사람이 fault를 내리거나 REPAIR 해야만 풀림 (그때 이 count 기준으로 idle/empty 결정)
    Serial.println("[restock] " + letterColorKey(l, c) + " disable 상태라 count만 갱신, disable 유지 (fault 해제/REPAIR 필요)");
  } else {
    rail[l][c].status = (filled == 0) ? RAIL_EMPTY : RAIL_IDLE;
  }
  // Flask의 /restock/<letter>/<color> 엔드포인트가 이 호출 안에서 이미
  // 모비우스에 CIN을 올려줌(order_id: null) - 여기서 reportRailStatus를
  // 또 부르면 같은 restock 이벤트가 모비우스에 두 번 올라가는 중복 버그가
  // 생겨서 이 호출은 제거함. 로컬 rail 상태(count/status)만 갱신하면 됨.
  Serial.println("[restock] " + letterColorKey(l, c) + " -> " + String(filled) + "개로 채워짐");
}

// ============================== 게이트 서버 동기화 ==============================
// ---- 부팅 시: 서버(Redis)에 저장된 게이트 상태 복원 ----
// 가정하는 응답 형식: {"locked": true, "order_id": "order_test_8017"} 또는 {"locked": false}
// [11/04] 성공 여부를 반환. 조회/파싱 실패 시 예전처럼 "열림"으로 넘어가지 않고
// tryRestoreFromServer()가 다시 시도함 (열림으로 시작하면 직전 배출이 아직 픽업 안 됐는데
// 다음 주문을 배출하거나, 이미 배출한 주문을 다시 큐에 넣을 수 있음).
bool restoreGateFromServer() {
  String path = String(GATE_PATH) + "/" + BOARD_NAME;
  String resp;
  if (!getJsonFromFlask(path.c_str(), resp)) {
    Serial.println("[게이트] 서버 상태 조회 실패 - 재시도 예정");
    return false;
  }

  StaticJsonDocument<192> doc;
  DeserializationError err = deserializeJson(doc, resp);
  if (err) {
    Serial.print("[게이트] 응답 파싱 실패: ");
    Serial.println(err.c_str());
    return false;
  }

  bool locked = doc["locked"] | false;
  if (locked) {
    gateOrderId = doc["order_id"] | "";
    if (gateOrderId.length() == 0) {
      // 서버가 locked=true인데 order_id를 안 줬으면 판단 불가 - 안전하게 열림 처리
      gateOpen = true;
      Serial.println("[게이트] 복원: locked=true인데 order_id 없음 - 방어적으로 열림 처리");
      return true;
    }
    gateOpen = false;
    Serial.println("[게이트] 복원: " + gateOrderId + " pickup 대기 중 (잠금 상태로 시작, 이 주문은 다시 적재하지 않음)");
  } else {
    gateOpen = true;
    gateOrderId = "";
    Serial.println("[게이트] 복원: 잠긴 것 없음 (열림 상태로 시작)");
  }
  return true;
}

// ---- 게이트 잠글 때(배출 직후) 서버에도 기록 - 재부팅 대비 ----
void lockGateOnServer(const String& orderId) {
  String path = String(GATE_PATH) + "/" + BOARD_NAME;
  StaticJsonDocument<128> doc;
  doc["order_id"] = orderId;
  char payload[128];
  serializeJson(doc, payload, sizeof(payload));
  String resp;
  postJsonToFlask(path.c_str(), payload, resp);
}

// ---- 게이트 열릴 때 서버 기록도 해제 ----
void clearGateOnServer() {
  String path = String(GATE_PATH) + "/" + BOARD_NAME + "/clear";
  String resp;
  postJsonToFlask(path.c_str(), "{}", resp);
}

// ============================== 상태 보고 ==============================
void reportRailStatus(char letter, char color, RailStatus status, const String& orderId, const char* reason) {
  int l = letterSlot(letter);
  int c = colorSlot(color);
  if (l < 0 || c < 0) return;

  StaticJsonDocument<256> doc;
  if (orderId.length() == 0) {
    doc["order_id"] = nullptr; // 빈 문자열로 넘어오면 명시적으로 JSON null 전송
                                // (예: 드라이버 고장 시 이번 주문과 무관한 나머지 7개 레일)
  } else {
    doc["order_id"] = orderId;
  }
  doc["letter_color"] = String(letter) + "_" + String(color);
  doc["count"] = rail[l][c].count;
  switch (status) {
    case RAIL_IDLE:    doc["status"] = "idle";    break;
    case RAIL_BUSY:    doc["status"] = "busy";    break;
    case RAIL_EMPTY:   doc["status"] = "empty";   break;
    case RAIL_DISABLE: doc["status"] = "disable"; break;
  }
  if (status == RAIL_DISABLE && reason != nullptr && reason[0] != '\0') {
    doc["reason"] = reason; // disable일 때만 reason 필드를 실음
  }

  char payload[256];
  serializeJson(doc, payload, sizeof(payload));
  String resp;
  // Mobius CIN 생성을 거치는 호출 - Flask 재시도 워스트케이스(~6.5초)에 맞춘 타임아웃 적용
  postJsonToFlask(REPORT_STATUS_PATH, payload, resp, FLASK_MOBIUS_TIMEOUT_MS);
}

void reportDispenseResult(const String& orderId, char letter, char color) {
  StaticJsonDocument<128> doc;
  doc["order_id"] = orderId;
  doc["keycap"] = String(letter) + "_" + String(color);

  char payload[128];
  serializeJson(doc, payload, sizeof(payload));
  String resp;
  // Mobius CIN 생성을 거치는 호출 - Flask 재시도 워스트케이스(~6.5초)에 맞춘 타임아웃 적용
  postJsonToFlask(REPORT_DISPENSE_PATH, payload, resp, FLASK_MOBIUS_TIMEOUT_MS);
}

// ============================== [11/04] REPAIR 공통 처리 (시리얼 + MQTT) ==============================
// 시리얼 REPAIR와 MQTT 원격 REPAIR가 같은 함수를 씀. source는 로그 구분용("시리얼"/"MQTT").

// ---- 레일 1개 복구: 재고 기준 상태(0이면 empty, 아니면 idle)로 되돌리고 서버(Mobius)에 보고 ----
// 지금 배출 중(busy)인 레일은 건드리지 않음 - 배출 결과 처리와 꼬이므로, 끝난 뒤 다시 보내면 됨.
// 처리했으면 true, busy라서 건너뛰었으면 false.
bool repairRail(int l, int c, const char* source) {
  String key = letterColorKey(l, c);
  if (rail[l][c].status == RAIL_BUSY) {
    Serial.println("[" + String(source) + "] REPAIR " + key + " -> 지금 배출 중이라 건너뜀 (끝난 뒤 다시 보낼 것)");
    return false;
  }
  rail[l][c].status = (rail[l][c].count == 0) ? RAIL_EMPTY : RAIL_IDLE;
  reportRailStatus(letterOf(l), colorOf(c), rail[l][c].status, ""); // 명령발 - order_id 없음(null)
  Serial.println("[" + String(source) + "] REPAIR " + key + " -> " + (rail[l][c].status == RAIL_EMPTY ? "empty" : "idle") + "로 복구, 서버에도 보고함");
  if (pcaDriverFailed) {
    Serial.println("[" + String(source) + "] 참고: driver 고장으로 배출 금지 중 - fault를 내리거나 REPAIR ALL 해야 배출이 재개됨");
  }
  return true;
}

// ---- 전체 복구: disable 레일 전부 복구 + driver 고장 배출 금지 해제 ----
// (PCA 고장 당시 대기 주문이 없어 플랫폼에 내릴 fault가 없을 때 등)
void repairAllRails(const char* source) {
  for (int l = 0; l < 2; l++) {
    for (int c = 0; c < 4; c++) {
      if (rail[l][c].status != RAIL_DISABLE) continue;
      rail[l][c].status = (rail[l][c].count == 0) ? RAIL_EMPTY : RAIL_IDLE;
      reportRailStatus(letterOf(l), colorOf(c), rail[l][c].status, ""); // 명령발 - order_id 없음(null)
    }
  }
  pcaDriverFailed = false;
  pcaHasRelatedOrder = false;
  Serial.println("[" + String(source) + "] REPAIR ALL -> disable 레일 전부 복구, 배출 금지 해제, 서버에도 보고함");
}

// ---- [11/04] MQTT 원격 명령 처리 (sjuiot/warehouse/command) ----
// 형식: {"cmd": "REPAIR", "rail": "E_r"} / {"cmd": "REPAIR", "rail": "ALL", "board": "EI"}
// - board가 있으면 내 보드일 때만 처리, 없으면 rail로만 판단
// - rail이 내 담당이 아니면 조용히 무시 (4대가 같은 토픽을 받음)
// - REPAIR 외의 cmd는 무시 (SETEMPTY 등은 원격으로 열지 않음)
void handleCommandMessage(byte* payload, unsigned int length) {
  StaticJsonDocument<256> doc;
  DeserializationError err = deserializeJson(doc, payload, length);
  if (err) {
    Serial.print("[MQTT명령] JSON 파싱 실패: ");
    Serial.println(err.c_str());
    return;
  }

  const char* board = doc["board"] | "";
  if (board[0] != '\0' && strcmp(board, BOARD_NAME) != 0) return;  // 다른 보드 대상

  String cmd  = String((const char*)(doc["cmd"] | ""));
  String railStr = String((const char*)(doc["rail"] | ""));
  cmd.trim();
  railStr.trim();
  cmd.toUpperCase();

  if (cmd == "CALIB") {   // [11/11] 영점 맞추기
    String posStr = String((const char*)(doc["pos"] | ""));
    posStr.trim();
    handleCalibCommand(railStr, posStr, board[0] != '\0', "MQTT명령");
    return;
  }

  if (cmd != "REPAIR") {
    if (board[0] != '\0') {  // 나를 콕 집어 보낸 명령인데 모르는 cmd면 로그만 남김
      Serial.println("[MQTT명령] 알 수 없는 cmd: " + cmd);
    }
    return;
  }

  if (railStr.equalsIgnoreCase("ALL")) {
    if (board[0] == '\0') {
      Serial.println("[MQTT명령] REPAIR ALL (board 지정 없음 - 4대 공통으로 처리됨)");
    }
    repairAllRails("MQTT명령");
    return;
  }

  if (railStr.length() < 3 || railStr.charAt(1) != '_') {
    if (board[0] != '\0') Serial.println("[MQTT명령] rail 형식 오류: " + railStr + " (예: E_r)");
    return;
  }
  int l = letterSlot(railStr.charAt(0));
  int c = colorSlot(railStr.charAt(2));
  if (l < 0 || c < 0) {
    if (board[0] != '\0') Serial.println("[MQTT명령] " + railStr + " 는 이 보드(" + String(BOARD_NAME) + ") 담당 레일이 아님");
    return;  // 다른 보드 담당 - 정상적으로 무시
  }
  repairRail(l, c, "MQTT명령");
}

// ============================== [11/10] 센서 판정 설정 (MQTT retain) ==============================
// 형식: {"N_r": true, "N_g": false, ...} - 이 보드 칸만 반영, 없는 칸/다른 보드 칸은 무시.
// 배출 중인 칸은 curIrActive로 이번 사이클이 보호되므로 바로 바꿔도 됨 (다음 배출부터 적용).
void handleIrConfig(byte* payload, unsigned int length) {
  StaticJsonDocument<512> doc;
  DeserializationError err = deserializeJson(doc, payload, length);
  if (err) {
    Serial.print("[센서설정] JSON 파싱 실패: ");
    Serial.println(err.c_str());
    return;
  }
  bool changed = false;
  for (int l = 0; l < 2; l++) {
    for (int c = 0; c < 4; c++) {
      String key = letterColorKey(l, c);
      if (!doc[key].is<bool>()) continue;
      bool on = doc[key].as<bool>();
      if (irEnabled[l][c] != on) {
        irEnabled[l][c] = on;
        changed = true;
        Serial.println("[센서설정] " + key + " 센서 판정 " + (on ? "켬" : "끔 (판정 없이 항상 성공 처리)") +
                       ((dsState != DS_IDLE && letterSlot(currentJob.letter) == l && colorSlot(currentJob.color) == c)
                          ? " - 지금 배출 중이라 다음 배출부터 적용" : ""));
      }
    }
  }
  if (changed) printIrConfig();
}

void printIrConfig() {
  Serial.print("[센서설정] ");
  for (int l = 0; l < 2; l++) {
    for (int c = 0; c < 4; c++) {
      Serial.print(letterColorKey(l, c));
      Serial.print(!IR_INSTALLED[l][c] ? "=미배선 " : (irEnabled[l][c] ? "=켬 " : "=끔 "));
    }
  }
  Serial.println();
}

// ============================== [11/11] 영점 맞추기 (CALIB) ==============================
// home = SERVO_HOME_ANGLE(140), push = PUSH_ANGLE(40)으로 보내고 힘 준 채 유지 (혼 다시 끼우기용).
// off = 붙잡던 칸 전부 힘 빼고 배출 재개. rail은 "N_r" 같은 한 칸 또는 "ALL".
// targeted: 이 보드를 콕 집은 명령인지 (MQTT board 지정 / 시리얼). 아니면 남의 칸이어도 조용히 무시.
void handleCalibCommand(const String& railStr, const String& posStr, bool targeted, const char* source) {
  String pos = posStr;
  pos.toLowerCase();
  bool all = railStr.equalsIgnoreCase("ALL");
  int l = -1, c = -1;
  if (!all) {
    if (railStr.length() < 3 || railStr.charAt(1) != '_') {
      if (targeted) Serial.println("[" + String(source) + "] CALIB 칸 형식 오류: " + railStr + " (예: N_r 또는 ALL)");
      return;
    }
    l = letterSlot(railStr.charAt(0));
    c = colorSlot(railStr.charAt(2));
    if (l < 0 || c < 0) {
      if (targeted) Serial.println("[" + String(source) + "] " + railStr + " 는 이 보드(" + String(BOARD_NAME) + ") 담당 레일이 아님");
      return;  // 4대가 같은 토픽을 받으므로 다른 보드 칸이면 정상적으로 무시
    }
  }

  if (pos == "off") {
    calibEnd(source);
    return;
  }

  int angle;
  if (pos == "home")      angle = SERVO_HOME_ANGLE;
  else if (pos == "push") angle = PUSH_ANGLE;
  else {
    // [11/12] 숫자(0~180)면 그 각도로 이동: 예) CALIB N_r 90 / ANGLE ALL 120
    bool numeric = (pos.length() > 0 && pos.length() <= 3);
    for (unsigned int i = 0; i < pos.length(); i++) if (!isDigit(pos.charAt(i))) numeric = false;
    int a = numeric ? pos.toInt() : -1;
    if (a < 0 || a > 180) {
      Serial.println("[" + String(source) + "] CALIB pos 오류: " + posStr + " (home / push / off / 0~180 숫자)");
      return;
    }
    angle = a;
  }

  if (dsState != DS_IDLE) {
    Serial.println("[" + String(source) + "] CALIB 거절 - 지금 배출 중 (" + letterColorKey(letterSlot(currentJob.letter), colorSlot(currentJob.color)) + "), 끝난 뒤 다시 보낼 것");
    return;
  }

  calibActive = true;
  calibLastMs = millis();
  if (all) {
    for (int ll = 0; ll < 2; ll++) {
      for (int cc = 0; cc < 4; cc++) {
        moveServoAngle(SERVO_CHANNEL[ll][cc], angle);
        calibHeld[ll][cc] = true;
        delay(SERVO_STARTUP_STAGGER_MS);   // 부팅 때처럼 한 개씩 (돌입전류 분산)
      }
    }
  } else {
    moveServoAngle(SERVO_CHANNEL[l][c], angle);
    calibHeld[l][c] = true;
  }
  Serial.println("[" + String(source) + "] CALIB " + (all ? String("ALL") : letterColorKey(l, c)) + " -> " + String(angle) +
                 "도로 이동, 힘 준 채 유지 (배출 멈춤, OFF 또는 " + String(CALIB_TIMEOUT_MS / 1000) + "초 뒤 자동 해제)");
}

// 영점 작업 끝: 붙잡던 칸 전부 힘 빼고 배출 재개
void calibEnd(const char* source) {
  if (!calibActive) {
    Serial.println("[" + String(source) + "] CALIB OFF - 영점 작업 중이 아님 (그대로)");
    return;
  }
  for (int l = 0; l < 2; l++) {
    for (int c = 0; c < 4; c++) {
      if (calibHeld[l][c]) {
        releaseServo(SERVO_CHANNEL[l][c]);
        calibHeld[l][c] = false;
      }
    }
  }
  calibActive = false;
  Serial.println("[" + String(source) + "] CALIB 끝 - 힘 빼고 배출 재개");
}

// loop()마다: 마지막 CALIB 명령 후 CALIB_TIMEOUT_MS가 지나면 자동 종료 (OFF를 깜빡해도 발열/배출 정지 방지)
void checkCalibTimeout() {
  if (!calibActive) return;
  if (millis() - calibLastMs < CALIB_TIMEOUT_MS) return;
  calibEnd("자동해제");
}

// ============================== [11/12] IRCHECK - 8칸 센서 한 번에 점검 ==============================
// 시리얼 명령:
//   IRCHECK       : 1초 동안 8칸을 동시에 측정해서 칸별 상태(정상/계속 끊김/불안정) 출력.
//                   측정 중 빔을 가리지 말고 그대로 둘 것. 배출 중(DS_IDLE 아님)엔 안 함.
//   IRCHECK LIVE  : 실시간 모드(최대 120초). 빔을 하나씩 손으로 가려보며 어느 칸이 반응하는지 확인.
//                   상태가 바뀔 때마다 + 1초마다 8칸 전체를 한 줄로 출력. 아무 글자나 보내면 종료.
String irCheckLabel(uint8_t i) { return letterColorKey(i / 4, i % 4); }

bool irCheckBroken(uint8_t i) {
  return digitalRead(IR_PIN[i / 4][i % 4]) == IR_DETECT_LEVEL;
}

void irCheckSnapshot() {
  const unsigned long durMs = 1000;
  unsigned long n = 0, brk[8], chg[8];
  bool last[8];
  for (uint8_t i = 0; i < 8; i++) { brk[i] = 0; chg[i] = 0; last[i] = irCheckBroken(i); }
  Serial.println("[IRCHECK] " + String(durMs) + "ms 동안 8칸 동시 측정 - 빔을 가리지 말고 그대로 두세요");
  unsigned long t0 = millis();
  while (millis() - t0 < durMs) {
    for (uint8_t i = 0; i < 8; i++) {
      bool b = irCheckBroken(i);
      if (b) brk[i]++;
      if (b != last[i]) { chg[i]++; last[i] = b; }
    }
    n++;
  }
  for (uint8_t i = 0; i < 8; i++) {
    Serial.print("  "); Serial.print(irCheckLabel(i));
    Serial.print(" (D"); Serial.print(IR_PIN[i / 4][i % 4]); Serial.print("): ");
    unsigned long pct = n ? (brk[i] * 100UL / n) : 0;
    if (chg[i] == 0 && brk[i] == 0)        Serial.print("정상 (빔 연결)");
    else if (chg[i] == 0 && brk[i] == n)   Serial.print("이상: 계속 끊김 (정렬/전원/배선 확인)");
    else { Serial.print("불안정: 끊김 "); Serial.print(pct); Serial.print("%, 변화 "); Serial.print(chg[i]); Serial.print("회"); }
    Serial.print("  [IR 사용 "); Serial.print(IR_INSTALLED[i / 4][i % 4] ? "예" : "아니오"); Serial.println("]");
  }
  Serial.println("[IRCHECK] 끝 (샘플 " + String(n) + "회)");
}

void irCheckLive() {
  Serial.println("[IRCHECK LIVE] 빔을 하나씩 가려보세요. 아무 글자나 보내면 종료 (최대 120초)");
  while (Serial.available()) Serial.read();
  bool prev[8];
  for (uint8_t i = 0; i < 8; i++) prev[i] = irCheckBroken(i);
  unsigned long start = millis(), lastPrint = 0;
  bool first = true;
  while (millis() - start < 120000UL) {
    mqttClient.loop();   // 이 모드 중에도 MQTT 연결 유지
    bool changed = false;
    bool cur[8];
    for (uint8_t i = 0; i < 8; i++) { cur[i] = irCheckBroken(i); if (cur[i] != prev[i]) changed = true; }
    if (first || changed || millis() - lastPrint >= 1000) {
      first = false;
      lastPrint = millis();
      Serial.print(changed ? ">> " : "   ");
      for (uint8_t i = 0; i < 8; i++) {
        Serial.print(irCheckLabel(i)); Serial.print(cur[i] ? ":끊김 " : ":OK ");
        prev[i] = cur[i];
        if (i == 3) Serial.print("| ");
      }
      Serial.println();
    }
    if (Serial.available()) { while (Serial.available()) Serial.read(); break; }
  }
  Serial.println("[IRCHECK LIVE] 종료");
}

// ============================== 시리얼 테스트 명령 ==============================
// SETEMPTY E_r  : 해당 레일을 강제로 count=0/empty로 만듦 (restock 흐름 테스트용)
// REPAIR E_r    : disable 상태인 레일을 idle로 복구 (count는 그대로 유지)
// REPAIR ALL    : disable 레일 전부 복구 + driver 고장 배출 금지 해제
//                 (PCA 고장 당시 대기 주문이 없어 플랫폼에 내릴 fault가 없을 때 사용)
// [11/04] REPAIR 처리는 repairRail()/repairAllRails()로 옮김 (MQTT 원격 명령과 공용)
void checkSerialCommand() {
  if (!Serial.available()) return;
  String line = Serial.readStringUntil('\n');
  line.trim();
  if (line.length() == 0) return;

  int sp = line.indexOf(' ');
  String cmd = (sp < 0) ? line : line.substring(0, sp);
  String arg = (sp < 0) ? "" : line.substring(sp + 1);
  arg.trim();

  if (cmd == "CALIB" || cmd == "calib" || cmd == "ANGLE" || cmd == "angle") {   // [11/11] (ANGLE는 [11/12] 별칭: ANGLE N_r 90) CALIB N_r HOME / CALIB ALL PUSH / CALIB N_r OFF
    int sp2 = arg.indexOf(' ');
    String railArg = (sp2 < 0) ? arg : arg.substring(0, sp2);
    String posArg  = (sp2 < 0) ? "" : arg.substring(sp2 + 1);
    railArg.trim();
    posArg.trim();
    handleCalibCommand(railArg, posArg, true, "명령");
    return;
  }

  if (cmd == "IRCHECK" || cmd == "ircheck") {   // [11/12] 8칸 센서 한 번에 점검
    if (dsState != DS_IDLE) { Serial.println("[IRCHECK] 배출 중에는 할 수 없음 - 끝난 뒤 다시"); return; }
    if (arg == "LIVE" || arg == "live") irCheckLive(); else irCheckSnapshot();
    return;
  }

  if (cmd == "IR" || cmd == "ir") {   // [11/10] 센서 판정 설정 보기 (바꾸기는 상태판에서)
    printIrConfig();
    return;
  }

  if (cmd == "REPAIR" && (arg == "ALL" || arg == "all")) {
    repairAllRails("명령");
    return;
  }

  if (cmd == "SETEMPTY" || cmd == "REPAIR") {
    if (arg.length() < 3 || arg.charAt(1) != '_') {
      Serial.println("[명령] 형식: " + cmd + " E_r (글자_색상)");
      return;
    }
    char letter = arg.charAt(0);
    char color = arg.charAt(2);
    int l = letterSlot(letter);
    int c = colorSlot(color);
    if (l < 0 || c < 0) {
      Serial.println("[명령] " + arg + " 는 이 보드(" + String(AXIS_NAME) + ") 담당 레일이 아님");
      return;
    }

    if (cmd == "SETEMPTY") {
      rail[l][c].count = 0;
      rail[l][c].status = RAIL_EMPTY;
      reportRailStatus(letter, color, RAIL_EMPTY, ""); // 시리얼 명령발 - order_id 없음(null)
      Serial.println("[명령] " + arg + " -> empty로 강제 설정, 서버에도 보고함");
    } else { // REPAIR
      repairRail(l, c, "명령");
    }
    return;
  }

  Serial.println("[명령] 알 수 없는 명령: " + cmd);
}

// ============================== 유틸 ==============================
int letterSlot(char c) {
  if (c == LETTER_0) return 0;
  if (c == LETTER_1) return 1;
  return -1;
}
int colorSlot(char c) {
  for (int i = 0; i < 4; i++) if (COLOR_LIST[i] == c) return i;
  return -1;
}
