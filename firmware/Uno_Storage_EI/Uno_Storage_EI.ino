/*
  ================================================================================
  키캡 배출 아두이노 - E/I 축 담당 (Arduino Uno R4 WiFi)
  ================================================================================
  역할
   1) HiveMQ Cloud(MQTT, TLS)에 직접 연결해서 "sjuiot/warehouse/process" 구독
      (주문 push는 MQTT 한 방향으로만 옴: Mobius -> Flask 웹훅 -> MQTT publish)
   2) status == "assigned" 인 주문에서, 내 축(keycap 문자열의 AXIS_INDEX 위치)
      글자와 그 위치의 색상을 뽑아냄
      예) keycap:"ESFJ", colors:["r","y","b","g"] 이고 내가 EI(index 0) 보드면
          -> letter='E', color='r'  => "E_r" 레일 담당
   3) 해당 레일 상태를 busy로 Flask에 HTTPS POST -> 서보로 배출 -> IR센서로 배출 확인
   4) 배출 결과 보고는 MQTT가 아니라 Flask에 HTTPS POST (짧게 연결->전송->종료)
      - 성공했을 때만 /report/keycap-dispense 호출
      - 성공/실패 관계없이 /report/keycap-status는 항상 호출 (성공: idle/empty, 실패: disable)
   5) Flask가 이 보고를 받아서 Mobius에 CIN 업로드 (아두이노는 Mobius를 직접 모름)

  주의: MQTT 연결(주문 수신용)을 계속 유지한 채로 매 배출마다 별도의
  HTTPS 연결을 짧게 열었다 닫는 구조. Uno R4 WiFi(RA4M1, 32KB SRAM +
  ESP32-S3 동반 모듈)에서 두 개의 TLS 세션을 "동시에" 유지하는 조합은
  비슷한 아키텍처(WiFiNINA 계열)에서 불안정 사례가 보고된 적이 있으므로,
  실제 배출 사이클 전체(주문 수신 중 HTTPS 보고 발생)를 초기에 반드시
  하드웨어로 테스트해볼 것. 문제가 생기면 reportRailStatus/reportDispenseResult
  호출 전후로 mqttClient.disconnect() / tryConnectMQTT() 로 감싸서 MQTT를
  잠깐 끊었다 재연결하는 방식으로 바꿀 수 있음 (HiveMQ에 persistent session +
  QoS1 구독이면 끊긴 동안의 주문 push도 재연결 시 유실 없이 받음).

  다른 3개 보드(N/S, T/F, P/J)는 이 파일을 복사한 뒤
  "보드별 설정" 블록만 아래 표대로 바꿔서 각 보드에 업로드하면 됩니다.

  ------------------------------------------------------------------------------
   보드      LETTER_0 / LETTER_1   AXIS_INDEX   MQTT_CLIENT_ID
  ------------------------------------------------------------------------------
   E/I       'E' / 'I'             0            "arduino_EI"
   N/S       'N' / 'S'             1            "arduino_NS"
   T/F       'T' / 'F'             2            "arduino_TF"
   P/J       'P' / 'J'             3            "arduino_PJ"
  ------------------------------------------------------------------------------

  필요 라이브러리 (라이브러리 매니저에서 설치)
   - WiFiS3            (Uno R4 보드 패키지 설치 시 자동 포함)
   - PubSubClient       (Nick O'Leary) - 표준 라이브러리 사용, 컴파일 문제 시
                         "pubsubclient_UnoR4" 포크로 교체
   - ArduinoJson        (v6.21.x 기준으로 작성됨. v7 사용 시 StaticJsonDocument
                         관련 문법 확인 필요)
   - ArduinoHttpClient  (Arduino 공식) - Flask에 보고용 HTTPS POST 보낼 때 사용
   - Adafruit PWM Servo Driver Library (+ 의존성 Adafruit BusIO)

  하드웨어 관련 주의사항 (실측 후 반드시 조정)
   - SERVO_MIN_PULSE / SERVO_MAX_PULSE / SERVO_DISPENSE_ANGLE : 서보 개체별로 다름
   - IR_DETECT_LEVEL : 사용하는 IR 센서가 감지 시 HIGH/LOW 중 무엇을 주는지 확인
   - IR_PIN 배열의 실제 배선 핀 번호
   - PCA9685 I2C 주소가 0x40이 아니면 pwm 생성자 인자 수정
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
#define SERVO_HOME_ANGLE      0
#define SERVO_DISPENSE_ANGLE  90
#define DISPENSE_HOLD_MS      400   // 배출 각도 유지 시간
#define IR_CHECK_DELAY_MS     250   // 서보 복귀 후 IR 판정까지 대기
#define IR_DETECT_LEVEL       LOW   // 센서가 "감지됨"일 때 주는 레벨 (실측 후 조정)

// ---- 초기 재고 (실제 값으로 교체하거나, 부팅 시 별도 동기화 로직 추가) ----
const uint8_t INITIAL_COUNT = 8;

// ---- 레일 상태 (로컬 캐시) ----
enum RailStatus : uint8_t { RAIL_IDLE, RAIL_BUSY, RAIL_EMPTY, RAIL_DISABLE };
struct RailState {
  uint8_t count;
  RailStatus status;
  String lastOrderId;
};
RailState rail[2][4]; // [글자슬롯][색상슬롯]

// ---- 최근 처리한 (주문,글자,색상) 조합 기록 -> 중복 배출 방지 ----
// (동일 스냅샷 재전송, MQTT 중복 등으로 같은 assigned 를 두 번 받는 상황 대비)
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

// ---- 배출 대기열 (동시에 여러 assigned 주문이 들어와도 순차 처리) ----
struct DispenseJob {
  String orderId;
  char letter;
  char color;
};
#define JOB_QUEUE_SIZE 4
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

// ---- 배출 상태 머신 ----
enum DispenseState { DS_IDLE, DS_MOVING, DS_RETURNING, DS_WAIT_IR };
DispenseState dsState = DS_IDLE;
DispenseJob   currentJob;
unsigned long dsTimer = 0;

// ---- 마지막으로 처리한 cnt_process 스냅샷 seq (같으면 무시) ----
long lastSeq = -1;

// ---- 네트워크 / 하드웨어 객체 ----
WiFiSSLClient wifiSSLClient;      // MQTT 전용 - 상시 연결 유지 (주문 수신)
PubSubClient  mqttClient(wifiSSLClient);
WiFiSSLClient httpSSLClient;      // Flask 보고 전용 - 배출할 때만 짧게 연결
Adafruit_PWMServoDriver pwm = Adafruit_PWMServoDriver(0x40);

// ---- MQTT 토픽 (주문 push 수신 전용, 확정된 이름) ----
const char* TOPIC_CNT_PROCESS = "sjuiot/warehouse/process";

// ---- Flask(Cloud Run) 보고 엔드포인트 (아두이노 -> Flask -> Mobius CIN) ----
const char* REPORT_STATUS_PATH   = "/report/keycap-status";
const char* REPORT_DISPENSE_PATH = "/report/keycap-dispense";

// ============================== 함수 선언 ==============================
void ensureWiFi();
void ensureMQTT();
bool tryConnectMQTT();
void mqttCallback(char* topic, byte* payload, unsigned int length);
void handleAssignedOrder(const String& orderId, const char* keycap, JsonArray colors);
void runDispenseStateMachine();
void moveServoAngle(uint8_t channel, uint8_t angle);
void reportRailStatus(char letter, char color, RailStatus status, const String& orderId);
void reportDispenseResult(const String& orderId, char letter, char color);
int  letterSlot(char c);
int  colorSlot(char c);

// ============================== setup / loop ==============================
void setup() {
  Serial.begin(115200);

  // 재고 초기화 (TODO: 실제 초기 재고값 반영, 또는 부팅 시 플랫폼과 동기화)
  for (int l = 0; l < 2; l++) {
    for (int c = 0; c < 4; c++) {
      rail[l][c].count = INITIAL_COUNT;
      rail[l][c].status = RAIL_IDLE;
    }
  }

  // IR 센서 핀 설정
  for (int l = 0; l < 2; l++) {
    for (int c = 0; c < 4; c++) {
      pinMode(IR_PIN[l][c], INPUT);
    }
  }

  // PCA9685 초기화 및 전 채널 홈 포지션 이동
  Wire.begin();
  pwm.begin();
  pwm.setPWMFreq(50); // 서보용 표준 주파수
  delay(50);
  for (int l = 0; l < 2; l++) {
    for (int c = 0; c < 4; c++) {
      moveServoAngle(SERVO_CHANNEL[l][c], SERVO_HOME_ANGLE);
    }
  }

  // WiFi 연결 (최초 1회는 블로킹으로 확인)
  Serial.print("[WiFi] 연결 시도: ");
  Serial.println(WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  // TLS 루트 CA 설정 (config.h)
  // MQTT(HiveMQ Cloud)와 HTTPS(Flask/Cloud Run)는 인증서 발급 체인이 다르므로
  // 루트 CA 상수를 따로 둠 (config.h의 GOOGLE_ROOT_CA 참고)

  mqttClient.setServer(MQTT_HOST, MQTT_PORT);
  mqttClient.setBufferSize(2048); // cnt_process 스냅샷이 기본 256바이트보다 큼
  mqttClient.setCallback(mqttCallback);

  Serial.print("[");
  Serial.print(AXIS_NAME);
  Serial.println("] setup 완료");
}

void loop() {
  ensureWiFi();
  ensureMQTT();
  mqttClient.loop();
  runDispenseStateMachine();
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

  // 문서 크기는 cnt_process 스냅샷 크기에 맞춰 조정 (필요시 늘리기)
  StaticJsonDocument<2048> doc;
  DeserializationError err = deserializeJson(doc, payload, length);
  if (err) {
    Serial.print("[MQTT] JSON 파싱 실패: ");
    Serial.println(err.c_str());
    return;
  }

  long seq = doc["seq"] | -1L;
  if (seq == lastSeq) return; // 동일 스냅샷 -> 재전송 무시
  lastSeq = seq;

  JsonObject orders = doc["orders"];
  for (JsonPair kv : orders) {
    JsonObject order = kv.value().as<JsonObject>();
    const char* status = order["status"] | "";
    if (strcmp(status, "assigned") != 0) continue;
    if (!order["fault"].isNull()) continue; // fault 있으면 정지된 주문 -> 스킵

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

  // order_id + letter + color 조합으로 중복 배출 방지
  String jobKey = orderId + "_" + String(letter) + "_" + String(color);
  if (alreadyProcessed(jobKey)) return;
  markProcessed(jobKey);

  DispenseJob job{orderId, letter, color};
  if (!queuePush(job)) {
    Serial.println("[큐] 가득 참 - 주문이 유실될 수 있음. JOB_QUEUE_SIZE 조정 검토 필요");
  }
}

// ============================== 배출 상태 머신 ==============================
void runDispenseStateMachine() {
  if (dsState == DS_IDLE) {
    DispenseJob job;
    if (!queuePop(job)) return;

    int l = letterSlot(job.letter);
    int c = colorSlot(job.color);
    if (l < 0 || c < 0) return;

    if (rail[l][c].status == RAIL_DISABLE || rail[l][c].status == RAIL_EMPTY) {
      // TODO: 배출 불가 상태에서 들어온 주문을 어떻게 보고할지 플랫폼팀과 협의 필요
      Serial.println("[배출] 해당 레일 사용 불가(disable/empty) - 건너뜀");
      return;
    }

    currentJob = job;
    rail[l][c].status = RAIL_BUSY;
    rail[l][c].lastOrderId = job.orderId;
    reportRailStatus(job.letter, job.color, RAIL_BUSY, job.orderId);

    moveServoAngle(SERVO_CHANNEL[l][c], SERVO_DISPENSE_ANGLE);
    dsTimer = millis();
    dsState = DS_MOVING;
    return;
  }

  if (dsState == DS_MOVING) {
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
    return; // 다음 loop()에서 바로 판정
  }

  if (dsState == DS_WAIT_IR) {
    int l = letterSlot(currentJob.letter);
    int c = colorSlot(currentJob.color);
    bool success = (digitalRead(IR_PIN[l][c]) == IR_DETECT_LEVEL);

    if (success) {
      if (rail[l][c].count > 0) rail[l][c].count--;
      rail[l][c].status = (rail[l][c].count == 0) ? RAIL_EMPTY : RAIL_IDLE;
      // 배출 결과 보고(cnt_keycap_dispense)는 성공했을 때만 전송
      reportDispenseResult(currentJob.orderId, currentJob.letter, currentJob.color);
    } else {
      rail[l][c].status = RAIL_DISABLE;
    }

    // 상태 보고(cnt_keycap_status)는 성공/실패 관계없이 항상 전송
    reportRailStatus(currentJob.letter, currentJob.color, rail[l][c].status, currentJob.orderId);

    dsState = DS_IDLE;
    return;
  }
}

// ============================== 서보 제어 ==============================
void moveServoAngle(uint8_t channel, uint8_t angle) {
  uint16_t pulse = map(angle, 0, 180, SERVO_MIN_PULSE, SERVO_MAX_PULSE);
  pwm.setPWM(channel, 0, pulse);
}

// ============================== Flask HTTPS 보고 ==============================
// 공통: JSON 문자열을 지정한 path로 POST. 연결 열고-보내고-끊는 짧은 수명.
// MQTT(wifiSSLClient)는 건드리지 않고 별도의 httpSSLClient만 사용.
bool postJsonToFlask(const char* path, const char* payload) {
  HttpClient http(httpSSLClient, FLASK_HOST, FLASK_PORT);
  http.setTimeout(5000); // 보고 1건이 무한 대기하지 않도록 타임아웃 설정

  int err = http.post(path, "application/json", payload);
  if (err != 0) {
    Serial.print("[HTTPS] 연결/전송 실패, err=");
    Serial.println(err);
    http.stop();
    return false;
  }

  int statusCode = http.responseStatusCode();
  String body = http.responseBody(); // 응답을 읽어야 연결이 깔끔히 정리됨
  http.stop();

  Serial.print("[HTTPS] ");
  Serial.print(path);
  Serial.print(" -> ");
  Serial.print(payload);
  Serial.print("  (status=");
  Serial.print(statusCode);
  Serial.println(")");

  if (statusCode < 200 || statusCode >= 300) {
    Serial.print("[HTTPS] Flask 응답 실패: ");
    Serial.println(body);
    // TODO: 재시도 정책 필요 여부는 테스트 후 결정 (지금은 1회만 시도)
    return false;
  }
  return true;
}

void reportRailStatus(char letter, char color, RailStatus status, const String& orderId) {
  int l = letterSlot(letter);
  int c = colorSlot(color);
  if (l < 0 || c < 0) return;

  StaticJsonDocument<256> doc;
  doc["order_id"] = orderId;
  doc["letter_color"] = String(letter) + "_" + String(color); // 예: "E_r" - Flask가 어떤 cnt_keycap_status 인지 식별
  doc["count"] = rail[l][c].count;
  switch (status) {
    case RAIL_IDLE:    doc["status"] = "idle";    break;
    case RAIL_BUSY:    doc["status"] = "busy";    break;
    case RAIL_EMPTY:   doc["status"] = "empty";   break;
    case RAIL_DISABLE: doc["status"] = "disable"; break;
  }

  char payload[256];
  serializeJson(doc, payload, sizeof(payload));
  postJsonToFlask(REPORT_STATUS_PATH, payload);
}

void reportDispenseResult(const String& orderId, char letter, char color) {
  StaticJsonDocument<128> doc;
  doc["order_id"] = orderId;
  doc["keycap"] = String(letter) + "_" + String(color); // 예: "E_r"

  char payload[128];
  serializeJson(doc, payload, sizeof(payload));
  postJsonToFlask(REPORT_DISPENSE_PATH, payload);
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
