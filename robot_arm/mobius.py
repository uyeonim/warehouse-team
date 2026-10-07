# -*- coding: utf-8 -*-
"""
Mobius(oneM2M) 게시판에 글을 읽고 쓰는 공용 함수 모음.
다른 스크립트에서 `import mobius` 로 가져다 씁니다.

  get_latest(path)            폴더의 최신 글 읽기           (HTTP GET)
  create_container(parent, n) 폴더 만들기                  (HTTP POST, ty=3)
  create_cin(path, con)       폴더에 글 한 장 쓰기           (HTTP POST, ty=4)
  create_subscription(...)    폴더에 "새 글 오면 알려줘" 등록 (HTTP POST, ty=23)
  get_resource(path)          폴더/구독 자체 정보 읽기       (HTTP GET)
  list_children(path)         폴더 아래 리소스 목록           (HTTP GET ?fu=1)
  list_cins(path)             폴더의 글 전부 (시각, 내용)      (HTTP GET)
  delete_resource(path)       폴더/구독 통째로 삭제          (HTTP DELETE)
"""

import os
import uuid

import requests
from dotenv import load_dotenv

load_dotenv()  # 같은 폴더의 .env 파일을 읽어서 환경변수로 등록

_REQUIRED = ["MOBIUS_URL", "MOBIUS_CB", "MOBIUS_AE_RN",
             "MOBIUS_AEI", "MOBIUS_API_KEY", "MOBIUS_CUSTOM_LECTURE", "MOBIUS_CUSTOM_CREATOR"]
_missing = [k for k in _REQUIRED if not os.environ.get(k)]
if _missing:
    raise SystemExit(f".env 파일에 값이 비어 있습니다: {', '.join(_missing)}")

# 예: https://onem2m.iotcoss.ac.kr/Mobius/ae_sjuiot_keyring_2026
BASE_URL = f"{os.environ['MOBIUS_URL']}/{os.environ['MOBIUS_CB']}/{os.environ['MOBIUS_AE_RN']}"
TIMEOUT = 5


def _headers(ty=None):
    """모든 요청에 붙는 인증 헤더. ty 가 있으면 '무엇을 만드는 요청인지' 표시."""
    headers = {
        "X-M2M-RI": str(uuid.uuid4()),                  # 요청 번호 (매번 새로)
        "X-M2M-Origin": os.environ["MOBIUS_AEI"],       # 누가 보내는지
        "X-API-KEY": os.environ["MOBIUS_API_KEY"],
        "X-AUTH-CUSTOM-LECTURE": os.environ["MOBIUS_CUSTOM_LECTURE"],
        "X-AUTH-CUSTOM-CREATOR": os.environ["MOBIUS_CUSTOM_CREATOR"],
        "Accept": "application/json",
    }
    if ty is not None:
        headers["Content-Type"] = f"application/json;ty={ty}"  # 3=폴더(cnt), 4=글(cin), 23=구독(sub)
    return headers


def _url(path):
    return f"{BASE_URL}/{path}" if path else BASE_URL


def _fail(resp, what):
    raise RuntimeError(f"{what} 실패: HTTP {resp.status_code} {resp.text}")


def get_latest(path):
    """path 폴더의 최신 글 내용(con)을 돌려줌. 글이 하나도 없으면 None."""
    resp = requests.get(f"{_url(path)}/la", headers=_headers(), timeout=TIMEOUT)
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        _fail(resp, f"{path} 읽기")
    return resp.json()["m2m:cin"]["con"]


def create_container(parent, name):
    """parent 폴더 아래에 name 폴더를 만듦. 새로 만들면 True, 이미 있으면 False."""
    body = {"m2m:cnt": {"rn": name}}
    resp = requests.post(_url(parent), headers=_headers(ty=3), json=body, timeout=TIMEOUT)
    if resp.status_code == 409:  # 409 Conflict = 같은 이름이 이미 있음
        return False
    if resp.status_code != 201:
        _fail(resp, f"{parent}/{name} 폴더 만들기")
    return True


def create_cin(path, con):
    """path 폴더에 글 한 장을 씀. con 은 dict 그대로 (문자열로 바꾸지 않음)."""
    body = {"m2m:cin": {"con": con}}
    resp = requests.post(_url(path), headers=_headers(ty=4), json=body, timeout=TIMEOUT)
    if resp.status_code != 201:
        _fail(resp, f"{path} 글쓰기")


def get_resource(path):
    """폴더나 구독 자체의 정보(글 내용이 아니라 설정값)를 dict 로 돌려줌. 없으면 None."""
    resp = requests.get(_url(path), headers=_headers(), timeout=TIMEOUT)
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        _fail(resp, f"{path} 정보 읽기")
    return resp.json()


def list_children(path):
    """path 아래 모든 리소스 경로 목록 (폴더·글·구독 포함). 없으면 None."""
    resp = requests.get(f"{_url(path)}?fu=1", headers=_headers(), timeout=TIMEOUT)
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        _fail(resp, f"{path} 목록 읽기")
    return resp.json().get("m2m:uril", [])


def list_cins(path):
    """path 폴더에 바로 들어 있는 글(CIN) 전부를 [(생성시각 ct, con), ...] 오래된 순으로. 폴더가 없으면 []."""
    depth = len(_url(path).split("/"))
    uris = [u for u in (list_children(path) or [])
            if u.rsplit("/", 1)[-1].startswith("4-") and len(f"{os.environ['MOBIUS_URL']}/{u}".split("/")) == depth + 1]
    rows = []
    for uri in uris:
        resp = requests.get(f"{os.environ['MOBIUS_URL']}/{uri}", headers=_headers(), timeout=TIMEOUT)
        if resp.status_code == 200:
            cin = resp.json()["m2m:cin"]
            rows.append((cin["ct"], cin["con"]))
    return sorted(rows, key=lambda r: r[0])


def delete_resource(path):
    """폴더/구독을 안의 내용까지 통째로 삭제 (되돌릴 수 없음). 삭제하면 True, 원래 없으면 False."""
    resp = requests.delete(_url(path), headers=_headers(), timeout=TIMEOUT)
    if resp.status_code == 404:
        return False
    if resp.status_code not in (200, 202, 204):
        _fail(resp, f"{path} 삭제")
    return True


def create_subscription(path, name, notify_url):
    """path 폴더에 새 글(cin)이 생기면 notify_url 로 알림(POST)을 보내달라고 등록.
    새로 만들면 True, 같은 이름이 이미 있으면 False.
    ※ 등록 순간 Mobius 가 notify_url 로 '확인용 알림'을 먼저 보내므로 서버가 켜져 있어야 함."""
    body = {"m2m:sub": {
        "rn": name,
        "nu": [notify_url],     # 알림 받을 주소
        "enc": {"net": [3]},    # 3 = 자식 리소스(새 글) 생성 시 알림
        "nct": 1,               # 1 = 알림에 새 글 전체를 담아서 보냄
    }}
    resp = requests.post(_url(path), headers=_headers(ty=23), json=body, timeout=15)
    if resp.status_code == 409:
        return False
    if resp.status_code != 201:
        _fail(resp, f"{path}/{name} 구독 등록")
    return True
