#include "config.h"
#include <WiFiS3.h>
#include <Servo.h>

// ==================================================
// 스마트 창고 - 선반 아두이노 (R4 WiFi)
// ==================================================
// 아두이노 5대, 선반(storage)당 1대.
//   storage 0~3 (EI/NS/FT/JP): 레일 8개
//   storage 4   (board)      : 레일 4개
//   rail 번호 = servoPins[] 배열의 인덱스 (그 레일 서보가 물린 실제 핀)
//
// [2026-09-02 변경 사항 - 서보 구동 방식 교체]
//  - PCA9685(I2C, Adafruit_PWMServoDriver) 방식 폐기
//    -> 실기 테스트로 동작이 확인된 표준 Servo 라이브러리 + GPIO 직결 방식으로 전환
//  - 레일당 PUSH_OUT/PUSH_IDLE 각도 왕복 방식 폐기
//    -> 0도 -> 180도 풀 스윕 방식으로 교체 (1도씩 15ms 간격, 실기 테스트값)
//  - Wire / Adafruit_PWMServoDriver 의존성 제거
//  - 통신(WiFi/HTTP), job_id 멱등 처리, JSON 응답 로직은 기존 구조 그대로 유지
//  - 배출은 HTTP /dispense 요청으로만 트리거됨 (버튼 관련 코드 없음)
// ==================================================

WiFiServer server(80);

// ---- 레일 <-> 실제 서보 신호핀 매핑 ----
// !! 반드시 실제 배선에 맞게 이 배열만 수정하세요 !!
// 인덱스 = rail 번호, 값 = 그 레일 서보가 연결된 디지털 핀 번호
// (rail 0 = pin 9만 실측값이고, 나머지는 임시값입니다)
const int servoPins[8] = { 9, 10, 11, 12, 13, A0, A1, A2 };

Servo servos[8];

// 0 -> 180도 스윕 시 1도 이동할 때마다 대기하는 시간 (실기 테스트로 확인된 값)
const int SWEEP_STEP_DELAY_MS = 15;

int railCountOfThisBoard() {
  return (MY_STORAGE_ID == 4) ? 4 : 8;
}

// ==================================================
// 멱등 처리 (job_id 중복 방어) - 기존 로직 그대로
// ==================================================
// ESP32가 응답 타임아웃 후 같은 job_id로 재시도할 수 있다.
// 이미 처리한 job_id면 서보를 다시 돌리지 않고 이전 결과만 돌려준다.

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

void rememberJob(const String& id, bool success) {
  if (id.length() == 0) return;
  jobIds[jobHistoryIdx] = id;
  jobResults[jobHistoryIdx] = success;
  jobHistoryIdx = (jobHistoryIdx + 1) % JOB_HISTORY_SIZE;
}

// ==================================================
// 서보 제어 (실기 테스트로 확인된 방식: 0 -> 180도 풀 스윕)
// ==================================================

// 복귀(0도로 되돌아가는) 동작 후 실제로 0도에 도달할 때까지 기다려주는 시간.
// 스텝 없이 한 번에 명령을 보내므로 서보 자체 최고 속도로 움직인다.
// 기구 저항 때문에 0도까지 못 미치고 배출 스윕이 시작되면 이 값을 늘리고,
// 이미 충분히 빠르면 줄여서 전체 사이클 시간을 단축하면 된다.
const int RETURN_DELAY_MS = 400;

void sweepRail(int rail) {
  Servo& s = servos[rail];

  // 복귀: 현재 위치가 어디든 0도로 한 번에 명령 (스텝 없이 최고 속도로 이동)
  s.write(0);
  delay(RETURN_DELAY_MS);

  // 배출: 0 -> 180도, 1도씩 천천히 (실기 테스트로 확인된 속도, 그대로 유지)
  for (int pos = 0; pos <= 180; pos++) {
    s.write(pos);
    delay(SWEEP_STEP_DELAY_MS);
  }
}

bool dispenseRail(int rail) {
  if (rail < 0 || rail >= railCountOfThisBoard()) {
    Serial.println("   !! rail " + String(rail) + " 는 이 보드(storage " + String(MY_STORAGE_ID) + ") 범위 밖");
    return false;
  }
  if (rail >= WIRED_RAIL_COUNT) {
    Serial.println("   !! rail " + String(rail) + " 는 아직 서보 미배선, 동작 생략");
    return false;
  }

  Serial.println("   STEP: 서보 스윕 시작 (rail " + String(rail) + ", pin " + String(servoPins[rail]) + ")");
  sweepRail(rail);
  Serial.println("   STEP: 서보 스윕 완료");

  return true;
}

// ==================================================
// HTTP 파라미터 파싱 - 기존 로직 그대로
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

  // 배선된 레일 전부 서보 attach만 하고, 각도는 쓰지 않는다.
  // (write()를 호출해야 실제로 펄스가 나가서 서보가 움직이므로, attach()만 하면
  //  업로드/재부팅해도 서보가 저절로 안 움직인다 - 첫 배출 요청이 와야 비로소 움직임)
  // attach()에 펄스 범위(500~2500us)를 넓게 줘서, 서보 스펙상 0~180도보다
  // 실제로 조금 더 넓게 움직이게 한다 (푸셔가 부품을 끝까지 밀어내지 못하는 문제 대응).
  // 서보마다 기계적 한계가 있어 여기까지 넓혀도 안 움직이면 그게 진짜 물리적 한계다.
  for (int r = 0; r < WIRED_RAIL_COUNT && r < railCountOfThisBoard(); r++) {
    servos[r].attach(servoPins[r], 500, 2500);
  }
  Serial.println("서보 attach 완료 (배선 " + String(WIRED_RAIL_COUNT) + "개, 첫 배출 요청 전까지는 움직이지 않음)");

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("WiFi 연결 중");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();
  Serial.println("WiFi 연결됨 (연결 상태만 확인됨, IP는 아직 못 받았을 수 있음)");

  // R4 WiFi는 WL_CONNECTED가 뜬 직후에도 DHCP로 실제 IP를 아직 못 받은 경우가 있다.
  // localIP()가 0.0.0.0이 아닐 때까지 최대 10초 더 기다린다.
  Serial.print("IP 할당 대기 중");
  unsigned long ipWaitStart = millis();
  while (WiFi.localIP() == IPAddress(0, 0, 0, 0) && millis() - ipWaitStart < 10000) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();

  if (WiFi.localIP() == IPAddress(0, 0, 0, 0)) {
    Serial.println("!! 10초 지나도 IP를 못 받음 - 공유기 DHCP 상태 확인 필요");
  }
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
  bool success = dispenseRail(rail);
  rememberJob(jobId, success);

  if (success) {
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
