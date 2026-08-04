// config.h
#ifndef CONFIG_H
#define CONFIG_H

// WiFi 정보
#define WIFI_SSID "****"
#define WIFI_PASSWORD "****"

// Mobius 서버 정보
#define COSS_HOST "https://onem2m.iotcoss.ac.kr"   // 실제 호스트 주소
#define COSS_API_KEY "****"             // API
#define COSS_LECTURF "****"           // 강의코드
#define COSS_ORIGIN "****"              // origin
#define COSS_CREATOR "****"           // 식별코드

// AE/CNT 경로
#define AE_NAME "ae_sjuiot"          // 실제 AE 이름
#define CNT_ORDER_NAME "cnt_order"
#define CNT_STOCK_NAME "cnt_stock"
#define CNT_REPLACE_NAME "cnt_replace"
#define CNT_GATE_NAME "cnt_gate"

// ---------- 보드 IP (각 보드 시리얼모니터에서 확인 후 입력) ----------
#define BOARD_F1_IP "172.20.10.12"   // 1층 - 축 4종
#define BOARD_F2_IP "172.20.10.6"   // 2층 - Red
#define BOARD_F3_IP "172.20.10.7"   // 3층 - Yellow
#define BOARD_F4_IP "172.20.10.8"   // 4층 - Blue
#define BOARD_F5_IP "172.20.10.9"   // 5층 - Green
#define BOARD_GATE_IP "172.20.10.10" // 배출구

#endif