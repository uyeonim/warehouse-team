#include <WiFiS3.h>
#include <Servo.h>

const char* ssid = "****";
const char* password = "****";

WiFiServer server(80);

Servo rearServo;   // 뒤 스토퍼
Servo frontServo;  // 앞 스토퍼

const int rearPin = 2;
const int frontPin = 3;

const int OPEN = 0;
const int CLOSED = 120;

void setup() {
  Serial.begin(9600);

  rearServo.attach(rearPin);
  frontServo.attach(frontPin);
  delay(500);

  rearServo.write(OPEN);
  delay(500);
  frontServo.write(CLOSED);
  delay(500);

  WiFi.begin(ssid, password);
  Serial.print("WiFi 연결 중");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();
  Serial.println("WiFi 연결됨");
  Serial.print("이 보드의 IP: ");
  Serial.println(WiFi.localIP());

  server.begin();
  Serial.println("서버 시작, 명령 대기중...");
}

void loop() {
  WiFiClient client = server.available();
  if (client) {
    String request = client.readStringUntil('\r');
    client.flush();
    Serial.println("요청 수신: " + request);

    if (request.indexOf("/dispense") >= 0) {
      String floorVal = getParamValue(request, "floor=");
      String railVal = getParamValue(request, "rail=");
      String countVal = getParamValue(request, "count=");

      Serial.println(">> 배출 요청: floor=" + floorVal + " rail=" + railVal + " count=" + countVal);

      // count 값과 무관하게 스토퍼 동작은 항상 1회
      // (count=4인 축도 물리적으로 한 번에 4개가 쏟아지는 구조라 서보 동작은 1번)
      dispenseOne();

      client.println("HTTP/1.1 200 OK");
      client.println("Content-Type: text/plain");
      client.println();
      client.println("OK - dispensed (floor=" + floorVal + " rail=" + railVal + " count=" + countVal + ")");
    }
    client.stop();
  }
}

String getParamValue(String request, String key) {
  int idx = request.indexOf(key);
  if (idx < 0) return "?";
  int start = idx + key.length();
  int end1 = request.indexOf('&', start);
  int end2 = request.indexOf(' ', start);
  int end = (end1 >= 0 && end1 < end2) ? end1 : end2;
  if (end < 0) end = request.length();
  return request.substring(start, end);
}

void dispenseOne() {
  rearServo.write(CLOSED);
  Serial.println("STEP1: 뒤 닫힘 (분리)");
  delay(500);

  frontServo.write(OPEN);
  Serial.println("STEP2: 앞 열림 (통과)");
  delay(500);

  frontServo.write(CLOSED);
  Serial.println("STEP3: 앞 닫힘 (복귀)");
  delay(500);

  rearServo.write(OPEN);
  Serial.println("STEP4: 뒤 열림 (다음 준비)");
  delay(500);

  Serial.println(">> 배출 완료, 대기 상태로 복귀");
}