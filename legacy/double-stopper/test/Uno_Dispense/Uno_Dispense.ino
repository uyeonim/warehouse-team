// [테스트] 배출 보드 : PCA9685 채널 0(뒤) / 1(앞), 이중 스토퍼 1레일
// 어떤 floor/rail 값이 와도 이 한 레일이 동작함
#include <WiFiS3.h>
#include <Wire.h>
#include <Adafruit_PWMServoDriver.h>

const char* ssid     = "****";
const char* password = "****";

WiFiServer server(80);

Adafruit_PWMServoDriver pwm = Adafruit_PWMServoDriver(0x40);

#define SERVO_MIN 102
#define SERVO_MAX 512

const int REAR_CH  = 0;
const int FRONT_CH = 1;

// 스토퍼별 각도 (개체마다 다르므로 분리 관리, 실측값 반영)
const int REAR_OPEN    = 0;
const int REAR_CLOSED  = 110;
const int FRONT_OPEN   = 0;
const int FRONT_CLOSED = 110;

int dispenseCounter = 0;

void setServoAngle(int channel, int angle) {
  int pulse = map(angle, 0, 180, SERVO_MIN, SERVO_MAX);
  pwm.setPWM(channel, 0, pulse);
}

void setup() {
  Serial.begin(9600);

  Wire.begin();
  pwm.begin();
  pwm.setPWMFreq(50);
  delay(300);

  setServoAngle(REAR_CH, REAR_OPEN);
  delay(500);
  setServoAngle(FRONT_CH, FRONT_CLOSED);
  delay(500);
  Serial.println("서보 초기 위치 설정 완료");
  Serial.println("  뒤 열림 " + String(REAR_OPEN) + "도 / 앞 닫힘 " + String(FRONT_CLOSED) + "도");

  connectWiFi();

  server.begin();
  Serial.println("배출 보드 서버 시작, 명령 대기중...");
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
    Serial.print("배출 보드 IP: ");
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

  if (request.indexOf("/dispense") >= 0) {
    String floorVal = getParamValue(request, "floor=");
    String railVal  = getParamValue(request, "rail=");
    String countVal = getParamValue(request, "count=");

    dispenseCounter++;
    Serial.println(">> [" + String(dispenseCounter) + "번째] 배출 요청: floor="
                    + floorVal + " rail=" + railVal + " count=" + countVal);

    dispenseOne();

    client.println("HTTP/1.1 200 OK");
    client.println("Content-Type: text/plain");
    client.println();
    client.println("OK - dispensed #" + String(dispenseCounter)
                    + " (floor=" + floorVal + " rail=" + railVal + " count=" + countVal + ")");
  }
  client.stop();
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
  setServoAngle(REAR_CH, REAR_CLOSED);
  Serial.println("   STEP1: 뒤 닫힘 (" + String(REAR_CLOSED) + "도) - 1개 분리");
  delay(500);

  setServoAngle(FRONT_CH, FRONT_OPEN);
  Serial.println("   STEP2: 앞 열림 (" + String(FRONT_OPEN) + "도) - 통과");
  delay(500);

  setServoAngle(FRONT_CH, FRONT_CLOSED);
  Serial.println("   STEP3: 앞 닫힘 (" + String(FRONT_CLOSED) + "도) - 복귀");
  delay(500);

  setServoAngle(REAR_CH, REAR_OPEN);
  Serial.println("   STEP4: 뒤 열림 (" + String(REAR_OPEN) + "도) - 다음 준비");
  delay(500);

  Serial.println("   >> 배출 완료");
}