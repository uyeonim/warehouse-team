//거의 최종본(재고관리X)
#include "config.h"
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

WiFiClientSecure secureClient;
String lastOrderRI = "";

void setup() {
  Serial.begin(115200);
  randomSeed(analogRead(0));

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

  Serial.println("=== cnt_order 폴링 시작 (3초 간격) ===");
}

void loop() {
  checkNewOrder();
  delay(1000); //폴링시간
}

// ---------- Mobius 폴링 ----------

String generateRI() {
  String ri = "";
  const char hex[] = "0123456789abcdef";
  for (int i = 0; i < 16; i++) {
    ri += hex[random(0, 16)];
  }
  return ri;
}

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
        Serial.println(">> 최초 실행, 기준 RI 저장");
      } else if (currentRI != lastOrderRI) {
        Serial.println(">> ★ 새 주문 감지! ★");
        Serial.println(">> 원본: " + conData);
        processOrder(conData);
      }
      lastOrderRI = currentRI;
    } else {
      Serial.println("JSON 파싱 실패: " + String(error.c_str()));
    }
  } else if (httpCode > 0) {
    Serial.println("응답 코드(비정상): " + String(httpCode));
  } else {
    Serial.println("요청 실패: " + http.errorToString(httpCode));
  }

  http.end();
}

// ---------- 주문 파싱 ----------

void processOrder(String conData) {
  // con 필드 자체가 JSON 문자열이므로 한 번 더 파싱
  StaticJsonDocument<512> orderDoc;
  DeserializationError error = deserializeJson(orderDoc, conData);

  if (error) {
    Serial.println("주문 파싱 실패: " + String(error.c_str()));
    return;
  }

  String orderId = orderDoc["order_id"].as<String>();
  String keycap = orderDoc["keycap"].as<String>();     // 예: "INFP"
  String switchName = orderDoc["switch"].as<String>(); // 예: "black"
  int boardCount = orderDoc["board"].as<int>();         // 예: 4

  JsonArray colorsArr = orderDoc["colors"];
  String colors = "";
  for (JsonVariant v : colorsArr) {
    colors += v.as<String>();  // 예: "yygr" 이런 식으로 이어붙임 (r,y,b,g 문자만 있어서 문제없음)
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

const int SWITCH_RAIL_INDEX = 8;

// ---------- 실제 배출 명령 전송 ----------

void dispatchOrder(String keycap, String colors, String switchName, int boardCount) {
  // 키캡 4글자 처리
  for (int i = 0; i < 4; i++) {
    char letter = keycap[i];
    char colorCode = colors[i];
    int floor = colorToFloor(colorCode);
    int railIdx = letterToRailIndex(letter);

    Serial.println("배출 대상: 글자=" + String(letter) + " 색상=" + String(colorCode) 
                    + " → 층" + String(floor) + " 레일" + String(railIdx));

    triggerDispense(floor, railIdx);
    delay(300);
  }

  // 축 처리
  int switchFloor = switchNameToFloor(switchName);
  for (int i = 0; i < boardCount; i++) {
    Serial.println("축 배출: " + switchName + " → 층" + String(switchFloor) + " 레일" + String(SWITCH_RAIL_INDEX));
    triggerDispense(switchFloor, SWITCH_RAIL_INDEX);
    delay(300);
  }
}

// ---------- 우노에 실제 HTTP 요청 (지금은 우노 1대뿐이라 전부 같은 곳으로 보냄) ----------

void triggerDispense(int floor, int railIdx) {
  // TODO: 우노/서보 늘어나면 floor에 따라 다른 IP로 분기해야 함
  // 지금은 우노 1대, 서보 1개뿐이라 무조건 UNO_TEST_IP로만 보냄

  HTTPClient http;
  String url = "http://" + String(UNO_TEST_IP) + "/dispense?floor=" + String(floor) + "&rail=" + String(railIdx);

  Serial.println("  → 우노 전송: " + url);
  http.begin(url);
  int httpCode = http.GET();

  if (httpCode == 200) {
    Serial.println("  → 성공: " + http.getString());
  } else {
    Serial.println("  → 실패, 코드: " + String(httpCode));
  }
  http.end();
}