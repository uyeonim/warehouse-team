import os
import requests
import json
import random
import string
from dotenv import load_dotenv

load_dotenv()

COSS_BASE_URL = "https://onem2m.iotcoss.ac.kr/Mobius"
AE_NAME = "ae_sjuiot"
CNT_NAME = "cnt_order"
CNT_ORDER_PATH = f"{COSS_BASE_URL}/{AE_NAME}/{CNT_NAME}"

ORIGIN = os.getenv("COSS_ORIGIN")
API_KEY = os.getenv("COSS_API_KEY")
CREATOR = os.getenv("COSS_CREATOR")
LECTURE = os.getenv("COSS_LECTURE")

headers = {
    "X-M2M-Origin": ORIGIN,
    "X-M2M-RI": "test_order_" + "".join(random.choices(string.digits, k=6)),
    "X-M2M-RVI": "3",
    "Content-Type": "application/vnd.onem2m-res+json;ty=4",
    "X-API-KEY": API_KEY,
    "X-AUTH-CUSTOM-CREATOR": CREATOR,
    "X-AUTH-CUSTOM-LECTURE": LECTURE
}

def random_keycap():
    axis1 = random.choice(["E", "E"])
    axis2 = random.choice(["N", "S"])
    axis3 = random.choice(["F", "T"])
    axis4 = random.choice(["J", "P"])
    return axis1 + axis2 + axis3 + axis4

BOARD_COLORS = ["red", "black", "blue", "brown"]
KEYCAP_COLORS = ["r", "r", "r", "r"]

def random_order():
    return {
        "order_id": "order_test_" + "".join(random.choices(string.digits, k=4)),
        "station_id": str(random.randint(1, 4)),
        "order_seq": random.randint(1, 999),
        "board": random.choice(BOARD_COLORS),
        "keycap": random_keycap(),
        "colors": [random.choice(KEYCAP_COLORS) for _ in range(4)],
    }

order_content = random_order()

body = {
    "m2m:cin": {
        "con": json.dumps(order_content)
    }
}

print("생성된 주문:", json.dumps(order_content, ensure_ascii=False, indent=2))

res = requests.post(CNT_ORDER_PATH, headers=headers, data=json.dumps(body))
print("Status Code:", res.status_code)
print("Response:", res.text)