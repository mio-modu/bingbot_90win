"""
리스크 거버너 테스트
────────────────────
    python tests/test_risk_governor.py

가장 중요한 것은 **데드락 검증**이다 (섹션 5).
"4연패 → 정지" 를 그대로 두면 진입이 막혀 이길 기회가 없고, 이기지 못하니
연패 카운터가 영영 안 풀린다 — 봇이 영구 정지된다.
"""

import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import risk_governor                                   # noqa: E402
_TMP = tempfile.mkdtemp(prefix="govtest_")
risk_governor.STATE_FILE = os.path.join(_TMP, "gov.json")

import config                                          # noqa: E402
import trade_journal                                   # noqa: E402
import paper_trader                                    # noqa: E402
from risk_governor import RiskGovernor                 # noqa: E402

trade_journal.JOURNAL_FILE = os.path.join(_TMP, "trades.jsonl")
paper_trader.STATE_FILE    = os.path.join(_TMP, "state.json")
config.LIVE_TRADING = False

_TODAY = datetime.now(risk_governor.KST).strftime("%Y-%m-%d")


def fresh(tag: str) -> RiskGovernor:
    """섹션마다 독립된 상태파일 — 앞 섹션의 중지가 새어들지 않게"""
    risk_governor.STATE_FILE = os.path.join(_TMP, f"gov_{tag}.json")
    return RiskGovernor(config)


def test_drawdown_ladder():
    """고점 대비 낙폭에 따라 시드가 단계적으로 줄어드는가

    기준값은 gateio_90win 실측 고점 $518 (LESSONS.md).
    """
    print("\n[1] 낙폭 사다리 — 실제 붕괴 구간 $518 → $234 재생")
    print(f"    {'자본':>8}{'낙폭':>8}{'시드':>8}  판정")
    results = {}
    for eq in [518, 500, 486, 466, 440, 414, 234]:
        g = fresh(f"dd{eq}")
        g.peak_equity = 518.0
        g.day_key = _TODAY
        g.day_start_equity = g.day_peak_equity = eq   # 반납 규칙과 분리
        m = g.seed_multiplier(eq)
        dd = (518 - eq) / 518
        print(f"    {eq:>8.0f}{dd*100:>7.1f}%{m:>8.0%}  "
              + ("진입 중지" if m == 0 else f"시드 {m:.0%}"))
        results[eq] = m

    assert results[518] == 1.0,  "고점에서 축소되면 안 됨"
    assert results[486] == 0.80, "-6% 구간"
    assert results[466] == 0.60, "-10% 구간"
    assert results[440] == 0.35, "-15% 구간"
    assert results[414] == 0.0,  "-20% 는 진입 중지여야 함"
    print("    ✅ 실제로는 제동 없이 $234 까지 갔다. 이제 $414 에서 멈춘다.")


def test_consec_loss_ladder():
    """연속 손실에 따른 축소와 익절 1회 즉시 복구"""
    print("\n[2] 연속 손실 사다리")
    g = fresh("cl")
    g.update_equity(1050.0)
    seen = []
    for i in range(1, 5):
        g.record_trade(-50.0)
        m = g.seed_multiplier(1050.0)
        seen.append(m)
        print(f"    {i}연패 → 시드 {m:.0%}" + ("  (진입 중지)" if m == 0 else ""))
    assert seen == [1.0, 0.65, 0.40, 0.0], f"사다리 불일치: {seen}"

    g.halt_until = 0.0
    g.record_trade(+5.0)
    m = g.seed_multiplier(1050.0)
    print(f"    익절 1회 → 연속손실 {g.consec_losses}, 시드 {m:.0%}")
    assert g.consec_losses == 0 and m == 1.0, "익절 후 즉시 복구되지 않음"
    assert not g.recovery_mode, "익절했는데 회복 모드가 남음"
    print("    ✅ 익절 1회로 완전 복귀")


def test_giveback():
    """당일 수익 반납 감지 — 번 것을 되돌려주면 그날은 접는다"""
    print("\n[3] 당일 수익 반납 (시작 $1,050 → 고점 $1,200, +$150)")
    outcomes = {}
    for eq in [1180, 1150, 1130]:
        g = fresh(f"gb{eq}")
        g.day_key = _TODAY
        g.day_start_equity, g.day_peak_equity = 1050.0, 1200.0
        g.peak_equity = 1200.0
        m = g.seed_multiplier(eq)
        back = (1200 - eq) / 150
        print(f"    ${eq:,} — 수익의 {back:>3.0%} 반납 → "
              + ("당일 매매 중지" if m == 0 else f"시드 {m:.0%}"))
        outcomes[eq] = m
    assert outcomes[1180] > 0 and outcomes[1150] > 0, "13%·33% 반납은 계속 매매"
    assert outcomes[1130] == 0.0, "47% 반납 시 중지되어야 함"

    # 날짜가 바뀌면 자동 해제되어야 한다 (하루 단위 규칙)
    g = fresh("gb_next")
    g.day_key = "2000-01-01"
    g.halt_until  = 9e12
    g.halt_reason = "당일 고점 …에서 수익 … 반납"
    g.update_equity(1130.0)
    assert g.halt_until == 0.0, "날짜가 바뀌었는데 당일 중지가 안 풀림"
    print("    ✅ 47% 반납 시 중지, 익일 자동 해제")


def test_open_position_untouched():
    """브레이크가 이미 열려 있는 포지션의 손절선을 당기면 안 된다"""
    print("\n[4] 보유 포지션 불간섭")
    g = fresh("pos")
    pt = paper_trader.PaperTrader(live_api=None, governor=g)
    pt.open_position("XUSDT", "UP", 100.0)
    before = pt._get_max_loss_usd()

    g.peak_equity = 2000.0            # 인위적 대낙폭 → 배율 0
    assert g.seed_multiplier(1050.0) == 0.0
    after = pt._get_max_loss_usd()

    print(f"    시드 배율 0% 상태에서 손절 한도: ${before:.0f} → ${after:.0f}")
    assert abs(after - before) < 0.01, \
        "거버너가 열린 포지션의 손절선을 당겼다 — 조기 손절 위험"
    print("    ✅ 진입 당시 시드 기준이라 그대로 유지")

    # 반면 새 진입 시드에는 반영되어야 한다
    pt.close_position(100.0, "테스트청산")
    g.halt_until = 0.0
    g.recovery_mode = False
    g.peak_equity = pt.total_capital + pt.total_pnl
    g.consec_losses = 2
    reduced = pt._get_initial_position_usd()
    g.consec_losses = 0
    full = pt._get_initial_position_usd()
    print(f"    신규 시드: 2연패 ${reduced:.2f} vs 정상 ${full:.2f}")
    assert reduced < full, "연속 손실이 신규 시드에 반영되지 않음"


def test_no_deadlock():
    """★ 가장 중요 — 정지 상태에서 빠져나올 수 있는가

    거래로만 이길 수 있고, 이겨야 카운터가 풀린다.
    정지가 영구적이면 봇은 죽은 것이나 같다.
    """
    print("\n[5] ★ 데드락 방지")
    g = fresh("deadlock")
    g.update_equity(1050.0)

    for _ in range(4):
        g.record_trade(-50.0)
    assert g.seed_multiplier(1050.0) == 0.0
    print(f"    4연패 → 진입 중지 (연속손실 {g.consec_losses}회)")

    # 중지 시간이 지났다고 가정
    g.halt_until = 0.0
    m = g.seed_multiplier(1050.0)
    print(f"    중지 해제 후 → 시드 {m:.0%} (연속손실은 여전히 {g.consec_losses}회)")
    assert m == config.GOVERNOR_RECOVERY_MULT, \
        f"중지 해제 후에도 0% — 영구 데드락! (m={m})"
    assert g.can_enter(1050.0)[0], "중지 해제 후에도 진입 불가 — 데드락"

    # 재개 후 또 지면 다시 중지 (최소 규모로 무한정 흘리지 않도록)
    g.record_trade(-10.0)
    assert g.seed_multiplier(1050.0) == 0.0, "회복 모드에서 졌는데 중지 안 됨"
    print(f"    회복 모드에서 재차 손절 → 다시 중지 (연속손실 {g.consec_losses}회)")

    # 이기면 완전 복귀
    g.halt_until = 0.0
    g.record_trade(+8.0)
    assert g.seed_multiplier(1050.0) == 1.0, "익절했는데 복귀 안 됨"
    print("    익절 → 시드 100% 완전 복귀")

    # 낙폭 정지도 같은 방식으로 빠져나올 수 있어야 한다
    g2 = fresh("deadlock_dd")
    g2.day_key = _TODAY
    g2.peak_equity = 1000.0
    g2.day_start_equity = g2.day_peak_equity = 700.0
    assert g2.seed_multiplier(700.0) == 0.0        # -30%
    g2.halt_until = 0.0
    m2 = g2.seed_multiplier(700.0)
    print(f"    낙폭 -30% 정지 → 해제 후 시드 {m2:.0%} (자본 회복 경로 확보)")
    assert m2 == config.GOVERNOR_RECOVERY_MULT, "낙폭 정지가 영구 데드락"
    print("    ✅ 두 경로 모두 탈출 가능")


def test_persistence():
    """재시작으로 브레이크가 풀리면 의미가 없다"""
    print("\n[6] 재시작 후 상태 유지")
    g = fresh("persist")
    g.update_equity(1050.0)
    g.peak_equity   = 1500.0
    g.consec_losses = 3
    g.recovery_mode = True
    g._save()

    g2 = RiskGovernor(config)   # 같은 파일에서 복원
    print(f"    고점 ${g2.peak_equity:,.0f} / 연속손실 {g2.consec_losses} / "
          f"회복모드 {g2.recovery_mode}")
    assert (g2.peak_equity == 1500.0 and g2.consec_losses == 3
            and g2.recovery_mode), "재시작으로 브레이크가 풀렸다"
    print("    ✅ 유지됨")


def test_account_switch_is_not_a_drawdown():
    """★ 실제로 터진 사고 — 계좌를 옮겼더니 거버너가 영구 정지시켰다

    메인계좌($1,050)에서 서브계좌($500)로 옮기자 거버너는 디스크에 저장된
    고점 $1,050 을 그대로 복원해 "52.4% 폭락"으로 읽고 매매를 중지했다.
    $500 이 $1,050 으로 돌아갈 일이 없으니 영구 정지였다.
    """
    print("\n[6b] ★ 계좌 변경을 폭락으로 오인하지 않는가")
    g = fresh("acct")
    g.update_equity(1050.0)          # 메인계좌에서 돌던 시절
    assert g.peak_equity == 1050.0

    # 서브계좌 $500 으로 갈아탐 — 아직 기준선을 안 옮긴 상태
    mult, why = g._drawdown_mult(500.0)
    ok_before, why_before = g.can_enter(500.0)
    print(f"    재설정 전: 시드배율 {mult:.0%} / 진입 {ok_before} ({why_before})")
    assert mult == 0.0, "이 시나리오에서는 원래 진입이 막혀야 한다(재현 확인)"

    # 자본동기화가 기준선을 옮긴다
    g.reset_baseline(500.0, why="자본동기화 $1,050 → $500")
    print(f"    재설정 후: 고점 ${g.peak_equity:,.0f} / "
          f"당일시작 ${g.day_start_equity:,.0f}")
    assert g.peak_equity == 500.0
    assert g.day_start_equity == 500.0 and g.day_peak_equity == 500.0
    assert g.halt_until == 0.0, "잘못된 낙폭으로 걸린 중지가 안 풀렸다"

    ok, why2 = g.can_enter(500.0)
    print(f"    진입 가능 {ok} / 시드배율 {g.seed_multiplier(500.0):.0%}")
    assert ok is True, f"기준선을 옮겼는데도 막힌다: {why2}"
    assert g.seed_multiplier(500.0) == 1.0
    print("    ✅ 입출금·계좌변경은 손실로 치지 않는다")


def test_reset_baseline_keeps_trade_history():
    """계좌를 옮겼다고 최근 매매 성적이 좋아지는 건 아니다"""
    print("\n[6c] 기준선 재설정이 연속손실 기록까지 지우지는 않는다")
    g = fresh("acct2")
    g.update_equity(1000.0)
    for _ in range(3):
        g.record_trade(-10.0)
    before = g.consec_losses
    g.reset_baseline(500.0)
    print(f"    연속손실 {before} → {g.consec_losses}")
    assert g.consec_losses == before, "매매 기록까지 지우면 브레이크가 헐거워진다"
    print("    ✅ 유지됨")


def test_disabled():
    """끄면 완전히 무개입이어야 한다"""
    print("\n[7] GOVERNOR_ENABLED = False")
    config.GOVERNOR_ENABLED = False
    try:
        g = fresh("off")
        g.peak_equity   = 10_000.0     # 극단적 낙폭
        g.consec_losses = 99
        assert g.seed_multiplier(100.0) == 1.0
        assert g.can_enter(100.0)[0] is True
        print("    ✅ 완전 무개입")
    finally:
        config.GOVERNOR_ENABLED = True


def main():
    print("=" * 62)
    print("  리스크 거버너 테스트")
    print("=" * 62)
    for fn in [test_drawdown_ladder, test_consec_loss_ladder, test_giveback,
               test_open_position_untouched, test_no_deadlock,
               test_persistence,
               test_account_switch_is_not_a_drawdown,
               test_reset_baseline_keeps_trade_history,
               test_disabled]:
        fn()
    print("\n" + "=" * 62)
    print("  ✅ 전부 통과")
    print("=" * 62)


if __name__ == "__main__":
    main()
