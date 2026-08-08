"""BingX API 주문 테스트 — 실제 요청/응답 확인용"""
import os, sys, hmac, hashlib, time, urllib.parse, requests
sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

API_KEY    = os.getenv("BINGX_API_KEY", "")
SECRET_KEY = os.getenv("BINGX_SECRET_KEY", "")
BASE_URL   = "https://open-api.bingx.com"

print(f"API_KEY 길이: {len(API_KEY)}, 앞4자: {API_KEY[:4]}")
print(f"SECRET_KEY 길이: {len(SECRET_KEY)}")
print()

def sign(params):
    params["timestamp"] = int(time.time() * 1000)
    query = urllib.parse.urlencode(sorted(params.items()))
    sig = hmac.new(SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    return query + "&signature=" + sig

session = requests.Session()
session.headers.update({"X-BX-APIKEY": API_KEY})

# ── 테스트 1: 레버리지 설정 ──────────────────────────
print("=== 테스트 1: set_leverage ===")
params = {"symbol": "ORDI-USDT", "side": "LONG", "leverage": 8}
signed = sign(params)
url = f"{BASE_URL}/openApi/swap/v2/trade/leverage?{signed}"
print(f"URL: {url[:120]}...")
r = session.post(url)
print(f"응답: {r.text[:300]}")
print()

# ── 테스트 2: 잔고 확인 (auth 테스트) ──────────────
print("=== 테스트 2: get_balance ===")
params = {}
signed = sign(params)
url = f"{BASE_URL}/openApi/swap/v2/user/balance?{signed}"
r = session.get(url)
print(f"응답: {r.text[:300]}")
print()

# ── 테스트 3: 주문 (다양한 방식 시도) ──────────────
print("=== 테스트 3: place_order (BOTH) ===")
params = {"symbol": "ORDI-USDT", "side": "SELL", "positionSide": "BOTH",
          "type": "MARKET", "quantity": 10}
signed = sign(params)
url = f"{BASE_URL}/openApi/swap/v2/trade/order?{signed}"
print(f"URL: {url[:150]}...")
r = session.post(url)
print(f"응답: {r.text[:300]}")
print()

print("=== 테스트 4: place_order (positionSide 없음) ===")
params = {"symbol": "ORDI-USDT", "side": "SELL",
          "type": "MARKET", "quantity": 10}
signed = sign(params)
url = f"{BASE_URL}/openApi/swap/v2/trade/order?{signed}"
r = session.post(url)
print(f"응답: {r.text[:300]}")
