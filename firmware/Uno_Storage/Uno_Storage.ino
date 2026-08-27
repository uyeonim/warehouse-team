#include "config.h"
#include <WiFiS3.h>
#include <Wire.h>
#include <Adafruit_PWMServoDriver.h>

// ==================================================
// 스마트 창고 - 선반 아두이노 (R4 WiFi)
// ==================================================
// 아두이노 5대, 선반(storage)당 1대.
//   storage 0~3 (EI/NS/FT/JP): 레일 8개 -> PCA9685 채널 0~7
//   storage 4   (board)      : 레일 4개 -> PCA9685 채널 0~3
//   rail 번호 = PCA9685 채널 번호 (1:1 대응)
//
// [2026-08-27 변경 사항]
//  - 이중 스토퍼(레일당 서보 2개) 폐기 -> 단일 푸셔(레일당 서보 1개)
//  - 파라미터명 floor -> storage_id (플랫폼팀 확정 명칭)
//  - job_id 기반 멱등 처리 추가: 같은 job_id 재요청 시 실제 배출 없이 이전 결과만 응답
//    (ESP32가 타임아웃 후 안전하게 재시도할 수 있게 하기 위함)
//  - 실패 시 HTTP 500 반환 (기존에는 실패해도 200이라 ESP32가 성공으로 오인)
//  - 응답을 JSON으로 통일
//  - 로봇암 트리거는 아두이노가 하지 않는다 (ESP32가 GPIO로 직접 처리)
//    로봇암 1대가 선반 여러 개를 커버해서, 아두이노 여러 대가 같은 Magic Box 핀에
//    물리면 출력끼리 충돌하기 때문. 아두이노는 순수 서보 배출만 담당한다.
//  - Wi-Fi 비밀값을 config.h로 분리 (.gitignore 필수)
// ==================================================

WiFiServer server(80);
Adafruit_PWMServoDriver pwm = Adafruit_PWMServoDriver(0x40);

// PCA9685 펄스 범위 (SG90 계열 기준, 실측 후 조정)
const int SERVO_MIN = 102;   // 0도
const int SERVO_MAX = 512;   // 180도

// 레일별 각도 보정값. 특정 레일이 덜 밀리거나 과하게 밀리면 여기만 조정.
// (인덱스 = rail 번호, 단위 = 도)
int railAngleOffset[8] = { 0, 0, 0, 0, 0, 0, 0, 0 };

int railCountOfThisBoard() {
  return (MY_STORAGE_ID == 4) ? 4 : 8;
}

// ==================================================
// 멱등 처리 (job_id 중복 방어)
// ==================================================
// ESP32가 응답 타임아웃 후 같은 job_id로 재시도할 수 있다.
// 이미 처리한 job_id면 서보를 다시 돌리지 않고 이전 결과만 돌려준다.
// -> "응답만 유실됐는데 실물이 두 개 나가는" 사고를 막는 핵심 장치.

const int JOB_HISTORY_SIZE = 10;

String jobIds[JOB_HISTORY_SIZE];
bool   jobResults[JOB_HISTORY_SIZE];
int    jobHistoryIdx = 0;

int findJob(const String& id) {
  if (id.length() == 0) return -1;
  for (int i = 0; i < JOB_HISTORY_SIZE; i++) {
    if (jobIds[i].length() > 0 && jobIds[i] == id) return i;
  }
  return -1;
}

void rememberJob(const String& id, bool ok) {
  if (id.length() == 0) return;
  jobIds[jobHistoryIdx] = id;
  jobResults[jobHistoryIdx] = ok;
  jobHistoryIdx = (jobHistoryIdx + 1) % JOB_HISTORY_SIZE;
}

// ==================================================
// 서보 제어
// ==================================================

void setServoAngle(int channel, int angle) {
  angle = constrain(angle, 0, 180);
  int pulse = map(angle, 0, 180, SERVO_MIN, SERVO_MAX);
  pwm.setPWM(channel, 0, pulse);
}

// 단일 푸셔: 밀어냄 -> 원위치. 맨 아래 1개만 배출된다.
bool dispenseRail(int rail) {
  if (rail < 0 || rail >= railCountOfThisBoard()) {
    Serial.println("   !! rail " + String(rail) + " 는 이 보드(storage " + String(MY_STORAGE_ID) + ") 범위 밖");
    return false;
  }
  if (rail >= WIRED_RAIL_COUNT) {
    Serial.println("   !! rail " + String(rail) + " 는 아직 서보 미배선, 동작 생략");
    return false;
  }

  int offset = (rail < 8) ? railAngleOffset[rail] : 0;

  setServoAngle(rail, PUSH_OUT_ANGLE + offset);
  Serial.println("   STEP1: 푸셔 밀어냄 (ch" + String(rail) + ", " + String(PUSH_OUT_ANGLE + offset) + "도)");
  delay(PUSH_HOLD_MS);

  setServoAngle(rail, PUSH_IDLE_ANGLE);
  Serial.println("   STEP2: 푸셔 복귀");
  delay(PUSH_RETURN_MS);

  return true;
}

// ==================================================
// HTTP 파라미터 파싱
// ==================================================

String getParamValue(const String& request, const String& key) {
  int idx = request.indexOf(key);
  if (idx < 0) return "";

  int start = idx + key.length();
  int end1 = request.indexOf('&', start);
  int end2 = request.indexOf(' ', start);

  int end;
  if (end1 < 0)      end = end2;
  else if (end2 < 0) end = end1;
  else               end = min(end1, end2);

  if (end < 0) end = request.length();
  return request.substring(start, end);
}

void sendJson(WiFiClient& client, int statusCode, const String& body) {
  client.println("HTTP/1.1 " + String(statusCode) + (statusCode == 200 ? " OK" : " Internal Server Error"));
  client.println("Content-Type: application/json");
  client.println("Connection: close");
  client.println();
  client.println(body);
}

// ==================================================
// setup / loop
// ==================================================

void setup() {
  Serial.begin(9600);

  Wire.begin();
  pwm.begin();
  pwm.setPWMFreq(50);
  delay(300);

  // 배선된 레일 전부 초기 위치(푸셔 후퇴)로
  for (int r = 0; r < WIRED_RAIL_COUNT && r < railCountOfThisBoard(); r++) {
    setServoAngle(r, PUSH_IDLE_ANGLE);
    delay(200);
  }
  Serial.println("서보 초기 위치 설정 완료 (배선 " + String(WIRED_RAIL_COUNT) + "개)");

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
  Serial.println("담당 storage_id: " + String(MY_STORAGE_ID) +
                 " (레일 " + String(railCountOfThisBoard()) + "개)");

  server.begin();
  Serial.println("서버 시작, 명령 대기중...");
}

void loop() {
  WiFiClient client = server.available();
  if (!client) return;

  String request = client.readStringUntil('\r');
  client.flush();
  Serial.println("요청 수신: " + request);

  if (request.indexOf("/dispense") < 0) {
    sendJson(client, 200, "{\"result\":\"ignored\"}");
    client.stop();
    return;
  }

  String storageVal = getParamValue(request, "storage_id=");
  String railVal    = getParamValue(request, "rail=");
  String letterVal  = getParamValue(request, "letter=");
  String jobId      = getParamValue(request, "job_id=");

  int storageId = storageVal.toInt();
  int rail      = railVal.toInt();

  Serial.println(">> 배출 요청: storage_id=" + storageVal + " rail=" + railVal +
                 " letter=" + letterVal + " job_id=" + jobId);

  // --- storage_id 검증: 다른 선반 요청이 잘못 들어온 경우 방어 ---
  if (storageVal.length() > 0 && storageId != MY_STORAGE_ID) {
    Serial.println("   !! 이 보드는 storage " + String(MY_STORAGE_ID) + " 담당인데 " +
                   storageVal + " 요청이 옴 - 거부");
    sendJson(client, 500, "{\"result\":\"wrong_storage\",\"my_storage\":" + String(MY_STORAGE_ID) + "}");
    client.stop();
    return;
  }

  // --- 멱등 처리: 이미 처리한 job_id면 서보를 다시 돌리지 않는다 ---
  int known = findJob(jobId);
  if (known >= 0) {
    bool prev = jobResults[known];
    Serial.println("   ** 이미 처리한 job_id - 재배출 없이 이전 결과 반환 (" +
                   String(prev ? "success" : "fail") + ")");
    sendJson(client, prev ? 200 : 500,
             "{\"job_id\":\"" + jobId + "\",\"result\":\"" + String(prev ? "success" : "fail") +
             "\",\"cached\":true}");
    client.stop();
    return;
  }

  // --- 실제 배출 + 로봇암 픽업 ---
  bool ok = dispenseRail(rail);
  rememberJob(jobId, ok);

  if (ok) {
    Serial.println(">> 배출 완료");
    sendJson(client, 200,
             "{\"job_id\":\"" + jobId + "\",\"result\":\"success\",\"cached\":false,\"rail\":" +
             String(rail) + "}");
  } else {
    Serial.println(">> 배출 실패");
    sendJson(client, 500,
             "{\"job_id\":\"" + jobId + "\",\"result\":\"fail\",\"cached\":false,\"rail\":" +
             String(rail) + "}");
  }

  client.stop();
}
