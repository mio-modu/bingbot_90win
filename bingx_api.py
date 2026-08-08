"""
BingX API 클라이언트
- 인증(HMAC-SHA256)
- 주문 / 포지션 / 잔고 / 시세 조회
"""

import hashlib
import hmac
import time
import urllib.parse
import requests
import logging
from config import API_KEY, SECRET_KEY, BASE_URL

logger = logging.getLogger(__name__)


class BingXAPI:
    def __init__(self):
        self.api_key    = API_KEY
        self.secret_key = SECRET_KEY
        self.base_url   = BASE_URL
        self.session    = requests.Session()
        self.session.headers.update({
            "X-BX-APIKEY": self.api_key,
        })
        self._time_offset_ms = 0
        self._sync_server_time()

    def _sync_server_time(self):
        """BingX 서버 시간과 로컬 시계 오프셋 계산 (타임스탬프 불일치 방지)"""
        try:
            resp = self.session.get(
                f"{self.base_url}/openApi/swap/v2/server/time", timeout=5
            )
            server_ms = resp.json().get("data", {}).get("serverTime", 0)
            if server_ms:
                self._time_offset_ms = int(server_ms) - int(time.time() * 1000)
                logger.info(f"[시간동기화] 서버 오프셋: {self._time_offset_ms}ms")
        except Exception as e:
            logger.warning(f"[시간동기화 실패] {e} — 로컬 시간 사용")

    # ────────────────────────────────────────────────
    #  인증
    # ────────────────────────────────────────────────
    def _sign(self, params: dict) -> str:
        params["timestamp"] = int(time.time() * 1000) + self._time_offset_ms
        query = urllib.parse.urlencode(sorted(params.items()))
        signature = hmac.new(
            self.secret_key.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        return query + "&signature=" + signature

    def _get(self, path: str, params: dict = None) -> dict:
        params = params or {}
        signed = self._sign(params)
        url = f"{self.base_url}{path}?{signed}"
        resp = self.session.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, params: dict = None) -> dict:
        params = params or {}
        signed = self._sign(params)
        url = f"{self.base_url}{path}?{signed}"
        resp = self.session.post(url, timeout=10)
        resp.raise_for_status()
        return resp.json()

    # ────────────────────────────────────────────────
    #  잔고
    # ────────────────────────────────────────────────
    def get_balance(self) -> float:
        """사용 가능한 USDT 잔고 반환"""
        data = self._get("/openApi/swap/v2/user/balance")
        bal = data.get("data", {}).get("balance", [])
        # BingX 응답이 dict 단건 / list 두 형태 모두 관측됨 → 양쪽 지원
        if isinstance(bal, dict):
            bal = [bal]
        for asset in bal:
            if isinstance(asset, dict) and asset.get("asset") == "USDT":
                return float(asset.get("availableMargin", 0))
        return 0.0

    # ────────────────────────────────────────────────
    #  시세
    # ────────────────────────────────────────────────
    def get_ticker(self, symbol: str) -> dict:
        """24h 티커 정보"""
        data = self._get("/openApi/swap/v2/quote/ticker", {"symbol": symbol})
        return data.get("data", {})

    def get_price(self, symbol: str) -> float:
        """현재가"""
        data = self._get("/openApi/swap/v2/quote/price", {"symbol": symbol})
        return float(data.get("data", {}).get("price", 0))

    def get_klines(self, symbol: str, interval: str, limit: int = 100) -> list:
        """캔들 데이터
        interval: 1m, 5m, 15m, 30m, 1h, 4h, 1d
        반환: [ [timestamp, open, high, low, close, volume], ... ]
        """
        data = self._get("/openApi/swap/v2/quote/klines", {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        })
        return data.get("data", [])

    def get_all_tickers(self) -> list:
        """전체 종목 24h 티커 리스트"""
        data = self._get("/openApi/swap/v2/quote/ticker")
        return data.get("data", [])

    def get_contracts(self) -> list:
        """선물 컨트랙트 정보 (수량 최소단위 등)"""
        try:
            resp = self.session.get(
                f"{self.base_url}/openApi/swap/v2/quote/contracts", timeout=10
            )
            resp.raise_for_status()
            return resp.json().get("data", [])
        except Exception as e:
            logger.warning(f"[contracts] 조회 실패: {e}")
            return []

    # ────────────────────────────────────────────────
    #  레버리지 설정
    # ────────────────────────────────────────────────
    def set_leverage(self, symbol: str, leverage: int, side: str = "LONG") -> dict:
        """레버리지 설정 (헤지모드: LONG/SHORT 각각 설정)"""
        return self._post("/openApi/swap/v2/trade/leverage", {
            "symbol":   symbol,
            "side":     side,
            "leverage": leverage
        })

    # ────────────────────────────────────────────────
    #  주문
    # ────────────────────────────────────────────────
    def place_order(
        self,
        symbol: str,
        side: str,           # BUY / SELL
        position_side: str,  # LONG / SHORT (헤지모드)
        quantity: float,
        order_type: str = "MARKET"
    ) -> dict:
        """시장가 주문 (헤지모드 — positionSide=LONG/SHORT)"""
        params = {
            "symbol":       symbol,
            "side":         side,
            "positionSide": position_side,
            "type":         order_type,
            "quantity":     quantity
        }
        return self._post("/openApi/swap/v2/trade/order", params)

    def close_position(self, symbol: str, position_side: str = "LONG", qty: float = None) -> dict:
        """포지션 전량 청산 (헤지모드 — positionSide=LONG/SHORT).
        qty 지정 시 해당 수량으로 주문, 미지정 시 closePosition=true 먼저 시도 후
        109400 에러 시 실제 수량 조회해 재시도."""
        side = "SELL" if position_side == "LONG" else "BUY"
        if qty is not None:
            return self._post("/openApi/swap/v2/trade/order", {
                "symbol":       symbol,
                "side":         side,
                "positionSide": position_side,
                "type":         "MARKET",
                "quantity":     qty,
            })
        # qty 없으면 closePosition 먼저 시도
        result = self._post("/openApi/swap/v2/trade/order", {
            "symbol":        symbol,
            "side":          side,
            "positionSide":  position_side,
            "type":          "MARKET",
            "closePosition": "true",
        })
        if str(result.get("code", "?")) == "0":
            return result
        # closePosition 실패(109400 등) → 실제 수량 조회 후 재시도
        try:
            for p in self.get_positions(symbol):
                if p.get("positionSide") == position_side:
                    real_qty = abs(float(p.get("positionAmt", 0)))
                    if real_qty > 0:
                        return self._post("/openApi/swap/v2/trade/order", {
                            "symbol":       symbol,
                            "side":         side,
                            "positionSide": position_side,
                            "type":         "MARKET",
                            "quantity":     real_qty,
                        })
        except Exception:
            pass
        return result

    # ────────────────────────────────────────────────
    #  포지션 조회
    # ────────────────────────────────────────────────
    def get_positions(self, symbol: str = None) -> list:
        """현재 보유 포지션 목록"""
        params = {}
        if symbol:
            params["symbol"] = symbol
        data = self._get("/openApi/swap/v2/user/positions", params)
        return data.get("data", [])

    def get_open_position(self, symbol: str) -> dict | None:
        """특정 심볼의 롱 포지션 반환 (없으면 None)"""
        positions = self.get_positions(symbol)
        for p in positions:
            if (p.get("symbol") == symbol and
                    p.get("positionSide") == "LONG" and
                    float(p.get("positionAmt", 0)) > 0):
                return p
        return None
