/*
  ================================================================================
  키캡 배출 아두이노 - N/S 축 담당 (Arduino Uno R4 WiFi)
  ================================================================================
  역할
   1) HiveMQ Cloud(MQTT, TLS)에 직접 연결해서 "sjuiot/warehouse/process" 구독
      (주문/재고보충 push는 MQTT 한 방향으로만 옴: Mobius -> Flask 웹훅 -> MQTT publish.
       Flask(/notify/process)는 con 내용을 파싱하지 않고 그대로 relay만 하므로,
       orders/restock 파싱과 그에 따른 후속 HTTP 호출은 전부 이 아두이노가 직접 함)
   2) status == "assigned" 인 주문에서, 내 축(keycap 문자열의 AXIS_INDEX 위치)
      글자와 그 위치의 색상을 뽑아서 로컬 큐 + 서버 큐(Redis) 양쪽에 적재
   3) 배출은 반드시 "직전에 우리가 배출한 주문이 pickup_reachedd 상태가 될 때까지"
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
   ※ Preferences 라이브러리는 더 이상 사용하지 않음

  하드웨어 관련 주의사항 (보드마다 실측 후 반드시 조정 — 아래 참고)
   - SERVO_MIN_PULSE / SERVO_MAX_PULSE, SWEEP_STEP_DELAY_MS,
     BREAKAWAY_KICK_DEG / BREAKAWAY_SETTLE_MS, IR_DETECT_LEVEL, IR_PIN,
     PCA9685 I2C 주소(0x40 아니면 수정)
   ⚠️ 이 값들은 "보드별 설정" 블록이 아니라 파일 하단부 매크로/배열에 있음.
     4개 보드가 서보/센서 개체가 전부 다르므로 서보 튐/유격, IR 감도는
     보드마다 따로 실측해서 이 부분을 개별 조정할 것 (복사만 하고 넘어가면 안 됨).
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
#define AXIS_NAME               "NS"
const char  LETTER_0            = 'N';
const char  LETTER_1            = 'S';
const uint8_t AXIS_INDEX        = 1;      // keycap 문자열에서의 내 축 위치 (E/I=0, N/S=1, T/F=2, P/J=3)
const char* MQTT_CLIENT_ID      = "arduino_NS";
const char* BOARD_NAME           = "NS";  // /queue/{board}/*, /stock/keycap?board=* 에 그대로 씀
// ===========================================================================

// ---- 색상 순서 (colors 배열/보드 내부 인덱싱과 동일 순서로 고정) ----
const char COLOR_LIST[4] = {'r', 'y', 'b', 'g'};

// ---- PCA9685 채널 매핑: [글자슬롯(0/1)][색상슬롯(0~3)] ----
// 보드마다 자기 전용 PCA9685(0x40)를 따로 갖고 있으므로 채널 번호는
// 0~7 그대로 재사용 가능. 실배선이 다르면 보드별로 수정.
const uint8_t SERVO_CHANNEL[2][4] = {
  {0, 1, 2, 3},   // LETTER_0 - r,y,b,g
  {4, 5, 6, 7}    // LETTER_1 - r,y,b,g
};

// ---- IR 센서 핀 매핑: [글자슬롯(0/1)][색상슬롯(0~3)] (실배선에 맞게 수정) ----
const uint8_t IR_PIN[2][4] = {
  {2, 3, 4, 5},
  {6, 7, 8, 9}
};

// ---- 서보 동작 파라미터 (보드마다 실측 후 조정) ----
#define SERVO_MIN_PULSE       150
#define SERVO_MAX_PULSE       600
#define SERVO_HOME_ANGLE       90
#define SWEEP_STEP_DELAY_MS    15
#define RETURN_DELAY_MS       400
#define DISPENSE_HOLD_MS      400
#define IR_CHECK_DELAY_MS     250
#define IR_DETECT_LEVEL       LOW

// ---- [IR 센서 미장착 보드용] ----
// 이 보드는 IR 센서 수량 부족으로 물리 연결이 안 돼 있음.
// 0으로 두면 DS_WAIT_IR 단계에서 실제 핀을 읽지 않고 무조건 성공(idle) 처리함.
// 나중에 센서 확보되면 이 값만 1로 바꾸고 IR_PIN 배선하면 원래대로 동작함.
#define IR_SENSOR_INSTALLED 0
#define BREAKAWAY_KICK_DEG      4
#define BREAKAWAY_SETTLE_MS    60

#define MAX_CAPACITY 30

enum RailStatus : uint8_t { RAIL_IDLE, RAIL_BUSY, RAIL_EMPTY, RAIL_DISABLE };
struct RailState {
  uint8_t count;
  RailStatus status;
  String lastOrderId;
};
RailState rail[2][4];

char letterOf(int l) { return (l == 0) ? LETTER_0 : LETTER_1; }
char colorOf(int c)  { return COLOR_LIST[c]; }
String letterColorKey(int l, int c) {
  return String(letterOf(l)) + "_" + String(colorOf(c));
}

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

bool restockInProgress[2][4] = { {false, false, false, false}, {false, false, false, false} };

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
  if (next == jobHead) return false;
  jobQueue[jobTail] = job;
  jobTail = next;
  return true;
}
bool queuePop(DispenseJob& job) {
  if (jobHead == jobTail) return false;
  job = jobQueue[jobHead];
  jobHead = (jobHead + 1) % JOB_QUEUE_SIZE;
  return true;
}

bool   gateOpen = true;
String gateOrderId = "";

enum DispenseState { DS_IDLE, DS_HOMING, DS_BREAKAWAY_WAIT, DS_SWEEPING, DS_HOLDING, DS_RETURNING, DS_WAIT_IR };
DispenseState dsState = DS_IDLE;
DispenseJob   currentJob;
unsigned long dsTimer = 0;
int           sweepAngle = 0;

String lastSeqStr = "";

WiFiSSLClient wifiSSLClient;
PubSubClient  mqttClient(wifiSSLClient);
WiFiSSLClient httpSSLClient;
Adafruit_PWMServoDriver pwm = Adafruit_PWMServoDriver(0x40);

const char* TOPIC_CNT_PROCESS = "sjuiot/warehouse/process";

const char* REPORT_STATUS_PATH   = "/report/keycap-status";
const char* REPORT_DISPENSE_PATH = "/report/keycap-dispense";

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

void setup() {
  Serial.begin(115200);
  delay(500);

  for (int l = 0; l < 2; l++) {
    for (int c = 0; c < 4; c++) {
      pinMode(IR_PIN[l][c], INPUT);
      rail[l][c].status = RAIL_IDLE;
    }
  }

  Wire.begin();
  pwm.begin();
  pwm.setPWMFreq(50);
  delay(50);
  for (int l = 0; l < 2; l++) {
    for (int c = 0; c < 4; c++) {
      moveServoAngle(SERVO_CHANNEL[l][c], SERVO_HOME_ANGLE);
    }
  }

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

  mqttClient.setServer(MQTT_HOST, MQTT_PORT);
  mqttClient.setBufferSize(2048);
  mqttClient.setCallback(mqttCallback);

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
  if (seqStr == lastSeqStr) return;
  lastSeqStr = seqStr;

  JsonObject orders = doc["orders"];

  checkGateAgainstOrders(orders);
  handleOrdersField(orders);

  JsonArray restock = doc["keycap_restock"];
  handleRestockField(restock);
}

void checkGateAgainstOrders(JsonObject orders) {
  if (gateOpen) return;
  if (gateOrderId.length() == 0) { gateOpen = true; return; }

  if (!orders.containsKey(gateOrderId.c_str())) {
    Serial.println("[게이트] " + gateOrderId + " 목록에서 사라짐 -> 게이트 열림");
    gateOpen = true;
    gateOrderId = "";
    return;
  }

  JsonObject order = orders[gateOrderId.c_str()].as<JsonObject>();
  const char* status = order["status"] | "";
  if (strcmp(status, "pickup_reached") == 0) {
    Serial.println("[게이트] " + gateOrderId + " pickup_reached 확인 -> 게이트 열림");
    gateOpen = true;
    gateOrderId = "";
  }
}

void handleOrdersField(JsonObject orders) {
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
  if (letterSlot(letter) < 0) return;

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
  enqueueToServer(job);
}

void handleRestockField(JsonArray restock) {
  bool presentNow[2][4] = { {false,false,false,false}, {false,false,false,false} };

  for (JsonVariant item : restock) {
    String rc = item.as<String>();
    if (rc.length() < 3) continue;
    char letter = rc.charAt(0);
    char color  = rc.charAt(2);
    int l = letterSlot(letter);
    int c = colorSlot(color);
    if (l < 0 || c < 0) continue;

    presentNow[l][c] = true;

    if (!restockInProgress[l][c]) {
      restockInProgress[l][c] = true;
      callRestock(l, c);
    }
  }

  for (int l = 0; l < 2; l++) {
    for (int c = 0; c < 4; c++) {
      if (restockInProgress[l][c] && !presentNow[l][c]) {
        restockInProgress[l][c] = false;
      }
    }
  }
}

void runDispenseStateMachine() {
  if (dsState == DS_IDLE) {
    if (!gateOpen) return;

    DispenseJob job;
    if (!queuePop(job)) return;

    int l = letterSlot(job.letter);
    int c = colorSlot(job.color);
    if (l < 0 || c < 0) return;

    if (rail[l][c].status == RAIL_DISABLE || rail[l][c].status == RAIL_EMPTY) {
      Serial.println("[배출] 해당 레일 사용 불가(disable/empty) - 건너뜀");
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
#if IR_SENSOR_INSTALLED
    bool success = (digitalRead(IR_PIN[l][c]) == IR_DETECT_LEVEL);
#else
    bool success = true; // IR 센서 미장착 - 실제 판정 없이 항상 성공(정상 배출)으로 간주
#endif

    if (success) {
      if (rail[l][c].count > 0) rail[l][c].count--;
      rail[l][c].status = (rail[l][c].count == 0) ? RAIL_EMPTY : RAIL_IDLE;
      reportDispenseResult(currentJob.orderId, currentJob.letter, currentJob.color);
    } else {
      rail[l][c].status = RAIL_DISABLE;
    }

    reportRailStatus(currentJob.letter, currentJob.color, rail[l][c].status, currentJob.orderId);
    completeOnServer(currentJob);

    gateOrderId = currentJob.orderId;
    gateOpen = false;

    dsState = DS_IDLE;
    return;
  }
}

void moveServoAngle(uint8_t channel, uint8_t angle) {
  uint16_t pulse = map(angle, 0, 180, SERVO_MIN_PULSE, SERVO_MAX_PULSE);
  pwm.setPWM(channel, 0, pulse);
}

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

void callRestock(int l, int c) {
  char letter = letterOf(l);
  char color = colorOf(c);
  String path = String("/restock/") + letter + "/" + color;

  String resp;
  if (!postJsonToFlask(path.c_str(), "{}", resp)) {
    Serial.println("[restock] 호출 실패 - 로컬 count 변경 안 함, 다음 스냅샷에서 재시도됨");
    restockInProgress[l][c] = false;
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
  reportRailStatus(letter, color, rail[l][c].status, "restock");

  Serial.println("[restock] " + letterColorKey(l, c) + " -> " + String(filled) + "개로 채워짐");
}

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
    } else {
      rail[l][c].status = (rail[l][c].count == 0) ? RAIL_EMPTY : RAIL_IDLE;
      reportRailStatus(letter, color, rail[l][c].status, "manual");
      Serial.println("[명령] " + arg + " -> " + (rail[l][c].status == RAIL_EMPTY ? "empty" : "idle") + "로 복구, 서버에도 보고함");
    }
    return;
  }

  Serial.println("[명령] 알 수 없는 명령: " + cmd);
}

int letterSlot(char c) {
  if (c == LETTER_0) return 0;
  if (c == LETTER_1) return 1;
  return -1;
}
int colorSlot(char c) {
  for (int i = 0; i < 4; i++) if (COLOR_LIST[i] == c) return i;
  return -1;
}
