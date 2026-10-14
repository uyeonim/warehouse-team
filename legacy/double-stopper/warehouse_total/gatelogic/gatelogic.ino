//배출구용
#include <WiFiS3.h>
#include <Servo.h>

const char* ssid = "****";
const char* password = "****";

WiFiServer server(80);

Servo gateServo;
const int gatePin = 2;

const int GATE_CLOSED = 0;
const int GATE_OPEN   = 90;
const int OPEN_HOLD_MS = 3000;  // 개방 유지 시간

void setup() {
  Serial.begin(9600);

  gateServo.attach(gatePin);
  gateServo.write(GATE_CLOSED);
  delay(500);

  WiFi.begin(ssid, password);
  Serial.print("WiFi 연결 중");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();
  Serial.println("WiFi 연결됨");
  Serial.print("배출구 보드 IP: ");
  Serial.println(WiFi.localIP());

  server.begin();
  Serial.println("배출구 서버 시작, 명령 대기중...");
}

void loop() {
  WiFiClient client = server.available();
  if (client) {
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
}

void openGate() {
  gateServo.write(GATE_OPEN);
  Serial.println("배출구 개방");
  delay(OPEN_HOLD_MS);

  gateServo.write(GATE_CLOSED);
  Serial.println("배출구 폐쇄");
}