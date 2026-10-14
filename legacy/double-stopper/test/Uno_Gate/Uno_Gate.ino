// [테스트] 배출구 보드 : 서보 1개 (D2 직결)
#include <WiFiS3.h>
#include <Servo.h>

const char* ssid     = "****";
const char* password = "****";

WiFiServer server(80);

Servo gateServo;
const int gatePin = 2;

const int GATE_CLOSED = 0;
const int GATE_OPEN   = 90;
const int OPEN_HOLD_MS = 3000;

void setup() {
  Serial.begin(9600);

  gateServo.attach(gatePin);
  gateServo.write(GATE_CLOSED);
  delay(500);
  Serial.println("배출구 초기 위치(닫힘) 설정 완료");

  connectWiFi();

  server.begin();
  Serial.println("배출구 보드 서버 시작, 명령 대기중...");
}

void connectWiFi() {
  WiFi.begin(ssid, password);
  Serial.print("WiFi 연결 중");

  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("WiFi 연결됨");
    Serial.print("배출구 보드 IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("!! WiFi 연결 실패");
  }
}

void ensureWiFi() {
  static unsigned long lastRetry = 0;
  if (WiFi.status() == WL_CONNECTED) return;
  if (millis() - lastRetry < 5000) return;
  lastRetry = millis();

  Serial.println("!! WiFi 끊김 → 재연결 시도");
  WiFi.disconnect();
  WiFi.begin(ssid, password);

  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 8000) delay(250);

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("   → 재연결 성공, IP: " + WiFi.localIP().toString());
    server.begin();
  }
}

void loop() {
  ensureWiFi();

  WiFiClient client = server.available();
  if (!client) return;

  String request = client.readStringUntil('\r');
  client.flush();
  Serial.println("요청 수신: " + request);

  if (request.indexOf("/gate") >= 0) {
    Serial.println(">> 배출구 개방 명령 수신");
    openGate();

    client.println("HTTP/1.1 200 OK");
    client.println("Content-Type: text/plain");
    client.println();
    client.println("OK - gate opened");
  }
  client.stop();
}

void openGate() {
  gateServo.write(GATE_OPEN);
  Serial.println("   배출구 개방");
  delay(OPEN_HOLD_MS);

  gateServo.write(GATE_CLOSED);
  Serial.println("   배출구 폐쇄");
}