#!/usr/bin/env python3
"""아침 투자 브리핑 → 텔레그램.

desk.py·config.yaml 은 '★투자 도우미' 폴더의 사본입니다. 워치리스트를 바꿀 땐
그쪽을 고치고 이 폴더로 복사합니다 (투자 판단·모의매매는 그쪽에서만 합니다).

    python briefing/send.py --dry-run         # 전송 없이 콘솔 출력
    python briefing/send.py --send-at 06:30   # 준비를 끝내고 정각에 전송
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import desk  # noqa: E402
from movers import esc, load_dotenv, now_kst, send_telegram, wait_until, weekday_ko  # noqa: E402

STATE = HERE / "state.json"
SHOW = 15  # 어제 대비 목록을 이 개수까지만 펼침


def label(row):
    """'XLE (에너지)' — 티커 뒤 괄호에 설명."""
    return esc(f"{row['t']} ({row['name']})")


def section(title, lines):
    return ["", f"<b>{title}</b>"] + (lines or ["  · 없음"])


def render(data, d):
    rows = [r for r in data["rows"] if r["s"]]
    by_t = {r["t"]: r for r in rows}
    now = now_kst()
    us = by_t.get("^GSPC", {}).get("date", "")
    kr = next((r["date"] for r in rows if r["t"].endswith(".KS")), "")
    out = [f"<b>📋 투자 브리핑 · {now.month}/{now.day}({weekday_ko(now.date())})</b>",
           f"<i>미국 {us[5:]} · 한국 {kr[5:]} 종가 기준</i>"]

    grp = []
    for g in desk.CFG["watchlist"]:
        got = [r for r in rows if r["group"] == g]
        if got:
            avg = sum(r["s"]["chg"] for r in got) / len(got)
            above = sum(1 for r in got if r["s"]["above_ma"])
            grp.append(f"  {'🔺' if avg > 0 else '🔻'} {esc(g)}  {avg:+.1%} · 200일선 위 {above}/{len(got)}")
    out += section("📊 그룹 평균", grp)

    if d:
        chg = []
        for mark, key in (("🆕", "new"), ("✅", "gone")):
            items = d[key]
            chg += [f"  {mark} {esc(t)} {esc(x)}" for t, x in items[:SHOW]]
            if len(items) > SHOW:
                chg.append(f"  {mark} … 외 {len(items) - SHOW}건")
        chg.append(f"  ➖ 그대로 {len(d['kept'])}건")
        out += section(f"🔄 {d['since'][5:]} 대비 (🆕 새로 걸림 / ✅ 풀림)", chg)
    else:
        out += ["", "<i>🔄 첫 브리핑 — 어제 대비는 다음 회차부터</i>"]

    def having(sig):
        return [r for r in rows if sig in r["sig"]]

    out += section("🏆 52주 신고가", [f"  · {label(r)}" for r in having("52주 신고가")])
    out += section("🧊 52주 신저가", [f"  · {label(r)}" for r in having("52주 신저가")])

    tp = []
    for r in rows:
        for x in r["sig"]:
            if x.startswith("돌파 "):
                tp.append(f"  🟢 돌파  {label(r)} · {esc(x[3:].lstrip('+'))}")
            elif x.endswith("추격구간"):
                tp.append(f"  🟠 추격주의  {label(r)} · 돌파선 {x.split(' ')[2]}")
    out += section("🎯 타점", tp)

    moves = sorted((r for r in rows if any("급변" in x for x in r["sig"])), key=lambda r: (r["s"]["chg"] <= 0, -abs(r["s"]["chg"])))  # 급등 큰 순 → 급락 큰 순
    ma_n = desk.CFG["signals"]["ma"]
    mv = [f"  {'🔺' if r['s']['chg'] > 0 else '🔻'} {r['s']['chg']:+.1%}  {label(r)}" for r in moves]
    mv += [f"  📉 {ma_n}일선 이탈  {label(r)}" for r in having(f"{ma_n}일선 이탈")]
    mv += [f"  📈 {ma_n}일선 회복  {label(r)}" for r in having(f"{ma_n}일선 회복")]
    out += section(f"⚡ 급변 ±{desk.CFG['signals']['day_move']:.0%} · {ma_n}일선", mv)

    mac = []
    wti = by_t.get("CL=F")
    if wti:
        mac.append(f"  🛢️ WTI 유가  <b>{wti['s']['last']:.2f}</b>  {wti['s']['chg']:+.1%}")
    for m in data["macro"]:
        v = m["m"]
        if v is None:
            mac.append(f"  · {esc(m['name'])}  데이터 없음")
            continue
        d20 = "" if v["d20"] is None else f" (20D {v['d20']:+.0f}bp)"
        tag = f" · {', '.join(m['sig'])}" if m["sig"] else ""
        mac.append(f"  · {esc(m['name'])}  <b>{v['last']:.2f}</b>  {v['d1']:+.0f}bp{d20}{esc(tag)}")
    if data["vix"]:
        v = data["vix"]
        mac.append(f"  😨 VIX  <b>{v['s']['last']:.1f}</b>  {v['s']['chg']:+.1%} · {esc(', '.join(v['sig']) or '평온')}")
    out += section("🌐 매크로", mac)

    if data["fails"]:
        out += ["", "⚠️ 조회 실패: " + esc(", ".join(data["fails"]))]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="아침 투자 브리핑")
    ap.add_argument("--dry-run", action="store_true", help="전송·상태 저장 없이 콘솔 출력")
    ap.add_argument("--send-at", default="", metavar="HH:MM")
    ap.add_argument("--marker", default="", metavar="파일")
    ap.add_argument("--force", action="store_true", help="이미 보낸 미국 거래일이어도 다시 보냄")
    args = ap.parse_args()
    load_dotenv(HERE.parent / ".env")

    data = desk.collect()
    sess = next((r["date"] for r in data["rows"] if r["t"] == "^GSPC" and r["s"]), "")
    if not sess:
        print("  ! S&P500 조회 실패 — 기준 거래일을 알 수 없어 보내지 않습니다", file=sys.stderr)
        return 1
    try:
        last = json.loads(STATE.read_text(encoding="utf-8")).get("last_session")
    except (OSError, json.JSONDecodeError):
        last = None
    # 주말·미국 휴장이면 새 거래일이 없음 → 어제 브리핑을 다시 보내지 않음
    if last == sess and not args.force:
        print(f"  · 미국 {sess} 은 이미 보냈습니다. 종료합니다.")
        return 0

    if args.dry_run:
        print(render(data, None))
        print("\n(연습 실행 — 전송·저장 안 함, 어제 대비는 생략)")
        return 0

    d = desk.save_and_diff(data, desk.today_kst())
    msg = render(data, d)
    wait_until(args.send_at)
    if not send_telegram(msg, "TELEGRAM_CHAT_ID_BRIEF"):
        return 1
    STATE.write_text(json.dumps({"last_session": sess, "last_run": now_kst().isoformat(timespec="seconds")},
                                ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.marker:
        Path(args.marker).write_text(f"{sess}\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
