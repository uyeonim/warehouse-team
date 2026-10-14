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

// ---------- 테스트용 보드 2개 ----------
// 배출 보드: PCA9685 + 서보 2개 (이중 스토퍼 1레일)
#define BOARD_DISPENSE_IP "172.20.10.2"
// 배출구 보드: 서보 1개
#define BOARD_GATE_IP     "172.20.10.3"

#endif