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
    return f"{row['name']}({row['t']})"


def render(data, d):
    rows = [r for r in data["rows"] if r["s"]]
    now = now_kst()
    us = next((r["date"] for r in rows if r["t"] == "^GSPC"), "")
    kr = next((r["date"] for r in rows if r["t"].endswith(".KS")), "")
    out = [f"<b>📋 투자 브리핑 · {now.month}/{now.day}({weekday_ko(now.date())})</b>",
           f"<i>미국 {us[5:]} · 한국 {kr[5:]} 종가 기준</i>"]

    out += ["", "──── 그룹 ────"]
    for g in desk.CFG["watchlist"]:
        got = [r for r in rows if r["group"] == g]
        if got:
            avg = sum(r["s"]["chg"] for r in got) / len(got)
            above = sum(1 for r in got if r["s"]["above_ma"])
            out.append(f"{esc(g)}  평균 {avg:+.1%} · 200일선 위 {above}/{len(got)}")

    if d:
        out += ["", f"──── {d['since'][5:]} 대비 ────"]
        for mark, key in (("🆕 새로 걸림", "new"), ("✅ 풀림", "gone")):
            items = d[key]
            body = "; ".join(f"{t} {x}" for t, x in items[:SHOW]) + (f" 외 {len(items) - SHOW}건" if len(items) > SHOW else "")
            out.append(f"{mark} {len(items)}: {esc(body) or '-'}")
        out.append(f"· 계속 {len(d['kept'])}건")
    else:
        out += ["", "<i>첫 브리핑 — 비교는 다음 회차부터</i>"]

    def pick(pred):
        return [r for r in rows if any(pred(x) for x in r["sig"])]

    hi, lo = pick(lambda x: x == "52주 신고가"), pick(lambda x: x == "52주 신저가")
    out += ["", "──── 신고가 / 신저가 ────",
            "▲ " + (esc(", ".join(map(label, hi))) or "-"),
            "▼ " + (esc(", ".join(map(label, lo))) or "-")]

    brk = [f"{label(r)} {x}" for r in rows for x in r["sig"] if x.startswith("돌파 ")]
    chase = [f"{r['t']} {x.split(' ')[2]}" for r in rows for x in r["sig"] if x.endswith("추격구간")]
    out += ["", "──── 타점 ────",
            "🎯 " + (esc(", ".join(brk)) or "돌파 없음"),
            "⚠️ 추격구간: " + (esc(", ".join(chase)) or "-")]

    moves = sorted((r for r in rows if any("급변" in x for x in r["sig"])), key=lambda r: r["s"]["chg"])
    ma_n = desk.CFG["signals"]["ma"]
    broke = [r["t"] for r in rows if f"{ma_n}일선 이탈" in r["sig"]]
    back = [r["t"] for r in rows if f"{ma_n}일선 회복" in r["sig"]]
    out += ["", f"──── 급변 ±{desk.CFG['signals']['day_move']:.0%} ────",
            esc(" · ".join(f"{r['t']} {r['s']['chg']:+.1%}" for r in moves)) or "-",
            f"{ma_n}일선 이탈: {esc(', '.join(broke)) or '-'} / 회복: {esc(', '.join(back)) or '-'}"]

    out += ["", "──── 매크로 ────"]
    for m in data["macro"]:
        v = m["m"]
        if v is None:
            out.append(f"{esc(m['name'])}  데이터 없음")
            continue
        d20 = "" if v["d20"] is None else f" (20D {v['d20']:+.0f}bp)"
        tag = f" · {', '.join(m['sig'])}" if m["sig"] else ""
        out.append(f"{esc(m['name'])}  <b>{v['last']:.2f}</b>  {v['d1']:+.0f}bp{d20}{esc(tag)}")
    if data["vix"]:
        v = data["vix"]
        out.append(f"VIX  <b>{v['s']['last']:.1f}</b>  {v['s']['chg']:+.1%}  {esc(', '.join(v['sig']) or '평온')}")

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
