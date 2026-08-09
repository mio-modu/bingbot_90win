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

    def _delete(self, path: str, params: dict = None) -> dict:
        params = params or {}
        signed = self._sign(params)
        url = f"{self.base_url}{path}?{signed}"
        resp = self.session.delete(url, timeout=10)
        resp.raise_for_status()
        return resp.json()

    # ────────────────────────────────────────────────
    #  잔고
    # ────────────────────────────────────────────────
    def _usdt_balance_raw(self) -> dict:
        """USDT 잔고 원본 dict (없으면 빈 dict)"""
        data = self._get("/openApi/swap/v2/user/balance")
        bal = data.get("data", {}).get("balance", [])
        # BingX 응답이 dict 단건 / list 두 형태 모두 관측됨 → 양쪽 지원
        if isinstance(bal, dict):
            bal = [bal]
        for asset in bal:
            if isinstance(asset, dict) and asset.get("asset") == "USDT":
                return asset
        return {}

    def get_balance(self) -> float:
        """사용 가능한 USDT 잔고 (마진에 묶인 금액 제외)"""
        return float(self._usdt_balance_raw().get("availableMargin", 0) or 0)

    def get_equity(self) -> float:
        """계좌 순자산 (미실현 손익 포함, 마진 포함)

        봇 장부와 대조할 때 쓴다. availableMargin 은 포지션에 묶인 마진이
        빠져 있어서 보유 중에는 장부와 비교할 수 없다.
        BingX 는 equity 를 주지만 필드가 없을 때를 대비해 단계적으로 대체한다.
        """
        raw = self._usdt_balance_raw()
        for key in ("equity", "balance", "availableMargin"):
            v = raw.get(key)
            if v not in (None, ""):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
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
    #  거래소 강제 손절 (STOP_MARKET)
    # ────────────────────────────────────────────────
    # 봇이 죽거나 인터넷이 끊겨도 거래소가 직접 손절을 집행하도록
    # 진입 즉시 서버 측에 STOP_MARKET 주문을 걸어둔다.
    # 봇의 자체 손절(-$250 등)이 정상 작동하면 이 주문은 발동하지 않는다.

    def place_stop_market(self, symbol: str, position_side: str,
                          stop_price: float, quantity: float) -> dict:
        """포지션 방향 반대편에 STOP_MARKET 청산 주문을 건다 (헤지모드)"""
        side = "SELL" if position_side == "LONG" else "BUY"
        return self._post("/openApi/swap/v2/trade/order", {
            "symbol":       symbol,
            "side":         side,
            "positionSide": position_side,
            "type":         "STOP_MARKET",
            "stopPrice":    stop_price,
            "quantity":     quantity,
        })

    def get_order(self, symbol: str, order_id) -> dict:
        """주문 1건 조회 — **실제 체결가**를 얻기 위한 것.

        봇은 지금까지 체결가를 '마지막 시세 × 고정 슬리피지'로 추정해 왔다.
        시장가 청산은 호가창을 먹고 들어가므로 추정과 실제가 크게 벌어질 수
        있고, 그 차이가 그대로 장부 오차로 쌓인다.
        (실측: 추정 0.002692 / 실제 0.002662 — 1.1% 차이, $64 오차)
        """
        data = self._get("/openApi/swap/v2/trade/order",
                         {"symbol": symbol, "orderId": order_id})
        d = data.get("data", {})
        if isinstance(d, dict):
            return d.get("order", d) or {}
        return {}

    def get_open_orders(self, symbol: str = None) -> list:
        """미체결 주문 목록"""
        params = {}
        if symbol:
            params["symbol"] = symbol
        data = self._get("/openApi/swap/v2/trade/openOrders", params)
        orders = data.get("data", {})
        if isinstance(orders, dict):
            orders = orders.get("orders", [])
        return orders or []

    def cancel_order(self, symbol: str, order_id) -> dict:
        """주문 1건 취소"""
        return self._delete("/openApi/swap/v2/trade/order", {
            "symbol":  symbol,
            "orderId": order_id,
        })

    def cancel_all_open_orders(self, symbol: str) -> dict:
        """해당 심볼의 미체결 주문 전부 취소 (청산 후 고아 STOP 주문 정리)"""
        return self._delete("/openApi/swap/v2/trade/allOpenOrders", {
            "symbol": symbol,
        })

    def get_price_precision(self, symbol: str) -> int:
        """심볼의 가격 소수점 자릿수 (STOP 가격 반올림용)"""
        try:
            for c in self.get_contracts():
                if c.get("symbol") == symbol:
                    # BingX 는 pricePrecision 또는 tickSize 중 하나를 준다
                    if c.get("pricePrecision") is not None:
                        return int(c["pricePrecision"])
                    tick = float(c.get("tickSize", 0) or 0)
                    if tick > 0:
                        import math as _m
                        return max(0, int(round(-_m.log10(tick))))
        except Exception as e:
            logger.warning(f"[price_precision] {symbol} 조회 실패: {e}")
        return 6

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
