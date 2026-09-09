/*
  ================================================================================
  키캡 배출 아두이노 - E/I 축 담당 (Arduino Uno R4 WiFi)
  ================================================================================
  역할
   1) HiveMQ Cloud(MQTT, TLS)에 직접 연결해서 "sjuiot/warehouse/process" 구독
      (주문/재고보충 push는 MQTT 한 방향으로만 옴: Mobius -> Flask 웹훅 -> MQTT publish.
       Flask(/notify/process)는 con 내용을 파싱하지 않고 그대로 relay만 하므로,
       orders/restock 파싱과 그에 따른 후속 HTTP 호출은 전부 이 아두이노가 직접 함)
   2) status == "assigned" 인 주문에서, 내 축(keycap 문자열의 AXIS_INDEX 위치)
      글자와 그 위치의 색상을 뽑아서 로컬 큐 + 서버 큐(Redis) 양쪽에 적재
   3) 배출은 반드시 "직전에 우리가 배출한 주문이 keycap_packed 상태가 될 때까지"
      대기했다가 순서대로 진행 (로봇암/컨베이어가 1대뿐이라 동시 진행 불가)
   4) 배출 성공/실패는 IR센서로 판정, 성공 시에만 dispense 이벤트 보고
   5) 재고는 Redis가 원본(source of truth). 아두이노 로컬 count는
      "즉각 empty 판단용 캐시"일 뿐 - 부팅 시 서버에서 조회해서 채움
   6) restock(카트리지 재고보충)도 cnt_process의 restock 배열에서 직접 감지해서
      서버에 알리고, 서버가 계산한 채워진 개수를 응답받아 로컬 count 동기화

  ============================== 통신 대상 (Flask) ==============================
   GET  /stock/keycap?board=EI         부팅 시 재고 8칸 초기 조회
   GET  /queue/EI/next                 부팅 시 미완료 큐 복원
   POST /queue/EI/enqueue              새 주문 감지 시 서버 큐에도 적재
   POST /queue/EI/complete             배출 성공 후 서버 큐에서 제거
   POST /restock/{letter}/{color}      restock 감지 시 호출, 채워진 개수 응답받음
   POST /report/keycap-status          기존 그대로 (상태 보고, 성공/실패 무관 항상)
   POST /report/keycap-dispense        기존 그대로 (배출 성공했을 때만)

  ※ 위 신규 라우트(/stock, /queue/*, /restock/*)는 이번에 새로 정의한 스펙이라
    Flask 쪽 실제 응답 형식과 어긋나면 즉시 팀원과 맞춰서 파싱부만 고칠 것.
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
   - 큐(대기 주문 목록)도 서버(Redis)에 같이 적재해서, 재부팅 시 GET /queue/EI/next
     로 미완료 목록을 복원함 (재부팅 시점에 진행 중이던 배출 자체는 유실되지만,
     아직 시작 안 한 대기 주문은 복원됨).

  [2026-09-09 keycap_packed 게이트 추가]
   - 배경: 로봇암/컨베이어가 1대뿐이라, 우리가 배출을 마쳐도 그 결과물이
     "keycap_packed" 상태(트레이에 실제로 담겨 다음 단계로 넘어감)가 되기 전에
     다음 배출을 시작하면 안 됨.
   - 방식: 배출 성공/실패 처리 직후 gateOrderId에 그 주문의 order_id를 기록하고
     게이트를 잠금. 이후 수신하는 모든 cnt_process 스냅샷에서 그 order_id의
     status가 "keycap_packed"이거나 orders 목록에서 아예 사라졌으면 게이트를 열어
     다음 큐 항목을 진행함. 큐잉 순서 자체는 스냅샷에 등장한 순서를 그대로 따름.

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

  [2026-09-09 restock 필드명 수정 + order_id 명시]
   - Mobius cnt_process의 실제 재고보충 필드명은 "restock"이 아니라 "key_restock"
     이었음 -> doc["key_restock"]로 파싱 필드명 수정 (기존엔 잘못된 필드명이라
     restock 배열을 아예 못 읽고 있었음).
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

  다른 3개 보드(N/S, T/F, P/J)는 이 파일을 복사한 뒤
  "보드별 설정" 블록만 아래 표대로 바꿔서 각 보드에 업로드하면 됩니다.

  ------------------------------------------------------------------------------
   보드      LETTER_0 / LETTER_1   AXIS_INDEX   MQTT_CLIENT_ID   BOARD_NAME
  ------------------------------------------------------------------------------
   E/I       'E' / 'I'             0            "arduino_EI"     "EI"
   N/S       'N' / 'S'             1            "arduino_NS"     "NS"
   T/F       'T' / 'F'             2            "arduino_TF"     "TF"
   P/J       'P' / 'J'             3            "arduino_PJ"     "PJ"
  ------------------------------------------------------------------------------

  필요 라이브러리 (라이브러리 매니저에서 설치)
   - WiFiS3, PubSubClient, ArduinoJson(v6), ArduinoHttpClient,
     Adafruit PWM Servo Driver Library (+ Adafruit BusIO)
   ※ Preferences 라이브러리는 더 이상 사용하지 않음 (위 변경사항 참고)

  하드웨어 관련 주의사항 (실측 후 반드시 조정)
   - SERVO_MIN_PULSE / SERVO_MAX_PULSE, SWEEP_STEP_DELAY_MS,
     BREAKAWAY_KICK_DEG / BREAKAWAY_SETTLE_MS, IR_DETECT_LEVEL, IR_PIN,
     PCA9685 I2C 주소(0x40 아니면 수정)
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
#define AXIS_NAME               "EI"
const char  LETTER_0            = 'E';
const char  LETTER_1            = 'I';
const uint8_t AXIS_INDEX        = 0;      // keycap 문자열에서의 내 축 위치 (E/I=0, N/S=1, T/F=2, P/J=3)
const char* MQTT_CLIENT_ID      = "arduino_EI";
const char* BOARD_NAME           = "EI";  // /queue/{board}/*, /stock/keycap?board=* 에 그대로 씀
// ===========================================================================

// ---- 색상 순서 (colors 배열/보드 내부 인덱싱과 동일 순서로 고정) ----
const char COLOR_LIST[4] = {'r', 'y', 'b', 'g'};

// ---- PCA9685 채널 매핑: [글자슬롯(0/1)][색상슬롯(0~3)] ----
const uint8_t SERVO_CHANNEL[2][4] = {
  {0, 1, 2, 3},   // LETTER_0 (예: E) - r,y,b,g
  {4, 5, 6, 7}    // LETTER_1 (예: I) - r,y,b,g
};

// ---- IR 센서 핀 매핑: [글자슬롯(0/1)][색상슬롯(0~3)] (실배선에 맞게 수정) ----
const uint8_t IR_PIN[2][4] = {
  {2, 3, 4, 5},
  {6, 7, 8, 9}
};

// ---- 서보 동작 파라미터 (실측 후 조정) ----
#define SERVO_MIN_PULSE       150
#define SERVO_MAX_PULSE       600
#define SERVO_HOME_ANGLE       90   // 실측 확정값
#define SWEEP_STEP_DELAY_MS    15   // 1도 이동마다 대기 (구버전 실기 테스트값)
#define RETURN_DELAY_MS       400   // 복귀 명령 후 실제 도달까지 대기
#define DISPENSE_HOLD_MS      400   // 180도(완전 배출) 도달 후 유지 시간
#define IR_CHECK_DELAY_MS     250   // 복귀 후 IR 판정까지 대기
#define IR_DETECT_LEVEL       LOW   // 센서가 "감지됨"일 때 주는 레벨 (실측 후 조정)
#define BREAKAWAY_KICK_DEG      4   // 스윕 시작 시 정지마찰을 넘기기 위한 초기 킥 각도
#define BREAKAWAY_SETTLE_MS    60   // 킥 이동 후 실제 도달까지 대기

// ---- 카트리지 최대 용량 (restock 시 이 값까지 채움, 전체 풀 부족하면 서버가 그보다 적게 응답) ----
#define MAX_CAPACITY 30

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

// ============================== 최근 처리한 주문 기록 (중복 배출 방지) ==============================
#define ORDER_HISTORY_SIZE 8
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

// ============================== 배출 대기열 (로컬 캐시, 서버 큐와 동기화) ==============================
struct DispenseJob {
  String orderId;
  char letter;
  char color;
};
#define JOB_QUEUE_SIZE 8
DispenseJob jobQueue[JOB_QUEUE_SIZE];
uint8_t jobHead = 0, jobTail = 0;

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

// ============================== keycap_packed 게이트 ==============================
// gateOpen == true  : 다음 큐 항목을 시작해도 됨
// gateOpen == false : gateOrderId가 keycap_packed 되기를 기다리는 중, 새 배출 시작 금지
bool   gateOpen = true;
String gateOrderId = "";

// ============================== 배출 상태 머신 ==============================
enum DispenseState { DS_IDLE, DS_HOMING, DS_BREAKAWAY_WAIT, DS_SWEEPING, DS_HOLDING, DS_RETURNING, DS_WAIT_IR };
DispenseState dsState = DS_IDLE;
DispenseJob   currentJob;
unsigned long dsTimer = 0;
int           sweepAngle = 0;

// ---- 마지막으로 처리한 cnt_process 스냅샷 seq (문자열 비교, 자릿수 제한 없음) ----
String lastSeqStr = "";

// ---- 네트워크 / 하드웨어 객체 ----
WiFiSSLClient wifiSSLClient;      // MQTT 전용 - 상시 연결 유지 (주문 수신)
PubSubClient  mqttClient(wifiSSLClient);
WiFiSSLClient httpSSLClient;      // Flask 보고/조회 전용 - 필요할 때만 짧게 연결
Adafruit_PWMServoDriver pwm = Adafruit_PWMServoDriver(0x40);

// ---- MQTT 토픽 ----
const char* TOPIC_CNT_PROCESS = "sjuiot/warehouse/process";

// ---- Flask(Cloud Run) 엔드포인트 ----
const char* REPORT_STATUS_PATH   = "/report/keycap-status";
const char* REPORT_DISPENSE_PATH = "/report/keycap-dispense";

// ============================== 함수 선언 ==============================
void ensureWiFi();
void ensureMQTT();
bool tryConnectMQTT();
void mqttCallback(char* topic, byte* payload, unsigned int length);
void handleOrdersField(JsonObject orders);
void handleRestockField(JsonArray restock);
void checkGateAgainstOrders(JsonObject orders);
void handleAssignedOrder(const String& orderId, const char* keycap, JsonArray colors);
void runDispenseStateMachine();
void moveServoAngle(uint8_t channel, uint8_t angle);
void reportRailStatus(char letter, char color, RailStatus status, const String& orderId);
void reportDispenseResult(const String& orderId, char letter, char color);
int  letterSlot(char c);
int  colorSlot(char c);
bool postJsonToFlask(const char* path, const char* payload, String& responseOut);
bool getJsonFromFlask(const char* path, String& responseOut);
void fetchInitialStock();
void restoreQueueFromServer();
void enqueueToServer(const DispenseJob& job);
void completeOnServer(const DispenseJob& job);
void callRestock(int l, int c);
void checkSerialCommand();

// ============================== setup / loop ==============================
void setup() {
  Serial.begin(115200);
  delay(500);

  // IR 센서 핀 설정
  for (int l = 0; l < 2; l++) {
    for (int c = 0; c < 4; c++) {
      pinMode(IR_PIN[l][c], INPUT);
      rail[l][c].status = RAIL_IDLE; // 서버 조회 전 임시값, fetchInitialStock()에서 덮어씀
    }
  }

  // PCA9685 초기화 및 전 채널 홈 포지션 이동
  Wire.begin();
  pwm.begin();
  pwm.setPWMFreq(50);
  delay(50);
  for (int l = 0; l < 2; l++) {
    for (int c = 0; c < 4; c++) {
      moveServoAngle(SERVO_CHANNEL[l][c], SERVO_HOME_ANGLE);
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

  // TLS 인증서는 Arduino IDE "SSL 루트 인증서 업로드" 도구로 R4 WiFi 모듈에
  // 직접 구워 넣는 방식을 쓰므로, 코드에서 setCACert() 등을 호출할 필요 없음.
  // Flask 보고/조회용 httpSSLClient도 같은 방식으로 인증서가 미리 등록돼 있어야 함.

  mqttClient.setServer(MQTT_HOST, MQTT_PORT);
  mqttClient.setBufferSize(2048);
  mqttClient.setCallback(mqttCallback);

  // 서버(Redis)에서 재고/큐 복원 - WiFi 연결된 상태에서만 의미 있음
  if (WiFi.status() == WL_CONNECTED) {
    fetchInitialStock();
    restoreQueueFromServer();
  } else {
    Serial.println("[경고] WiFi 미연결 상태로 setup 종료 - 재고/큐 복원 실패, 로컬 기본값(0) 사용됨");
  }

  Serial.print("[");
  Serial.print(AXIS_NAME);
  Serial.println("] setup 완료");
  Serial.println("[안내] 시리얼 명령: SETEMPTY E_r / REPAIR E_r (테스트용)");
}

void loop() {
  ensureWiFi();
  ensureMQTT();
  mqttClient.loop();
  runDispenseStateMachine();
  checkSerialCommand();
}

// ============================== WiFi / MQTT 연결 관리 (논블로킹) ==============================
unsigned long lastWifiAttempt = 0;
const unsigned long WIFI_RETRY_MS = 5000;

void ensureWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  unsigned long now = millis();
  if (now - lastWifiAttempt < WIFI_RETRY_MS) return;
  lastWifiAttempt = now;
  Serial.println("[WiFi] 재연결 시도...");
  WiFi.begin(WIFI_SSID, WIFI_PASS);
}

unsigned long lastMqttAttempt = 0;
const unsigned long MQTT_RETRY_MS = 3000;

void ensureMQTT() {
  if (WiFi.status() != WL_CONNECTED) return;
  if (mqttClient.connected()) return;
  unsigned long now = millis();
  if (now - lastMqttAttempt < MQTT_RETRY_MS) return;
  lastMqttAttempt = now;
  tryConnectMQTT();
}

bool tryConnectMQTT() {
  Serial.print("[MQTT] 연결 시도... ");
  if (mqttClient.connect(MQTT_CLIENT_ID, MQTT_USER, MQTT_PASS)) {
    Serial.println("성공");
    mqttClient.subscribe(TOPIC_CNT_PROCESS, 1);
    return true;
  }
  Serial.print("실패, state=");
  Serial.println(mqttClient.state());
  return false;
}

// ============================== MQTT 수신 콜백 ==============================
void mqttCallback(char* topic, byte* payload, unsigned int length) {
  if (strcmp(topic, TOPIC_CNT_PROCESS) != 0) return;

  StaticJsonDocument<2048> doc;
  DeserializationError err = deserializeJson(doc, payload, length);
  if (err) {
    Serial.print("[MQTT] JSON 파싱 실패: ");
    Serial.println(err.c_str());
    return;
  }

  String seqStr = doc["seq"].as<String>();
  if (seqStr == lastSeqStr) return; // 동일 스냅샷 -> 재전송 무시
  lastSeqStr = seqStr;

  JsonObject orders = doc["orders"];

  // 게이트 확인이 먼저 - 이번 스냅샷에서 우리가 기다리던 주문이 keycap_packed 됐는지 체크
  checkGateAgainstOrders(orders);

  handleOrdersField(orders);

  JsonArray restock = doc["key_restock"];  // 실제 Mobius 필드명은 "restock"이 아니라 "key_restock"
  handleRestockField(restock);
}

// ---- 게이트: 우리가 기다리던 주문이 완료됐는지 확인, 완료면 게이트 열기 ----
void checkGateAgainstOrders(JsonObject orders) {
  if (gateOpen) return; // 기다리는 중이 아니면 확인할 것 없음
  if (gateOrderId.length() == 0) { gateOpen = true; return; }

  if (!orders.containsKey(gateOrderId.c_str())) {
    // 목록에서 아예 사라짐 = 완료 처리된 것으로 간주
    Serial.println("[게이트] " + gateOrderId + " 목록에서 사라짐 -> 게이트 열림");
    gateOpen = true;
    gateOrderId = "";
    return;
  }

  JsonObject order = orders[gateOrderId.c_str()].as<JsonObject>();
  const char* status = order["status"] | "";
  if (strcmp(status, "keycap_packed") == 0) {
    Serial.println("[게이트] " + gateOrderId + " keycap_packed 확인 -> 게이트 열림");
    gateOpen = true;
    gateOrderId = "";
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
  markProcessed(jobKey);

  DispenseJob job{orderId, letter, color};
  if (!queuePush(job)) {
    Serial.println("[큐] 가득 참 - 주문이 유실될 수 있음. JOB_QUEUE_SIZE 조정 검토 필요");
    return;
  }
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

// ============================== 배출 상태 머신 (90->180도 non-blocking 스윕 + 시작 스티션 킥 + 게이트) ==============================
void runDispenseStateMachine() {
  if (dsState == DS_IDLE) {
    if (!gateOpen) return; // 직전 주문이 keycap_packed 되기 전까지 다음 배출 금지

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

    moveServoAngle(SERVO_CHANNEL[l][c], SERVO_HOME_ANGLE);
    dsTimer = millis();
    dsState = DS_HOMING;
    return;
  }

  if (dsState == DS_HOMING) {
    if (millis() - dsTimer < RETURN_DELAY_MS) return;

    int l = letterSlot(currentJob.letter);
    int c = colorSlot(currentJob.color);

    sweepAngle = SERVO_HOME_ANGLE + BREAKAWAY_KICK_DEG;
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

    sweepAngle++;
    moveServoAngle(SERVO_CHANNEL[l][c], sweepAngle);

    if (sweepAngle >= 180) {
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
    dsTimer = millis();
    dsState = DS_RETURNING;
    return;
  }

  if (dsState == DS_RETURNING) {
    if (millis() - dsTimer < IR_CHECK_DELAY_MS) return;
    dsState = DS_WAIT_IR;
    return;
  }

  if (dsState == DS_WAIT_IR) {
    int l = letterSlot(currentJob.letter);
    int c = colorSlot(currentJob.color);
    bool success = (digitalRead(IR_PIN[l][c]) == IR_DETECT_LEVEL);

    if (success) {
      if (rail[l][c].count > 0) rail[l][c].count--;
      rail[l][c].status = (rail[l][c].count == 0) ? RAIL_EMPTY : RAIL_IDLE;
      reportDispenseResult(currentJob.orderId, currentJob.letter, currentJob.color);
    } else {
      rail[l][c].status = RAIL_DISABLE;
    }

    reportRailStatus(currentJob.letter, currentJob.color, rail[l][c].status, currentJob.orderId);
    completeOnServer(currentJob); // 성공/실패 관계없이 서버 큐에서는 제거 (더 이상 대기열에 있을 이유 없음)

    // 게이트 잠금: 이 주문이 keycap_packed 될 때까지 다음 배출 시작 안 함
    gateOrderId = currentJob.orderId;
    gateOpen = false;

    dsState = DS_IDLE;
    return;
  }
}

// ============================== 서보 제어 ==============================
void moveServoAngle(uint8_t channel, uint8_t angle) {
  uint16_t pulse = map(angle, 0, 180, SERVO_MIN_PULSE, SERVO_MAX_PULSE);
  pwm.setPWM(channel, 0, pulse);
}

// ============================== Flask HTTPS 공통 통신 (실패 시 1회 재시도) ==============================
bool postJsonToFlask(const char* path, const char* payload, String& responseOut) {
  for (uint8_t attempt = 0; attempt < 2; attempt++) {
    HttpClient http(httpSSLClient, FLASK_HOST, FLASK_PORT);
    http.setTimeout(5000);

    int err = http.post(path, "application/json", payload);
    if (err != 0) {
      Serial.print("[HTTPS] POST 연결/전송 실패, err=");
      Serial.print(err);
      Serial.println(attempt == 0 ? " (재시도)" : "");
      http.stop();
      delay(300);
      continue;
    }

    int statusCode = http.responseStatusCode();
    responseOut = http.responseBody();
    http.stop();

    Serial.print("[HTTPS] POST ");
    Serial.print(path);
    Serial.print(" -> ");
    Serial.print(payload);
    Serial.print("  (status=");
    Serial.print(statusCode);
    Serial.println(")");

    if (statusCode < 200 || statusCode >= 300) {
      Serial.print("[HTTPS] 응답 실패: ");
      Serial.println(responseOut);
      return false;
    }
    return true;
  }
  return false;
}

bool getJsonFromFlask(const char* path, String& responseOut) {
  for (uint8_t attempt = 0; attempt < 2; attempt++) {
    HttpClient http(httpSSLClient, FLASK_HOST, FLASK_PORT);
    http.setTimeout(5000);

    int err = http.get(path);
    if (err != 0) {
      Serial.print("[HTTPS] GET 연결/전송 실패, err=");
      Serial.print(err);
      Serial.println(attempt == 0 ? " (재시도)" : "");
      http.stop();
      delay(300);
      continue;
    }

    int statusCode = http.responseStatusCode();
    responseOut = http.responseBody();
    http.stop();

    Serial.print("[HTTPS] GET ");
    Serial.print(path);
    Serial.print("  (status=");
    Serial.print(statusCode);
    Serial.println(")");

    if (statusCode < 200 || statusCode >= 300) {
      Serial.print("[HTTPS] 응답 실패: ");
      Serial.println(responseOut);
      return false;
    }
    return true;
  }
  return false;
}

// ---- 부팅 시 재고 초기 조회 ----
// 가정하는 응답 형식: {"E_r":8,"E_y":8,"E_b":8,"E_g":8,"I_r":8,"I_y":8,"I_b":8,"I_g":8}
// (형식 다르면 Flask 쪽과 맞춰서 이 파싱부만 수정할 것)
void fetchInitialStock() {
  String path = String("/stock/keycap?board=") + BOARD_NAME;
  String resp;
  if (!getJsonFromFlask(path.c_str(), resp)) {
    Serial.println("[재고] 초기 조회 실패 - 로컬 count 0으로 시작 (empty 취급)");
    for (int l = 0; l < 2; l++) {
      for (int c = 0; c < 4; c++) {
        rail[l][c].count = 0;
        rail[l][c].status = RAIL_EMPTY;
      }
    }
    return;
  }

  StaticJsonDocument<512> doc;
  DeserializationError err = deserializeJson(doc, resp);
  if (err) {
    Serial.print("[재고] 응답 파싱 실패: ");
    Serial.println(err.c_str());
    return;
  }

  Serial.println("[재고] 서버에서 복원된 값:");
  for (int l = 0; l < 2; l++) {
    for (int c = 0; c < 4; c++) {
      String key = letterColorKey(l, c);
      uint8_t count = doc[key] | 0;
      rail[l][c].count = count;
      rail[l][c].status = (count == 0) ? RAIL_EMPTY : RAIL_IDLE;
      Serial.println("  " + key + " = " + String(count));
    }
  }
}

// ---- 부팅 시 미완료 큐 복원 ----
// 가정하는 응답 형식: [{"order_id":"o1","letter":"E","color":"r"}, ...]
void restoreQueueFromServer() {
  String path = String("/queue/") + BOARD_NAME + "/next";
  String resp;
  if (!getJsonFromFlask(path.c_str(), resp)) {
    Serial.println("[큐] 서버 큐 조회 실패 - 빈 큐로 시작");
    return;
  }

  StaticJsonDocument<1024> doc;
  DeserializationError err = deserializeJson(doc, resp);
  if (err) {
    Serial.print("[큐] 응답 파싱 실패: ");
    Serial.println(err.c_str());
    return;
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
  rail[l][c].status = (filled == 0) ? RAIL_EMPTY : RAIL_IDLE;
  reportRailStatus(letter, color, rail[l][c].status, "restock"); // 특정 order_id에 종속 안 되므로 식별용 문자열 사용

  Serial.println("[restock] " + letterColorKey(l, c) + " -> " + String(filled) + "개로 채워짐");
}

// ============================== 상태 보고 ==============================
void reportRailStatus(char letter, char color, RailStatus status, const String& orderId) {
  int l = letterSlot(letter);
  int c = colorSlot(color);
  if (l < 0 || c < 0) return;

  StaticJsonDocument<256> doc;
  doc["order_id"] = orderId;
  doc["letter_color"] = String(letter) + "_" + String(color);
  doc["count"] = rail[l][c].count;
  switch (status) {
    case RAIL_IDLE:    doc["status"] = "idle";    break;
    case RAIL_BUSY:    doc["status"] = "busy";    break;
    case RAIL_EMPTY:   doc["status"] = "empty";   break;
    case RAIL_DISABLE: doc["status"] = "disable"; break;
  }

  char payload[256];
  serializeJson(doc, payload, sizeof(payload));
  String resp;
  postJsonToFlask(REPORT_STATUS_PATH, payload, resp);
}

void reportDispenseResult(const String& orderId, char letter, char color) {
  StaticJsonDocument<128> doc;
  doc["order_id"] = orderId;
  doc["keycap"] = String(letter) + "_" + String(color);

  char payload[128];
  serializeJson(doc, payload, sizeof(payload));
  String resp;
  postJsonToFlask(REPORT_DISPENSE_PATH, payload, resp);
}

// ============================== 시리얼 테스트 명령 ==============================
// SETEMPTY E_r  : 해당 레일을 강제로 count=0/empty로 만듦 (restock 흐름 테스트용)
// REPAIR E_r    : disable 상태인 레일을 idle로 복구 (count는 그대로 유지)
void checkSerialCommand() {
  if (!Serial.available()) return;
  String line = Serial.readStringUntil('\n');
  line.trim();
  if (line.length() == 0) return;

  int sp = line.indexOf(' ');
  String cmd = (sp < 0) ? line : line.substring(0, sp);
  String arg = (sp < 0) ? "" : line.substring(sp + 1);
  arg.trim();

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
      reportRailStatus(letter, color, RAIL_EMPTY, "manual");
      Serial.println("[명령] " + arg + " -> empty로 강제 설정, 서버에도 보고함");
    } else { // REPAIR
      rail[l][c].status = (rail[l][c].count == 0) ? RAIL_EMPTY : RAIL_IDLE;
      reportRailStatus(letter, color, rail[l][c].status, "manual");
      Serial.println("[명령] " + arg + " -> " + (rail[l][c].status == RAIL_EMPTY ? "empty" : "idle") + "로 복구, 서버에도 보고함");
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
