#include <Servo.h>

Servo rearServo;
Servo frontServo;

const int rearPin  = 2;
const int frontPin = 3;

// 스토퍼별 각도 (개체마다 다르므로 따로 관리)
int REAR_OPEN    = 0;
int REAR_CLOSED  = 110;   // 실측값
int FRONT_OPEN   = 0;
int FRONT_CLOSED = 110;   // 조정하면서 찾을 값

int rearAngle  = 0;
int frontAngle = 0;
bool rearAttached  = false;
bool frontAttached = false;

char target = 'R';

void setup() {
  Serial.begin(9600);
  delay(1000);

  printHelp();

  attachAndMove('R', REAR_OPEN);
  delay(500);
  attachAndMove('F', FRONT_CLOSED);
  Serial.println(">> 초기 상태: 뒤 열림(" + String(REAR_OPEN) + "도) / 앞 닫힘(" + String(FRONT_CLOSED) + "도)");
}

void printHelp() {
  Serial.println("=== 이중 스토퍼 조정 & 테스트 ===");
  Serial.println("[ 대상 선택 ]");
  Serial.println("  R / F        : 뒤 / 앞 스토퍼 선택");
  Serial.println("[ 조정 ]");
  Serial.println("  숫자         : 해당 각도로 이동");
  Serial.println("  + / -        : 1도씩");
  Serial.println("  ++ / --      : 5도씩");
  Serial.println("  FREE / HOLD  : 힘 빼기 / 다시 유지");
  Serial.println("  NOW          : 현재 상태 확인");
  Serial.println("[ 각도값 저장 ]");
  Serial.println("  SETOPEN      : 현재 각도를 선택된 쪽의 '열림'으로 저장");
  Serial.println("  SETCLOSED    : 현재 각도를 선택된 쪽의 '닫힘'으로 저장");
  Serial.println("  ANGLES       : 저장된 각도값 전체 확인");
  Serial.println("[ 실동작 테스트 ]");
  Serial.println("  GO           : 이중 스토퍼 1회 배출");
  Serial.println("  GO5          : 5회 연속 (주문 1건 분량)");
  Serial.println("  RESET        : 초기 상태 복귀");
  Serial.println("  HELP         : 도움말");
  Serial.println();
}

void loop() {
  if (!Serial.available()) return;

  String cmd = Serial.readStringUntil('\n');
  cmd.trim();
  if (cmd.length() == 0) return;

  String upper = cmd;
  upper.toUpperCase();

  // ---- 대상 선택 ----
  if (upper == "R") {
    target = 'R';
    Serial.println(">> 조정 대상: 뒤 스토퍼 (현재 " + String(rearAngle) + "도)");
    return;
  }
  if (upper == "F") {
    target = 'F';
    Serial.println(">> 조정 대상: 앞 스토퍼 (현재 " + String(frontAngle) + "도)");
    return;
  }

  // ---- 조립 보조 ----
  if (upper == "FREE") {
    if (target == 'R') { rearServo.detach();  rearAttached = false; }
    else               { frontServo.detach(); frontAttached = false; }
    Serial.println(">> " + targetName() + " 힘 빠짐 (기록 각도: " + String(currentOf(target)) + "도)");
    return;
  }
  if (upper == "HOLD") {
    attachAndMove(target, currentOf(target));
    Serial.println(">> " + targetName() + " " + String(currentOf(target)) + "도 유지");
    return;
  }
  if (upper == "NOW") {
    Serial.println(">> 뒤=" + String(rearAngle) + "도(" + (rearAttached ? "유지" : "힘빠짐") + ")"
                    + " / 앞=" + String(frontAngle) + "도(" + (frontAttached ? "유지" : "힘빠짐") + ")");
    return;
  }

  // ---- 각도값 저장 ----
  if (upper == "SETOPEN") {
    if (target == 'R') { REAR_OPEN = rearAngle;   Serial.println(">> 뒤 열림 = " + String(REAR_OPEN) + "도 저장"); }
    else               { FRONT_OPEN = frontAngle; Serial.println(">> 앞 열림 = " + String(FRONT_OPEN) + "도 저장"); }
    return;
  }
  if (upper == "SETCLOSED") {
    if (target == 'R') { REAR_CLOSED = rearAngle;   Serial.println(">> 뒤 닫힘 = " + String(REAR_CLOSED) + "도 저장"); }
    else               { FRONT_CLOSED = frontAngle; Serial.println(">> 앞 닫힘 = " + String(FRONT_CLOSED) + "도 저장"); }
    return;
  }
  if (upper == "ANGLES") {
    Serial.println(">> 저장된 각도값");
    Serial.println("   뒤 : 열림 " + String(REAR_OPEN) + "도 / 닫힘 " + String(REAR_CLOSED) + "도");
    Serial.println("   앞 : 열림 " + String(FRONT_OPEN) + "도 / 닫힘 " + String(FRONT_CLOSED) + "도");
    return;
  }

  // ---- 실동작 테스트 ----
  if (upper == "GO")  { dispenseOne(1); return; }
  if (upper == "GO5") {
    Serial.println(">> 5회 연속 배출 시작 (주문 1건 = 키캡4 + 축1)");
    for (int i = 1; i <= 5; i++) {
      dispenseOne(i);
      delay(300);
    }
    Serial.println(">> 5회 완료\n");
    return;
  }
  if (upper == "RESET") {
    attachAndMove('R', REAR_OPEN);
    delay(500);
    attachAndMove('F', FRONT_CLOSED);
    Serial.println(">> 초기 상태 복귀 (뒤 열림 / 앞 닫힘)");
    return;
  }
  if (upper == "HELP") { printHelp(); return; }

  // ---- 미세 조정 ----
  if (cmd == "+")  { adjust(1);   return; }
  if (cmd == "-")  { adjust(-1);  return; }
  if (cmd == "++") { adjust(5);   return; }
  if (cmd == "--") { adjust(-5);  return; }

  // ---- 숫자 = 절대 각도 ----
  int angle = cmd.toInt();
  if (angle > 0 || cmd == "0") {
    if (angle >= 0 && angle <= 180) {
      attachAndMove(target, angle);
      Serial.println(">> " + targetName() + " → " + String(angle) + "도");
    } else {
      Serial.println("0~180 사이 숫자를 입력하세요");
    }
    return;
  }

  Serial.println("알 수 없는 명령: " + cmd + "  (HELP 입력 시 도움말)");
}

// ================= 보조 함수 =================

String targetName() {
  return (target == 'R') ? "뒤 스토퍼" : "앞 스토퍼";
}

int currentOf(char t) {
  return (t == 'R') ? rearAngle : frontAngle;
}

void adjust(int delta) {
  int next = currentOf(target) + delta;
  if (next < 0) next = 0;
  if (next > 180) next = 180;
  attachAndMove(target, next);
  Serial.println(">> " + targetName() + " " + String(next) + "도");
}

void attachAndMove(char t, int angle) {
  if (t == 'R') {
    if (!rearAttached) { rearServo.attach(rearPin); rearAttached = true; delay(50); }
    rearAngle = angle;
    rearServo.write(angle);
  } else {
    if (!frontAttached) { frontServo.attach(frontPin); frontAttached = true; delay(50); }
    frontAngle = angle;
    frontServo.write(angle);
  }
}

// ================= 이중 스토퍼 시퀀스 =================

void dispenseOne(int n) {
  Serial.println(">> [" + String(n) + "] 배출 시작");

  attachAndMove('R', REAR_CLOSED);
  Serial.println("   STEP1: 뒤 닫힘 (" + String(REAR_CLOSED) + "도) - 1개 분리");
  delay(500);

  attachAndMove('F', FRONT_OPEN);
  Serial.println("   STEP2: 앞 열림 (" + String(FRONT_OPEN) + "도) - 통과");
  delay(500);

  attachAndMove('F', FRONT_CLOSED);
  Serial.println("   STEP3: 앞 닫힘 (" + String(FRONT_CLOSED) + "도) - 복귀");
  delay(500);

  attachAndMove('R', REAR_OPEN);
  Serial.println("   STEP4: 뒤 열림 (" + String(REAR_OPEN) + "도) - 다음 준비");
  delay(500);

  Serial.println("   >> 배출 완료");
}