#include "config.h"
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <PubSubClient.h>
#include <Preferences.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

// Arduino IDE가 자동 생성하는 프로토타입에서 아래 타입을 참조하므로 미리 선언한다.
struct Storage;
struct RobotArm;
struct PendingOrder;
struct PendingReplace;
struct DispenseJob;
struct DispenseGroup;
struct IdRing;

// ==================================================
// 시스템 개요
// ==================================================
// Mobius -> Flask -> HiveMQ(MQTT) -> ESP32(이 코드) -> 아두이노(서보) / Magic Box(로봇암)
//
// 실행 흐름
//   MQTT 콜백  : JSON 파싱 + 큐 적재만 (네트워크 I/O 없음)
//   loop()     : serviceDispatch()가 상태머신을 돌린다
//                IDLE -> RUNNING(배출 단계 -> 픽업 단계) -> WAITING_LOAD 또는 IDLE
//                WAITING_LOAD는 AGV의 cnt_load 신호로 IDLE로 돌아간다
//   배출 단계  : 항목 1개당 태스크 1개. 아두이노 5대에 동시에 HTTP 요청 (전 레일 동시 배출)
//                전부 끝나면 메인 루프가 재고 차감 + cnt_stock 즉시 업로드
//                하나라도 실패하면 cnt_dispense partial(detail=실패 레일 전부) 후 주문 중단 (픽업 안 함)
//   픽업 단계  : 로봇암 1대당 태스크 1개. 같은 팔을 쓰는 선반은 순차, 다른 팔은 병렬
//                픽업이 하나 실패하면 그 팔의 나머지 픽업은 중단
//   NVS        : 재고 + 큐 + 상태 + 중복방지 ID를 주문 단위로 저장 (플래시 수명 보호)
//
// 외부 인터페이스(변경 금지)
//   MQTT 토픽 3개(주문/교체/cnt_load), 아두이노 /dispense URL, Mobius cnt_dispense·cnt_stock payload

// ==================================================
// 1. 하드웨어 구성 (저장소 / 로봇암 / 레일 매핑)
// ==================================================
// 파일럿 단계에서는 storage 0,1만 enabled=true. 나머지는 배선 후 true로만 바꾸면 된다.
// 로봇암 1대가 선반 여러 개를 커버하면 그 선반들은 동시에 픽업할 수 없으므로
// 픽업 단계의 병렬 처리 단위는 "로봇암"이다. 파일럿(0,1 모두 armId 0)에서는 그룹이 하나라 실제로는 순차다.

struct Storage {
  const char* name;
  const char* ip;
  bool enabled;
  int  armId;         // 담당 로봇암 인덱스
  int  armStartPin;   // 이 선반 위치를 집으라고 Magic Box에 알리는 START 핀
};

const int NUM_STORAGES = 5;
const int STORAGE_BOARD = 4;

Storage storages[NUM_STORAGES] = {
  { "EI 선반",    BOARD_STORAGE0_IP, true,  0, 25 },
  { "NS 선반",    BOARD_STORAGE1_IP, true,  0, 26 },
  { "FT 선반",    BOARD_STORAGE2_IP, false, 0, 27 },
  { "JP 선반",    BOARD_STORAGE3_IP, false, 0, 32 },
  { "board 선반", BOARD_STORAGE4_IP, false, 1, 33 },
};

// DONE 핀은 입력 전용 GPIO(34~39) 사용. 내부 풀업/풀다운이 없으니 외부 풀다운 10kΩ 필수.
// Magic Box 쪽 DONE 펄스는 100ms 이상 유지 (10ms 폴링).
struct RobotArm {
  const char* name;
  int  donePin;
  bool enabled;    // Magic Box 실배선 + 티칭 완료 시 true
};

const int NUM_ARMS = 3;

RobotArm robotArms[NUM_ARMS] = {
  { "로봇암1(키캡)",  34, false },
  { "로봇암2(board)", 35, false },
  { "로봇암3(예비)",  36, false },
};

const unsigned long ARM_START_PULSE_MS   = 150;
const unsigned long ARM_DONE_TIMEOUT_MS  = 8000;
const unsigned long ARM_CLEAR_TIMEOUT_MS = 1500;
const int           ARM_POLL_MS          = 10;

// storage 0~3의 글자 쌍 (알파벳순, 2026-08-13 확정)
const int  NUM_LETTER_STORAGES = 4;
const char storageLetterPairs[NUM_LETTER_STORAGES][2] = {
  { 'E', 'I' }, { 'N', 'S' }, { 'F', 'T' }, { 'J', 'P' },
};

// 색상 축: r=0, y=1, b=2, g=3
// storage 0~3: rail = pairPos*4 + colorIdx (0~7),  storage 4(board): rail = colorIdx (0~3)
const int  NUM_COLORS = 4;
const char COLOR_CHARS[NUM_COLORS] = { 'r', 'y', 'b', 'g' };
const int  MAX_RAILS = 8;

// ==================================================
// 2. 동작 파라미터
// ==================================================

// 재고
const int LOW_STOCK_THRESHOLD = 1;
const int CARTRIDGE_CAPACITY  = 7;    // 테스트값. 바꾼 뒤에는 FACTORYRESET 필요
const int TOTAL_PER_RAIL      = 300;  // 레일당 예비재고 총량(임시값)

// 배출 / 픽업
const int  MAX_JOBS                 = 8;
const int  DISPENSE_TASK_STACK      = 8192;   // HTTPClient + 문자열 연결
const int  PICK_TASK_STACK          = 4096;   // GPIO + 로그만
const int  TASK_PRIO                = 1;
const int  SAME_ARM_GAP_MS          = 300;
const int  DISPENSE_RETRY_DELAY_MS  = 500;
const unsigned long SLOW_WARN_MS    = 25000;
const char* DISPENSE_FAIL_STATUS    = "partial";   // 플랫폼팀 확정 시 이 한 줄만 변경

// 큐 / 중복방지
const int ORDER_QUEUE_SIZE   = 10;
const int REPLACE_QUEUE_SIZE = 8;
const int RECENT_ID_SIZE     = 12;

// NVS
const char* NVS_NAMESPACE  = "warehouse";
const int   NVS_SCHEMA_VER = 2;   // 2: 상태 저장 포맷 통합(st). 구조가 바뀌면 올릴 것 (불일치 시 전체 초기화)
const size_t RUNTIME_STATE_DOC_SIZE = 4096;   // saveRuntimeState/loadRuntimeState 공용 풀 크기

// 네트워크
const unsigned long WIFI_CONNECT_TIMEOUT_MS   = 20000;
const unsigned long WIFI_RECONNECT_TIMEOUT_MS = 8000;
const unsigned long WIFI_RETRY_INTERVAL_MS    = 5000;
const unsigned long NTP_TIMEOUT_MS            = 15000;
const time_t        TIME_VALID_MIN            = 8 * 3600 * 2;   // 이보다 작으면 1970년 근처 = 미동기화
const unsigned long MQTT_BOOT_TIMEOUT_MS      = 30000;
const unsigned long MQTT_RETRY_INTERVAL_MS    = 3000;
const int           MQTT_BUFFER_SIZE          = 512;
const int           HTTP_CONNECT_TIMEOUT_MS   = 3000;
const int           HTTP_TIMEOUT_MS           = 8000;

// ==================================================
// 3. 공용 자료구조
// ==================================================
// ※ 템플릿/구조체 멤버 함수는 Arduino의 프로토타입 자동생성 대상이 아니므로
//    반드시 사용 지점보다 앞(이 절)에 정의돼 있어야 한다.

// 고정 크기 링큐. 주문 큐와 교체 큐가 같은 구현을 쓴다.
template <typename T, int N>
struct RingQueue {
  T   items[N];
  int head  = 0;
  int count = 0;

  bool empty() const { return count == 0; }
  bool push(const T& v) {
    if (count >= N) return false;
    items[(head + count) % N] = v;
    count++;
    return true;
  }
  bool pop(T& out) {
    if (count == 0) return false;
    out  = items[head];
    head = (head + 1) % N;
    count--;
    return true;
  }
  const T& at(int i) const { return items[(head + i) % N]; }
  void clear() { head = 0; count = 0; }
};

// 최근 처리한 ID 링버퍼 (MQTT QoS1 재전송 중복 방지). NVS에 칸 순서 + 다음 인덱스를 함께 저장한다.
struct IdRing {
  String ids[RECENT_ID_SIZE];
  int    next = 0;

  bool contains(const String& id) const {
    if (id.length() == 0) return false;
    for (int i = 0; i < RECENT_ID_SIZE; i++) {
      if (ids[i].length() > 0 && ids[i] == id) return true;
    }
    return false;
  }
  void remember(const String& id) {
    if (id.length() == 0) return;
    ids[next] = id;
    next = (next + 1) % RECENT_ID_SIZE;
  }
  void clear() {
    for (int i = 0; i < RECENT_ID_SIZE; i++) ids[i] = "";
    next = 0;
  }
  void save(JsonDocument& doc, const char* arrKey, const char* idxKey) const {
    doc[idxKey] = next;
    JsonArray a = doc.createNestedArray(arrKey);
    for (int i = 0; i < RECENT_ID_SIZE; i++) a.add(ids[i]);
  }
  void load(JsonDocument& doc, const char* arrKey, const char* idxKey) {
    clear();
    int i = 0;
    for (JsonVariant v : doc[arrKey].as<JsonArray>()) {
      if (i >= RECENT_ID_SIZE) break;
      ids[i++] = v.as<String>();
    }
    next = (doc[idxKey] | 0) % RECENT_ID_SIZE;
  }
};

struct PendingOrder {
  String orderId;
  String keycap;      // MBTI 4글자
  String colorsStr;   // keycap[i]에 대응하는 색상 첫 글자 (예: "rybg")
  String boardColor;  // board 색상 문자열
};

struct PendingReplace {
  int storageId;
  int rail;
};

enum JobFail { FAIL_NONE = 0, FAIL_NO_STOCK, FAIL_DISPENSE, FAIL_PICKUP };

// 한 주문의 배출 항목 하나 (키캡 1글자 또는 board 1개)
struct DispenseJob {
  int    storageId;
  int    rail;
  char   letter;      // 아두이노 URL/로그용 (board는 'B')
  String jobId;       // 멱등키. 재시도해도 같은 값
  JobFail fail;       // 메인 루프에서만 기록 (cnt_dispense detail용)
  volatile bool dispensed;   // ① 아두이노 배출 성공
  volatile bool picked;      // ② 로봇암 픽업 성공
  volatile bool done;        // 배출 단계 태스크 종료 표시
};

// 로봇암 1대에 배정된 Job 묶음 (픽업 단계). 태스크 1개가 순차 처리한다.
struct DispenseGroup {
  int  armId;
  int  jobIdx[MAX_JOBS];
  int  jobCount;
  volatile bool done;
};

// ==================================================
// 4. 전역 상태
// ==================================================

enum DispatchState {
  DISPATCH_IDLE = 0,          // 다음 주문을 시작할 수 있음
  DISPATCH_RUNNING = 1,       // 워커 태스크 실행 중 (NVS에 이 값이 남아 있으면 배출 중 재부팅된 것)
  DISPATCH_WAITING_LOAD = 2,  // AGV의 cnt_load 신호 대기 중
};

// RUNNING 안의 세부 단계. NVS에는 저장하지 않는다 (재부팅 시 RUNNING이면 어느 단계든 partial 처리).
enum DispatchPhase { PHASE_DISPENSE = 0, PHASE_PICK = 1 };

WiFiClientSecure secureClient;       // Mobius용 (메인 루프 전용, 의도적으로 setInsecure 유지)
WiFiClientSecure mqttSecureClient;   // HiveMQ용 (루트 CA 검증)
PubSubClient     mqttClient(mqttSecureClient);
Preferences      prefs;

int stock[NUM_STORAGES][MAX_RAILS];
int totalPool[NUM_STORAGES][MAX_RAILS];

DispatchState dispatchState = DISPATCH_IDLE;
DispatchPhase dispatchPhase = PHASE_DISPENSE;
String        currentOrderId = "";

RingQueue<PendingOrder,   ORDER_QUEUE_SIZE>   orderQueue;
RingQueue<PendingReplace, REPLACE_QUEUE_SIZE> replaceQueue;
IdRing recentOrderIds;
IdRing recentLoadIds;

DispenseJob   dispenseJobs[MAX_JOBS];
int           dispenseJobCount = 0;
DispenseGroup dispenseGroups[NUM_ARMS];
int           dispenseGroupCount = 0;
unsigned long dispatchStartMs = 0;
unsigned long lastSlowWarnMs  = 0;

// ==================================================
// 5. 전방 선언
// ==================================================

void printStock();
void saveStockToNVS();
void saveRuntimeState();
void syncStockToMobius();
void notifyDispenseStatus(const String& orderId, const char* status, bool withErrors = false);
bool triggerDispense(const DispenseJob& job);
bool triggerRobotArm(int storageId);
void processOrder(const String& conData);
void processReplaceRequest(const String& conData);
void processLoadConfirmation(const String& conData);
void beginDispatch(const PendingOrder& o);
void finishDispensePhase();
void finalizeDispatch();
void printStorageStatus();

// ==================================================
// 6. 레일 매핑 유틸
// ==================================================

int railCountOf(int storageId) {
  return (storageId == STORAGE_BOARD) ? NUM_COLORS : MAX_RAILS;
}

int letterToStorage(char letter) {
  letter = toupper(letter);
  for (int s = 0; s < NUM_LETTER_STORAGES; s++) {
    if (storageLetterPairs[s][0] == letter || storageLetterPairs[s][1] == letter) return s;
  }
  return -1;
}

// 색상 이름이 "blue"든 "b"든 첫 글자로 판별한다.
int colorToIndex(char c) {
  c = tolower(c);
  for (int i = 0; i < NUM_COLORS; i++) {
    if (COLOR_CHARS[i] == c) return i;
  }
  return -1;
}

// 저장소 + 글자 + 색상 -> 레일. board 저장소는 글자를 무시한다. 실패 시 -1.
int resolveRail(int storageId, char letter, int colorIdx) {
  if (colorIdx < 0 || colorIdx >= NUM_COLORS) return -1;
  if (storageId == STORAGE_BOARD) return colorIdx;
  if (storageId < 0 || storageId >= NUM_LETTER_STORAGES) return -1;

  letter = toupper(letter);
  int pairPos = -1;
  if (storageLetterPairs[storageId][0] == letter) pairPos = 0;
  else if (storageLetterPairs[storageId][1] == letter) pairPos = 1;
  if (pairPos < 0) return -1;

  return pairPos * NUM_COLORS + colorIdx;
}

bool isValidLocation(int storageId, int rail) {
  if (storageId < 0 || storageId >= NUM_STORAGES) return false;
  return rail >= 0 && rail < railCountOf(storageId);
}

String railLabel(int storageId, int rail) {
  if (!isValidLocation(storageId, rail)) return "?";
  if (storageId == STORAGE_BOARD) return String("board-") + COLOR_CHARS[rail];
  char letter = storageLetterPairs[storageId][rail / NUM_COLORS];
  return String(letter) + "-" + COLOR_CHARS[rail % NUM_COLORS];
}

String locationName(int storageId, int rail) {
  return String(storages[storageId].name) + " " + railLabel(storageId, rail);
}

// ==================================================
// 7. 재고
// ==================================================

void initStockDefaults() {
  for (int s = 0; s < NUM_STORAGES; s++) {
    for (int r = 0; r < MAX_RAILS; r++) {
      bool used = r < railCountOf(s);
      stock[s][r]     = used ? CARTRIDGE_CAPACITY : 0;
      totalPool[s][r] = used ? TOTAL_PER_RAIL - CARTRIDGE_CAPACITY : 0;
    }
  }
}

void printStock() {
  Serial.println("[STOCK] 현재 재고");
  for (int s = 0; s < NUM_STORAGES; s++) {
    if (!storages[s].enabled) continue;
    String line = String("  ") + storages[s].name + " | ";
    for (int r = 0; r < railCountOf(s); r++) {
      line += railLabel(s, r) + ":" + String(stock[s][r]) + "  ";
    }
    Serial.println(line);
  }
}

// 메인 루프에서만 호출. NVS 저장은 호출자가 주문/교체 단위로 한 번 수행한다.
void reduceStock(int storageId, int rail) {
  if (stock[storageId][rail] > 0) stock[storageId][rail]--;

  String item = locationName(storageId, rail);
  Serial.println("[STOCK] " + item + " -> " + String(stock[storageId][rail]) + "개");

  if (stock[storageId][rail] == 0) {
    Serial.println("[STOCK] " + item + " 재고 소진, 즉시 카트리지 교체 필요");
  } else if (stock[storageId][rail] <= LOW_STOCK_THRESHOLD) {
    Serial.println("[STOCK] " + item + " 재고 부족 경고");
  }
}

void replaceCartridge(int storageId, int rail) {
  String item = locationName(storageId, rail);

  if (stock[storageId][rail] > 0) {
    Serial.println("[REPLACE] " + item + " 아직 재고 남아있어 교체하지 않음 (0개일 때만 교체)");
    return;
  }
  if (totalPool[storageId][rail] == 0) {
    Serial.println("[REPLACE] " + item + " 총 재고 완전 소진, 리필 불가");
    return;
  }

  int refill = min(totalPool[storageId][rail], CARTRIDGE_CAPACITY);
  if (refill < CARTRIDGE_CAPACITY) {
    Serial.println("[REPLACE] " + item + " 예비재고 부족, 부분 리필");
  }
  stock[storageId][rail]      = refill;
  totalPool[storageId][rail] -= refill;

  Serial.println("[REPLACE] " + item + " 교체 완료 -> " + String(refill) + "개");

  saveStockToNVS();
  syncStockToMobius();
}

// ==================================================
// 8. NVS 저장 / 복원
// ==================================================
//   "schema" : 포맷 버전. 다르면 재고/상태 구분 없이 전체 초기화 (옛 큐·중복방지 ID 잔존 방지)
//   "stock", "pool" : 재고 배열 통째
//   "state"  : 상태머신 + 현재 주문 + 주문 큐 + 중복방지 ID (JSON 한 덩어리)

void saveStockToNVS() {
  prefs.putBytes("stock", stock, sizeof(stock));
  prefs.putBytes("pool", totalPool, sizeof(totalPool));
}

bool loadStockFromNVS() {
  if (prefs.getBytesLength("stock") != sizeof(stock)) return false;
  if (prefs.getBytesLength("pool") != sizeof(totalPool)) return false;
  prefs.getBytes("stock", stock, sizeof(stock));
  prefs.getBytes("pool", totalPool, sizeof(totalPool));
  return true;
}

void saveRuntimeState() {
  DynamicJsonDocument doc(RUNTIME_STATE_DOC_SIZE);

  doc["st"]  = (int)dispatchState;
  doc["cur"] = currentOrderId;

  // 중복방지 ID를 큐보다 먼저 쓴다. 풀이 넘치면 뒤에 넣은 큐 꼬리부터 잘리므로
  // 상태머신과 중복방지는 항상 온전하게 남고, 유실은 최신 주문 몇 건으로 한정된다.
  recentOrderIds.save(doc, "ro", "roi");
  recentLoadIds.save(doc, "rl", "rli");

  JsonArray q = doc.createNestedArray("q");
  for (int i = 0; i < orderQueue.count; i++) {
    const PendingOrder& o = orderQueue.at(i);
    JsonObject e = q.createNestedObject();
    e["i"] = o.orderId;
    e["k"] = o.keycap;
    e["c"] = o.colorsStr;
    e["b"] = o.boardColor;
  }

  String s;
  serializeJson(doc, s);
  if (doc.overflowed()) {
    int savedQ = doc["q"].as<JsonArray>().size();
    Serial.println("[NVS] !! 상태 저장 용량 초과 - 큐 " + String(orderQueue.count) + "건 중 " +
                   String(savedQ) + "건만 저장됨, RUNTIME_STATE_DOC_SIZE 키울 것");
    for (int i = savedQ; i < orderQueue.count; i++) {
      Serial.println("[NVS]    재부팅 시 유실될 주문: " + orderQueue.at(i).orderId);
    }
  }
  if (prefs.putString("state", s) == 0) {
    Serial.println("[NVS] !! 상태 저장 실패 (NVS 쓰기 오류)");
  }
}

bool loadRuntimeState() {
  String s = prefs.getString("state", "");
  if (s.length() == 0) return false;

  DynamicJsonDocument doc(RUNTIME_STATE_DOC_SIZE);
  if (deserializeJson(doc, s)) {
    Serial.println("[NVS] 저장된 상태 파싱 실패 - 무시하고 새로 시작");
    return false;
  }

  int st = doc["st"] | 0;
  dispatchState  = (st >= 0 && st <= DISPATCH_WAITING_LOAD) ? (DispatchState)st : DISPATCH_IDLE;
  currentOrderId = doc["cur"] | "";

  orderQueue.clear();
  for (JsonObject e : doc["q"].as<JsonArray>()) {
    PendingOrder o;
    o.orderId    = e["i"] | "";
    o.keycap     = e["k"] | "";
    o.colorsStr  = e["c"] | "";
    o.boardColor = e["b"] | "";
    if (!orderQueue.push(o)) break;
  }

  recentOrderIds.load(doc, "ro", "roi");
  recentLoadIds.load(doc, "rl", "rli");
  return true;
}

void resetRuntimeState() {
  dispatchState  = DISPATCH_IDLE;
  dispatchPhase  = PHASE_DISPENSE;
  currentOrderId = "";
  orderQueue.clear();
  replaceQueue.clear();
  recentOrderIds.clear();
  recentLoadIds.clear();
}

// NVS를 비우고 재고/상태를 기본값으로 되돌려 저장한다. 스키마 변경 시와 FACTORYRESET에서 공용.
void applyFactoryDefaults() {
  prefs.clear();
  initStockDefaults();
  resetRuntimeState();
  prefs.putInt("schema", NVS_SCHEMA_VER);
  saveStockToNVS();
  saveRuntimeState();
}

// 카트리지를 전부 새로 채웠을 때 / CARTRIDGE_CAPACITY를 바꿨을 때 사용
void factoryReset() {
  applyFactoryDefaults();
  Serial.println("[NVS] 공장 초기화 완료 - 재고를 카트리지 용량으로 되돌림");
  printStock();
  syncStockToMobius();
}

void initPersistence() {
  prefs.begin(NVS_NAMESPACE, false);

  if (prefs.getInt("schema", 0) != NVS_SCHEMA_VER) {
    Serial.println("[NVS] 스키마 변경 감지 - 저장 데이터 전체 초기화");
    applyFactoryDefaults();
    printStock();
    return;
  }

  if (loadStockFromNVS()) {
    Serial.println("[NVS] 저장된 재고 복원 완료");
  } else {
    Serial.println("[NVS] 저장된 재고 없음 - 기본값으로 초기화");
    initStockDefaults();
    saveStockToNVS();
  }

  if (loadRuntimeState()) {
    Serial.println("[NVS] 상태 복원 - 대기 주문 " + String(orderQueue.count) + "개, AGV 대기 " +
                   String(dispatchState == DISPATCH_WAITING_LOAD ? "예" : "아니오"));
  } else {
    Serial.println("[NVS] 저장된 상태 없음 - 빈 큐로 시작");
    saveRuntimeState();
  }

  printStock();
}

// 배출 도중 재부팅되면 NVS에 RUNNING이 남는다. 실물은 나갔는데 재고 차감은 안 됐을 수 있다.
// 어느 레일에서 끊겼는지는 알 수 없으므로 detail 없이 partial만 올린다.
void recoverInterruptedDispatch() {
  if (dispatchState != DISPATCH_RUNNING) return;

  Serial.println();
  Serial.println("[복구] 이전 배출이 완료되지 않은 채 재부팅됨: " + currentOrderId);
  Serial.println("[복구] 실물 배출분과 저장된 재고가 어긋났을 수 있음 - 선반/배출구 실물 확인 권장");

  notifyDispenseStatus(currentOrderId, DISPENSE_FAIL_STATUS);
  dispatchState = DISPATCH_IDLE;   // 어느 단계에서 끊겼는지 알 수 없으므로 대기 없이 큐 재개
  saveRuntimeState();
}

// ==================================================
// 9. 워커 태스크 (배출 단계 / 픽업 단계)
// ==================================================
// 전역 재고·NVS는 여기서 절대 건드리지 않는다. 결과는 job 플래그에만 쓴다.

// 한 번 실패하면 잠깐 쉬고 한 번 더 시도한다. (템플릿이라 사용 지점보다 앞에 있어야 함)
template <typename F>
bool attemptTwice(F action, const String& retryLog) {
  if (action()) return true;
  Serial.println(retryLog);
  vTaskDelay(pdMS_TO_TICKS(DISPENSE_RETRY_DELAY_MS));
  return action();
}

// [배출 단계] 항목 1개당 태스크 1개. 아두이노가 선반마다 따로라 병렬로 쏴도 충돌 없다.
// (MBTI 한 주문은 선반당 최대 1개라 같은 아두이노에 두 요청이 겹치지 않는다)
// 배출은 job_id 멱등 처리 덕분에 재요청해도 실물이 두 번 안 나간다.
// ※ 아두이노는 같은 job_id가 다시 오면 서보를 돌리지 않고 200을 돌려줘야 한다.
void dispenseTask(void* param) {
  int j = (int)(intptr_t)param;
  DispenseJob& job = dispenseJobs[j];

  job.dispensed = attemptTwice(
    [&]() { return triggerDispense(job); },
    "[RETRY] " + job.jobId + " 배출 실패 - 동일 job_id로 1회 재시도");
  if (!job.dispensed) Serial.println("[DISPENSE] " + job.jobId + " 배출 최종 실패");

  Serial.println("[TASK] dispense " + String(j) + " 스택 여유: " +
                 String(uxTaskGetStackHighWaterMark(NULL)) + " bytes");
  job.done = true;
  vTaskDelete(NULL);
}

// [픽업 단계] 로봇암 1대당 태스크 1개. 같은 팔은 순차, 다른 팔은 병렬.
// 픽업 실패는 이미 나온 물건을 다시 집는 것이라 배출 없이 픽업만 재시도한다.
// 한 번 최종 실패하면 그 팔의 나머지 픽업은 시도하지 않는다 (배출구 상태를 알 수 없으므로).
void pickWorkerTask(void* param) {
  int g = (int)(intptr_t)param;
  DispenseGroup& group = dispenseGroups[g];

  for (int i = 0; i < group.jobCount; i++) {
    DispenseJob& job = dispenseJobs[group.jobIdx[i]];
    job.picked = attemptTwice(
      [&]() { return triggerRobotArm(job.storageId); },
      "[RETRY] " + job.jobId + " 픽업 실패 - 1회 재시도");
    if (!job.picked) {
      Serial.println("[ARM] " + job.jobId + " 픽업 최종 실패 - 배출구에 물건이 남아있을 수 있음, 이 팔의 나머지 픽업 중단");
      break;
    }
    if (i < group.jobCount - 1) vTaskDelay(pdMS_TO_TICKS(SAME_ARM_GAP_MS));
  }

  Serial.println("[TASK] pick group " + String(g) + " 스택 여유: " +
                 String(uxTaskGetStackHighWaterMark(NULL)) + " bytes");
  group.done = true;
  vTaskDelete(NULL);
}

// ==================================================
// 10. 배출 계획 / 단계 전이 (메인 루프)
// ==================================================

// 배출 항목 하나를 Job으로 등록. 비활성 저장소는 파일럿 범위 밖이라 집계에서도 제외한다.
void addDispenseJob(int storageId, int rail, char letter) {
  if (dispenseJobCount >= MAX_JOBS) return;

  String tag = (storageId == STORAGE_BOARD) ? String("board") : String(letter);

  if (!isValidLocation(storageId, rail)) {
    Serial.println("[DISPENSE] " + tag + " 매핑 실패 (글자/색상 확인), 스킵");
    return;
  }
  if (!storages[storageId].enabled) {
    Serial.println("[DISPENSE] " + tag + " -> " + storages[storageId].name + " 비활성, 스킵 (파일럿 범위 아님)");
    return;
  }

  DispenseJob& job = dispenseJobs[dispenseJobCount];
  job.storageId = storageId;
  job.rail      = rail;
  job.letter    = letter;
  job.jobId     = currentOrderId + "_" + String(dispenseJobCount);
  job.fail      = (stock[storageId][rail] > 0) ? FAIL_NONE : FAIL_NO_STOCK;
  job.dispensed = false;
  job.picked    = false;
  job.done      = false;

  Serial.println("[DISPENSE] " + tag + " -> " + storages[storageId].name +
                 " rail " + String(rail) + " (" + railLabel(storageId, rail) + ")" +
                 (job.fail == FAIL_NO_STOCK ? "  !! 재고 없음" : ""));
  dispenseJobCount++;
}

bool allJobsDone() {
  for (int j = 0; j < dispenseJobCount; j++) {
    if (!dispenseJobs[j].done) return false;
  }
  return true;
}

// Job을 로봇암 기준으로 묶는다 (픽업 단계용). 같은 팔은 한 그룹에서 순차 처리된다.
void buildDispenseGroups() {
  dispenseGroupCount = 0;
  for (int j = 0; j < dispenseJobCount; j++) {
    int armId  = storages[dispenseJobs[j].storageId].armId;
    int target = -1;
    for (int g = 0; g < dispenseGroupCount; g++) {
      if (dispenseGroups[g].armId == armId) { target = g; break; }
    }
    if (target < 0) {
      // done이 volatile이라 구조체 통째 대입 대신 필드별로 넣는다
      target = dispenseGroupCount++;
      dispenseGroups[target].armId    = armId;
      dispenseGroups[target].jobCount = 0;
      dispenseGroups[target].done     = false;
    }
    DispenseGroup& group = dispenseGroups[target];
    group.jobIdx[group.jobCount++] = j;
  }
}

bool allGroupsDone() {
  for (int g = 0; g < dispenseGroupCount; g++) {
    if (!dispenseGroups[g].done) return false;
  }
  return true;
}

// 주문을 닫고 다음 상태를 정한다. 바구니에 뭔가 들어갔으면 AGV를 기다리고, 아니면 바로 다음 주문.
void closeDispatch(int pickedCount) {
  if (pickedCount > 0) {
    dispatchState = DISPATCH_WAITING_LOAD;
    Serial.println("[DISPATCH] AGV cnt_load 신호 대기 중");
  } else {
    dispatchState = DISPATCH_IDLE;
    Serial.println("[DISPATCH] 바구니에 실린 항목 없음 - AGV 대기 없이 다음 주문 진행");
  }
  dispenseJobCount   = 0;
  dispenseGroupCount = 0;
  saveRuntimeState();
}

void beginDispatch(const PendingOrder& o) {
  currentOrderId   = o.orderId;
  dispenseJobCount = 0;

  Serial.println();
  Serial.println("[DISPATCH] " + o.orderId + " 배출 계획 | keycap=" + o.keycap +
                 " | colors=" + o.colorsStr + " | board=" + o.boardColor);

  for (int i = 0; i < o.keycap.length(); i++) {
    char letter    = toupper(o.keycap.charAt(i));
    char colorChar = (i < o.colorsStr.length()) ? o.colorsStr.charAt(i) : '?';
    int  storageId = letterToStorage(letter);
    if (storageId < 0) continue;   // 정의되지 않은 글자(오타 등)
    addDispenseJob(storageId, resolveRail(storageId, letter, colorToIndex(colorChar)), letter);
  }

  if (o.boardColor.length() > 0) {
    int colorIdx = colorToIndex(o.boardColor.charAt(0));
    addDispenseJob(STORAGE_BOARD, resolveRail(STORAGE_BOARD, 'B', colorIdx), 'B');
  }

  // 배출할 게 없으면 (전부 비활성/매핑 실패) 실패로 닫는다
  if (dispenseJobCount == 0) {
    Serial.println("[DISPATCH] 배출 가능한 항목 없음 - " + String(DISPENSE_FAIL_STATUS) + " 처리");
    notifyDispenseStatus(currentOrderId, DISPENSE_FAIL_STATUS);
    closeDispatch(0);
    return;
  }

  // 재고가 하나라도 없으면 아무것도 배출하지 않는다 (배출구에 고아 부품이 남지 않게).
  // 플랫폼이 재고 0 주문을 막더라도 cnt_stock 유실 등으로 어긋날 수 있어 여기서 한 번 더 막는다.
  bool anyNoStock = false;
  for (int j = 0; j < dispenseJobCount; j++) {
    if (dispenseJobs[j].fail == FAIL_NO_STOCK) {
      anyNoStock = true;
      Serial.println("[DISPATCH] " + locationName(dispenseJobs[j].storageId, dispenseJobs[j].rail) + " 재고 없음");
    }
  }
  if (anyNoStock) {
    Serial.println("[DISPATCH] 주문 전체 중단, 배출 안 함");
    notifyDispenseStatus(currentOrderId, DISPENSE_FAIL_STATUS, true);
    closeDispatch(0);
    return;
  }

  dispatchState   = DISPATCH_RUNNING;
  dispatchPhase   = PHASE_DISPENSE;
  dispatchStartMs = millis();
  lastSlowWarnMs  = dispatchStartMs;
  saveRuntimeState();   // 이 시점 이후 재부팅되면 부팅 시 RUNNING이 남아 미완료로 감지된다

  Serial.println("[DISPATCH] 배출 단계 시작 - " + String(dispenseJobCount) + "개 레일 동시 배출 (heap " +
                 String(ESP.getFreeHeap()) + " bytes)");

  for (int j = 0; j < dispenseJobCount; j++) {
    char taskName[24];
    snprintf(taskName, sizeof(taskName), "dispense_%d", j);
    BaseType_t ok = xTaskCreatePinnedToCore(dispenseTask, taskName, DISPENSE_TASK_STACK,
                                            (void*)(intptr_t)j, TASK_PRIO, NULL, tskNO_AFFINITY);
    if (ok != pdPASS) {
      Serial.println("[DISPATCH] 배출 태스크 생성 실패 (job " + String(j) + ") - 실패 처리");
      dispenseJobs[j].done = true;   // dispensed는 false로 남아 실패로 집계
    }
  }

  notifyDispenseStatus(currentOrderId, "started");   // 태스크를 띄운 뒤 보내야 POST 대기가 배출을 늦추지 않는다
}

// 배출 단계 종료. 재고를 즉시 반영하고, 하나라도 실패했으면 여기서 주문을 닫는다.
void finishDispensePhase() {
  int dispensedCount = 0;
  int failCount = 0;
  for (int j = 0; j < dispenseJobCount; j++) {
    if (dispenseJobs[j].dispensed) {
      reduceStock(dispenseJobs[j].storageId, dispenseJobs[j].rail);
      dispensedCount++;
    } else {
      dispenseJobs[j].fail = FAIL_DISPENSE;
      failCount++;
    }
  }

  // 실물이 나간 순간 재고가 줄었으므로 픽업 결과와 무관하게 지금 반영한다
  saveStockToNVS();
  printStock();
  syncStockToMobius();

  Serial.println("[DISPATCH] 배출 단계 결과 " + String(dispensedCount) + "/" + String(dispenseJobCount) +
                 " (소요 " + String(millis() - dispatchStartMs) + "ms)");

  if (failCount > 0) {
    Serial.println("[DISPATCH] 배출 실패 " + String(failCount) + "건 - 주문 전체 중단, 픽업 생략");
    if (dispensedCount > 0) {
      Serial.println("[DISPATCH] !! 이미 배출된 " + String(dispensedCount) + "개가 배출구에 남아 있음 - 수동 회수 필요");
    }
    notifyDispenseStatus(currentOrderId, DISPENSE_FAIL_STATUS, true);
    closeDispatch(0);   // 바구니에 실린 게 없으니 AGV를 기다리지 않는다
    return;
  }

  // 전부 배출됨 -> 픽업 단계
  buildDispenseGroups();
  dispatchPhase = PHASE_PICK;

  for (int g = 0; g < dispenseGroupCount; g++) {
    char taskName[24];
    snprintf(taskName, sizeof(taskName), "pick_%d", g);
    BaseType_t ok = xTaskCreatePinnedToCore(pickWorkerTask, taskName, PICK_TASK_STACK,
                                            (void*)(intptr_t)g, TASK_PRIO, NULL, tskNO_AFFINITY);
    if (ok != pdPASS) {
      Serial.println("[DISPATCH] 픽업 태스크 생성 실패 (group " + String(g) + ") - 해당 그룹 실패 처리");
      dispenseGroups[g].done = true;
    }
  }

  Serial.println("[DISPATCH] 픽업 단계 시작 - 로봇암 " + String(dispenseGroupCount) +
                 "대 (같은 팔 담당분은 순차)");
}

// 픽업 단계 종료. 재고는 배출 단계에서 이미 반영됐으므로 여기서는 건드리지 않는다.
void finalizeDispatch() {
  int pickedCount = 0;
  int failCount = 0;
  for (int j = 0; j < dispenseJobCount; j++) {
    if (dispenseJobs[j].picked) {
      pickedCount++;
    } else {
      dispenseJobs[j].fail = FAIL_PICKUP;
      failCount++;
    }
  }

  Serial.println("[DISPATCH] " + currentOrderId + " 픽업 결과 " + String(pickedCount) + "/" +
                 String(dispenseJobCount) + " (총 소요 " + String(millis() - dispatchStartMs) + "ms)");

  if (failCount == 0) {
    notifyDispenseStatus(currentOrderId, "done");
  } else {
    Serial.println("[DISPATCH] 픽업 실패 " + String(failCount) + "건");
    notifyDispenseStatus(currentOrderId, DISPENSE_FAIL_STATUS, true);
  }

  closeDispatch(pickedCount);
}

// 상태머신 한 스텝. loop()에서 매번 호출된다.
void serviceDispatch() {
  switch (dispatchState) {
    case DISPATCH_RUNNING:
      if (dispatchPhase == PHASE_DISPENSE) {
        if (allJobsDone()) { finishDispensePhase(); break; }
      } else {
        if (allGroupsDone()) { finalizeDispatch(); break; }
      }
      if (millis() - lastSlowWarnMs > SLOW_WARN_MS) {
        lastSlowWarnMs = millis();
        Serial.println("[DISPATCH] " + String(dispatchPhase == PHASE_DISPENSE ? "배출" : "픽업") + " 단계가 " +
                       String((millis() - dispatchStartMs) / 1000) + "초째 진행 중 - 응답 지연 확인 필요");
      }
      break;

    case DISPATCH_WAITING_LOAD:
      break;   // cnt_load 수신 시 processLoadConfirmation()이 IDLE로 되돌린다

    case DISPATCH_IDLE: {
      PendingOrder next;
      if (orderQueue.pop(next)) {
        Serial.println("[QUEUE] 대기열에서 " + next.orderId + " 꺼냄 (남은 대기: " + String(orderQueue.count) + "개)");
        beginDispatch(next);
      }
      break;
    }
  }
}

// 교체는 재고 배열을 건드리므로 배출 중이 아닐 때만 처리한다.
void serviceReplaceQueue() {
  if (dispatchState == DISPATCH_RUNNING) return;
  PendingReplace req;
  if (replaceQueue.pop(req)) {
    Serial.println("[REPLACE] 처리 시작 storage=" + String(req.storageId) + " rail=" + String(req.rail));
    replaceCartridge(req.storageId, req.rail);
  }
}

// ==================================================
// 11. 아두이노 / 로봇암 트리거 (워커 태스크에서 호출)
// ==================================================
// HTTPClient는 함수 안에서 생성해 태스크 간 안전하다.

bool triggerDispense(const DispenseJob& job) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[DISPENSE] Wi-Fi 미연결");
    return false;
  }

  String url = "http://" + String(storages[job.storageId].ip) +
               "/dispense?storage_id=" + String(job.storageId) +
               "&rail=" + String(job.rail) +
               "&letter=" + String(job.letter) +
               "&count=1" +
               "&job_id=" + job.jobId;
  Serial.println("[DISPENSE] 요청 -> " + url);

  HTTPClient http;
  http.setConnectTimeout(HTTP_CONNECT_TIMEOUT_MS);
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.begin(url);
  int httpCode = http.GET();

  bool ok = (httpCode == 200);
  if (ok) Serial.println("[DISPENSE] 완료 -> " + String(job.letter) + " | " + http.getString());
  else    Serial.println("[DISPENSE] 실패 -> " + String(job.letter) + " | HTTP " + String(httpCode));

  http.end();
  return ok;
}

// ESP32 armStartPin(출력) -> Magic Box 입력 : 선반마다 별도 핀
// Magic Box 출력 -> ESP32 donePin(입력)     : 로봇암마다 1개
// 로봇암 1대당 태스크가 1개라 같은 DONE 핀을 두 태스크가 동시에 읽는 일은 없다.
bool triggerRobotArm(int storageId) {
  const Storage&  storage = storages[storageId];
  const RobotArm& arm     = robotArms[storage.armId];

  if (!arm.enabled) {
    Serial.println("[ARM] " + String(arm.name) + " 비활성 - 트리거 생략 (성공 처리)");
    return true;
  }

  // 이전 동작의 DONE이 아직 HIGH면 오판하므로 내려갈 때까지 대기
  unsigned long t0 = millis();
  while (digitalRead(arm.donePin) == HIGH) {
    if (millis() - t0 > ARM_CLEAR_TIMEOUT_MS) {
      Serial.println("[ARM] " + String(arm.name) + " DONE이 계속 HIGH - 로봇암 상태 확인 필요");
      return false;
    }
    vTaskDelay(pdMS_TO_TICKS(ARM_POLL_MS));
  }

  digitalWrite(storage.armStartPin, HIGH);
  vTaskDelay(pdMS_TO_TICKS(ARM_START_PULSE_MS));
  digitalWrite(storage.armStartPin, LOW);
  Serial.println("[ARM] " + String(arm.name) + " <- " + storage.name +
                 " 픽업 START (pin " + String(storage.armStartPin) + "), DONE 대기");

  t0 = millis();
  while (millis() - t0 < ARM_DONE_TIMEOUT_MS) {
    if (digitalRead(arm.donePin) == HIGH) {
      Serial.println("[ARM] " + String(arm.name) + " DONE 수신 (" + String(millis() - t0) + "ms)");
      return true;
    }
    vTaskDelay(pdMS_TO_TICKS(ARM_POLL_MS));
  }

  Serial.println("[ARM] " + String(arm.name) + " DONE 타임아웃 (" + String(ARM_DONE_TIMEOUT_MS) + "ms) - 픽업 실패");
  return false;
}

// ==================================================
// 12. Mobius(COSS) 업로드 (메인 루프 전용)
// ==================================================

String generateRI() {
  const char hex[] = "0123456789abcdef";
  String ri;
  for (int i = 0; i < 16; i++) ri += hex[random(0, 16)];
  return ri;
}

// 컨테이너에 cin 하나를 POST한다. con은 이미 직렬화된 JSON 문자열.
bool postToMobius(const char* container, const String& con, const String& what) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[COSS] " + what + " 생략 - Wi-Fi 미연결");
    return false;
  }

  DynamicJsonDocument cinDoc(con.length() + 128);
  cinDoc["m2m:cin"]["con"] = con;
  String body;
  serializeJson(cinDoc, body);

  HTTPClient http;
  http.begin(secureClient, String(COSS_HOST) + "/Mobius/" + AE_NAME + "/" + container);
  http.addHeader("X-M2M-Origin", COSS_ORIGIN);
  http.addHeader("X-M2M-RI", generateRI());
  http.addHeader("X-M2M-RVI", "3");
  http.addHeader("X-API-KEY", COSS_API_KEY);
  http.addHeader("X-AUTH-CUSTOM-CREATOR", COSS_CREATOR);
  http.addHeader("X-AUTH-CUSTOM-LECTURE", COSS_LECTURE);
  http.addHeader("Content-Type", "application/vnd.onem2m-res+json;ty=4");
  http.addHeader("Accept", "application/json");

  int httpCode = http.POST(body);
  http.end();

  bool ok = (httpCode == 200 || httpCode == 201);
  if (ok) Serial.println("[COSS] " + what + " 완료");
  else    Serial.println("[COSS] " + what + " 실패 - HTTP " + String(httpCode));
  return ok;
}

// cnt_dispense (창고팀 내부용 detail):
//   {"order_id":..., "status":"started"|"done"|"partial", "detail":[{"storage_id":n,"rail":n}, ...]}
//   detail = 실패한 레일 전부 (1~5개). 실패가 없으면(started/done) 필드 자체를 생략한다.
void notifyDispenseStatus(const String& orderId, const char* status, bool withErrors) {
  StaticJsonDocument<512> doc;
  doc["order_id"] = orderId;
  doc["status"]   = status;

  String what = "cnt_dispense 업로드 " + orderId + " (status=" + status;

  if (withErrors) {
    JsonArray detail = doc.createNestedArray("detail");
    for (int j = 0; j < dispenseJobCount; j++) {
      const DispenseJob& job = dispenseJobs[j];
      if (job.fail == FAIL_NONE) continue;
      JsonObject e = detail.createNestedObject();
      e["storage_id"] = job.storageId;
      e["rail"]       = job.rail;
      what += ", " + railLabel(job.storageId, job.rail);
    }
  }

  String con;
  serializeJson(doc, con);
  postToMobius(CNT_DISPENSE_NAME, con, what + ")");
}

// cnt_stock: {"keycap":{글자:{색상:n}}, "board":{색상:n}}  비활성 저장소는 0으로 표기
void syncStockToMobius() {
  StaticJsonDocument<1024> doc;
  JsonObject keycapObj = doc.createNestedObject("keycap");
  for (int s = 0; s < NUM_LETTER_STORAGES; s++) {
    for (int pairPos = 0; pairPos < 2; pairPos++) {
      JsonObject letterObj = keycapObj.createNestedObject(String(storageLetterPairs[s][pairPos]));
      for (int c = 0; c < NUM_COLORS; c++) {
        letterObj[String(COLOR_CHARS[c])] = storages[s].enabled ? stock[s][pairPos * NUM_COLORS + c] : 0;
      }
    }
  }
  JsonObject boardObj = doc.createNestedObject("board");
  for (int c = 0; c < NUM_COLORS; c++) {
    boardObj[String(COLOR_CHARS[c])] = storages[STORAGE_BOARD].enabled ? stock[STORAGE_BOARD][c] : 0;
  }

  String con;
  serializeJson(doc, con);
  postToMobius(CNT_STOCK_NAME, con, "cnt_stock 동기화");
}

// ==================================================
// 13. MQTT 수신 처리 (파싱 + 큐 적재만. 실제 작업은 loop()에서)
// ==================================================

void processOrder(const String& conData) {
  StaticJsonDocument<512> doc;
  if (deserializeJson(doc, conData)) {
    Serial.println("[ORDER] 주문 파싱 실패");
    return;
  }

  PendingOrder o;
  o.orderId    = doc["order_id"].as<String>();
  o.keycap     = doc["keycap"].as<String>();
  o.boardColor = doc["board"] | "";
  for (JsonVariant v : doc["colors"].as<JsonArray>()) {
    String c = v.as<String>();
    o.colorsStr += (c.length() > 0) ? c.charAt(0) : '?';
  }

  if (o.orderId.length() == 0) {
    Serial.println("[ORDER] order_id 없음 - 중복 판별 불가, 그대로 진행");
  } else if (recentOrderIds.contains(o.orderId)) {
    Serial.println("[ORDER] " + o.orderId + " 는 이미 접수된 주문, 무시 (중복 수신)");
    return;
  }

  Serial.println();
  Serial.println("[ORDER] " + o.orderId + " | keycap=" + o.keycap + " | colors=" + o.colorsStr + " | board=" + o.boardColor);

  if (!orderQueue.push(o)) {
    Serial.println("[QUEUE] 큐가 가득 참 - 주문 " + o.orderId + " 유실, 확인 필요");
    return;
  }
  Serial.println("[QUEUE] " + o.orderId + " 대기열에 추가 (현재 대기: " + String(orderQueue.count) + "개)");
  recentOrderIds.remember(o.orderId);
  saveRuntimeState();
}

// 교체는 관리자가 실물 교체 후 누르는 것이라 재부팅 시 유실돼도 재요청 가능 -> NVS 저장 안 함
void processReplaceRequest(const String& conData) {
  StaticJsonDocument<256> doc;
  if (deserializeJson(doc, conData)) {
    Serial.println("[REPLACE] 요청 데이터 파싱 실패");
    return;
  }

  PendingReplace req = { doc["storage_id"] | -1, doc["rail"] | -1 };
  if (!isValidLocation(req.storageId, req.rail)) {
    Serial.println("[REPLACE] 잘못된 저장소/레일 (storage=" + String(req.storageId) + ", rail=" + String(req.rail) + ")");
    return;
  }

  Serial.println("[REPLACE] 요청 접수 storage=" + String(req.storageId) + " rail=" + String(req.rail));
  if (!replaceQueue.push(req)) {
    Serial.println("[REPLACE] 교체 요청 큐가 가득 참 - 요청 유실");
  }
}

void processLoadConfirmation(const String& conData) {
  StaticJsonDocument<256> doc;
  String loadedOrderId = "";
  if (!deserializeJson(doc, conData)) loadedOrderId = doc["order_id"] | "";

  Serial.println("[LOAD] cnt_load 수신 (order_id=" + loadedOrderId + ")");

  if (recentLoadIds.contains(loadedOrderId)) {
    Serial.println("[LOAD] 이미 처리한 cnt_load, 무시 (중복 수신)");
    return;
  }
  recentLoadIds.remember(loadedOrderId);

  if (dispatchState == DISPATCH_WAITING_LOAD) {
    dispatchState = DISPATCH_IDLE;
    Serial.println("[LOAD] 대기 해제 - 다음 주문 진행 가능");
  } else {
    Serial.println("[LOAD] AGV 대기 상태가 아닌데 cnt_load 수신 - 무시 (순서 확인 필요)");
  }
  saveRuntimeState();
}

void mqttCallback(char* topic, byte* payload, unsigned int length) {
  String message;
  message.reserve(length);
  for (unsigned int i = 0; i < length; i++) message += (char)payload[i];

  String topicStr(topic);
  Serial.println("[MQTT] 수신 topic=" + topicStr);
  Serial.println("[MQTT] payload=" + message);

  if      (topicStr == MQTT_TOPIC)         processOrder(message);
  else if (topicStr == MQTT_REPLACE_TOPIC) processReplaceRequest(message);
  else if (topicStr == MQTT_LOAD_TOPIC)    processLoadConfirmation(message);
  else Serial.println("[MQTT] 알 수 없는 토픽, 무시");
}

// ==================================================
// 14. 네트워크 연결 관리
// ==================================================

bool waitForWiFi(unsigned long timeoutMs, unsigned long pollMs, bool printDots) {
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < timeoutMs) {
    delay(pollMs);
    if (printDots) Serial.print(".");
  }
  return WiFi.status() == WL_CONNECTED;
}

void logWiFiResult(bool ok, const char* okMsg, const char* failMsg) {
  if (ok) Serial.println(String(okMsg) + " | IP: " + WiFi.localIP().toString());
  else    Serial.println(failMsg);
}

void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("[WIFI] 연결 중");
  bool ok = waitForWiFi(WIFI_CONNECT_TIMEOUT_MS, 500, true);
  Serial.println();
  logWiFiResult(ok, "[WIFI] 연결 완료", "[WIFI] 연결 실패");
}

void ensureWiFi() {
  static unsigned long lastRetry = 0;
  if (WiFi.status() == WL_CONNECTED) return;
  if (dispatchState == DISPATCH_RUNNING) return;   // 배출 중 재연결하면 진행 중인 요청이 깨진다
  if (millis() - lastRetry < WIFI_RETRY_INTERVAL_MS) return;
  lastRetry = millis();

  Serial.println("[WIFI] 재연결 시도");
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  bool ok = waitForWiFi(WIFI_RECONNECT_TIMEOUT_MS, 250, false);
  logWiFiResult(ok, "[WIFI] 재연결 완료", "[WIFI] 재연결 실패");
}

bool timeIsSynced() {
  return time(nullptr) >= TIME_VALID_MIN;
}

// MQTT TLS 인증서 검증에 필요. 시계가 안 맞으면 유효기간 검사가 실패해 MQTT 연결 자체가 안 된다.
void syncTime() {
  configTime(9 * 3600, 0, "pool.ntp.org", "time.google.com");   // KST
  Serial.print("[TIME] NTP 동기화 중");
  unsigned long start = millis();
  while (!timeIsSynced() && millis() - start < NTP_TIMEOUT_MS) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();

  if (timeIsSynced()) {
    time_t now = time(nullptr);
    Serial.print("[TIME] 동기화 완료: ");
    Serial.print(ctime(&now));
  } else {
    Serial.println("[TIME] 동기화 실패 - MQTT 인증서 검증에 필요, 실패 시 MQTT 연결이 안 됨");
  }
}

// 한 번만 시도하고 결과를 돌려준다 (블로킹 없음)
bool tryConnectMQTT() {
  if (!timeIsSynced()) {
    Serial.println("[MQTT] 시간 미동기화 - 인증서 검증 불가, NTP 대기 중");
    return false;
  }

  Serial.print("[MQTT] 브로커 연결 시도...");
  String clientId = "esp32-warehouse-" + String(random(0xffff), HEX);
  if (!mqttClient.connect(clientId.c_str(), MQTT_USERNAME, MQTT_PASSWORD)) {
    Serial.println(" 실패, rc=" + String(mqttClient.state()));
    return false;
  }

  Serial.println(" 성공");
  mqttClient.subscribe(MQTT_TOPIC);
  mqttClient.subscribe(MQTT_REPLACE_TOPIC);
  mqttClient.subscribe(MQTT_LOAD_TOPIC);
  Serial.println("[MQTT] 구독 시작: " + String(MQTT_TOPIC) + ", " + MQTT_REPLACE_TOPIC + ", " + MQTT_LOAD_TOPIC);
  return true;
}

// 부팅 시에만 최대 MQTT_BOOT_TIMEOUT_MS까지 기다린다. 이후는 ensureMQTT()가 비블로킹으로 재시도.
void connectMQTTBlocking() {
  unsigned long start = millis();
  while (!tryConnectMQTT() && millis() - start < MQTT_BOOT_TIMEOUT_MS) {
    delay(MQTT_RETRY_INTERVAL_MS);
  }
  if (!mqttClient.connected()) Serial.println("[MQTT] 초기 연결 실패 - loop에서 계속 재시도");
}

// 브로커가 죽어 있어도 loop()가 계속 돌아야 serviceDispatch()가 멈추지 않는다.
void ensureMQTT() {
  static unsigned long lastRetry = 0;
  if (mqttClient.connected()) {
    mqttClient.loop();
    return;
  }
  if (millis() - lastRetry >= MQTT_RETRY_INTERVAL_MS) {
    lastRetry = millis();
    tryConnectMQTT();
  }
}

// ==================================================
// 15. 시리얼 명령 (STATUS / TEST E|I|N|S / FACTORYRESET)
// ==================================================

void printStorageStatus() {
  Serial.println("[STORAGE] 연결 상태 (파일럿: 0,1만 ON)");
  for (int i = 0; i < NUM_STORAGES; i++) {
    const Storage& s = storages[i];
    Serial.println("  " + String(i) + " | " + s.name + " | " + s.ip + " | " + (s.enabled ? "ON " : "OFF") +
                   " | " + robotArms[s.armId].name + " | START pin " + String(s.armStartPin));
  }
  Serial.println("[ARM] 로봇암 상태");
  for (int a = 0; a < NUM_ARMS; a++) {
    Serial.println("  " + String(robotArms[a].name) + " | DONE pin " + String(robotArms[a].donePin) +
                   " | " + (robotArms[a].enabled ? "ON" : "OFF"));
  }
}

void printSystemStatus() {
  printStorageStatus();
  printStock();
  Serial.println("현재 주문: " + (currentOrderId.length() > 0 ? currentOrderId : "(없음)"));
  String stateStr = "대기";
  if (dispatchState == DISPATCH_RUNNING) {
    stateStr = (dispatchPhase == PHASE_DISPENSE) ? "배출 단계 진행 중" : "픽업 단계 진행 중";
  }
  Serial.println("배출 상태: " + stateStr);
  Serial.println("MQTT 연결: " + String(mqttClient.connected() ? "연결됨" : "끊김"));
  Serial.println("AGV cnt_load 대기 중: " + String(dispatchState == DISPATCH_WAITING_LOAD ? "예" : "아니오"));
  Serial.println("대기열(큐)에 쌓인 주문: " + String(orderQueue.count) + "개");
  Serial.println("대기 중인 교체 요청: " + String(replaceQueue.count) + "개");
  Serial.println("NVS 스키마 버전: " + String(prefs.getInt("schema", 0)));
  Serial.println("NVS 여유 엔트리: " + String(prefs.freeEntries()));
  Serial.println("Free heap: " + String(ESP.getFreeHeap()) + " bytes");
}

// 재고를 건드리지 않고 실물만 배출한다. job_id에 millis()를 섞어 멱등 캐시에 걸리지 않게 한다.
void runTestDispense(char letter) {
  int storageId = letterToStorage(letter);
  int rail      = resolveRail(storageId, letter, 0);   // 색상은 항상 r
  if (storageId < 0 || rail < 0) {
    Serial.println("[TEST] E/I/N/S만 가능 (파일럿 범위)");
    return;
  }
  if (!storages[storageId].enabled) {
    Serial.println("[TEST] " + String(storages[storageId].name) + " 비활성 상태");
    return;
  }

  DispenseJob job;
  job.storageId = storageId;
  job.rail      = rail;
  job.letter    = letter;
  job.jobId     = "test_" + String(millis());
  job.fail      = FAIL_NONE;
  job.dispensed = false;
  job.picked    = false;
  job.done      = false;

  if (triggerDispense(job)) triggerRobotArm(storageId);
}

void checkSerialCommand() {
  if (!Serial.available()) return;
  String input = Serial.readStringUntil('\n');
  input.trim();
  input.toUpperCase();
  if (input.length() == 0) return;

  bool running = (dispatchState == DISPATCH_RUNNING);

  if (input == "STATUS") {
    printSystemStatus();
  } else if (input == "FACTORYRESET") {
    if (running) Serial.println("[NVS] 배출 진행 중에는 초기화할 수 없음");
    else factoryReset();
  } else if (input.startsWith("TEST ") && input.length() >= 6) {
    if (running) Serial.println("[TEST] 배출 진행 중에는 테스트 명령을 실행하지 않음");
    else runTestDispense(input.charAt(5));
  } else {
    Serial.println("[SYSTEM] 사용 가능: STATUS, TEST E, TEST I, TEST N, TEST S, FACTORYRESET");
  }
}

// ==================================================
// 16. setup / loop
// ==================================================

void initRobotArmPins() {
  for (int s = 0; s < NUM_STORAGES; s++) {
    pinMode(storages[s].armStartPin, OUTPUT);
    digitalWrite(storages[s].armStartPin, LOW);
  }
  for (int a = 0; a < NUM_ARMS; a++) pinMode(robotArms[a].donePin, INPUT);
  Serial.println("[ARM] GPIO 초기화 완료 (START 5개 / DONE 3개)");
}

void setup() {
  Serial.begin(115200);
  randomSeed(analogRead(0));

  initRobotArmPins();
  initPersistence();
  connectWiFi();
  syncTime();

  secureClient.setInsecure();
  delay(1000);

  recoverInterruptedDispatch();
  syncStockToMobius();

  mqttSecureClient.setCACert(HIVEMQ_ROOT_CA);
  mqttClient.setServer(MQTT_BROKER, MQTT_PORT);
  mqttClient.setCallback(mqttCallback);
  mqttClient.setBufferSize(MQTT_BUFFER_SIZE);
  connectMQTTBlocking();

  printStorageStatus();
  Serial.println("[SYSTEM] MQTT 구독 기반 주문 수신 - 36레일(색상 반영) 구조, AGV cnt_load 큐 방식");
  Serial.println("[SYSTEM] 배출: 전 레일 동시 배출 -> 즉시 cnt_stock -> 로봇암 픽업. 하나라도 실패 시 주문 중단");
  Serial.println("[SYSTEM] MQTT는 TLS 인증서 검증 적용 (HiveMQ 루트 CA)");
  Serial.println("[SYSTEM] 명령: STATUS, TEST E/I/N/S, FACTORYRESET");
}

void loop() {
  ensureWiFi();
  checkSerialCommand();
  if (WiFi.status() == WL_CONNECTED) ensureMQTT();
  serviceDispatch();
  serviceReplaceQueue();
  delay(10);
}
