//재고 등록 및 교체까지 코드(cnt_replace이용-대기열적용X)
#include "config.h"
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

WiFiClientSecure secureClient;
String lastOrderRI = "";
String lastReplaceRI = "";

// ---------- 재고 관리 ----------
const int NUM_FLOORS = 4;
const int NUM_RAILS = 9;
int stock[NUM_FLOORS][NUM_RAILS];       // 현재 로드된(배출 가능한) 재고
int totalPool[NUM_FLOORS][NUM_RAILS];   // 예비 재고 풀
int spareBin[NUM_FLOORS][NUM_RAILS];    // 빈 카트리지 적립분

const int LOW_STOCK_THRESHOLD = 5;
const int SWITCH_RAIL_INDEX = 8;
const int SWITCH_SET_SIZE = 4;
const int CARTRIDGE_CAPACITY = 7; //카트리지 재고 수

const int TOTAL_KEYCAP_PER_RAIL = 300;
const int TOTAL_SWITCH_SETS_PER_RAIL = 500;

void initStock() {
  for (int f = 0; f < NUM_FLOORS; f++) {
    for (int r = 0; r < NUM_RAILS; r++) {
      int totalCap = (r < 8) ? TOTAL_KEYCAP_PER_RAIL : TOTAL_SWITCH_SETS_PER_RAIL;
      stock[f][r] = CARTRIDGE_CAPACITY;
      totalPool[f][r] = totalCap - CARTRIDGE_CAPACITY;
      spareBin[f][r] = 0;
    }
  }
  Serial.println("=== 초기 재고 설정 완료 ===");
  printStock();
}

void printStock() {
  const char* floorNames[4] = {"1층(Red)", "2층(Yellow)", "3층(Blue)", "4층(Green)"};
  const char* railNames[9] = {"E", "I", "S", "N", "F", "T", "P", "J", "축"};

  for (int f = 0; f < NUM_FLOORS; f++) {
    String line = String(floorNames[f]) + ": ";
    for (int r = 0; r < NUM_RAILS; r++) {
      String unit = (r == SWITCH_RAIL_INDEX) ? "세트" : "개";
      line += String(railNames[r]) + "=" + String(stock[f][r]) + unit + " ";
    }
    Serial.println(line);
  }
}

bool hasStock(int floor, int rail, int needed) {
  if (floor < 0 || floor >= NUM_FLOORS || rail < 0 || rail >= NUM_RAILS) {
    Serial.println("  !! 잘못된 층/레일 번호");
    return false;
  }
  return stock[floor][rail] >= needed;
}

void reduceStock(int floor, int rail, int count) {
  stock[floor][rail] -= count;
  if (stock[floor][rail] < 0) stock[floor][rail] = 0;

  const char* floorNames[4] = {"1층(Red)", "2층(Yellow)", "3층(Blue)", "4층(Green)"};
  const char* railNames[9] = {"E", "I", "S", "N", "F", "T", "P", "J", "축"};
  bool isSwitch = (rail == SWITCH_RAIL_INDEX);
  String unit = isSwitch ? "세트" : "개";

  Serial.println("  재고 차감: " + String(floorNames[floor]) + " " + String(railNames[rail]) 
                  + " → 남은 재고: " + String(stock[floor][rail]) + unit);

  if (stock[floor][rail] == 0) {
    Serial.println("  !!!! 재고 소진 !!!! " + String(floorNames[floor]) + " " + String(railNames[rail]) 
                    + " 즉시 카트리지 교체 필요");
  } else if (stock[floor][rail] <= LOW_STOCK_THRESHOLD) {
    Serial.println("  ⚠ 재고 부족 경고 ⚠ " + String(floorNames[floor]) + " " + String(railNames[rail]) 
                    + " 잔여 " + String(stock[floor][rail]) + unit + ", 교체 준비 필요");
  }
}

// ---------- 카트리지 교체 ----------

void replaceCartridge(int floor, int rail) {
  if (floor < 0 || floor >= NUM_FLOORS || rail < 0 || rail >= NUM_RAILS) {
    Serial.println("!! 잘못된 층/레일 번호");
    return;
  }

  const char* floorNames[4] = {"1층(Red)", "2층(Yellow)", "3층(Blue)", "4층(Green)"};
  const char* railNames[9] = {"E", "I", "S", "N", "F", "T", "P", "J", "축"};
  bool isSwitch = (rail == SWITCH_RAIL_INDEX);
  String unit = isSwitch ? "세트" : "개";

  Serial.println("=== 카트리지 교체: " + String(floorNames[floor]) + " " + String(railNames[rail]) + " ===");

  if (stock[floor][rail] > LOW_STOCK_THRESHOLD) {
    Serial.println("  아직 재고가 " + String(stock[floor][rail]) + unit + " 남아있어 교체 불필요 (기준: " 
                    + String(LOW_STOCK_THRESHOLD) + unit + " 이하)");
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
    Serial.println("  !!!! 총 재고 완전 소진 !!!! 더 이상 리필 불가, 실물 카트리지 추가 보급 필요");
  }

  Serial.println("  현재 상태: 로드=" + String(stock[floor][rail]) + unit 
                  + " / 예비풀=" + String(totalPool[floor][rail]) + unit 
                  + " / 빈카트리지=" + String(spareBin[floor][rail]) + unit);

  syncStockToMobius();
}

// ---------- setup / loop ----------

void setup() {
  Serial.begin(115200);
  randomSeed(analogRead(0));

  initStock();

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("WiFi 연결 중");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();
  Serial.println("WiFi 연결됨");
  Serial.print("이 보드의 IP: ");
  Serial.println(WiFi.localIP());

  secureClient.setInsecure();
  delay(1000); // 네트워크 안정화 대기 추가
  syncStockToMobius();
  Serial.println("=== cnt_order / cnt_replace 폴링 시작 ===");
}

void loop() {
  checkNewOrder();
  checkReplaceRequest();
  delay(1000);
}

// ---------- Mobius 폴링 공통 ----------

String generateRI() {
  String ri = "";
  const char hex[] = "0123456789abcdef";
  for (int i = 0; i < 16; i++) {
    ri += hex[random(0, 16)];
  }
  return ri;
}

// ---------- cnt_order 폴링 ----------

void checkNewOrder() {
  HTTPClient http;
  String url = String(COSS_HOST) + "/Mobius/" + String(AE_NAME) + "/" + String(CNT_ORDER_NAME) + "/la";

  http.begin(secureClient, url);
  http.addHeader("X-M2M-Origin", COSS_ORIGIN);
  http.addHeader("X-M2M-RI", generateRI());
  http.addHeader("X-M2M-RVI", "3");
  http.addHeader("X-API-KEY", COSS_API_KEY);
  http.addHeader("X-AUTH-CUSTOM-CREATOR", COSS_CREATOR);
  http.addHeader("X-AUTH-CUSTOM-LECTURE", COSS_LECTURF);
  http.addHeader("Accept", "application/vnd.onem2m-res+json");

  int httpCode = http.GET();

  if (httpCode == 200) {
    String response = http.getString();

    StaticJsonDocument<1024> doc;
    DeserializationError error = deserializeJson(doc, response);

    if (!error) {
      String currentRI = doc["m2m:cin"]["ri"].as<String>();
      String conData = doc["m2m:cin"]["con"].as<String>();

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

// ---------- cnt_replace 폴링 ----------

void checkReplaceRequest() {
  HTTPClient http;
  String url = String(COSS_HOST) + "/Mobius/" + String(AE_NAME) + "/" + String(CNT_REPLACE_NAME) + "/la";

  http.begin(secureClient, url);
  http.addHeader("X-M2M-Origin", COSS_ORIGIN);
  http.addHeader("X-M2M-RI", generateRI());
  http.addHeader("X-M2M-RVI", "3");
  http.addHeader("X-API-KEY", COSS_API_KEY);
  http.addHeader("X-AUTH-CUSTOM-CREATOR", COSS_CREATOR);
  http.addHeader("X-AUTH-CUSTOM-LECTURE", COSS_LECTURF);
  http.addHeader("Accept", "application/vnd.onem2m-res+json");

  int httpCode = http.GET();

  if (httpCode == 200) {
    String response = http.getString();

    StaticJsonDocument<512> doc;
    DeserializationError error = deserializeJson(doc, response);

    if (!error) {
      String currentRI = doc["m2m:cin"]["ri"].as<String>();
      String conData = doc["m2m:cin"]["con"].as<String>();

      if (lastReplaceRI == "") {
        Serial.println(">> [교체] 최초 실행, 기준 RI 저장");
      } else if (currentRI != lastReplaceRI) {
        Serial.println(">> ★ 교체 요청 감지! ★");
        Serial.println(">> 원본: " + conData);
        processReplaceRequest(conData);
      }
      lastReplaceRI = currentRI;
    } else {
      Serial.println("[교체] JSON 파싱 실패: " + String(error.c_str()));
    }
  } else if (httpCode != 404) {
    Serial.println("[교체] 응답 코드: " + String(httpCode));
  }
  // 404는 cnt_replace에 아직 데이터가 없는 정상 상황일 수 있어 조용히 무시

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
  int rail = doc["rail"].as<int>();

  Serial.println(">> 교체 요청: 층=" + String(floor) + " 레일=" + String(rail));
  replaceCartridge(floor, rail);
}

// ---------- 주문 파싱 ----------

void processOrder(String conData) {
  StaticJsonDocument<512> orderDoc;
  DeserializationError error = deserializeJson(orderDoc, conData);

  if (error) {
    Serial.println("주문 파싱 실패: " + String(error.c_str()));
    return;
  }

  String orderId = orderDoc["order_id"].as<String>();
  String keycap = orderDoc["keycap"].as<String>();
  String switchName = orderDoc["switch"].as<String>();
  int boardCount = orderDoc["board"].as<int>();

  JsonArray colorsArr = orderDoc["colors"];
  String colors = "";
  for (JsonVariant v : colorsArr) {
    colors += v.as<String>();
  }

  Serial.println("--- 주문 파싱 결과 ---");
  Serial.println("주문번호: " + orderId);
  Serial.println("키캡글자: " + keycap);
  Serial.println("색상순서: " + colors);
  Serial.println("스위치: " + switchName + " x " + String(boardCount) + "개");

  dispatchOrder(keycap, colors, switchName, boardCount);
}

// ---------- 레일 매핑 ----------

int colorToFloor(char colorCode) {
  switch (colorCode) {
    case 'r': return 0;
    case 'y': return 1;
    case 'b': return 2;
    case 'g': return 3;
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

int switchNameToFloor(String switchName) {
  if (switchName == "red")   return 0;
  if (switchName == "black") return 1;
  if (switchName == "blue")  return 2;
  if (switchName == "brown") return 3;
  return -1;
}

// ---------- 배출 명령 조립 ----------

void dispatchOrder(String keycap, String colors, String switchName, int boardCount) {
  for (int i = 0; i < 4; i++) {
    char letter = keycap[i];
    char colorCode = colors[i];
    int floor = colorToFloor(colorCode);
    int railIdx = letterToRailIndex(letter);

    Serial.println("배출 대상: 글자=" + String(letter) + " 색상=" + String(colorCode) 
                    + " → 층" + String(floor) + " 레일" + String(railIdx));

    if (!hasStock(floor, railIdx, 1)) {
      Serial.println("  !! 재고 부족으로 배출 취소 (재고: " + String(stock[floor][railIdx]) + "개)");
      continue;
    }

    bool success = triggerDispense(floor, railIdx, 1);
    if (success) {
      reduceStock(floor, railIdx, 1);
    }
    delay(300);
  }

  int switchFloor = switchNameToFloor(switchName);
  Serial.println("축 배출(묶음): " + switchName + " 1세트(" + String(boardCount) + "개) → 층" 
                  + String(switchFloor) + " 레일" + String(SWITCH_RAIL_INDEX));

  if (!hasStock(switchFloor, SWITCH_RAIL_INDEX, 1)) {
    Serial.println("  !! 축 재고 부족으로 배출 취소 (남은 세트: " + String(stock[switchFloor][SWITCH_RAIL_INDEX]) + ")");
  } else {
    bool success = triggerDispense(switchFloor, SWITCH_RAIL_INDEX, boardCount);
    if (success) {
      reduceStock(switchFloor, SWITCH_RAIL_INDEX, 1);
    }
  }

  Serial.println("--- 주문 처리 후 재고 현황 ---");
  printStock();

  syncStockToMobius();
}

// ---------- 우노에 실제 HTTP 요청 ----------

bool triggerDispense(int floor, int railIdx, int count) {
  HTTPClient http;
  String url = "http://" + String(UNO_TEST_IP) + "/dispense?floor=" + String(floor) 
               + "&rail=" + String(railIdx) + "&count=" + String(count);

  Serial.println("  → 우노 전송: " + url);
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

// ---------- cnt_stock에 재고 반영 ----------

void syncStockToMobius() {
  HTTPClient http;
  String url = String(COSS_HOST) + "/Mobius/" + String(AE_NAME) + "/" + String(CNT_STOCK_NAME);

  StaticJsonDocument<1024> stockDoc;

  const char* letters[8] = {"E", "I", "S", "N", "F", "T", "P", "J"};
  const char* colorNames[4] = {"r", "y", "b", "g"};
  const char* switchNames[4] = {"red", "black", "blue", "brown"};

  JsonObject keycapObj = stockDoc.createNestedObject("keycap");
  for (int letterIdx = 0; letterIdx < 8; letterIdx++) {
    JsonObject colorObj = keycapObj.createNestedObject(letters[letterIdx]);
    for (int floor = 0; floor < NUM_FLOORS; floor++) {
      colorObj[colorNames[floor]] = stock[floor][letterIdx];
    }
  }

  JsonObject switchObj = stockDoc.createNestedObject("switch");
  for (int floor = 0; floor < NUM_FLOORS; floor++) {
    switchObj[switchNames[floor]] = stock[floor][SWITCH_RAIL_INDEX];
  }

  String stockJson;
  serializeJson(stockDoc, stockJson);

  StaticJsonDocument<1200> cinDoc;
  cinDoc["m2m:cin"]["con"] = stockJson;

  String body;
  serializeJson(cinDoc, body);

  http.begin(secureClient, url);
  http.addHeader("X-M2M-Origin", COSS_ORIGIN);
  http.addHeader("X-M2M-RI", generateRI());
  http.addHeader("X-M2M-RVI", "3");
  http.addHeader("X-API-KEY", COSS_API_KEY);
  http.addHeader("X-AUTH-CUSTOM-CREATOR", COSS_CREATOR);
  http.addHeader("X-AUTH-CUSTOM-LECTURE", COSS_LECTURF);
  http.addHeader("Content-Type", "application/vnd.onem2m-res+json;ty=4");
  http.addHeader("Accept", "application/json");

  int httpCode = http.POST(body);

  if (httpCode == 200 || httpCode == 201) {
    Serial.println("  ✓ cnt_stock 동기화 성공 (코드: " + String(httpCode) + ")");
  } else {
    Serial.println("  ✗ cnt_stock 동기화 실패, 코드: " + String(httpCode));
    Serial.println("  응답: " + http.getString());
  }

  http.end();
}