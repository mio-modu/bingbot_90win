"""
최근 흐름 엔진 테스트
──────────────────────
    python tests/test_recency.py

검증하는 것:
  · 방향을 최근(1h/15m)이 정하고 과거(일봉)는 거부권만 갖는가
  · 20일 추세는 위인데 최근이 꺾인 코인을 걸러내는가 ← "철 지난 흐름"의 핵심
  · 이미 크게 벌어진 자리(과신장)를 거부하는가
  · 식어가는 흐름(모멘텀 감쇠)을 거부하는가
  · 오래됐지만 눌린 자리는 **통과**시키는가 (나이만으로 막지 않는다)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import recency                                          # noqa: E402


def line(start, step, n):
    """일정한 기울기의 종가 열"""
    return [start + step * i for i in range(n)]


def hl(closes, rng):
    """종가 열 → (고가, 저가, 종가) 열. rng 는 봉 하나의 폭."""
    return [(c + rng / 2, c - rng / 2, c) for c in closes]


# ── 1. 방향 결정 ─────────────────────────────────────────────

def test_recent_up_gives_up():
    print("\n[1] 최근이 오르면 UP")
    v = recency.decide_direction(
        closes_15m=line(100, 0.15, 20),
        closes_1h=line(100, 0.4, 40),
        closes_4h=line(100, 1.0, 20),
        closes_1d=line(100, 2.0, 30))
    print(f"    {v['trend']} / {v['reason']}")
    assert v["trend"] == "UP", v
    assert v["agree"] is True, "15m·1h 가 같은 방향인데 일치로 안 잡혔다"
    print("    ✅")


def test_stale_daily_uptrend_but_recent_down_is_blocked():
    """★ 이 봇이 계속 지던 자리 — 20일은 위, 지금은 아래"""
    print("\n[2] ★ 일봉은 상승인데 최근은 하락 → 롱 안 잡는다")
    v = recency.decide_direction(
        closes_15m=line(100, -0.20, 20),    # 지금 내려간다
        closes_1h=line(100, -0.50, 40),     # 6시간째 내려간다
        closes_4h=line(100, -1.0, 20),      # 하루째 내려간다
        closes_1d=line(100, 0.5, 30))       # 20일 평균은 아직 완만한 상승
    print(f"    방향 {v['trend']} (일봉 기울기 {v['s1d']:+.2%})")
    assert v["trend"] != "UP", \
        "일봉 상승 라벨에 끌려가면 안 된다 — 지금 흐름은 하락이다"
    assert v["trend"] == "DOWN", v["reason"]
    print("    ✅ 최근 흐름을 따라 DOWN (예전 구조라면 UP 으로 잡았다)")


def test_very_strong_daily_uptrend_blocks_the_counter_short():
    """같은 상황이라도 일봉이 **아주** 강하면 역방향 숏도 막는다"""
    print("\n[2b] 일봉이 아주 강한 상승이면 최근이 꺾여도 숏은 안 잡는다")
    v = recency.decide_direction(
        closes_15m=line(100, -0.20, 20),
        closes_1h=line(100, -0.50, 40),
        closes_4h=line(100, -1.0, 20),
        closes_1d=line(100, 3.0, 30))       # 하루 3% 씩 20일 = 강한 상승
    print(f"    방향 {v['trend']} / {v['reason']}")
    assert v["trend"] is None, "강한 상승 추세에 역행 숏을 잡으면 안 된다"
    print("    ✅ 진입 안 함 (롱도 숏도 아님)")


def test_strong_daily_opposition_vetoes():
    print("\n[3] 일봉이 강하게 반대면 거부권 발동")
    v = recency.decide_direction(
        closes_15m=line(100, 0.20, 20),
        closes_1h=line(100, 0.50, 40),
        closes_4h=line(100, 0.30, 20),
        closes_1d=line(100, -5.0, 30))      # 일봉이 강하게 하락
    print(f"    방향 {v['trend']} / {v['reason']}")
    assert v["trend"] is None, "일봉 강한 역행인데 진입 허용됐다"
    print("    ✅ 차단")


def test_weak_daily_opposition_allowed():
    """약한 일봉 역행까지 막으면 되돌림 구간을 통째로 버린다"""
    print("\n[4] 일봉이 **약하게** 반대면 통과")
    v = recency.decide_direction(
        closes_15m=line(100, 0.20, 20),
        closes_1h=line(100, 0.50, 40),
        closes_4h=line(100, 0.30, 20),
        closes_1d=line(100, -0.03, 30))     # 20일 기울기 ≈ -0.6%
    print(f"    일봉 기울기 {v['s1d']:+.2%} → 방향 {v['trend']}")
    assert v["trend"] == "UP", "약한 일봉 역행까지 막으면 기회가 없다"
    print("    ✅ 통과")


def test_4h_opposition_vetoes():
    print("\n[5] 4시간봉이 반대면 거부")
    v = recency.decide_direction(
        closes_15m=line(100, 0.20, 20),
        closes_1h=line(100, 0.50, 40),
        closes_4h=line(100, -1.5, 20),
        closes_1d=line(100, 0.5, 30))
    print(f"    {v['reason']}")
    assert v["trend"] is None
    print("    ✅ 차단")


def test_near_term_conflict_is_skipped():
    print("\n[6] 15m 과 1h 가 서로 반대면 진입 안 함")
    v = recency.decide_direction(
        closes_15m=line(100, -0.30, 20),
        closes_1h=line(100, 0.50, 40),
        closes_4h=line(100, 0.5, 20),
        closes_1d=line(100, 1.0, 30))
    print(f"    {v['reason']}")
    assert v["trend"] is None
    print("    ✅ 차단")


def test_flat_market_is_skipped():
    print("\n[7] 근거리가 전부 횡보면 진입 안 함")
    v = recency.decide_direction(
        closes_15m=[100.0] * 20,
        closes_1h=[100.0] * 40,
        closes_4h=line(100, 1.0, 20),
        closes_1d=line(100, 2.0, 30))
    print(f"    {v['reason']}")
    assert v["trend"] is None
    print("    ✅ 차단")


# ── 2. 신선도 ────────────────────────────────────────────────

def test_overextended_is_blocked():
    print("\n[8] ★ 이미 크게 벌어진 자리(과신장) 거부")
    # 봉 폭(ATR)은 0.2 인데 마지막에 3.0 이 수직으로 튀었다 = 15 ATR
    closes = line(100, 0.05, 40)
    closes[-1] += 3.0
    v = recency.freshness_verdict(
        price=closes[-1], closes_1h=closes, hl_1h=hl(closes, 0.2), trend="UP",
        max_extension_atr=2.5, max_age=18, min_momentum=0.15)
    print(f"    {v['why']}")
    assert v["ok"] is False and "과신장" in v["why"], v
    print("    ✅ 꼭대기 추격 진입 차단")


def test_decaying_momentum_is_blocked():
    print("\n[9] ★ 식어가는 흐름(모멘텀 감쇠) 거부")
    # 33봉 동안 봉당 +0.5 로 오르다가 최근 3봉에서 사실상 멈춤
    #   직전 3봉 이동 +1.5  vs  최근 3봉 이동 +0.08  →  비율 0.05
    closes = line(100, 0.5, 34) + [116.53, 116.56, 116.58]
    v = recency.freshness_verdict(
        price=closes[-1], closes_1h=closes, hl_1h=hl(closes, 0.5), trend="UP",
        max_extension_atr=2.5, max_age=18, min_momentum=0.15)
    print(f"    {v['why']} (모멘텀 {v['mom']:.2f})")
    assert v["ok"] is False and "모멘텀" in v["why"], v
    print("    ✅ 멈춘 흐름 차단")


def test_old_trend_pulled_back_is_allowed():
    """★ 나이만으로 막지 않는다 — 오래됐어도 눌렸으면 좋은 자리다"""
    print("\n[10] ★ 오래된 흐름 + 평균까지 눌림 → 통과해야 한다")
    closes = line(100, 0.5, 40)          # 40봉 내내 상승 = 나이 많음
    v = recency.freshness_verdict(
        price=closes[-1], closes_1h=closes, hl_1h=hl(closes, 1.5), trend="UP",
        max_extension_atr=2.5, max_age=18, min_momentum=0.15)
    print(f"    나이 {v['age']}봉 / 신장 {v['ext']:+.2f}ATR → {v['ok']} ({v['why']})")
    assert v["age"] > 18, "이 캔들은 오래된 흐름이어야 한다"
    assert v["ok"] is True, "나이만으로 막으면 추세+눌림목을 통째로 버린다"
    assert v["bonus"] < 1.0 or v["bonus"] <= 1.25, "오래된 흐름은 점수 가산이 없어야"
    print("    ✅ 통과하되 점수는 가산 없음")


def test_fresh_trend_gets_bonus():
    print("\n[11] 갓 돌아선 흐름은 점수 가산")
    # 앞쪽은 하락, 최근 3봉만 상승 반전 → 나이 짧음
    closes = line(110, -0.5, 36) + [92.6, 93.4, 94.3]
    v = recency.freshness_verdict(
        price=closes[-1], closes_1h=closes, hl_1h=hl(closes, 1.0), trend="UP",
        max_extension_atr=2.5, max_age=18, min_momentum=0.15)
    print(f"    나이 {v['age']}봉 / 배율 {v['bonus']}")
    assert v["age"] <= 5, f"방금 돌아선 흐름인데 나이가 {v['age']}"
    assert v["bonus"] > 1.0, "신선한 자리인데 가산이 없다"
    print("    ✅")


# ── 3. 보조 함수 ─────────────────────────────────────────────

def test_momentum_ratio_flat_denominator():
    print("\n[12] 직전 구간이 거의 안 움직였을 때 (0으로 나누기 방지)")
    closes = [100.0] * 6 + [100.5]
    r = recency.momentum_ratio(closes, "UP")
    print(f"    비율 {r}")
    assert r == 1.0, "근거 없는 값으로 진입을 막으면 안 된다"
    print("    ✅ 판단 보류(1.0)")


# ── 4. 확신도 시드 ───────────────────────────────────────────

GOOD = {"dir_agree": True, "adx": 34, "fresh_ext": 0.3,
        "fresh_mom": 1.8, "consistency": 0.8}
BAD  = {"dir_agree": False, "adx": 18, "fresh_ext": 2.2,
        "fresh_mom": 0.3, "consistency": 0.5}


OKISH = {"dir_agree": True, "adx": 28, "fresh_ext": 1.0,
         "fresh_mom": 1.0, "consistency": 0.7}


def test_conviction_scales_with_setup_quality():
    print("\n[14] ★ 자신 있으면 세게, 애매하면 평범하게")
    g, gw = recency.conviction(GOOD)
    o, _  = recency.conviction(OKISH)
    b, bw = recency.conviction(BAD)
    n, _  = recency.conviction({})
    print(f"    확신 ×{g}  ({', '.join(gw)})")
    print(f"    보통 ×{o}")
    print(f"    애매 ×{b}  ({', '.join(bw)})")
    print(f"    지표없음 ×{n}")
    assert g > 1.4, f"확신 있는 자리인데 충분히 안 키운다: {g}"
    assert 1.0 < o < g, f"보통 자리가 확신 자리와 구분돼야 한다: {o} vs {g}"
    assert 0.8 <= b < 1.0, f"애매한 자리는 '평범하게'여야 한다 (과하게 깎지 말 것): {b}"
    assert n == 1.0, "지표가 없으면 중립이어야 한다"
    print("    ✅ 단계적으로 벌어진다")


def test_conviction_top_requires_everything():
    """조건 몇 개만 맞아도 상한에 닿으면 '괜찮음'과 '최상'이 같아진다"""
    print("\n[15] 상한은 모든 지표가 최상일 때만")
    near_top = {"dir_agree": True, "adx": 45, "fresh_ext": 0.1,
                "fresh_mom": 2.0, "consistency": 0.90}
    perfect  = {"dir_agree": True, "adx": 99, "fresh_ext": -5,
                "fresh_mom": 99, "consistency": 1.0}
    worst    = {"dir_agree": False, "adx": 0, "fresh_ext": 99,
                "fresh_mom": -99, "consistency": 0.0}
    n, _ = recency.conviction(near_top)
    p, _ = recency.conviction(perfect)
    w, _ = recency.conviction(worst)
    print(f"    거의 최상 ×{n} / 이론상 최대 ×{p} / 최악 ×{w}")
    assert recency.conviction(GOOD)[0] < recency.conviction(near_top)[0], \
        "좋은 자리와 아주 좋은 자리가 구분되지 않는다"
    assert p == 1.80, f"상한을 넘거나 못 닿는다: {p}"
    # 최악이어도 0.85 밑으로는 안 간다. 0.85 에 정확히 닿지는 않는데,
    # 1h 방향이 잡혀 있다는 사실 자체(agree=False 여도 0.35점)는
    # 0 점이 아니기 때문이다 — 어차피 모든 필터를 통과한 후보다.
    assert 0.85 <= w < 0.90, f"하한 근처여야 한다: {w}"
    print("    ✅")


def test_conviction_handles_missing_fields():
    """스캐너가 필드를 못 채운 경우에도 터지지 않고 중립(1.0)에 가까워야"""
    print("\n[16] 지표가 비어 있어도 안전")
    m, why = recency.conviction({})
    print(f"    빈 dict → ×{m} ({why})")
    assert m == 1.0, "지표가 없는데 배율을 조정하면 안 된다 (없는 근거로 베팅)"
    m2, _ = recency.conviction({"adx": None, "fresh_mom": None,
                                "consistency": None, "fresh_ext": None})
    assert m2 == 1.0, m2
    m3, _ = recency.conviction({"adx": "이상한값"})
    assert m3 == 1.0, m3
    print("    ✅ 전부 중립(1.0)")


def test_short_data_is_safe():
    print("\n[13] 데이터가 모자라도 터지지 않는다")
    assert recency.slope([], 6) == 0.0
    assert recency.trend_age([1, 2], "UP") == 0
    assert recency.momentum_ratio([1, 2], "UP") == 1.0
    assert recency.extension_atr(100, [], 0) == 0.0
    v = recency.decide_direction([], [], [], [])
    assert v["trend"] is None
    print("    ✅")


def main():
    print("=" * 62)
    print("  최근 흐름 엔진 테스트")
    print("=" * 62)
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    order = [test_recent_up_gives_up,
             test_stale_daily_uptrend_but_recent_down_is_blocked,
             test_very_strong_daily_uptrend_blocks_the_counter_short,
             test_strong_daily_opposition_vetoes,
             test_weak_daily_opposition_allowed,
             test_4h_opposition_vetoes,
             test_near_term_conflict_is_skipped,
             test_flat_market_is_skipped,
             test_overextended_is_blocked,
             test_decaying_momentum_is_blocked,
             test_old_trend_pulled_back_is_allowed,
             test_fresh_trend_gets_bonus,
             test_momentum_ratio_flat_denominator,
             test_conviction_scales_with_setup_quality,
             test_conviction_top_requires_everything,
             test_conviction_handles_missing_fields,
             test_short_data_is_safe]
    assert len(order) == len(fns), "테스트 목록 누락"
    for fn in order:
        fn()
    print("\n" + "=" * 62)
    print("  전부 통과")
    print("=" * 62)


if __name__ == "__main__":
    main()
