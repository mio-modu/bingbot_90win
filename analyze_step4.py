import re
import sys
from collections import defaultdict

sys.stdout.reconfigure(encoding='utf-8')

log_path = "bot.log"

step4_entries = []
close_events = []

# 손실트리거 중복 방지 (같은 코인 같은 날 한 번만)
seen_loss_trigger = set()

with open(log_path, encoding='utf-8') as f:
    for line in f:
        # 횡보DCA 4단계 강제 투입
        m = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*\[횡보DCA\] (\S+-USDT) \|.*4단계 강제 투입', line)
        if m:
            step4_entries.append((m.group(1), m.group(2), '횡보DCA'))

        # 4단계DCA 손실트리거 (코인+날짜 단위로 1회만 카운트)
        m = re.search(r'(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}).*\[4단계DCA-손실트리거\] (\S+-USDT) \|', line)
        if m:
            key = (m.group(1), m.group(3))
            if key not in seen_loss_trigger:
                seen_loss_trigger.add(key)
                step4_entries.append((m.group(1) + ' ' + m.group(2), m.group(3), '손실트리거'))

        # 청산 이벤트
        if '[청산' in line:
            ts_m = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
            coin_m = re.search(r'\] (\S+-USDT)', line)
            pnl_m = re.search(r'순손익: \$([+-]?\d+\.\d+)', line)
            reason_m = re.search(r'사유: ([^|]+)', line)
            if ts_m and coin_m and pnl_m:
                close_events.append((ts_m.group(1), coin_m.group(1), float(pnl_m.group(1)),
                                     reason_m.group(1).strip() if reason_m else ''))

        # 최대손실손절
        m = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*\[최대손실손절\] (\S+-USDT) \| 순손익 \$([+-]?\d+\.\d+)', line)
        if m:
            close_events.append((m.group(1), m.group(2), float(m.group(3)), '최대손실손절'))

# 날짜별 4단계 진입 횟수
daily = defaultdict(int)
for ts, coin, typ in step4_entries:
    daily[ts[:10]] += 1

print("=== 날짜별 4단계 DCA 진입 횟수 ===")
for d in sorted(daily):
    print(f"  {d} : {daily[d]}회")

# 4단계 진입 후 최초 청산 매칭
print("\n=== 4단계 DCA 진입 -> 결과 ===")
seen = set()
results = []
for s_ts, s_coin, typ in step4_entries:
    key = (s_ts, s_coin)
    if key in seen:
        continue
    seen.add(key)
    matched = None
    for c_ts, c_coin, pnl, reason in close_events:
        if c_coin == s_coin and c_ts >= s_ts:
            matched = (pnl, reason)
            break
    if matched:
        results.append((s_ts, s_coin, typ, matched[0], matched[1]))
    else:
        results.append((s_ts, s_coin, typ, None, '진행중'))

win = 0; loss = 0; total_win = 0.0; total_loss = 0.0
for s_ts, coin, typ, pnl, reason in results:
    if pnl is None:
        icon = '[진행중]'
        pnl_str = '진행중'
    elif pnl > 0:
        icon = '[WIN]'
        pnl_str = f"${pnl:+.2f}"
        win += 1; total_win += pnl
    else:
        icon = '[LOSS]'
        pnl_str = f"${pnl:+.2f}"
        loss += 1; total_loss += pnl
    print(f"  {s_ts[:10]} {s_ts[11:16]} | {coin:<28} | {icon:<10} {pnl_str:>10} | {reason[:35]}")

total = win + loss
print(f"\n=== 집계 (진행중 제외) ===")
if total:
    print(f"  승(익절) {win}회 / 패(손절) {loss}회 / 승률 {win/total*100:.1f}%")
print(f"  익절 합계: ${total_win:+.2f}")
print(f"  손절 합계: ${total_loss:+.2f}")
print(f"  4단계 순합계: ${total_win+total_loss:+.2f}")
