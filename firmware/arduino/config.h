#ifndef CONFIG_H
#define CONFIG_H

// ---- WiFi ----
const char* WIFI_SSID = "U+Net3994";
const char* WIFI_PASS = "0CB8577#7G";

// ---- HiveMQ Cloud (MQTT over TLS) ----
// HiveMQ 콘솔 > Overview 에서 확인 가능한 클러스터 URL
const char* MQTT_HOST = "b4ef179c42e5466a920d1de439985a42.s1.eu.hivemq.cloud";
const uint16_t MQTT_PORT = 8883;
const char* MQTT_USER   = "sjuiot_warehouse";
const char* MQTT_PASS   = "sjuiot1234!";

// ---- Flask (Cloud Run) 보고용 엔드포인트 ----
// "https://" 접두어 없이 호스트명만! (HttpClient가 host/port를 따로 받음)
const char* FLASK_HOST = "warehouse-flask-53685477799.asia-northeast3.run.app";
const uint16_t FLASK_PORT = 443;

#endif