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

  [2026-09-06 변경 사항 - 서보 배출 동작을 0~90도 점프 -> 0~180도 풀 스윕으로 교체]
   - 실물 테스트 결과 0~90도 점프 방식은 디스펜서가 부품을 끝까지 밀어내지 못함
     (각도 부족) -> 구버전(ESP32/Uno_Storage.ino 시절) 검증된 "0->180도, 1도씩
     천천히 스윕" 방식으로 교체
   - 단, delay()로 블로킹하면 그 몇 초 동안 mqttClient.loop()가 안 돌아 MQTT
     연결이 끊길 위험이 있으므로, 스윕 전체를 상태머신에 편입해 millis() 기준
     non-blocking으로 처리함 (delay() 호출 없음)
   - 복귀(180->0 또는 최초 0도 이동)는 구버전과 동일하게 스텝 없이 한 번에 점프
   - SWEEP_STEP_DELAY_MS(1도당 대기시간)는 구버전 실기 테스트값 15ms를 기본값으로
     둠 -> 실물로 배출 성공률/속도 확인 후 필요시 조정

  [여러 차례 시도했던 "대기각/스윕시작각 분리 + 복귀도 스텝화" 버전은 롤백함]
   - 대기 위치를 뒤로 젖히면서(SERVO_HOME_ANGLE=90) 동시에 0~180도 풀 폭을
     쓰려고 대기각<->스윕시작각을 분리했더니, 매 사이클마다 여분의 이동이
     하나 더 생겨서 "한 번 더 도는" 것처럼 느껴지는 부작용이 있었음.
   - 실기 테스트 결과 이 단순 버전(90->180 단일 스윕 + 점프 복귀)이 체감상
     더 낫다고 판단 -> 이 구조를 기준으로 되돌리고, 아래 스티션(정지마찰)
     문제만 별도로 해결함.

  [2026-09-07 스티션(정지마찰) 대응 - 스윕 시작 시 breakaway kick 추가]
   - 증상: 스윕 시작하자마자 제자리에서 부르르 떨다가(=스티션과 씨름) 한쪽
     방향으로 스무스하게 풀려나감.
   - 원인: 1도씩 미세하게 명령을 올리면 서보 위치제어 루프가 인식하는 오차가
     작아서 출력 토크도 작음 -> 정지마찰을 넘길 만큼의 토크가 안 나와서
     제자리에 머무름 -> 이후 스텝들이 계속 쌓이면서(오차 누적) 토크도 같이
     누적되다가, 마찰을 넘어서는 순간 누적된 만큼 한꺼번에 튀어나가는
     현상 (겉보기엔 "부르르 떨다가 훅 풀림"으로 보임).
   - 해결: DS_HOMING -> DS_SWEEPING 전환 시, 1도씩 올리지 않고 먼저
     BREAKAWAY_KICK_DEG(기본 4도)만큼 한 번에 점프시켜 마찰을 깔끔하게
     넘긴 뒤(BREAKAWAY_SETTLE_MS만큼 짧게 대기), 그 지점부터 기존과 동일하게
     1도씩 천천히 스윕을 이어감. 새 상태 DS_BREAKAWAY_WAIT 추가.
   - 주의: 킥 이후에도 여전히 떨림이 남아있다면 (a) BREAKAWAY_KICK_DEG를
     조금씩 올려보고, 그래도 안 되면 (b) 레일에 부품을 넣지 않은 무부하
     상태로도 같은 증상이 나는지 확인할 것. 무부하에서도 떨리면 이건
     소프트웨어가 아니라 전원(전압 강하)이나 기구적 간섭/마찰 문제일
     가능성이 높음.

  [2026-09-07 seq 비교를 정수 -> 문자열로 변경]
   - 증상: Postman 테스트용 seq를 Date.now() 등 13자리 큰 숫자로 만들면
     아두이노가 새 주문을 아예 인식 못 함 (mqttCallback 진입 로그조차 안 뜸).
   - 원인: doc["seq"]를 long/long long 정수로 파싱하던 중 32비트 long
     범위(약 21억, 10자리)를 넘는 값에서 ArduinoJson v6의 정수 처리가
     깨짐. 실측으로 "9자리는 항상 성공, 10자리는 값에 따라 반반, 11자리
     이상은 항상 실패"까지 확인함.
   - 해결: seq를 정수로 받지 않고 처음부터 String으로 받아서 비교.
     자릿수 제한이 사라지므로 플랫폼팀이 실제로 어떤 형식(카운터형이든
     타임스탬프형이든)으로 seq를 만들어도 안전함.

  [2026-09-07 재고 영속화 추가 - Preferences(NVS급 플래시 저장) 사용]
   - 배경: 이전까지는 rail[][].count가 RAM에만 있어서, 아두이노가
     재부팅(리셋/정전/재업로드)되면 재고가 INITIAL_COUNT(8)로 초기화되고
     실제 남은 재고와 어긋남.
   - 결정 배경: 재고 계산의 동시성 문제(레이스 컨디션)는 플랫폼이 이전
     주문의 배출 완료 신호를 확인한 뒤에만 다음 주문을 cnt_process에
     올리는 순차 처리 구조라 애초에 발생하지 않는 것으로 확인됨. 즉
     Redis 같은 원자적 카운터가 필요한 상황이 아니라서, 아두이노 자체
     로컬 영속화(ESP32 시절 NVS와 동일한 개념)만으로 충분하다고 판단.
   - 방식: R4 WiFi도 ESP32와 동일한 Preferences 라이브러리를 지원함.
     부팅 시(setup) 저장된 값을 복원하고, 배출 성공으로 count가 바뀔
     때마다 그 즉시 저장. 키는 "E_r" 같은 글자_색상 문자열 그대로 사용
     (Preferences 키 길이 제한 15자 이내로 여유 있음).
   - FACTORYRESET: 시리얼 모니터에 "FACTORYRESET" 입력 시 저장된 값을
     지우고 INITIAL_COUNT로 되돌림 (카트리지 실물 교체 후 사용).

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
   - Preferences        (Uno R4 보드 패키지 설치 시 자동 포함, ESP32의 NVS와 동일 개념)

  하드웨어 관련 주의사항 (실측 후 반드시 조정)
   - SERVO_MIN_PULSE / SERVO_MAX_PULSE : 서보 개체별로 다름
   - SWEEP_STEP_DELAY_MS : 1도 이동마다 대기 시간, 너무 빠르면 부품이 안 밀리고
     너무 느리면 전체 사이클이 늘어짐 (구버전 실기값 15ms 기본)
   - BREAKAWAY_KICK_DEG / BREAKAWAY_SETTLE_MS : 시작 시 스티션을 넘기기 위한
     초기 킥 각도/정착 대기시간. 킥이 너무 작으면 여전히 떨리고, 너무 크면
     그 자체가 또 다른 "훅 튀는 동작"처럼 보일 수 있음 - 실측하며 조정
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
#include <Preferences.h>
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
#define SERVO_HOME_ANGLE      90   // 실측 확정값. 대기 위치를 더 뒤로 젖히려고 0->90으로 변경함
#define SWEEP_STEP_DELAY_MS   15    // 1도 이동마다 대기 (구버전 실기 테스트값, 다르면 교체)
#define RETURN_DELAY_MS        400  // 복귀 명령(0도로 점프) 후 실제 도달까지 대기 (구버전값)
#define DISPENSE_HOLD_MS       400  // 180도(완전 배출 위치) 도달 후 유지 시간
#define IR_CHECK_DELAY_MS      250  // 복귀 후 IR 판정까지 대기
#define IR_DETECT_LEVEL       LOW   // 센서가 "감지됨"일 때 주는 레벨 (실측 후 조정)
#define BREAKAWAY_KICK_DEG      4   // 스윕 시작 시 정지마찰(스티션)을 한 번에 넘기기 위한 초기 킥 각도
#define BREAKAWAY_SETTLE_MS    60   // 킥 이동 후 실제 도달까지 대기 (실측 후 조정)

// ---- 초기 재고 (Preferences에 저장된 값이 없을 때만 사용됨, 최초 1회 세팅용) ----
const uint8_t INITIAL_COUNT = 8;

// ---- 재고 영속화 (Preferences, ESP32의 NVS와 동일 개념) ----
Preferences prefs;
const char* PREFS_NAMESPACE = "warehouse";

// ---- 레일 상태 (부팅 시 Preferences에서 복원, 배출 성공마다 갱신+저장) ----
enum RailStatus : uint8_t { RAIL_IDLE, RAIL_BUSY, RAIL_EMPTY, RAIL_DISABLE };
struct RailState {
  uint8_t count;
  RailStatus status;
  String lastOrderId;
};
RailState rail[2][4]; // [글자슬롯][색상슬롯]

// 레일 하나의 Preferences 키 문자열 생성 (예: "E_r")
String railPrefsKey(int l, int c) {
  char letter = (l == 0) ? LETTER_0 : LETTER_1;
  char color = COLOR_LIST[c];
  return String(letter) + "_" + String(color);
}

// 부팅 시: 저장된 재고 복원. 저장된 값이 없으면(최초 1회) INITIAL_COUNT로 시작
void loadStockFromPrefs() {
  for (int l = 0; l < 2; l++) {
    for (int c = 0; c < 4; c++) {
      String key = railPrefsKey(l, c);
      uint8_t count = prefs.getUChar(key.c_str(), INITIAL_COUNT);
      rail[l][c].count = count;
      rail[l][c].status = (count == 0) ? RAIL_EMPTY : RAIL_IDLE;
    }
  }
}

// 재고가 바뀔 때마다 즉시 해당 레일 값만 저장 (배출마다 1건이라 쓰기 부담 적음)
void saveStockToPrefs(int l, int c) {
  String key = railPrefsKey(l, c);
  prefs.putUChar(key.c_str(), rail[l][c].count);
}

// 카트리지 실물 교체 후 시리얼로 "FACTORYRESET" 입력 시 호출됨
void factoryResetStock() {
  for (int l = 0; l < 2; l++) {
    for (int c = 0; c < 4; c++) {
      rail[l][c].count = INITIAL_COUNT;
      rail[l][c].status = RAIL_IDLE;
      saveStockToPrefs(l, c);
    }
  }
  Serial.println("[NVS] 공장 초기화 완료 - 재고를 INITIAL_COUNT로 되돌림");
}

// 시리얼 명령 처리 (지금은 FACTORYRESET 하나만 지원)
void checkSerialCommand() {
  if (!Serial.available()) return;
  String cmd = Serial.readStringUntil('\n');
  cmd.trim();
  if (cmd == "FACTORYRESET") {
    factoryResetStock();
  }
}

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
// DS_HOMING        : 복귀(0도) 명령 후 실제 도달까지 대기
// DS_BREAKAWAY_WAIT: 스윕 시작 전, 스티션을 넘기기 위한 초기 킥(BREAKAWAY_KICK_DEG)
//                    이동 후 실제 도달까지 짧게 대기
// DS_SWEEPING      : (홈+킥 각도)->180도, 1도씩 천천히 배출 스윕 (non-blocking)
// DS_HOLDING       : 180도(완전 배출) 위치에서 잠깐 유지
// DS_RETURNING     : 복귀(0도) 명령 후 IR 판정 전까지 대기
// DS_WAIT_IR       : IR 센서로 배출 성공/실패 판정
enum DispenseState { DS_IDLE, DS_HOMING, DS_BREAKAWAY_WAIT, DS_SWEEPING, DS_HOLDING, DS_RETURNING, DS_WAIT_IR };
DispenseState dsState = DS_IDLE;
DispenseJob   currentJob;
unsigned long dsTimer = 0;
int           sweepAngle = 0;   // 현재 진행 중인 스윕 각도 (0~180)

// ---- 마지막으로 처리한 cnt_process 스냅샷 seq (문자열 비교, 자릿수 제한 없음) ----
String lastSeqStr = "";

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

  // 재고 복원 (Preferences, ESP32 NVS와 동일 개념 - 재부팅해도 유지됨)
  prefs.begin(PREFS_NAMESPACE, false);
  loadStockFromPrefs();

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

  // TLS 인증서는 Arduino IDE "SSL 루트 인증서 업로드" 도구로 R4 WiFi 모듈에
  // 직접 구워 넣는 방식을 쓰므로, 코드에서 setCACert() 등을 호출할 필요 없음.

  mqttClient.setServer(MQTT_HOST, MQTT_PORT);
  mqttClient.setBufferSize(2048); // cnt_process 스냅샷이 기본 256바이트보다 큼
  mqttClient.setCallback(mqttCallback);

  Serial.print("[");
  Serial.print(AXIS_NAME);
  Serial.println("] setup 완료");
  Serial.println("[NVS] 복원된 재고:");
  for (int l = 0; l < 2; l++) {
    for (int c = 0; c < 4; c++) {
      Serial.print("  " + railPrefsKey(l, c) + " = " + String(rail[l][c].count));
      Serial.println(rail[l][c].status == RAIL_EMPTY ? " (empty)" : "");
    }
  }
  Serial.println("[안내] 카트리지 교체 후 재고 초기화하려면 시리얼로 FACTORYRESET 입력");
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

  // 문서 크기는 cnt_process 스냅샷 크기에 맞춰 조정 (필요시 늘리기)
  StaticJsonDocument<2048> doc;
  DeserializationError err = deserializeJson(doc, payload, length);
  if (err) {
    Serial.print("[MQTT] JSON 파싱 실패: ");
    Serial.println(err.c_str());
    return;
  }

  // seq는 정수로 안 받고 문자열로 비교 (자릿수 제한 없음 - 상단 changelog 참고)
  String seqStr = doc["seq"].as<String>();
  if (seqStr == lastSeqStr) return; // 동일 스냅샷 -> 재전송 무시
  lastSeqStr = seqStr;

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

// ============================== 배출 상태 머신 (90->180도 non-blocking 스윕 + 시작 스티션 킥) ==============================
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

    // 복귀: 현재 위치가 어디든 홈 각도로 한 번에 명령 (구버전과 동일 - 스텝 없이 최고 속도)
    // 이미 홈에 있는 상태(직전 사이클 종료 후)라면 이 명령은 물리적으로는 no-op임
    moveServoAngle(SERVO_CHANNEL[l][c], SERVO_HOME_ANGLE);
    dsTimer = millis();
    dsState = DS_HOMING;
    return;
  }

  if (dsState == DS_HOMING) {
    if (millis() - dsTimer < RETURN_DELAY_MS) return;

    int l = letterSlot(currentJob.letter);
    int c = colorSlot(currentJob.color);

    // 정지마찰(스티션) 대응: 1도씩 미세하게 밀기 시작하면 토크가 부족해
    // 제자리에서 부르르 떨다가 여러 스텝 누적된 뒤에야 갑자기 풀려나가는
    // 현상이 있었음 -> 시작 시 BREAKAWAY_KICK_DEG만큼 한 번에 확 밀어서
    // 마찰을 깔끔하게 넘긴 뒤, 그 지점부터 기존과 동일하게 1도씩 스윕
    sweepAngle = SERVO_HOME_ANGLE + BREAKAWAY_KICK_DEG;
    moveServoAngle(SERVO_CHANNEL[l][c], sweepAngle);
    dsTimer = millis();
    dsState = DS_BREAKAWAY_WAIT;
    return;
  }

  if (dsState == DS_BREAKAWAY_WAIT) {
    if (millis() - dsTimer < BREAKAWAY_SETTLE_MS) return;
    // 킥으로 마찰을 넘긴 지점(sweepAngle)부터 이어서 1도씩 스윕 시작
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
      // 홈+킥 각도 -> 180도 스윕 완료 (구버전과 동일하게 1도씩 천천히)
      dsTimer = millis();
      dsState = DS_HOLDING;
    }
    return;
  }

  if (dsState == DS_HOLDING) {
    if (millis() - dsTimer < DISPENSE_HOLD_MS) return;

    int l = letterSlot(currentJob.letter);
    int c = colorSlot(currentJob.color);
    // 복귀도 한 번에 점프 (구버전과 동일 - 이 버전은 복귀 시 "한 번 더 도는" 느낌이
    // 없다고 확인됐으므로 그대로 유지)
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
      saveStockToPrefs(l, c); // 재고가 실제로 바뀐 순간 즉시 영속화
      // 배출 결과 보고(cnt_keycap_dispense)는 성공했을 때만 전송
      reportDispenseResult(currentJob.orderId, currentJob.letter, currentJob.color);
    } else {
      rail[l][c].status = RAIL_DISABLE;
      // count는 안 바뀌었으므로 저장할 것 없음. disable은 일시적 상태로
      // 취급해 재부팅 시 count 기준(0이면 empty, 아니면 idle)으로 복원됨.
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
