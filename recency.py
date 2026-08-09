"""
최근 흐름 판정 (Recency Engine)
────────────────────────────────
왜 필요한가
-----------
기존 스캐너는 **매매 방향을 20일 일봉 MA 기울기**로 정했다.
그런데 이 봇의 실제 보유 시간은 1~2시간이다 (MAX_COIN_DURATION_MIN=60).

    신호의 시간축: 20일
    거래의 시간축: 1~2시간
    → 480배 차이

20일 평균이 아직 위를 보고 있어도, 어제부터 꺾였으면 지금 롱을 잡는 건
이미 지나간 흐름에 올라타는 것이다. 4h·1h "역행 차단"을 붙여 증상은
막았지만, **방향을 고르는 근거 자체는 여전히 20일 전 데이터**였다.

여기서 위계를 뒤집는다.

    방향 결정 : 1시간봉 (보조: 15분봉)   ← 지금 흐름
    거부권    : 4시간봉, 일봉            ← 큰 그림과 정면충돌만 막는다

거기에 "지금 들어가도 되는 자리인가"를 세 가지로 본다.

    ① 신장도 (extension)  — 이미 평균에서 몇 ATR 벗어났나.
                            많이 벌어졌으면 남은 구간이 짧다.
    ② 나이   (age)        — 이 흐름이 몇 봉째인가. 오래됐으면 늦었다.
    ③ 감쇠   (decay)      — 최근 3봉이 그 전 3봉보다 얼마나 느려졌나.
                            식어가는 흐름에 물타기로 들어가면 못 빠져나온다.

모든 함수는 순수 함수다 (API·전역 상태 없음). 그래야 테스트할 수 있다.
"""

from __future__ import annotations

# ── 방향 판정 임계값 ──────────────────────────────────────
# slope() 는 "MA 가 한 봉 사이에 몇 % 움직였나"를 준다.
# 따라서 같은 숫자라도 시간축이 짧을수록 훨씬 빠른 움직임을 뜻한다.
#
#   0.0020 을 15분봉에 쓰면 = 시간당 0.8%
#   0.0020 을 1시간봉에 쓰면 = 시간당 0.2%
#
# 기존 get_trend() 는 0.002 하나를 모든 시간축에 그대로 썼다.
# 15분봉에는 4배 엄격하고 일봉에는 지나치게 느슨했다.
# 아래는 "시간당 몇 %"로 환산해 맞춘 값이다.
SLOPE_MIN_15M = 0.0008   # 봉당 0.08% = 시간당 0.32%
SLOPE_MIN_1H  = 0.0020   # 봉당 0.20% = 시간당 0.20%
SLOPE_MIN_4H  = 0.0030   # 봉당 0.30% = 시간당 0.075%
SLOPE_MIN_1D  = 0.0020   # 봉당 0.20% (기존 get_trend 와 동일)

# 거부권: 큰 시간축이 "반대로" 이 이상 기울어 있으면 진입 차단
OPPOSE_MAX_4H = 0.0030   # 4h 가 뚜렷하게 반대면 차단
OPPOSE_MAX_1D = 0.0100   # 일봉은 봉당 1.0%(=하루 1%) 이상 반대일 때만 차단.
                         # 일봉이 약하게 반대인 건 흔하다. 그것까지 막으면
                         # 되돌림·반등 구간을 통째로 버린다.


def slope(closes: list, period: int) -> float:
    """이동평균 기울기 (변화율). 데이터가 모자라면 0."""
    if not closes or len(closes) < period + 1:
        return 0.0
    old = closes[-(period + 1):-1]
    new = closes[-period:]
    ma_old = sum(old) / len(old)
    ma_new = sum(new) / len(new)
    if ma_old == 0:
        return 0.0
    return (ma_new - ma_old) / ma_old


def direction_of(sl: float, threshold: float) -> str:
    if sl > threshold:
        return "UP"
    if sl < -threshold:
        return "DOWN"
    return "SIDEWAYS"


def _sign(trend: str) -> int:
    return 1 if trend == "UP" else (-1 if trend == "DOWN" else 0)


# ────────────────────────────────────────────────────────────
#  ① 방향 결정 — 최근이 정하고, 과거는 거부권만
# ────────────────────────────────────────────────────────────
def decide_direction(closes_15m: list, closes_1h: list,
                     closes_4h: list, closes_1d: list) -> dict:
    """
    반환:
      {
        "trend":  "UP" | "DOWN" | None,     # None = 진입하지 않음
        "reason": 사람이 읽는 사유,
        "src":    무엇이 방향을 정했나 ("1h" | "15m"),
        "s15","s1h","s4h","s1d": 각 기울기,
        "agree":  근거리 두 축이 일치했는가,
      }
    """
    s15 = slope(closes_15m, 8)    # 8봉 × 15분 = 2시간
    s1h = slope(closes_1h,  6)    # 6봉 × 1시간 = 6시간
    s4h = slope(closes_4h,  6)    # 6봉 × 4시간 = 24시간
    s1d = slope(closes_1d, 20)    # 20일

    d15 = direction_of(s15, SLOPE_MIN_15M)
    d1h = direction_of(s1h, SLOPE_MIN_1H)
    d4h = direction_of(s4h, SLOPE_MIN_4H)
    d1d = direction_of(s1d, SLOPE_MIN_1D)

    out = {"s15": s15, "s1h": s1h, "s4h": s4h, "s1d": s1d,
           "d15": d15, "d1h": d1h, "d4h": d4h, "d1d": d1d,
           "trend": None, "src": None, "agree": False, "reason": ""}

    # ── 방향은 근거리에서 나온다 ────────────────────────
    if d1h != "SIDEWAYS" and d15 != "SIDEWAYS":
        if d1h != d15:
            out["reason"] = f"근거리 충돌 (1h={d1h} 15m={d15})"
            return out
        trend, src, agree = d1h, "1h", True
    elif d1h != "SIDEWAYS":
        # 15분봉은 조용한데 1시간 흐름은 살아 있다 → 눌림목일 수 있다.
        # 허용하되 일치 보너스는 주지 않는다.
        trend, src, agree = d1h, "1h", False
    elif d15 != "SIDEWAYS":
        # 1시간은 아직 평평한데 15분이 막 움직이기 시작했다 → 초입.
        # 가장 신선한 자리지만 가짜 신호도 가장 많다.
        trend, src, agree = d15, "15m", False
    else:
        out["reason"] = "근거리 방향 없음 (15m·1h 모두 횡보)"
        return out

    sg = _sign(trend)

    # ── 거부권: 4시간봉이 정면으로 반대 ─────────────────
    if _sign(d4h) == -sg and abs(s4h) > OPPOSE_MAX_4H:
        out["reason"] = f"4h 역행 ({s4h:+.3%})"
        return out

    # ── 거부권: 일봉이 강하게 반대 ──────────────────────
    # 약한 반대는 통과시킨다. 되돌림 구간을 전부 버리게 되기 때문.
    if _sign(d1d) == -sg and abs(s1d) > OPPOSE_MAX_1D:
        out["reason"] = f"일봉 강한 역행 ({s1d:+.3%})"
        return out

    out.update(trend=trend, src=src, agree=agree,
               reason=f"{src} 기준 {trend}" + (" (15m·1h 일치)" if agree else ""))
    return out


# ────────────────────────────────────────────────────────────
#  ② 신장도 — 평균에서 몇 ATR 벗어나 있나
# ────────────────────────────────────────────────────────────
def atr_of(klines_hl: list, period: int = 14) -> float:
    """(high-low) 평균. klines_hl 은 (high, low, close) 튜플 리스트."""
    if not klines_hl:
        return 0.0
    rows = klines_hl[-period:]
    rng = [h - l for h, l, _ in rows if h >= l]
    return sum(rng) / len(rng) if rng else 0.0


def extension_atr(price: float, closes: list, atr: float,
                  ma_period: int = 6) -> float:
    """
    현재가가 단기 이동평균에서 몇 ATR 떨어져 있나 (부호 있음).
    양수 = 위쪽, 음수 = 아래쪽.

    롱인데 +3 ATR 이면 이미 크게 오른 뒤다. 여기서 물타기를 시작하면
    되돌림 한 번에 여러 단계가 한꺼번에 물린다.
    """
    if atr <= 0 or len(closes) < ma_period:
        return 0.0
    ma = sum(closes[-ma_period:]) / ma_period
    return (price - ma) / atr


# ────────────────────────────────────────────────────────────
#  ③ 나이 — 이 흐름이 몇 봉째인가
# ────────────────────────────────────────────────────────────
def trend_age(closes: list, trend: str, ma_period: int = 6) -> int:
    """
    종가가 단기 MA 의 트렌드 쪽에 연속으로 머문 봉 수.

    3 이면 방금 돌아선 신선한 흐름, 25 면 한참 전에 시작해 이미
    많이 진행된 흐름이다. 물타기 봇은 "이제 막 시작한" 쪽이 유리하다.
    되돌림 여력이 남아 있어야 물탄 물량이 회복된다.
    """
    if trend not in ("UP", "DOWN") or len(closes) < ma_period + 1:
        return 0
    sg = _sign(trend)
    age = 0
    # 뒤에서부터 거슬러 올라가며 MA 대비 위치가 유지되는 구간을 센다
    for i in range(len(closes) - 1, ma_period - 2, -1):
        window = closes[i - ma_period + 1:i + 1]
        if len(window) < ma_period:
            break
        ma = sum(window) / ma_period
        if (closes[i] - ma) * sg > 0:
            age += 1
        else:
            break
    return age


# ────────────────────────────────────────────────────────────
#  ④ 감쇠 — 식어가는 흐름인가
# ────────────────────────────────────────────────────────────
REVERSAL_MOMENTUM = 2.0   # 갓 돌아선 흐름에 주는 값


def momentum_ratio(closes: list, trend: str, half: int = 3) -> float:
    """
    최근 half 봉의 이동량 ÷ 그 직전 half 봉의 이동량 (트렌드 방향 기준).

      2.0       → 방금 반대에서 돌아섰다 (가장 신선)
      1.0 이상  → 가속 중
      0.5 근처  → 유지
      0.15 아래 → 멈췄거나 되돌리는 중 → 진입 차단

    ⚠ 단순히 recent/prior 를 반환하면 안 된다.
      갓 반전한 흐름은 직전 구간이 **반대 방향**이라 prior 가 음수다.
      그러면 비율이 음수로 나와서, 가장 강한 가속을 "감쇠"로 읽고
      제일 좋은 자리를 차단해 버린다. 부호를 나눠서 처리한다.

    직전 구간이 거의 안 움직였으면(분모≈0) 판단 불가로 보고 1.0 을 준다.
    없는 근거로 진입을 막지 않기 위해서다.
    """
    need = half * 2 + 1
    if trend not in ("UP", "DOWN") or len(closes) < need:
        return 1.0
    sg = _sign(trend)
    recent = (closes[-1]        - closes[-1 - half])     * sg
    prior  = (closes[-1 - half] - closes[-1 - half * 2]) * sg
    base   = closes[-1 - half * 2]
    if base <= 0:
        return 1.0

    # 직전 구간 이동이 전체 가격의 0.05% 미만이면 분모로 쓰기엔 너무 작다
    if abs(prior) / base < 0.0005:
        return 1.0

    if prior < 0:
        # 직전에는 반대로 갔다 = 반전 국면
        #   지금 우리 방향으로 가고 있으면 가장 신선한 자리
        #   지금도 반대로 가고 있으면 우리 방향이 아니다
        return REVERSAL_MOMENTUM if recent > 0 else 0.0

    return recent / prior


# ────────────────────────────────────────────────────────────
#  종합 판정
# ────────────────────────────────────────────────────────────
def freshness_verdict(price: float, closes_1h: list, hl_1h: list, trend: str,
                      max_extension_atr: float,
                      max_age: int,
                      min_momentum: float) -> dict:
    """
    "지금 이 자리에 들어가도 되는가"

    반환: {"ok": bool, "why": str, "ext": float, "age": int, "mom": float,
           "bonus": float}
    bonus 는 점수 배율 (신선할수록 큼, 0.6 ~ 1.6).
    """
    atr = atr_of(hl_1h)
    ext = extension_atr(price, closes_1h, atr)
    age = trend_age(closes_1h, trend)
    mom = momentum_ratio(closes_1h, trend)
    sg  = _sign(trend)

    # 트렌드 방향으로의 신장도만 문제 삼는다.
    # (롱인데 평균 아래에 있는 건 오히려 좋은 자리다)
    ext_fwd = ext * sg

    # ── 차단 조건 ────────────────────────────────────────
    # ① 과신장 — 이미 크게 벌어진 자리. 물타기로 들어가면 되돌림 한 번에
    #    여러 단계가 한꺼번에 물린다. 가장 치명적이므로 차단한다.
    if ext_fwd > max_extension_atr:
        return {"ok": False, "why": f"과신장 {ext_fwd:.1f}ATR > {max_extension_atr}",
                "ext": ext_fwd, "age": age, "mom": mom, "bonus": 0.0}
    # ② 모멘텀 감쇠 — 흐름이 멈췄거나 되돌리는 중.
    if mom < min_momentum:
        return {"ok": False, "why": f"모멘텀 감쇠 {mom:.2f} < {min_momentum}",
                "ext": ext_fwd, "age": age, "mom": mom, "bonus": 0.0}

    # ③ 나이는 **차단하지 않는다** — 점수만 깎는다.
    #    처음엔 "오래된 흐름 = 나쁨"으로 차단하려 했는데 틀렸다.
    #    30시간째 오르는 코인이 방금 평균까지 눌렸다면(신장도 작음)
    #    그건 오히려 좋은 자리다. 추세 + 눌림목이니까.
    #    늦었는지 아닌지는 나이가 아니라 **신장도**가 말해준다.
    #    나이는 "얼마나 남았을까"에 대한 약한 힌트일 뿐이다.

    # ── 점수 배율 ────────────────────────────────────────
    # 신선(나이 적음) + 아직 안 벌어짐(신장도 작음) + 가속(모멘텀 높음)
    age_b = (1.25 if age <= max(2, max_age // 4)
             else 1.0 if age <= max_age
             else 0.8)
    ext_b = (1.2 if ext_fwd <= max_extension_atr * 0.4
             else 1.0 if ext_fwd <= max_extension_atr * 0.7
             else 0.8)
    mom_b = (1.2 if mom >= 1.0
             else 1.0 if mom >= 0.6
             else 0.85)
    bonus = age_b * ext_b * mom_b

    return {"ok": True,
            "why": f"신선 (나이 {age}봉, 신장 {ext_fwd:+.1f}ATR, 모멘텀 {mom:.2f})",
            "ext": ext_fwd, "age": age, "mom": mom, "bonus": round(bonus, 3)}
