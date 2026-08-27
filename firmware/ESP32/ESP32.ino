#include "config.h"
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <PubSubClient.h>
#include <Preferences.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

struct Storage;
struct RobotArm;
struct PendingOrder;
struct PendingReplace;
struct DispenseJob;
struct DispenseGroup;

// ==================================================
// [2026-08-27 리팩토링 요약]
//  1) order_id 중복 처리 방지 (MQTT QoS1 재전송 대비)
//  2) board 배출을 키캡과 동일한 배출 경로로 편입 -> cnt_dispense 성공 판정에 포함
//  3) 아두이노 동시 배출 + MQTT 콜백 논블로킹화
//     - MQTT 콜백은 파싱/큐 적재만 하고 즉시 리턴 (네트워크 I/O 없음)
//     - 실제 배출은 loop()의 상태머신(serviceDispatch)이 구동
//     - 배출 HTTP 요청은 아두이노(IP) 단위로 FreeRTOS 태스크를 띄워 병렬 실행
//  4) NVS(Preferences) 영속화
//     - 재고/예비재고, 주문 큐, AGV 대기 상태, 중복방지 ID를 플래시에 저장
//     - 재부팅해도 복원되며, 배출 도중 재부팅된 경우 경고 + partial 보고
//     - 플래시 수명을 위해 "주문 단위"로만 기록 (레일 하나 배출할 때마다 쓰지 않음)
//     - FACTORYRESET 시리얼 명령으로 공장 초기화 가능
//  5) cnt_dispense_start 폐지 (플랫폼팀 요청)
//     - cnt_dispense 한 컨테이너에 status="started"/"done"으로 통합
//     - config.h의 CNT_DISPENSE_START_NAME 정의는 이제 사용하지 않음 (삭제 가능)
// ==================================================

WiFiClientSecure secureClient;       // Mobius(COSS) 통신용 (메인 루프 전용)

WiFiClientSecure mqttSecureClient;   // MQTT(HiveMQ) 통신용, Mobius용과 분리
PubSubClient mqttClient(mqttSecureClient);

Preferences prefs;                   // NVS 핸들 (setup에서 열고 계속 유지)

String lastOrderRI   = "";
String lastReplaceRI = "";
String currentOrderId = "";

// ==================================================
// 0. 전방 선언
// ==================================================

void printStock();
void syncStockToMobius();
void replaceCartridge(int storageId, int rail);
bool triggerDispense(int storageId, int rail, char letter, int count, const String& jobId);
bool triggerRobotArm(int storageId);
void notifyDispenseStatus(String orderId, const char* status);
void processOrder(String conData);
void processReplaceRequest(String conData);
void processLoadConfirmation(String conData);
void printStorageStatus();
void checkSerialCommand();
void serviceDispatch();
void serviceReplaceQueue();
void saveStockToNVS();
void saveRuntimeState();
void initStockDefaults();
void initRobotArmPins();

// ==================================================
// 1. 저장소(storage) 설정
// ==================================================
// 5개 구조는 유지하되, 파일럿 단계에서는 0,1번만 enabled=true.
// 나중에 나머지 선반이 배선되면 해당 항목만 enabled=true로 바꾸면 됨.

// 아두이노는 선반당 1대(총 5대), 로봇암은 3대 구성.
//   로봇암1 : 키캡 선반 4개(EI/NS/FT/JP) 전부 커버
//   로봇암2 : board 선반
//   로봇암3 : 트레이 이동 전담 (배출 관여 없음 -> 여기서는 다루지 않음)
//
// [중요] 로봇암 1대가 선반 여러 개를 커버하면 그 선반들은 동시에 픽업할 수 없다.
//        그래서 병렬 처리 단위를 "아두이노"가 아니라 "로봇암"으로 잡는다.
//        같은 armId끼리는 순차, 다른 armId끼리는 병렬로 실행된다.
//
// 나중에 로봇암1을 둘로 쪼개면(EI/NS용 1대 + FT/JP용 1대)
// storage 2,3의 armId를 2로 바꾸고 robotArms[2].enabled를 켜면 끝.

struct Storage {
  const char* name;
  const char* ip;
  bool enabled;
  int  armId;         // 이 선반을 담당하는 로봇암 인덱스 (0-based)
  int  armStartPin;   // 이 선반 위치를 집으라고 Magic Box에 알리는 START 핀
};

const int NUM_STORAGES = 5;
const int STORAGE_BOARD = 4;   // board(축4개 결합체)

// ESP32 GPIO 배정 (실배선에 맞춰 조정할 것)
#define ARM_START_PIN_S0  25
#define ARM_START_PIN_S1  26
#define ARM_START_PIN_S2  27
#define ARM_START_PIN_S3  32
#define ARM_START_PIN_S4  33

Storage storages[NUM_STORAGES] = {
  { "EI 선반",    BOARD_STORAGE0_IP, true,  0, ARM_START_PIN_S0 },  // 파일럿 대상
  { "NS 선반",    BOARD_STORAGE1_IP, true,  0, ARM_START_PIN_S1 },  // 파일럿 대상
  { "FT 선반",    BOARD_STORAGE2_IP, false, 0, ARM_START_PIN_S2 },  // 미배선
  { "JP 선반",    BOARD_STORAGE3_IP, false, 0, ARM_START_PIN_S3 },  // 미배선
  { "board 선반", BOARD_STORAGE4_IP, false, 1, ARM_START_PIN_S4 },  // 미배선
};

// ---------- 로봇암 ----------
// DONE 핀은 입력 전용이라 ESP32의 입력 전용 GPIO(34~39)를 써도 된다.
// Magic Box 출력이 3.3V라 레벨시프터 없이 직결 가능.

#define ARM1_DONE_PIN 34
#define ARM2_DONE_PIN 35
#define ARM3_DONE_PIN 36   // 로봇암1 분할 시 사용할 예비

const int NUM_ARMS = 3;

struct RobotArm {
  const char* name;
  int  donePin;
  bool enabled;    // Magic Box 실배선 + 티칭 완료 시 true
};

RobotArm robotArms[NUM_ARMS] = {
  { "로봇암1(키캡)",  ARM1_DONE_PIN, false },
  { "로봇암2(board)", ARM2_DONE_PIN, false },
  { "로봇암3(예비)",  ARM3_DONE_PIN, false },
};

const unsigned long ARM_START_PULSE_MS  = 150;    // START 신호 유지 시간
const unsigned long ARM_DONE_TIMEOUT_MS = 8000;   // DONE 대기 최대 시간
const unsigned long ARM_CLEAR_TIMEOUT_MS = 1500;  // 이전 DONE이 내려가길 기다리는 시간

// pairPos0 = 쌍의 첫글자, pairPos1 = 둘째글자 (알파벳순 정렬, 2026-08-13 최종 확정)
const char storageLetterPairs[4][2] = {
  { 'E', 'I' },   // storage 0
  { 'N', 'S' },   // storage 1
  { 'F', 'T' },   // storage 2 (미배선)
  { 'J', 'P' },   // storage 3 (미배선)
};

// ==================================================
// 2. NVS(Preferences) 설정
// ==================================================
// 네임스페이스 하나에 아래 키를 저장한다.
//   "schema" : 저장 포맷 버전 (구조가 바뀌면 올려서 옛 데이터 무시)
//   "stock"  : 로드된 재고 배열 통째 (putBytes)
//   "pool"   : 예비재고 배열 통째 (putBytes)
//   "state"  : 큐 + AGV 대기상태 + 중복방지 ID (JSON 문자열)
//
// 키 이름은 NVS 제약상 15자 이내여야 한다.

const char* NVS_NAMESPACE   = "warehouse";
const int   NVS_SCHEMA_VER  = 1;   // 레일 구조/필드가 바뀌면 이 값을 올릴 것

// cnt_dispense의 실패 상태값 (2026-08-27)
// 플랫폼팀이 started/done 두 가지만 처리한다면 "done"으로 바꾸면 되고,
// 일부 실패를 구분해서 받겠다고 하면 "partial" 그대로 두면 된다.
const char* DISPENSE_FAIL_STATUS = "partial";

bool dispatchInterrupted = false;  // 배출 도중 재부팅됐는지 (복원 시 판별용)

// ==================================================
// 3. 재고 관리 (색상 축 반영, 2026-08-26 확정)
// ==================================================
// storage 0~3: 레일 0~7 = (글자 2개 x 색상 4개[r,y,b,g])
//   rail = pairPos*4 + colorIdx  (pairPos: 0=쌍의 첫글자, 1=둘째글자)
// storage 4(board): 레일 0~3 = 색상 4개[r,y,b,g]만 (글자 축 없음)
//   rail = colorIdx
// 색상 인덱스: r=0, y=1, b=2, g=3

const int LOW_STOCK_THRESHOLD = 1;

// 카트리지 용량 - 테스트값. 실제 운영값 확정되면 이 한 줄만 바꾸면 전체 반영됨.
// ※ 이 값을 바꾼 뒤에는 FACTORYRESET을 해야 NVS에 저장된 옛 재고가 새 값으로 갱신됨
const int CARTRIDGE_CAPACITY  = 7;
const int TOTAL_PER_RAIL      = 300; // 레일당 예비재고 총량(임시값, 한 곳만 바꾸면 됨)

const int MAX_RAILS = 8;   // storage 0~3용 최대 레일 수 (배열 크기 기준)

int stock[NUM_STORAGES][MAX_RAILS];
int totalPool[NUM_STORAGES][MAX_RAILS];

int railCountOf(int storageId) {
  return (storageId == STORAGE_BOARD) ? 4 : 8;
}

int letterToStorage(char letter) {
  letter = toupper(letter);
  for (int s = 0; s < 4; s++) {
    if (storageLetterPairs[s][0] == letter || storageLetterPairs[s][1] == letter) {
      return s;
    }
  }
  return -1;
}

// 글자가 쌍 안에서 몇 번째인지 (0=첫글자, 1=둘째글자)
int letterToPairPos(char letter, int storageId) {
  if (storageId < 0 || storageId >= 4) return -1;
  letter = toupper(letter);
  if (storageLetterPairs[storageId][0] == letter) return 0;
  if (storageLetterPairs[storageId][1] == letter) return 1;
  return -1;
}

// 색상 문자/문자열 -> 색상 인덱스 (r=0,y=1,b=2,g=3)
// 색상 이름이 전체 단어("blue")로 오든 한 글자("b")로 오든 첫 글자만 보고 판별한다.
int colorCharToIndex(char c) {
  switch (tolower(c)) {
    case 'r': return 0;
    case 'y': return 1;
    case 'b': return 2;
    case 'g': return 3;
  }
  return -1;
}

int colorStringToIndex(String colorStr) {
  if (colorStr.length() == 0) return -1;
  return colorCharToIndex(colorStr.charAt(0));
}

char colorIndexToChar(int idx) {
  const char colors[] = { 'r', 'y', 'b', 'g' };
  if (idx < 0 || idx >= 4) return '?';
  return colors[idx];
}

// 글자+색상 -> storage 0~3의 rail(0~7)
int letterColorToRail(char letter, int colorIdx, int storageId) {
  int pairPos = letterToPairPos(letter, storageId);
  if (pairPos < 0 || colorIdx < 0 || colorIdx >= 4) return -1;
  return pairPos * 4 + colorIdx;
}

// 색상 -> storage 4(board)의 rail(0~3)
int boardColorToRail(int colorIdx) {
  if (colorIdx < 0 || colorIdx >= 4) return -1;
  return colorIdx;
}

String railLabel(int storageId, int rail) {
  if (storageId == STORAGE_BOARD) {
    if (rail < 0 || rail >= 4) return "?";
    return String("board-") + colorIndexToChar(rail);
  }
  if (storageId < 0 || storageId >= 4 || rail < 0 || rail >= 8) return "?";
  int pairPos = rail / 4;
  int colorIdx = rail % 4;
  char letter = storageLetterPairs[storageId][pairPos];
  return String(letter) + "-" + colorIndexToChar(colorIdx);
}

bool isValidLocation(int storageId, int rail) {
  if (storageId < 0 || storageId >= NUM_STORAGES) return false;
  if (rail < 0 || rail >= railCountOf(storageId)) return false;
  return true;
}

// 최초 부팅(또는 FACTORYRESET) 시에만 호출 - 메모리 배열만 채운다
void initStockDefaults() {
  for (int s = 0; s < NUM_STORAGES; s++) {
    for (int r = 0; r < MAX_RAILS; r++) {
      stock[s][r] = 0;
      totalPool[s][r] = 0;
    }
    for (int r = 0; r < railCountOf(s); r++) {
      stock[s][r] = CARTRIDGE_CAPACITY;
      totalPool[s][r] = TOTAL_PER_RAIL - CARTRIDGE_CAPACITY;
    }
  }
}

void printStock() {
  Serial.println("[STOCK] 현재 재고");
  for (int s = 0; s < NUM_STORAGES; s++) {
    if (!storages[s].enabled) continue;   // 파일럿 단계라 비활성 저장소는 출력 생략
    Serial.print("  ");
    Serial.print(storages[s].name);
    Serial.print(" | ");
    for (int r = 0; r < railCountOf(s); r++) {
      Serial.print(railLabel(s, r));
      Serial.print(":");
      Serial.print(stock[s][r]);
      Serial.print("  ");
    }
    Serial.println();
  }
}

bool hasStock(int storageId, int rail, int needed) {
  if (!isValidLocation(storageId, rail)) {
    Serial.println("[ERROR] 잘못된 저장소/레일");
    return false;
  }
  return stock[storageId][rail] >= needed;
}

// 주의 1: 재고 갱신은 반드시 메인 루프(배출 태스크 전부 종료 후)에서만 호출한다.
// 주의 2: 여기서 NVS에 쓰지 않는다. 플래시 수명 보호를 위해
//        주문 단위(finalizeDispatch) 또는 교체 단위(replaceCartridge)로 한 번만 저장한다.
void reduceStock(int storageId, int rail, int count) {
  stock[storageId][rail] -= count;
  if (stock[storageId][rail] < 0) stock[storageId][rail] = 0;

  String item = String(storages[storageId].name) + " " + railLabel(storageId, rail);
  Serial.print("[STOCK] ");
  Serial.print(item);
  Serial.print(" -> ");
  Serial.print(stock[storageId][rail]);
  Serial.println("개");

  if (stock[storageId][rail] == 0) {
    Serial.println("[STOCK] " + item + " 재고 소진, 즉시 카트리지 교체 필요");
  } else if (stock[storageId][rail] <= LOW_STOCK_THRESHOLD) {
    Serial.println("[STOCK] " + item + " 재고 부족 경고");
  }
}

void replaceCartridge(int storageId, int rail) {
  if (!isValidLocation(storageId, rail)) {
    Serial.println("[REPLACE] 잘못된 저장소/레일");
    return;
  }

  String item = String(storages[storageId].name) + " " + railLabel(storageId, rail);

  if (stock[storageId][rail] > 0) {
    Serial.println("[REPLACE] " + item + " 아직 재고 남아있어 교체하지 않음 (0개일 때만 교체)");
    return;
  }

  if (totalPool[storageId][rail] >= CARTRIDGE_CAPACITY) {
    totalPool[storageId][rail] -= CARTRIDGE_CAPACITY;
    stock[storageId][rail] = CARTRIDGE_CAPACITY;
  } else if (totalPool[storageId][rail] > 0) {
    stock[storageId][rail] = totalPool[storageId][rail];
    totalPool[storageId][rail] = 0;
    Serial.println("[REPLACE] " + item + " 예비재고 부족, 부분 리필");
  } else {
    Serial.println("[REPLACE] " + item + " 총 재고 완전 소진, 리필 불가");
    return;
  }

  Serial.print("[REPLACE] ");
  Serial.print(item);
  Serial.print(" 교체 완료 -> ");
  Serial.print(stock[storageId][rail]);
  Serial.println("개");

  saveStockToNVS();
  syncStockToMobius();
}

// ==================================================
// 4. 중복 메시지 방지 (MQTT QoS1 재전송 대비, 2026-08-27 추가)
// ==================================================
// HiveMQ -> ESP32 구간은 QoS1이라 같은 메시지가 두 번 도착할 수 있다.
// 이미 처리한 order_id가 다시 오면 무시해서 중복 배출을 막는다.
// 이 기록도 NVS에 함께 저장되므로 재부팅 후에도 중복 방지가 유지된다.

const int RECENT_ID_SIZE = 12;

String recentOrderIds[RECENT_ID_SIZE];
int recentOrderIdx = 0;

String recentLoadIds[RECENT_ID_SIZE];
int recentLoadIdx = 0;

bool isKnownId(String* ring, const String& id) {
  if (id.length() == 0) return false;
  for (int i = 0; i < RECENT_ID_SIZE; i++) {
    if (ring[i].length() > 0 && ring[i] == id) return true;
  }
  return false;
}

void rememberId(String* ring, int& idx, const String& id) {
  if (id.length() == 0) return;
  ring[idx] = id;
  idx = (idx + 1) % RECENT_ID_SIZE;
}

// ==================================================
// 5. 주문 큐 (AGV cnt_load 신호 대기, 2026-08-27 확정)
// ==================================================
// AGV팀이 바구니 탑재 완료(cnt_load)를 알려주기 전까지는 다음 주문 배출을 시작하지 않는다.
// MQTT 콜백은 항상 큐에 넣기만 하고, 실제 배출 시작 여부는 serviceDispatch()가 판단한다.

const int ORDER_QUEUE_SIZE = 10;

struct PendingOrder {
  String orderId;
  String keycap;
  String colorsStr;
  String boardColor;
};

PendingOrder orderQueue[ORDER_QUEUE_SIZE];
int queueHead = 0;
int queueTail = 0;
int queueCount = 0;

bool waitingForLoad = false;   // true면 AGV의 cnt_load 신호를 기다리는 중 (다음 주문 보류)

bool enqueueOrder(const PendingOrder& o) {
  if (queueCount >= ORDER_QUEUE_SIZE) {
    Serial.println("[QUEUE] 큐가 가득 참 - 주문 " + o.orderId + " 유실, 확인 필요");
    return false;
  }
  orderQueue[queueTail] = o;
  queueTail = (queueTail + 1) % ORDER_QUEUE_SIZE;
  queueCount++;
  Serial.println("[QUEUE] " + o.orderId + " 대기열에 추가 (현재 대기: " + String(queueCount) + "개)");
  return true;
}

bool dequeueOrder(PendingOrder& out) {
  if (queueCount == 0) return false;
  out = orderQueue[queueHead];
  queueHead = (queueHead + 1) % ORDER_QUEUE_SIZE;
  queueCount--;
  return true;
}

// ==================================================
// 6. 교체 요청 큐 (2026-08-27 추가)
// ==================================================
// replaceCartridge()는 내부에서 NVS 쓰기 + cnt_stock POST(블로킹)를 하므로
// MQTT 콜백에서 바로 실행하지 않고 큐에 넣어 두었다가
// 배출이 진행 중이 아닐 때 메인 루프에서 처리한다.
// (교체 요청은 관리자가 실물 교체 후 누르는 것이라 재부팅 시 유실돼도 재요청 가능 -> NVS 저장 안 함)

const int REPLACE_QUEUE_SIZE = 8;

struct PendingReplace {
  int storageId;
  int rail;
};

PendingReplace replaceQueue[REPLACE_QUEUE_SIZE];
int replaceHead = 0;
int replaceTail = 0;
int replaceCount = 0;

bool enqueueReplace(int storageId, int rail) {
  if (replaceCount >= REPLACE_QUEUE_SIZE) {
    Serial.println("[REPLACE] 교체 요청 큐가 가득 참 - 요청 유실");
    return false;
  }
  replaceQueue[replaceTail].storageId = storageId;
  replaceQueue[replaceTail].rail = rail;
  replaceTail = (replaceTail + 1) % REPLACE_QUEUE_SIZE;
  replaceCount++;
  return true;
}

bool dequeueReplace(PendingReplace& out) {
  if (replaceCount == 0) return false;
  out = replaceQueue[replaceHead];
  replaceHead = (replaceHead + 1) % REPLACE_QUEUE_SIZE;
  replaceCount--;
  return true;
}

// ==================================================
// 7. NVS 저장 / 복원
// ==================================================

void saveStockToNVS() {
  prefs.putBytes("stock", stock, sizeof(stock));
  prefs.putBytes("pool", totalPool, sizeof(totalPool));
}

bool loadStockFromNVS() {
  if (prefs.getInt("schema", 0) != NVS_SCHEMA_VER) return false;
  if (prefs.getBytesLength("stock") != sizeof(stock)) return false;
  if (prefs.getBytesLength("pool") != sizeof(totalPool)) return false;

  prefs.getBytes("stock", stock, sizeof(stock));
  prefs.getBytes("pool", totalPool, sizeof(totalPool));
  return true;
}

// 큐 + AGV 대기상태 + 중복방지 ID + 배출진행 여부를 한 덩어리로 저장
// (여러 키로 쪼개서 쓰면 그만큼 플래시 쓰기 횟수가 늘어나므로 한 번에 묶음)
void saveRuntimeState() {
  DynamicJsonDocument doc(2560);

  doc["wfl"] = waitingForLoad;
  doc["cur"] = currentOrderId;
  doc["run"] = dispatchInterrupted;   // 배출 시작~완료 사이에 true

  JsonArray q = doc.createNestedArray("q");
  for (int i = 0; i < queueCount; i++) {
    int idx = (queueHead + i) % ORDER_QUEUE_SIZE;
    JsonObject e = q.createNestedObject();
    e["i"] = orderQueue[idx].orderId;
    e["k"] = orderQueue[idx].keycap;
    e["c"] = orderQueue[idx].colorsStr;
    e["b"] = orderQueue[idx].boardColor;
  }

  JsonArray ro = doc.createNestedArray("ro");
  for (int i = 0; i < RECENT_ID_SIZE; i++) {
    if (recentOrderIds[i].length() > 0) ro.add(recentOrderIds[i]);
  }

  JsonArray rl = doc.createNestedArray("rl");
  for (int i = 0; i < RECENT_ID_SIZE; i++) {
    if (recentLoadIds[i].length() > 0) rl.add(recentLoadIds[i]);
  }

  String s;
  serializeJson(doc, s);
  prefs.putString("state", s);
}

bool loadRuntimeState() {
  String s = prefs.getString("state", "");
  if (s.length() == 0) return false;

  DynamicJsonDocument doc(2560);
  if (deserializeJson(doc, s)) {
    Serial.println("[NVS] 저장된 상태 파싱 실패 - 무시하고 새로 시작");
    return false;
  }

  waitingForLoad      = doc["wfl"] | false;
  currentOrderId      = doc["cur"] | "";
  dispatchInterrupted = doc["run"] | false;

  queueHead = queueTail = queueCount = 0;
  for (JsonObject e : doc["q"].as<JsonArray>()) {
    if (queueCount >= ORDER_QUEUE_SIZE) break;
    orderQueue[queueTail].orderId    = e["i"] | "";
    orderQueue[queueTail].keycap     = e["k"] | "";
    orderQueue[queueTail].colorsStr  = e["c"] | "";
    orderQueue[queueTail].boardColor = e["b"] | "";
    queueTail = (queueTail + 1) % ORDER_QUEUE_SIZE;
    queueCount++;
  }

  for (int i = 0; i < RECENT_ID_SIZE; i++) {
    recentOrderIds[i] = "";
    recentLoadIds[i]  = "";
  }

  recentOrderIdx = 0;
  for (JsonVariant v : doc["ro"].as<JsonArray>()) {
    if (recentOrderIdx >= RECENT_ID_SIZE) break;
    recentOrderIds[recentOrderIdx++] = v.as<String>();
  }
  recentOrderIdx %= RECENT_ID_SIZE;

  recentLoadIdx = 0;
  for (JsonVariant v : doc["rl"].as<JsonArray>()) {
    if (recentLoadIdx >= RECENT_ID_SIZE) break;
    recentLoadIds[recentLoadIdx++] = v.as<String>();
  }
  recentLoadIdx %= RECENT_ID_SIZE;

  return true;
}

// 공장 초기화 - 저장된 모든 값을 지우고 재고를 카트리지 용량으로 되돌린다.
// 카트리지를 전부 새로 채웠을 때 / CARTRIDGE_CAPACITY 값을 바꿨을 때 사용.
void factoryReset() {
  prefs.clear();

  initStockDefaults();
  queueHead = queueTail = queueCount = 0;
  replaceHead = replaceTail = replaceCount = 0;
  waitingForLoad = false;
  dispatchInterrupted = false;
  currentOrderId = "";
  for (int i = 0; i < RECENT_ID_SIZE; i++) {
    recentOrderIds[i] = "";
    recentLoadIds[i]  = "";
  }
  recentOrderIdx = 0;
  recentLoadIdx  = 0;

  prefs.putInt("schema", NVS_SCHEMA_VER);
  saveStockToNVS();
  saveRuntimeState();

  Serial.println("[NVS] 공장 초기화 완료 - 재고를 카트리지 용량으로 되돌림");
  printStock();
  syncStockToMobius();
}

void initPersistence() {
  prefs.begin(NVS_NAMESPACE, false);   // false = 읽기/쓰기 모드

  if (loadStockFromNVS()) {
    Serial.println("[NVS] 저장된 재고 복원 완료");
  } else {
    Serial.println("[NVS] 저장된 재고 없음(또는 스키마 변경) - 기본값으로 초기화");
    initStockDefaults();
    prefs.putInt("schema", NVS_SCHEMA_VER);
    saveStockToNVS();
  }

  if (loadRuntimeState()) {
    Serial.println("[NVS] 상태 복원 - 대기 주문 " + String(queueCount) +
                   "개, AGV 대기 " + String(waitingForLoad ? "예" : "아니오"));
  } else {
    Serial.println("[NVS] 저장된 상태 없음 - 빈 큐로 시작");
    saveRuntimeState();
  }

  printStock();
}

// ==================================================
// 8. 배출 작업(Job) / 그룹(Group) 정의 - 동시 배출용
// ==================================================
// 한 주문은 최대 5개 항목(키캡 4 + board 1)을 배출한다.
// 각 항목을 Job으로 만들고, 같은 아두이노(IP)로 가는 Job들을 하나의 Group으로 묶는다.
// Group 단위로 FreeRTOS 태스크를 띄우므로:
//   - 서로 다른 아두이노 -> 완전 병렬 실행 (전체 소요시간 = 가장 느린 보드 1대 기준)
//   - 같은 아두이노 -> 태스크 내부에서 순차 실행 (동시 요청으로 보드가 꼬이는 것 방지)

const int MAX_JOBS = 8;

struct DispenseJob {
  int    storageId;
  int    rail;
  char   letter;      // 로그/URL용 (board는 'B')
  bool   isBoard;
  bool   counted;     // cnt_dispense 성공률(targetCount) 집계 대상인지
  bool   needsHttp;   // 실제로 아두이노에 요청을 보낼 대상인지 (재고 부족이면 false)
  String jobId;       // 멱등키. 재시도 시 같은 값을 그대로 다시 보낸다.
  volatile bool success;
};

// 그룹 = 로봇암 1대. 같은 팔을 쓰는 선반들은 이 안에서 순차 처리된다.
struct DispenseGroup {
  int  armId;
  int  jobIdx[MAX_JOBS];
  int  jobCount;
  volatile bool done;
};

DispenseJob   dispenseJobs[MAX_JOBS];
int           dispenseJobCount = 0;
DispenseGroup dispenseGroups[NUM_STORAGES];
int           dispenseGroupCount = 0;

enum DispatchState { DISPATCH_IDLE, DISPATCH_RUNNING };
DispatchState dispatchState = DISPATCH_IDLE;

unsigned long dispatchStartMs = 0;
unsigned long lastSlowWarnMs  = 0;

const int  DISPENSE_TASK_STACK   = 6144;   // HTTPClient 사용을 고려한 스택 크기
const int  DISPENSE_TASK_PRIO    = 1;      // Arduino loopTask와 동일 우선순위
const int  SAME_BOARD_GAP_MS     = 300;    // 같은 아두이노에 연속 요청 시 간격
const unsigned long SLOW_WARN_MS = 25000;  // 이 시간 넘게 안 끝나면 경고 로그

// 한 항목 처리 = ①아두이노 서보 배출 -> ②로봇암 픽업
// 두 단계를 따로 재시도한다. 배출은 job_id 멱등 처리 덕분에 재요청해도 실물이 두 번 안 나가고,
// 픽업 실패는 이미 배출된 물건을 다시 집는 것이라 배출 없이 픽업만 재시도하면 된다.
bool runDispenseJob(DispenseJob& job) {
  // --- 1단계: 배출 ---
  bool dispensed = triggerDispense(job.storageId, job.rail, job.letter, 1, job.jobId);

  if (!dispensed) {
    Serial.println("[RETRY] " + job.jobId + " 배출 실패 - 동일 job_id로 1회 재시도");
    vTaskDelay(pdMS_TO_TICKS(500));
    dispensed = triggerDispense(job.storageId, job.rail, job.letter, 1, job.jobId);
  }

  if (!dispensed) {
    Serial.println("[DISPENSE] " + job.jobId + " 배출 최종 실패");
    return false;
  }

  // --- 2단계: 로봇암 픽업 ---
  bool picked = triggerRobotArm(job.storageId);

  if (!picked) {
    Serial.println("[RETRY] " + job.jobId + " 픽업 실패 - 배출 없이 픽업만 1회 재시도");
    vTaskDelay(pdMS_TO_TICKS(500));
    picked = triggerRobotArm(job.storageId);
  }

  if (!picked) {
    Serial.println("[ARM] " + job.jobId + " 픽업 최종 실패 - 배출구에 물건이 남아있을 수 있음, 확인 필요");
  }

  return picked;
}

// 배출 워커 태스크 - 그룹 하나(= 로봇암 1대)를 담당
// 같은 팔을 쓰는 선반들은 여기서 순차 처리되고, 서로 다른 팔은 태스크가 나뉘어 병렬 실행된다.
void dispenseWorkerTask(void* param) {
  int g = (int)(intptr_t)param;
  DispenseGroup& group = dispenseGroups[g];

  for (int i = 0; i < group.jobCount; i++) {
    DispenseJob& job = dispenseJobs[group.jobIdx[i]];
    job.success = runDispenseJob(job);

    if (i < group.jobCount - 1) {
      vTaskDelay(pdMS_TO_TICKS(SAME_BOARD_GAP_MS));
    }
  }

  group.done = true;
  vTaskDelete(NULL);
}

// 하나의 배출 항목을 Job 목록에 추가 (재고 확인까지 여기서 수행)
void addDispenseJob(int storageId, int rail, char letter, bool isBoard) {
  if (dispenseJobCount >= MAX_JOBS) return;

  String tag = isBoard ? String("board") : String(letter);

  if (storageId < 0 || rail < 0) {
    Serial.println("[DISPENSE] " + tag + " 매핑 실패, 스킵");
    return;
  }
  if (!storages[storageId].enabled) {
    // 미배선 저장소(FT/JP/board 등)는 파일럿 범위 밖이라 집계에서도 제외
    Serial.println("[DISPENSE] " + tag + " -> " + String(storages[storageId].name) + " 비활성, 스킵 (파일럿 범위 아님)");
    return;
  }

  DispenseJob& job = dispenseJobs[dispenseJobCount];
  job.storageId = storageId;
  job.rail      = rail;
  job.letter    = letter;
  job.isBoard   = isBoard;
  job.counted   = true;      // 활성 저장소 대상이면 성공률 집계 대상
  job.success   = false;

  // 멱등키: 주문 안에서 항목마다 고유. 재시도해도 같은 값을 쓴다.
  job.jobId = currentOrderId + "_" + String(dispenseJobCount);

  Serial.println("[DISPENSE] " + tag + " -> " + String(storages[storageId].name) +
                 " rail " + String(rail) + " (" + railLabel(storageId, rail) + ")");

  if (!hasStock(storageId, rail, 1)) {
    Serial.println("[DISPENSE] " + tag + " 재고 없음, 배출 취소 (실패로 집계)");
    job.needsHttp = false;
  } else {
    job.needsHttp = true;
  }

  dispenseJobCount++;
}

// Job들을 로봇암 기준으로 묶어 그룹 구성
// 로봇암 1대는 한 번에 하나만 집을 수 있으므로 같은 팔을 쓰는 선반은 한 그룹에 모아 순차 처리한다.
void buildDispenseGroups() {
  dispenseGroupCount = 0;

  for (int j = 0; j < dispenseJobCount; j++) {
    if (!dispenseJobs[j].needsHttp) continue;

    int armId = storages[dispenseJobs[j].storageId].armId;
    int target = -1;

    for (int g = 0; g < dispenseGroupCount; g++) {
      if (dispenseGroups[g].armId == armId) {
        target = g;
        break;
      }
    }

    if (target < 0) {
      target = dispenseGroupCount++;
      dispenseGroups[target].armId = armId;
      dispenseGroups[target].jobCount = 0;
      dispenseGroups[target].done = false;
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

// ==================================================
// 9. 배출 상태머신 (블로킹 제거의 핵심)
// ==================================================

void beginDispatch(const PendingOrder& o) {
  currentOrderId = o.orderId;
  dispenseJobCount = 0;

  Serial.println();
  Serial.println("[DISPATCH] " + o.orderId + " 배출 시작 | keycap=" + o.keycap +
                 " | colors=" + o.colorsStr + " | board=" + o.boardColor);

  // --- 키캡 4글자 ---
  for (int i = 0; i < o.keycap.length(); i++) {
    char letter = toupper(o.keycap.charAt(i));
    char colorChar = (i < o.colorsStr.length()) ? o.colorsStr.charAt(i) : '?';
    int colorIdx = colorCharToIndex(colorChar);

    int storageId = letterToStorage(letter);
    if (storageId < 0) continue;   // 정의되지 않은 글자(오타 등)

    int rail = letterColorToRail(letter, colorIdx, storageId);
    if (rail < 0) {
      Serial.println("[DISPENSE] " + String(letter) + " 색상(" + String(colorChar) + ") 인식 실패, 스킵");
      continue;
    }

    addDispenseJob(storageId, rail, letter, false);
  }

  // --- board (2026-08-27: 키캡과 동일 경로로 편입, 성공 판정에 포함) ---
  // storage 4가 미배선(enabled=false)인 동안에는 addDispenseJob 안에서 스킵되고
  // 집계 대상에서도 빠지므로, 실제 배선 후 enabled=true로만 바꾸면 자동 반영된다.
  if (o.boardColor.length() > 0) {
    int boardColorIdx = colorStringToIndex(o.boardColor);
    int boardRail = boardColorToRail(boardColorIdx);
    addDispenseJob(STORAGE_BOARD, boardRail, 'B', true);
  }

  buildDispenseGroups();

  dispatchState   = DISPATCH_RUNNING;
  dispatchStartMs = millis();
  lastSlowWarnMs  = dispatchStartMs;

  // 큐에서 꺼낸 상태 + "배출 진행 중" 표시를 여기서 한 번 저장한다.
  // 이 시점 이후 정전/재부팅되면 부팅 시 미완료 주문으로 감지된다.
  dispatchInterrupted = true;
  saveRuntimeState();

  // 워커 태스크 기동 (아두이노 대수만큼 병렬)
  for (int g = 0; g < dispenseGroupCount; g++) {
    char taskName[24];
    snprintf(taskName, sizeof(taskName), "dispense_%d", g);
    BaseType_t ok = xTaskCreatePinnedToCore(
      dispenseWorkerTask, taskName, DISPENSE_TASK_STACK,
      (void*)(intptr_t)g, DISPENSE_TASK_PRIO, NULL, tskNO_AFFINITY
    );
    if (ok != pdPASS) {
      Serial.println("[DISPATCH] 워커 태스크 생성 실패 (group " + String(g) + ") - 해당 그룹 실패 처리");
      dispenseGroups[g].done = true;
    }
  }

  Serial.println("[DISPATCH] 로봇암 " + String(dispenseGroupCount) + "대가 병렬 처리 (총 " +
                 String(dispenseJobCount) + "개 항목, 같은 팔 담당분은 순차)");

  // 배출 시작 알림은 태스크를 띄운 뒤에 보낸다 (POST 대기 시간만큼 배출이 늦어지지 않도록)
  notifyDispenseStatus(currentOrderId, "started");
}

void finalizeDispatch() {
  int targetCount = 0;
  int successCount = 0;

  // 재고 차감은 모든 워커 태스크가 끝난 이 시점에 메인 루프에서만 수행 (경쟁 조건 방지)
  for (int j = 0; j < dispenseJobCount; j++) {
    DispenseJob& job = dispenseJobs[j];
    if (!job.counted) continue;

    targetCount++;
    if (job.success) {
      reduceStock(job.storageId, job.rail, 1);
      successCount++;
    }
  }

  saveStockToNVS();   // 주문 단위로 한 번만 기록 (플래시 수명 보호)
  printStock();
  syncStockToMobius();

  unsigned long elapsed = millis() - dispatchStartMs;
  Serial.println("[DISPATCH] " + currentOrderId + " 결과 " + String(successCount) + "/" +
                 String(targetCount) + " (소요 " + String(elapsed) + "ms)");

  bool success = (targetCount > 0) && (successCount == targetCount);
  notifyDispenseStatus(currentOrderId, success ? "done" : DISPENSE_FAIL_STATUS);

  // 실제로 배출된 게 하나도 없으면 AGV가 실을 바구니도 없으므로 대기하지 않는다.
  // (그대로 대기하면 cnt_load가 영원히 안 와서 큐 전체가 멈춤)
  if (successCount > 0) {
    waitingForLoad = true;
    Serial.println("[DISPATCH] AGV cnt_load 신호 대기 중");
  } else {
    waitingForLoad = false;
    Serial.println("[DISPATCH] 배출 성공 항목 없음 - AGV 대기 없이 다음 주문 진행");
  }

  dispenseJobCount    = 0;
  dispenseGroupCount  = 0;
  dispatchState       = DISPATCH_IDLE;
  dispatchInterrupted = false;

  saveRuntimeState();
}

void serviceDispatch() {
  if (dispatchState == DISPATCH_RUNNING) {
    if (allGroupsDone()) {
      finalizeDispatch();
    } else if (millis() - lastSlowWarnMs > SLOW_WARN_MS) {
      lastSlowWarnMs = millis();
      Serial.println("[DISPATCH] 배출이 " + String((millis() - dispatchStartMs) / 1000) +
                     "초째 진행 중 - 아두이노 응답 지연 확인 필요");
    }
    return;
  }

  if (waitingForLoad) return;      // AGV 탑재 완료 신호 대기 중
  if (queueCount == 0) return;

  PendingOrder next;
  if (dequeueOrder(next)) {
    Serial.println("[QUEUE] 대기열에서 " + next.orderId + " 꺼냄 (남은 대기: " + String(queueCount) + "개)");
    beginDispatch(next);
  }
}

// 교체 요청은 배출 진행 중이 아닐 때만 처리 (재고 배열 충돌 방지)
void serviceReplaceQueue() {
  if (dispatchState == DISPATCH_RUNNING) return;
  if (replaceCount == 0) return;

  PendingReplace req;
  if (dequeueReplace(req)) {
    Serial.println("[REPLACE] 처리 시작 storage=" + String(req.storageId) + " rail=" + String(req.rail));
    replaceCartridge(req.storageId, req.rail);
  }
}

// ==================================================
// 10. Wi-Fi 연결 관리
// ==================================================

void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("[WIFI] 연결 중");

  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("[WIFI] 연결 완료 | IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("[WIFI] 연결 실패");
  }
}

void ensureWiFi() {
  static unsigned long lastRetry = 0;
  if (WiFi.status() == WL_CONNECTED) return;

  // 배출 워커 태스크가 도는 중에 WiFi를 끊었다 붙이면 진행 중인 요청이 깨진다
  if (dispatchState == DISPATCH_RUNNING) return;

  if (millis() - lastRetry < 5000) return;
  lastRetry = millis();

  Serial.println("[WIFI] 재연결 시도");
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 8000) {
    delay(250);
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("[WIFI] 재연결 완료 | IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("[WIFI] 재연결 실패");
  }
}

// ==================================================
// 11. MQTT (HiveMQ) - 주문 push 수신
// ==================================================
// [중요] 콜백 안에서는 JSON 파싱과 큐 적재만 한다.
// HTTP 요청이나 배출 같은 오래 걸리는 작업을 여기서 하면
// mqttClient.loop()가 그동안 재호출되지 못해 다른 메시지가 밀리거나
// 세션 타임아웃으로 유실될 수 있다.

void mqttCallback(char* topic, byte* payload, unsigned int length) {
  String message;
  for (unsigned int i = 0; i < length; i++) {
    message += (char)payload[i];
  }

  String topicStr = String(topic);
  Serial.println("[MQTT] 수신 topic=" + topicStr);
  Serial.println("[MQTT] payload=" + message);

  if (topicStr == MQTT_TOPIC) {
    processOrder(message);
  } else if (topicStr == MQTT_REPLACE_TOPIC) {
    processReplaceRequest(message);
  } else if (topicStr == MQTT_LOAD_TOPIC) {
    processLoadConfirmation(message);
  } else {
    Serial.println("[MQTT] 알 수 없는 토픽, 무시");
  }
}

void connectMQTT() {
  while (!mqttClient.connected()) {
    Serial.print("[MQTT] 브로커 연결 시도...");
    String clientId = "esp32-warehouse-" + String(random(0xffff), HEX);

    if (mqttClient.connect(clientId.c_str(), MQTT_USERNAME, MQTT_PASSWORD)) {
      Serial.println(" 성공");
      mqttClient.subscribe(MQTT_TOPIC);
      mqttClient.subscribe(MQTT_REPLACE_TOPIC);
      mqttClient.subscribe(MQTT_LOAD_TOPIC);
      Serial.println("[MQTT] 구독 시작: " + String(MQTT_TOPIC) + ", " + String(MQTT_REPLACE_TOPIC) + ", " + String(MQTT_LOAD_TOPIC));
    } else {
      Serial.print(" 실패, rc=");
      Serial.print(mqttClient.state());
      Serial.println(" | 3초 후 재시도");
      delay(3000);
    }
  }
}

void ensureMQTT() {
  if (!mqttClient.connected()) {
    connectMQTT();
  }
  mqttClient.loop();   // 콜백 처리를 위해 반드시 주기적으로 호출해야 함
}

// ==================================================
// 12. setup / loop
// ==================================================

void initRobotArmPins() {
  for (int s = 0; s < NUM_STORAGES; s++) {
    pinMode(storages[s].armStartPin, OUTPUT);
    digitalWrite(storages[s].armStartPin, LOW);
  }
  for (int a = 0; a < NUM_ARMS; a++) {
    pinMode(robotArms[a].donePin, INPUT);
  }
  Serial.println("[ARM] GPIO 초기화 완료 (START 5개 / DONE 3개)");
}

// 배출 도중 재부팅된 경우의 복구 처리
// 실물은 일부 배출됐는데 재고 차감은 안 된 상태일 수 있어 수동 확인이 필요하다.
void recoverInterruptedDispatch() {
  if (!dispatchInterrupted) return;

  Serial.println();
  Serial.println("[복구] 이전 배출이 완료되지 않은 채 재부팅됨: " + currentOrderId);
  Serial.println("[복구] 실물 배출분과 저장된 재고가 어긋났을 수 있음 - 선반 실물 확인 권장");

  notifyDispenseStatus(currentOrderId, DISPENSE_FAIL_STATUS);   // 미완료로 보고

  dispatchInterrupted = false;
  waitingForLoad = false;   // 어느 단계에서 끊겼는지 알 수 없으므로 대기 해제 후 큐 재개
  saveRuntimeState();
}

void setup() {
  Serial.begin(115200);
  randomSeed(analogRead(0));

  initRobotArmPins();
  initPersistence();     // NVS에서 재고/큐/상태 복원 (없으면 기본값 생성)
  connectWiFi();

  secureClient.setInsecure();
  delay(1000);

  recoverInterruptedDispatch();
  syncStockToMobius();

  // ===== MQTT(HiveMQ) 연결 설정 =====
  mqttSecureClient.setInsecure();          // 인증서 검증 생략(프로토타입 단계)
  mqttClient.setServer(MQTT_BROKER, MQTT_PORT);
  mqttClient.setCallback(mqttCallback);
  mqttClient.setBufferSize(512);           // 기본 256바이트라 주문 JSON 담기게 확장
  connectMQTT();
  // ===================================

  printStorageStatus();

  Serial.println("[SYSTEM] MQTT 구독 기반 주문 수신 - 36레일(색상 반영) 구조, AGV cnt_load 큐 방식");
  Serial.println("[SYSTEM] 배출: 아두이노별 병렬 처리 + 논블로킹 상태머신, 상태는 NVS에 영속 저장");
  Serial.println("[SYSTEM] 명령: STATUS, TEST E/I/N/S, FACTORYRESET");
}

void loop() {
  ensureWiFi();
  checkSerialCommand();

  if (WiFi.status() == WL_CONNECTED) {
    ensureMQTT();       // 배출 중에도 매 루프 호출되므로 새 메시지가 밀리지 않음
  }

  serviceDispatch();    // 배출 진행/완료 감지 및 다음 주문 시작
  serviceReplaceQueue();

  delay(10);            // 배출이 논블로킹이 되어 루프 주기를 더 짧게 가져감
}

// ==================================================
// 13. 시리얼 모니터 테스트 명령
// ==================================================
// STATUS       : 저장소 연결 상태 + 재고 + 큐 상태 확인
// TEST E/I/N/S : 실제 주문 없이 개별 배출 요청 테스트 (색상은 항상 r로 고정)
// FACTORYRESET : NVS 초기화 (카트리지 전부 새로 채웠을 때 사용)

void printStorageStatus() {
  Serial.println("[STORAGE] 연결 상태 (파일럿: 0,1만 ON)");
  for (int i = 0; i < NUM_STORAGES; i++) {
    Serial.print("  ");
    Serial.print(i);
    Serial.print(" | ");
    Serial.print(storages[i].name);
    Serial.print(" | ");
    Serial.print(storages[i].ip);
    Serial.print(" | ");
    Serial.print(storages[i].enabled ? "ON " : "OFF");
    Serial.print(" | ");
    Serial.print(robotArms[storages[i].armId].name);
    Serial.print(" | START pin ");
    Serial.println(storages[i].armStartPin);
  }

  Serial.println("[ARM] 로봇암 상태");
  for (int a = 0; a < NUM_ARMS; a++) {
    Serial.print("  ");
    Serial.print(robotArms[a].name);
    Serial.print(" | DONE pin ");
    Serial.print(robotArms[a].donePin);
    Serial.print(" | ");
    Serial.println(robotArms[a].enabled ? "ON" : "OFF");
  }
}

void checkSerialCommand() {
  if (!Serial.available()) return;

  String input = Serial.readStringUntil('\n');
  input.trim();
  input.toUpperCase();
  if (input.length() == 0) return;

  if (input == "STATUS") {
    printStorageStatus();
    printStock();
    Serial.println("현재 주문: " + (currentOrderId.length() > 0 ? currentOrderId : "(없음)"));
    Serial.println("배출 상태: " + String(dispatchState == DISPATCH_RUNNING ? "진행 중" : "대기"));
    Serial.println("AGV cnt_load 대기 중: " + String(waitingForLoad ? "예" : "아니오"));
    Serial.println("대기열(큐)에 쌓인 주문: " + String(queueCount) + "개");
    Serial.println("대기 중인 교체 요청: " + String(replaceCount) + "개");
    Serial.println("NVS 스키마 버전: " + String(prefs.getInt("schema", 0)));
    Serial.println("NVS 여유 엔트리: " + String(prefs.freeEntries()));
    return;
  }

  if (input == "FACTORYRESET") {
    if (dispatchState == DISPATCH_RUNNING) {
      Serial.println("[NVS] 배출 진행 중에는 초기화할 수 없음");
      return;
    }
    factoryReset();
    return;
  }

  if (input.startsWith("TEST ") && input.length() >= 6) {
    if (dispatchState == DISPATCH_RUNNING) {
      Serial.println("[TEST] 배출 진행 중에는 테스트 명령을 실행하지 않음");
      return;
    }

    char letter = input.charAt(5);

    int storageId = letterToStorage(letter);
    int colorIdx = 0;   // 테스트는 항상 r(red) 색상으로 고정
    int rail = letterColorToRail(letter, colorIdx, storageId);

    if (storageId < 0 || rail < 0) {
      Serial.println("[TEST] E/I/N/S만 가능 (파일럿 범위)");
      return;
    }
    if (!storages[storageId].enabled) {
      Serial.println("[TEST] " + String(storages[storageId].name) + " 비활성 상태");
      return;
    }

    // 테스트 배출은 재고를 건드리지 않는다 (실물만 나감)
    // job_id에 millis()를 섞어 매번 새 값이 되게 한다 (멱등 캐시에 걸리지 않도록)
    String testJobId = "test_" + String(millis());
    if (triggerDispense(storageId, rail, letter, 1, testJobId)) {
      triggerRobotArm(storageId);
    }
    return;
  }

  Serial.println("[SYSTEM] 사용 가능: STATUS, TEST E, TEST I, TEST N, TEST S, FACTORYRESET");
}

// ==================================================
// 14. Mobius(COSS) 공통 요청 설정
// ==================================================

String generateRI() {
  String ri = "";
  const char hex[] = "0123456789abcdef";
  for (int i = 0; i < 16; i++) ri += hex[random(0, 16)];
  return ri;
}

void addMobiusHeaders(HTTPClient& http, bool isPost) {
  http.addHeader("X-M2M-Origin", COSS_ORIGIN);
  http.addHeader("X-M2M-RI", generateRI());
  http.addHeader("X-M2M-RVI", "3");
  http.addHeader("X-API-KEY", COSS_API_KEY);
  http.addHeader("X-AUTH-CUSTOM-CREATOR", COSS_CREATOR);
  http.addHeader("X-AUTH-CUSTOM-LECTURE", COSS_LECTURE);
  if (isPost) {
    http.addHeader("Content-Type", "application/vnd.onem2m-res+json;ty=4");
    http.addHeader("Accept", "application/json");
  } else {
    http.addHeader("Accept", "application/vnd.onem2m-res+json");
  }
}

// ==================================================
// 15. cnt_order 주문 확인 (참고용, 현재 loop()에서는 미사용)
// ==================================================
// MQTT 구독 방식으로 전환하면서 더 이상 loop()에서 호출하지 않음.
// ※ 플랫폼팀이 cnt_order를 폴링 미지원 형태로 올리기로 확정되면 이 함수는 삭제 대상.

void checkNewOrder() {
  HTTPClient http;
  String url = String(COSS_HOST) + "/Mobius/" + String(AE_NAME) + "/" + String(CNT_ORDER_NAME) + "/la";

  http.begin(secureClient, url);
  addMobiusHeaders(http, false);

  int httpCode = http.GET();

  if (httpCode == 200) {
    String response = http.getString();
    StaticJsonDocument<1024> doc;
    DeserializationError error = deserializeJson(doc, response);

    if (!error) {
      String currentRI = doc["m2m:cin"]["ri"].as<String>();
      String conData   = doc["m2m:cin"]["con"].as<String>();

      if (lastOrderRI == "") {
        Serial.println("[ORDER] 최초 RI 저장");
      } else if (currentRI != lastOrderRI) {
        Serial.println("[ORDER] 새 주문 감지");
        processOrder(conData);
      }
      lastOrderRI = currentRI;
    } else {
      Serial.println("[ORDER] JSON 파싱 실패");
    }
  } else if (httpCode > 0) {
    Serial.println("[ORDER] HTTP " + String(httpCode));
  } else {
    Serial.println("[ORDER] 요청 실패");
  }

  http.end();
}

// ==================================================
// 16. cnt_replace 교체 요청 확인 (참고용, 현재 loop()에서는 미사용)
// ==================================================

void checkReplaceRequest() {
  HTTPClient http;
  String url = String(COSS_HOST) + "/Mobius/" + String(AE_NAME) + "/" + String(CNT_REPLACE_NAME) + "/la";

  http.begin(secureClient, url);
  addMobiusHeaders(http, false);

  int httpCode = http.GET();

  if (httpCode == 200) {
    String response = http.getString();
    StaticJsonDocument<512> doc;
    DeserializationError error = deserializeJson(doc, response);

    if (!error) {
      String currentRI = doc["m2m:cin"]["ri"].as<String>();
      String conData   = doc["m2m:cin"]["con"].as<String>();

      if (lastReplaceRI == "") {
        Serial.println("[REPLACE] 최초 RI 저장");
      } else if (currentRI != lastReplaceRI) {
        Serial.println("[REPLACE] 새 교체 요청 감지");
        processReplaceRequest(conData);
      }
      lastReplaceRI = currentRI;
    } else {
      Serial.println("[REPLACE] JSON 파싱 실패");
    }
  } else if (httpCode != 404 && httpCode > 0) {
    Serial.println("[REPLACE] HTTP " + String(httpCode));
  }

  http.end();
}

// 파싱 + 큐 적재만 수행 (실제 교체는 serviceReplaceQueue에서)
void processReplaceRequest(String conData) {
  StaticJsonDocument<256> doc;
  DeserializationError error = deserializeJson(doc, conData);

  if (error) {
    Serial.println("[REPLACE] 요청 데이터 파싱 실패");
    return;
  }

  int storageId = doc["storage_id"] | -1;
  int rail      = doc["rail"] | -1;

  if (!isValidLocation(storageId, rail)) {
    Serial.println("[REPLACE] 잘못된 저장소/레일 (storage=" + String(storageId) + ", rail=" + String(rail) + ")");
    return;
  }

  Serial.println("[REPLACE] 요청 접수 storage=" + String(storageId) + " rail=" + String(rail));
  enqueueReplace(storageId, rail);
}

// ==================================================
// 17. AGV cnt_load 수신 처리
// ==================================================
// 중복 수신(QoS1 재전송) 및 대기 상태가 아닐 때의 수신을 모두 방어한다.

void processLoadConfirmation(String conData) {
  StaticJsonDocument<256> doc;
  DeserializationError error = deserializeJson(doc, conData);

  String loadedOrderId = "";
  if (!error) {
    loadedOrderId = doc["order_id"] | "";
  }

  Serial.println("[LOAD] cnt_load 수신 (order_id=" + loadedOrderId + ")");

  if (isKnownId(recentLoadIds, loadedOrderId)) {
    Serial.println("[LOAD] 이미 처리한 cnt_load, 무시 (중복 수신)");
    return;
  }

  if (!waitingForLoad) {
    Serial.println("[LOAD] AGV 대기 상태가 아닌데 cnt_load 수신 - 무시 (순서 확인 필요)");
    rememberId(recentLoadIds, recentLoadIdx, loadedOrderId);
    saveRuntimeState();
    return;
  }

  rememberId(recentLoadIds, recentLoadIdx, loadedOrderId);
  waitingForLoad = false;
  saveRuntimeState();

  Serial.println("[LOAD] 대기 해제 - 다음 주문 진행 가능");
  // 다음 주문 시작은 loop()의 serviceDispatch()가 처리
}

// ==================================================
// 18. 주문 JSON 파싱 (36레일: 키캡 32 + 보드 4, 2026-08-26 확정)
// ==================================================
// 주문 예시:
// {
//   "order_id": "ord_x",
//   "station_id": "2",
//   "order_seq": 10,
//   "board": "blue",              // 색상 문자열 (r/y/b/g 계열)
//   "keycap": "ESFJ",              // MBTI 4글자 통짜 문자열
//   "colors": ["r","y","b","g"]    // keycap[i]에 대응하는 색상
// }
//
// 이 함수는 파싱과 큐 적재까지만 수행하고 즉시 리턴한다 (논블로킹).

void processOrder(String conData) {
  StaticJsonDocument<512> orderDoc;
  DeserializationError error = deserializeJson(orderDoc, conData);

  if (error) {
    Serial.println("[ORDER] 주문 파싱 실패");
    return;
  }

  PendingOrder o;
  o.orderId    = orderDoc["order_id"].as<String>();
  o.keycap     = orderDoc["keycap"].as<String>();
  o.boardColor = orderDoc["board"] | "";

  if (o.orderId.length() == 0) {
    Serial.println("[ORDER] order_id 없음 - 중복 판별 불가, 그대로 진행");
  } else if (isKnownId(recentOrderIds, o.orderId)) {
    Serial.println("[ORDER] " + o.orderId + " 는 이미 접수된 주문, 무시 (중복 수신)");
    return;
  }

  // colors 배열 파싱 -> keycap[i]에 대응하는 색상의 첫 글자만 모아서 문자열로 보관
  // (예: colors=["r","y","b","g"] -> colorsStr="rybg")
  String colorsStr = "";
  JsonArray colorsArr = orderDoc["colors"].as<JsonArray>();
  for (JsonVariant v : colorsArr) {
    String c = v.as<String>();
    colorsStr += (c.length() > 0) ? c.charAt(0) : '?';
  }
  o.colorsStr = colorsStr;

  Serial.println();
  Serial.println("[ORDER] " + o.orderId + " | keycap=" + o.keycap + " | colors=" + o.colorsStr + " | board=" + o.boardColor);

  if (enqueueOrder(o)) {
    rememberId(recentOrderIds, recentOrderIdx, o.orderId);
    saveRuntimeState();   // 큐 + 중복방지 ID를 한 번에 기록
  }
}

// ==================================================
// 19. 아두이노에 배출 요청 전송
// ==================================================
// 아두이노가 배출 + 로봇암 트리거까지 로컬로 처리하고
// 그 결과를 HTTP 응답(200)으로 돌려줌.
//
// ※ 이 함수는 배출 워커 태스크에서 호출된다.
//    HTTPClient 인스턴스를 함수 내부에서 생성하므로 여러 태스크가 동시에 호출해도 안전하다.
//    단, 전역 재고 배열과 NVS는 여기서 절대 건드리지 않는다.

bool triggerDispense(int storageId, int rail, char letter, int count, const String& jobId) {
  if (!isValidLocation(storageId, rail)) {
    Serial.println("[DISPENSE] 잘못된 저장소/레일");
    return false;
  }

  Storage& storage = storages[storageId];

  if (!storage.enabled) {
    Serial.println("[DISPENSE] " + String(storage.name) + " 비활성, 명령 생략");
    return false;
  }
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[DISPENSE] Wi-Fi 미연결");
    return false;
  }

  String url = "http://" + String(storage.ip) +
               "/dispense?storage_id=" + String(storageId) +
               "&rail=" + String(rail) +
               "&letter=" + String(letter) +
               "&count=" + String(count) +
               "&job_id=" + jobId;

  Serial.println("[DISPENSE] 요청 -> " + url);

  HTTPClient http;
  http.setConnectTimeout(3000);
  http.setTimeout(8000);   // 로봇암 픽업까지 포함해서 응답 올 수 있어 여유있게
  http.begin(url);

  int httpCode = http.GET();
  bool success = false;

  if (httpCode == 200) {
    Serial.println("[DISPENSE] 완료 -> " + String(letter) + " | " + http.getString());
    success = true;
  } else {
    Serial.println("[DISPENSE] 실패 -> " + String(letter) + " | HTTP " + String(httpCode));
  }

  http.end();
  return success;
}

// ==================================================
// 19-2. 로봇암(Magic Box) GPIO 트리거
// ==================================================
// 로봇암 1대가 선반 여러 개를 커버하므로, 아두이노가 아니라 ESP32가 직접 트리거한다.
// (아두이노 4대가 같은 Magic Box 핀에 물리면 출력끼리 충돌하기 때문)
//
// 배선:
//   ESP32 armStartPin (출력) -> Magic Box EIO 입력   : 선반마다 별도 핀
//   Magic Box EIO 출력       -> ESP32 donePin (입력) : 로봇암마다 1개
//
// Magic Box 블록코딩 쪽에서는 "어느 입력이 켜졌는지"로 집을 위치를 분기하고,
// 동작이 끝나면 DONE 출력을 잠깐 올려주면 된다.
//
// ※ 이 함수는 워커 태스크에서 호출된다. 로봇암 1대당 태스크가 1개뿐이라
//   같은 DONE 핀을 두 태스크가 동시에 읽는 상황은 발생하지 않는다.

bool triggerRobotArm(int storageId) {
  if (storageId < 0 || storageId >= NUM_STORAGES) return false;

  Storage& storage = storages[storageId];
  int armId = storage.armId;

  if (armId < 0 || armId >= NUM_ARMS) {
    Serial.println("[ARM] 잘못된 로봇암 번호");
    return false;
  }

  RobotArm& arm = robotArms[armId];

  if (!arm.enabled) {
    // Magic Box 미배선/미티칭 단계에서는 픽업을 성공으로 간주하고 넘어간다
    Serial.println("[ARM] " + String(arm.name) + " 비활성 - 트리거 생략 (성공 처리)");
    return true;
  }

  // 이전 동작의 DONE이 아직 HIGH면 오판하므로 내려갈 때까지 대기
  unsigned long clearStart = millis();
  while (digitalRead(arm.donePin) == HIGH) {
    if (millis() - clearStart > ARM_CLEAR_TIMEOUT_MS) {
      Serial.println("[ARM] " + String(arm.name) + " DONE이 계속 HIGH - 로봇암 상태 확인 필요");
      return false;
    }
    vTaskDelay(pdMS_TO_TICKS(10));
  }

  // START 펄스
  digitalWrite(storage.armStartPin, HIGH);
  vTaskDelay(pdMS_TO_TICKS(ARM_START_PULSE_MS));
  digitalWrite(storage.armStartPin, LOW);

  Serial.println("[ARM] " + String(arm.name) + " <- " + String(storage.name) +
                 " 픽업 START (pin " + String(storage.armStartPin) + "), DONE 대기");

  unsigned long t0 = millis();
  while (millis() - t0 < ARM_DONE_TIMEOUT_MS) {
    if (digitalRead(arm.donePin) == HIGH) {
      Serial.println("[ARM] " + String(arm.name) + " DONE 수신 (" + String(millis() - t0) + "ms)");
      return true;
    }
    vTaskDelay(pdMS_TO_TICKS(10));
  }

  Serial.println("[ARM] " + String(arm.name) + " DONE 타임아웃 (" +
                 String(ARM_DONE_TIMEOUT_MS) + "ms) - 픽업 실패");
  return false;
}

// ==================================================
// 20. cnt_dispense 상태 업로드 (2026-08-27 플랫폼팀 요청으로 통합)
// ==================================================
// 기존에 cnt_dispense_start / cnt_dispense 두 컨테이너로 나눠 보내던 것을
// cnt_dispense 하나에 status 필드로 구분해서 올리는 방식으로 변경.
//
//   status = "started" : 배출 시퀀스 진입 (아직 실물은 안 나간 시점)
//   status = "done"    : 집계 대상 항목이 전부 배출 성공
//   status = DISPENSE_FAIL_STATUS : 일부 실패 / 배출 도중 재부팅 등
//
// ※ 플랫폼팀은 started/done 두 가지만 다루겠다고 했으므로,
//   실패 상태를 별도 값("partial")으로 보낼지 "done"으로 통일할지는 확인 필요.
//   확인되면 아래 DISPENSE_FAIL_STATUS 한 줄만 바꾸면 전체 반영된다.

void notifyDispenseStatus(String orderId, const char* status) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[COSS] cnt_dispense 생략 - Wi-Fi 미연결 (status=" + String(status) + ")");
    return;
  }

  HTTPClient http;
  String url = String(COSS_HOST) + "/Mobius/" + String(AE_NAME) + "/" + String(CNT_DISPENSE_NAME);

  StaticJsonDocument<256> resultDoc;
  resultDoc["order_id"] = orderId;
  resultDoc["status"] = status;

  String resultJson;
  serializeJson(resultDoc, resultJson);

  StaticJsonDocument<512> cinDoc;
  cinDoc["m2m:cin"]["con"] = resultJson;

  String body;
  serializeJson(cinDoc, body);

  http.begin(secureClient, url);
  addMobiusHeaders(http, true);

  int httpCode = http.POST(body);

  if (httpCode == 200 || httpCode == 201) {
    Serial.println("[COSS] cnt_dispense 업로드 완료: " + orderId + " (status=" + String(status) + ")");
  } else {
    Serial.println("[COSS] cnt_dispense 실패 - HTTP " + String(httpCode) + " (status=" + String(status) + ")");
  }

  http.end();
}

// ==================================================
// 22. cnt_stock 재고 동기화 (색상별 세부 구조 반영, 2026-08-26 확정)
// ==================================================
// 공식 스펙(cnt_stock)에 맞춰 keycap.{글자}.{색상}, board.{색상} 형태로 전송.
// 파일럿 단계라 E/I/N/S만 실제 재고값, 나머지(F/T/J/P/board)는
// 미배선 상태라 0으로 표기 (추후 나머지 선반이 붙으면 자동으로 실제값 반영됨)

void syncStockToMobius() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[COSS] cnt_stock 생략 - Wi-Fi 미연결");
    return;
  }

  HTTPClient http;
  String url = String(COSS_HOST) + "/Mobius/" + String(AE_NAME) + "/" + String(CNT_STOCK_NAME);

  StaticJsonDocument<1024> stockDoc;
  JsonObject keycapObj = stockDoc.createNestedObject("keycap");
  const char colorChars[4] = { 'r', 'y', 'b', 'g' };

  for (int s = 0; s < 4; s++) {
    for (int pairPos = 0; pairPos < 2; pairPos++) {
      char letter = storageLetterPairs[s][pairPos];
      JsonObject letterObj = keycapObj.createNestedObject(String(letter));
      for (int colorIdx = 0; colorIdx < 4; colorIdx++) {
        int rail = pairPos * 4 + colorIdx;
        int value = storages[s].enabled ? stock[s][rail] : 0;
        letterObj[String(colorChars[colorIdx])] = value;
      }
    }
  }

  JsonObject boardObj = stockDoc.createNestedObject("board");
  for (int colorIdx = 0; colorIdx < 4; colorIdx++) {
    int value = storages[STORAGE_BOARD].enabled ? stock[STORAGE_BOARD][colorIdx] : 0;
    boardObj[String(colorChars[colorIdx])] = value;
  }

  String stockJson;
  serializeJson(stockDoc, stockJson);

  StaticJsonDocument<1280> cinDoc;
  cinDoc["m2m:cin"]["con"] = stockJson;

  String body;
  serializeJson(cinDoc, body);

  http.begin(secureClient, url);
  addMobiusHeaders(http, true);

  int httpCode = http.POST(body);

  if (httpCode == 200 || httpCode == 201) {
    Serial.println("[COSS] cnt_stock 동기화 완료");
  } else {
    Serial.println("[COSS] cnt_stock 실패 - HTTP " + String(httpCode));
  }

  http.end();
}
