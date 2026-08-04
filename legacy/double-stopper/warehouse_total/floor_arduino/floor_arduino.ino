#include <WiFiS3.h>
#include <Wire.h>
#include <Adafruit_PWMServoDriver.h>

const char* ssid = "****";
const char* password = "****";

WiFiServer server(80);

// ---------- PCA9685 ----------
Adafruit_PWMServoDriver pwm = Adafruit_PWMServoDriver(0x40);

#define SERVO_MIN 102   // 0도
#define SERVO_MAX 512   // 180도

const int OPEN = 0;
const int CLOSED = 120;

// 실제로 서보가 연결된 레일 개수 (지금은 1개, 늘어나면 이 값만 수정)
const int CONNECTED_RAILS = 1;

// 레일 N → 뒤 스토퍼 = 채널 N*2, 앞 스토퍼 = 채널 N*2+1
int rearChannel(int rail)  { return rail * 2; }
int frontChannel(int rail) { return rail * 2 + 1; }

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

  // 연결된 레일들을 초기 상태로 (뒤 열림 / 앞 닫힘)
  for (int r = 0; r < CONNECTED_RAILS; r++) {
    setServoAngle(rearChannel(r), OPEN);
    delay(500);
    setServoAngle(frontChannel(r), CLOSED);
    delay(500);
  }
  Serial.println("서보 초기 위치 설정 완료 (연결된 레일 " + String(CONNECTED_RAILS) + "개)");

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
      bool ok = dispenseRail(railVal.toInt());

      client.println("HTTP/1.1 200 OK");
      client.println("Content-Type: text/plain");
      client.println();
      if (ok) {
        client.println("OK - dispensed (floor=" + floorVal + " rail=" + railVal + " count=" + countVal + ")");
      } else {
        client.println("OK - rail " + railVal + " not connected (skipped)");
      }
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

bool dispenseRail(int rail) {
  if (rail < 0 || rail >= CONNECTED_RAILS) {
    Serial.println("   !! 레일 " + String(rail) + "은 아직 서보가 연결되지 않음, 동작 생략");
    return false;
  }

  int rc = rearChannel(rail);
  int fc = frontChannel(rail);

  setServoAngle(rc, CLOSED);
  Serial.println("STEP1: 뒤 닫힘 (분리) - ch" + String(rc));
  delay(500);

  setServoAngle(fc, OPEN);
  Serial.println("STEP2: 앞 열림 (통과) - ch" + String(fc));
  delay(500);

  setServoAngle(fc, CLOSED);
  Serial.println("STEP3: 앞 닫힘 (복귀) - ch" + String(fc));
  delay(500);

  setServoAngle(rc, OPEN);
  Serial.println("STEP4: 뒤 열림 (다음 준비) - ch" + String(rc));
  delay(500);

  Serial.println(">> 배출 완료, 대기 상태로 복귀");
  return true;
}