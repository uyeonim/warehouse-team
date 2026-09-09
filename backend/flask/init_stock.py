"""
Redis에 초기 재고를 세팅하는 1회성 스크립트.
반드시 한 번만 실행할 것 - 재실행하면 재고가 다시 세팅됨.

가정: 랙(카트리지)에는 처음부터 색상당 30개씩 이미 장전되어 있는 상태.
      엑셀 표의 각 셀(글자+색상 조합)에서 이미 장전된 30개를 뺀 나머지가
      pool(창고 여유분)로 들어감. pool은 글자 단위가 아니라 색상별로 분리해서 관리.
"""
import os
from dotenv import load_dotenv

load_dotenv()

from upstash_redis import Redis

redis = Redis(
    url=os.environ["UPSTASH_REDIS_REST_URL"],
    token=os.environ["UPSTASH_REDIS_REST_TOKEN"]
)

RAIL_CAPACITY = 30

# 엑셀 표 그대로: letter_color -> 수량 (그린=g, 블루=b, 핑크=r, 옐로우=y)
EXCEL_STOCK = {
    "E_g": 158, "E_b": 157, "E_r": 158, "E_y": 157,
    "I_g": 143, "I_b": 142, "I_r": 143, "I_y": 142,
    "S_g": 168, "S_b": 167, "S_r": 168, "S_y": 167,
    "N_g": 133, "N_b": 132, "N_r": 133, "N_y": 132,
    "T_g": 138, "T_b": 137, "T_r": 138, "T_y": 137,
    "F_g": 163, "F_b": 162, "F_r": 163, "F_y": 162,
    "J_g": 148, "J_b": 147, "J_r": 148, "J_y": 147,
    "P_g": 153, "P_b": 152, "P_r": 153, "P_y": 152,
}

for letter_color, total in EXCEL_STOCK.items():
    stock_amount = min(RAIL_CAPACITY, total)          # 카트리지에 이미 장전된 양
    pool_amount = max(0, total - stock_amount)         # 나머지는 창고(pool)에

    redis.set(f"stock:{letter_color}", stock_amount)
    redis.set(f"pool:{letter_color}", pool_amount)
    print(f"{letter_color}: stock={stock_amount}, pool={pool_amount}")

print("\n초기 재고 세팅 완료")