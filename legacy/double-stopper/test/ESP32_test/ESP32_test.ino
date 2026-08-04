// [테스트 구성] 배출 보드 1대(이중 스토퍼 1레일) + 배출구 보드 1대
// 층/레일 매핑과 재고 처리는 실제 구조(5층 36레일) 그대로 유지하고,
// 물리적 배출 명령만 전부 배출 보드 1대로 보냄 → 한 주문당 5회 동작
#include "config.h"
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

WiFiClientSecure secureClient;

String lastOrderRI   = "";
String lastReplaceRI = "";
String lastGateRI    = "";

// ================= 재고 관리 (실제 구조 유지) =================
// floor 0 = 1층(축, 레일 0~3) / floor 1~4 = 2~5층 키캡(레일 0~7)
const int NUM_FLOORS = 5;
const int MAX_RAILS  = 8;

const int SWITCH_FLOOR = 0;
const int SWITCH_RAILS = 4;
const int KEYCAP_RAILS = 8;

int stock[NUM_FLOORS][MAX_RAILS];
int totalPool[NUM_FLOORS][MAX_RAILS];
int spareBin[NUM_FLOORS][MAX_RAILS];

const int LOW_STOCK_THRESHOLD = 5;
const int SWITCH_SET_SIZE     = 4;
const int CARTRIDGE_CAPACITY  = 7;   // 테스트용. 실제 운영 시 50

const int TOTAL_KEYCAP_PER_RAIL      = 300;
const int TOTAL_SWITCH_SETS_PER_RAIL = 500;

// ================= 배출구 상태 =================
bool dispenseComplete = false;
bool gateSignal       = false;
String currentOrderId = "";

// ================= 이름 테이블 =================
const char* floorNames[NUM_FLOORS] = {
  "1층(축)", "2층(Red)", "3층(Yellow)", "4층(Blue)", "5층(Green)"
};
const char* keycapNames[KEYCAP_RAILS] = { "E","I","S","N","F","T","P","J" };
const char* switchNames[SWITCH_RAILS] = { "red","black","blue","brown" };

int railCountOf(int floor) {
  return (floor == SWITCH_FLOOR) ? SWITCH_RAILS : KEYCAP_RAILS;
}

String railLabel(int floor, int rail) {
  if (floor == SWITCH_FLOOR) {
    return (rail >= 0 && rail < SWITCH_RAILS) ? String(switchNames[rail]) : "?";
  }
  return (rail >= 0 && rail < KEYCAP_RAILS) ? String(keycapNames[rail]) : "?";
}

String unitOf(int floor) {
  return (floor == SWITCH_FLOOR) ? "세트" : "개";
}

// ================= 재고 함수 =================

void printStock() {
  for (int f = 0; f < NUM_FLOORS; f++) {
    String line = String(floorNames[f]) + ": ";
    for (int r = 0; r < railCountOf(f); r++) {
      line += railLabel(f, r) + "=" + String(stock[f][r]) + unitOf(f) + " ";
    }
    Serial.println(line);
  }
}

void initStock() {
  for (int f = 0; f < NUM_FLOORS; f++) {
    for (int r = 0; r < MAX_RAILS; r++) {
      stock[f][r] = 0;
      totalPool[f][r] = 0;
      spareBin[f][r] = 0;
    }
    for (int r = 0; r < railCountOf(f); r++) {
      int totalCap = (f == SWITCH_FLOOR) ? TOTAL_SWITCH_SETS_PER_RAIL
                                         : TOTAL_KEYCAP_PER_RAIL;
      stock[f][r] = CARTRIDGE_CAPACITY;
      totalPool[f][r] = totalCap - CARTRIDGE_CAPACITY;
      spareBin[f][r] = 0;
    }
  }
  Serial.println("=== 초기 재고 설정 완료 ===");
  printStock();
}

bool isValidLocation(int floor, int rail) {
  if (floor < 0 || floor >= NUM_FLOORS) return false;
  if (rail < 0 || rail >= railCountOf(floor)) return false;
  return true;
}

bool hasStock(int floor, int rail, int needed) {
  if (!isValidLocation(floor, rail)) {
    Serial.println("  !! 잘못된 층/레일 번호");
    return false;
  }
  return stock[floor][rail] >= needed;
}

void reduceStock(int floor, int rail, int count) {
  stock[floor][rail] -= count;
  if (stock[floor][rail] < 0) stock[floor][rail] = 0;

  String unit = unitOf(floor);
  String where = String(floorNames[floor]) + " " + railLabel(floor, rail);

  Serial.println("  재고 차감: " + where + " → 남은 재고: "
                  + String(stock[floor][rail]) + unit);

  if (stock[floor][rail] == 0) {
    Serial.println("  !!!! 재고 소진 !!!! " + where + " 즉시 카트리지 교체 필요");
  } else if (stock[floor][rail] <= LOW_STOCK_THRESHOLD) {
    Serial.println("  ⚠ 재고 부족 경고 ⚠ " + where + " 잔여 "
                    + String(stock[floor][rail]) + unit + ", 교체 준비 필요");
  }
}

// ================= 카트리지 교체 =================

void replaceCartridge(int floor, int rail) {
  if (!isValidLocation(floor, rail)) {
    Serial.println("!! 잘못된 층/레일 번호");
    return;
  }

  String unit = unitOf(floor);
  String where = String(floorNames[floor]) + " " + railLabel(floor, rail);

  Serial.println("=== 카트리지 교체: " + where + " ===");

  if (stock[floor][rail] > LOW_STOCK_THRESHOLD) {
    Serial.println("  아직 재고가 " + String(stock[floor][rail]) + unit
                    + " 남아있어 교체 불필요 (기준: " + String(LOW_STOCK_THRESHOLD) + unit + " 이하)");
    return;
  }

  int leftover = stock[floor][rail];
  spareBin[floor][rail] += leftover;
  stock[floor][rail] = 0;
  Serial.println("  자투리 " + String(leftover) + unit + " → 빈 카트리지 적립 (누적: "
                  + String(spareBin[floor][rail]) + unit + ")");

  if (spareBin[floor][rail] >= CARTRIDGE_CAPACITY) {
    spareBin[floor][rail] -= CARTRIDGE_CAPACITY;
    stock[floor][rail] = CARTRIDGE_CAPACITY;
    Serial.println("  ♻ 재활용 카트리지로 리필 완료 → " + String(CARTRIDGE_CAPACITY) + unit);
  } else if (totalPool[floor][rail] >= CARTRIDGE_CAPACITY) {
    totalPool[floor][rail] -= CARTRIDGE_CAPACITY;
    stock[floor][rail] = CARTRIDGE_CAPACITY;
    Serial.println("  새 카트리지로 리필 완료 → " + String(CARTRIDGE_CAPACITY) + unit
                    + " (남은 예비재고: " + String(totalPool[floor][rail]) + unit + ")");
  } else if (totalPool[floor][rail] > 0) {
    stock[floor][rail] = totalPool[floor][rail];
    Serial.println("  ⚠ 예비재고 부족, 부분 리필 → " + String(stock[floor][rail]) + unit);
    totalPool[floor][rail] = 0;
  } else {
    Serial.println("  !!!! 총 재고 완전 소진 !!!! 더 이상 리필 불가");
  }

  Serial.println("  현재 상태: 로드=" + String(stock[floor][rail]) + unit
                  + " / 예비풀=" + String(totalPool[floor][rail]) + unit
                  + " / 빈카트리지=" + String(spareBin[floor][rail]) + unit);

  syncStockToMobius();
}

// ================= WiFi =================

void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("WiFi 연결 중");

  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("WiFi 연결됨");
    Serial.print("이 보드의 IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("!! WiFi 연결 실패, 이후 자동 재시도");
  }
}

void ensureWiFi() {
  static unsigned long lastRetry = 0;

  if (WiFi.status() == WL_CONNECTED) return;
  if (millis() - lastRetry < 5000) return;
  lastRetry = millis();

  Serial.println("!! WiFi 끊김 감지 → 재연결 시도");
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 8000) {
    delay(250);
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("   → 재연결 성공, IP: " + WiFi.localIP().toString());
  } else {
    Serial.println("   → 재연결 실패, 다음 주기에 재시도");
  }
}

// ================= setup / loop =================

void setup() {
  Serial.begin(115200);
  randomSeed(analogRead(0));

  initStock();
  connectWiFi();

  secureClient.setInsecure();
  delay(1000);
  syncStockToMobius();

  Serial.println("--- 테스트 보드 구성 ---");
  Serial.println("  배출 보드 : " + String(BOARD_DISPENSE_IP) + " (이중 스토퍼 1레일)");
  Serial.println("  배출구 보드: " + String(BOARD_GATE_IP) + " (서보 1개)");
  Serial.println("=== 폴링 시작 (cnt_order / cnt_replace / cnt_gate) ===");
  Serial.println("명령: STATUS  /  GATE (배출구 수동 개방)");
}

void loop() {
  ensureWiFi();
  checkSerialCommand();

  if (WiFi.status() == WL_CONNECTED) {
    checkNewOrder();
    checkReplaceRequest();
    checkGateSignal();
  }

  delay(1000);
}

// ================= 시리얼 명령 =================

void checkSerialCommand() {
  if (!Serial.available()) return;

  String input = Serial.readStringUntil('\n');
  input.trim();
  if (input.length() == 0) return;

  if (input == "STATUS") {
    printStock();
    Serial.println("현재 주문=" + (currentOrderId.length() > 0 ? currentOrderId : "(없음)"));
    Serial.println("배출완료=" + String(dispenseComplete ? "O" : "X")
                    + " / 게이트신호=" + String(gateSignal ? "O" : "X"));
    return;
  }

  if (input == "GATE") {
    Serial.println(">> 수동 배출구 개방 명령");
    openGate();
    return;
  }

  Serial.println("알 수 없는 명령: " + input);
}

// ================= Mobius 공통 =================

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
  http.addHeader("X-AUTH-CUSTOM-LECTURE", COSS_LECTURF);
  if (isPost) {
    http.addHeader("Content-Type", "application/vnd.onem2m-res+json;ty=4");
    http.addHeader("Accept", "application/json");
  } else {
    http.addHeader("Accept", "application/vnd.onem2m-res+json");
  }
}

// ---------- cnt_order ----------
void checkNewOrder() {
  HTTPClient http;
  String url = String(COSS_HOST) + "/Mobius/" + String(AE_NAME) + "/"
               + String(CNT_ORDER_NAME) + "/la";

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
        Serial.println(">> [주문] 최초 실행, 기준 RI 저장");
      } else if (currentRI != lastOrderRI) {
        Serial.println(">> ★ 새 주문 감지! ★");
        Serial.println(">> 원본: " + conData);
        processOrder(conData);
      }
      lastOrderRI = currentRI;
    } else {
      Serial.println("[주문] JSON 파싱 실패: " + String(error.c_str()));
    }
  } else if (httpCode > 0) {
    Serial.println("[주문] 응답 코드(비정상): " + String(httpCode));
  } else {
    Serial.println("[주문] 요청 실패: " + http.errorToString(httpCode));
  }

  http.end();
}

// ---------- cnt_replace ----------
void checkReplaceRequest() {
  HTTPClient http;
  String url = String(COSS_HOST) + "/Mobius/" + String(AE_NAME) + "/"
               + String(CNT_REPLACE_NAME) + "/la";

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
        Serial.println(">> [교체] 최초 실행, 기준 RI 저장");
      } else if (currentRI != lastReplaceRI) {
        Serial.println(">> ★ 교체 요청 감지! ★");
        Serial.println(">> 원본: " + conData);
        processReplaceRequest(conData);
      }
      lastReplaceRI = currentRI;
    }
  } else if (httpCode != 404 && httpCode > 0) {
    Serial.println("[교체] 응답 코드: " + String(httpCode));
  }

  http.end();
}

void processReplaceRequest(String conData) {
  StaticJsonDocument<256> doc;
  DeserializationError error = deserializeJson(doc, conData);

  if (error) {
    Serial.println("[교체] 요청 파싱 실패: " + String(error.c_str()));
    return;
  }

  int floor = doc["floor"].as<int>();
  int rail  = doc["rail"].as<int>();

  Serial.println(">> 교체 요청: 층=" + String(floor) + " 레일=" + String(rail));
  replaceCartridge(floor, rail);
}

// ---------- cnt_gate ----------
void checkGateSignal() {
  HTTPClient http;
  String url = String(COSS_HOST) + "/Mobius/" + String(AE_NAME) + "/"
               + String(CNT_GATE_NAME) + "/la";

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

      if (lastGateRI == "") {
        Serial.println(">> [게이트] 최초 실행, 기준 RI 저장");
      } else if (currentRI != lastGateRI) {
        Serial.println(">> [게이트] 새 신호 수신: " + conData);
        processGateSignal(conData);
      }
      lastGateRI = currentRI;
    }
  } else if (httpCode != 404 && httpCode > 0) {
    Serial.println("[게이트] 응답 코드: " + String(httpCode));
  }

  http.end();
}

void processGateSignal(String conData) {
  StaticJsonDocument<256> doc;
  DeserializationError error = deserializeJson(doc, conData);

  if (error) {
    Serial.println("   [게이트] 파싱 실패: " + String(error.c_str()));
    return;
  }

  String orderId = doc["order_id"].as<String>();
  bool arrived   = doc["agv_arrived"].as<bool>();

  Serial.println("   [게이트] 주문번호=" + orderId
                  + " / 도착=" + String(arrived ? "true" : "false"));

  // 도착 신호가 아니면 무시
  if (!arrived) {
    Serial.println("   [게이트] agv_arrived가 false → 무시");
    return;
  }

  // 현재 처리 중인 주문과 다르면 무시
  if (currentOrderId.length() > 0 && orderId != currentOrderId) {
    Serial.println("   [게이트] 다른 주문(" + orderId + ")의 신호, 현재 주문("
                    + currentOrderId + ")과 불일치 → 무시");
    return;
  }

  Serial.println(">> ★ AGV 도착 확인 ★");
  gateSignal = true;
  tryOpenGate();
}

// ================= 주문 파싱 =================

void processOrder(String conData) {
  StaticJsonDocument<512> orderDoc;
  DeserializationError error = deserializeJson(orderDoc, conData);

  if (error) {
    Serial.println("주문 파싱 실패: " + String(error.c_str()));
    return;
  }

  currentOrderId    = orderDoc["order_id"].as<String>();
  String keycap     = orderDoc["keycap"].as<String>();
  String switchName = orderDoc["switch"].as<String>();
  int boardCount    = orderDoc["board"].as<int>();

  JsonArray colorsArr = orderDoc["colors"];
  String colors = "";
  for (JsonVariant v : colorsArr) colors += v.as<String>();

  Serial.println("--- 주문 파싱 결과 ---");
  Serial.println("주문번호: " + currentOrderId);
  Serial.println("키캡글자: " + keycap);
  Serial.println("색상순서: " + colors);
  Serial.println("스위치: " + switchName + " x " + String(boardCount) + "개");

  dispenseComplete = false;
  gateSignal = false;

  dispatchOrder(keycap, colors, switchName, boardCount);
}

// ================= 레일 매핑 (5층 구조 그대로) =================

int colorToFloor(char colorCode) {
  switch (colorCode) {
    case 'r': return 1;  // 2층
    case 'y': return 2;  // 3층
    case 'b': return 3;  // 4층
    case 'g': return 4;  // 5층
  }
  return -1;
}

int letterToRailIndex(char letter) {
  switch (letter) {
    case 'E': return 0;
    case 'I': return 1;
    case 'S': return 2;
    case 'N': return 3;
    case 'F': return 4;
    case 'T': return 5;
    case 'P': return 6;
    case 'J': return 7;
  }
  return -1;
}

int switchNameToRail(String switchName) {
  if (switchName == "red")   return 0;
  if (switchName == "black") return 1;
  if (switchName == "blue")  return 2;
  if (switchName == "brown") return 3;
  return -1;
}

// ================= 배출 명령 조립 (총 5회) =================

void dispatchOrder(String keycap, String colors, String switchName, int boardCount) {
  int successCount = 0;
  int attemptCount = 0;

  // 키캡 4개 (2~5층)
  for (int i = 0; i < 4; i++) {
    char letter    = keycap[i];
    char colorCode = colors[i];
    int floor   = colorToFloor(colorCode);
    int railIdx = letterToRailIndex(letter);

    if (floor < 0 || railIdx < 0) {
      Serial.println("  !! 매핑 실패: " + String(letter) + "/" + String(colorCode));
      continue;
    }

    Serial.println("[" + String(i + 1) + "/5] 배출 대상: " + String(letter) + "-" + String(colorCode)
                    + " → " + String(floorNames[floor]) + " " + railLabel(floor, railIdx)
                    + " (레일" + String(railIdx) + ")");

    attemptCount++;

    if (!hasStock(floor, railIdx, 1)) {
      Serial.println("  !! 재고 부족으로 배출 취소 (재고: " + String(stock[floor][railIdx]) + "개)");
      continue;
    }

    if (triggerDispense(floor, railIdx, 1)) {
      reduceStock(floor, railIdx, 1);
      successCount++;
    }
    delay(300);
  }

  // 축 묶음 (1층)
  int switchRail = switchNameToRail(switchName);
  if (switchRail >= 0) {
    Serial.println("[5/5] 축 배출(묶음): " + switchName + " 1세트(" + String(boardCount) + "개) → "
                    + String(floorNames[SWITCH_FLOOR]) + " 레일" + String(switchRail));

    attemptCount++;

    if (!hasStock(SWITCH_FLOOR, switchRail, 1)) {
      Serial.println("  !! 축 재고 부족으로 배출 취소 (남은 세트: "
                      + String(stock[SWITCH_FLOOR][switchRail]) + ")");
    } else {
      if (triggerDispense(SWITCH_FLOOR, switchRail, boardCount)) {
        reduceStock(SWITCH_FLOOR, switchRail, 1);
        successCount++;
      }
    }
  } else {
    Serial.println("  !! 축 매핑 실패: " + switchName);
  }

  Serial.println("--- 주문 처리 후 재고 현황 ---");
  printStock();
  syncStockToMobius();

  Serial.println(">> 배출 결과: " + String(successCount) + "/" + String(attemptCount) + " 성공");

  if (attemptCount > 0 && successCount == attemptCount) {
    dispenseComplete = true;
    Serial.println(">> 배출 완료. AGV 도착 신호(cnt_gate) 대기 중...");
    tryOpenGate();
  } else {
    dispenseComplete = false;
    Serial.println(">> !! 일부 배출 실패, 배출구 개방 보류");
  }
}

// ================= 배출 보드에 요청 =================
// [테스트] 층 구분 없이 전부 배출 보드 1대로 보냄

bool triggerDispense(int floor, int railIdx, int count) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("  → WiFi 미연결로 명령 생략");
    return false;
  }

  HTTPClient http;
  String url = "http://" + String(BOARD_DISPENSE_IP) + "/dispense?floor=" + String(floor)
               + "&rail=" + String(railIdx) + "&count=" + String(count);

  Serial.println("  → 배출 보드 전송: " + url);
  http.setTimeout(5000);
  http.begin(url);
  int httpCode = http.GET();

  bool success = false;
  if (httpCode == 200) {
    Serial.println("  → 성공: " + http.getString());
    success = true;
  } else {
    Serial.println("  → 실패, 코드: " + String(httpCode));
  }
  http.end();
  return success;
}

// ================= 배출구 제어 =================

void tryOpenGate() {
  Serial.println("   [배출구 조건] 배출완료=" + String(dispenseComplete ? "O" : "X")
                  + " / AGV도착=" + String(gateSignal ? "O" : "X"));

  if (dispenseComplete && gateSignal) {
    openGate();
    dispenseComplete = false;
    gateSignal = false;
  }
}

void openGate() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("   [배출구] WiFi 미연결로 명령 생략");
    return;
  }

  HTTPClient http;
  String url = "http://" + String(BOARD_GATE_IP) + "/gate?action=open";

  Serial.println("   → 배출구 전송: " + url);
  http.setTimeout(6000);
  http.begin(url);
  int httpCode = http.GET();

  if (httpCode == 200) {
    Serial.println("   → ★ 배출구 개방 완료 ★ " + http.getString());
  } else {
    Serial.println("   → 배출구 개방 실패, 코드: " + String(httpCode));
  }
  http.end();
}

// ================= cnt_stock 동기화 =================

void syncStockToMobius() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("  ✗ WiFi 미연결로 cnt_stock 동기화 생략");
    return;
  }

  HTTPClient http;
  String url = String(COSS_HOST) + "/Mobius/" + String(AE_NAME) + "/" + String(CNT_STOCK_NAME);

  StaticJsonDocument<1024> stockDoc;
  const char* colorNames[4] = { "r", "y", "b", "g" };

  JsonObject keycapObj = stockDoc.createNestedObject("keycap");
  for (int rail = 0; rail < KEYCAP_RAILS; rail++) {
    JsonObject colorObj = keycapObj.createNestedObject(keycapNames[rail]);
    for (int c = 0; c < 4; c++) {
      colorObj[colorNames[c]] = stock[c + 1][rail];   // floor 1~4
    }
  }

  JsonObject switchObj = stockDoc.createNestedObject("switch");
  for (int rail = 0; rail < SWITCH_RAILS; rail++) {
    switchObj[switchNames[rail]] = stock[SWITCH_FLOOR][rail];
  }

  String stockJson;
  serializeJson(stockDoc, stockJson);

  StaticJsonDocument<1200> cinDoc;
  cinDoc["m2m:cin"]["con"] = stockJson;

  String body;
  serializeJson(cinDoc, body);

  http.begin(secureClient, url);
  addMobiusHeaders(http, true);

  int httpCode = http.POST(body);

  if (httpCode == 200 || httpCode == 201) {
    Serial.println("  ✓ cnt_stock 동기화 성공 (코드: " + String(httpCode) + ")");
  } else {
    Serial.println("  ✗ cnt_stock 동기화 실패, 코드: " + String(httpCode));
  }

  http.end();
}