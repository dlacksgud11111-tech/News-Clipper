"""모의 투자 데스크. 계산만 한다, 판단은 클로드가 대화에서.

python desk.py scan                 워치리스트 신호 점검
python desk.py brief                scan + 직전 브리핑 대비 변화 (briefs/에 저장)
python desk.py quote NVDA           한 종목 상태 (RSI·MACD 포함)
python desk.py chart NVDA [일수]    차트용 데이터 JSON (종가·200일선·추세선)
python desk.py buy NVDA 10 [메모]   모의 매수 (최근가 체결)
python desk.py sell NVDA 5 [메모]   모의 매도
python desk.py pf                   포트폴리오
python desk.py log                  매매 기록
"""
import json
import re
import sqlite3
import sys
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

warnings.filterwarnings("ignore")
sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent
CFG = yaml.safe_load((HERE / "config.yaml").read_text(encoding="utf-8"))
DB = HERE / "desk.db"
BRIEFS = HERE / "briefs"


# ---------- 순수 계산 (test_desk.py에서 검증) ----------

def ema(xs, n):
    a, out = 2 / (n + 1), [xs[0]]
    for x in xs[1:]:
        out.append(a * x + (1 - a) * out[-1])
    return out


def rsi(closes, n=14):
    """Wilder RSI. 데이터 부족하면 None."""
    if len(closes) <= n:
        return None
    d = [b - a for a, b in zip(closes, closes[1:])]
    up = sum(max(x, 0) for x in d[:n]) / n
    dn = sum(max(-x, 0) for x in d[:n]) / n
    for x in d[n:]:
        up = (up * (n - 1) + max(x, 0)) / n
        dn = (dn * (n - 1) + max(-x, 0)) / n
    return 100.0 if dn == 0 else 100 - 100 / (1 + up / dn)


def macd(closes, fast=12, slow=26, sig=9):
    """(MACD, 시그널, 전일 MACD-시그널). 데이터 부족하면 None."""
    if len(closes) < slow + sig:
        return None
    line = [f - s for f, s in zip(ema(closes, fast), ema(closes, slow))]
    sl = ema(line, sig)
    return line[-1], sl[-1], line[-2] - sl[-2]


def trend(ys):
    """최소제곱 추세선 값 리스트."""
    n = len(ys)
    mx, my = (n - 1) / 2, sum(ys) / n
    b = sum((i - mx) * (y - my) for i, y in enumerate(ys)) / sum((i - mx) ** 2 for i in range(n))
    a = my - b * mx
    return [a + b * i for i in range(n)]


def sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def stats(closes, ma_n, vols=None, pivot_skip=20):
    """closes: 오래된→최근 순 종가 리스트. 52주 = 최근 252거래일, 종가 기준."""
    last, prev = closes[-1], closes[-2]
    yr = closes[-252:]
    hi, lo = max(yr), min(yr)
    before = closes[-252:-1]
    ma = sma(closes, ma_n)
    ma_prev = sum(closes[-ma_n - 1:-1]) / ma_n if len(closes) > ma_n else None
    base = closes[-252:-pivot_skip]  # 최근 한 달 빼고 본 52주 고점 = 돌파 기준선
    avg_vol = sma(vols[:-1], 50) if vols else None
    return {
        "last": last,
        "chg": last / prev - 1,
        "dd": last / hi - 1,
        "up": last / lo - 1,
        "pos": (last - lo) / (hi - lo) if hi > lo else None,
        "new_high": last >= max(before),
        "new_low": last <= min(before),
        "ma": ma,
        "ma20": sma(closes, 20),
        "ma50": sma(closes, 50),
        "above_ma": None if ma is None else last > ma,
        "was_above_ma": None if ma_prev is None else prev > ma_prev,
        "pivot": max(base) if base else None,
        "prev": prev,
        "vol_ratio": vols[-1] / avg_vol if avg_vol else None,
        "rsi": rsi(closes),
        "macd": macd(closes),
    }


def ma_state(s):
    """('++-', '정배열'/'역배열'/'혼조'). 20·50·200일선 각각 위(+)/아래(-)."""
    mas = [s["ma20"], s["ma50"], s["ma"]]
    if None in mas:
        return "-", ""
    marks = "".join("+" if s["last"] > m else "-" for m in mas)
    if s["last"] > mas[0] > mas[1] > mas[2]:
        return marks, "정배열"
    if s["last"] < mas[0] < mas[1] < mas[2]:
        return marks, "역배열"
    return marks, "혼조"


def signals(s, cfg):
    out = []
    if s["new_high"]:
        out.append("52주 신고가")
    if s["new_low"]:
        out.append("52주 신저가")
    pv, vr = s["pivot"], s["vol_ratio"]
    if pv and s["prev"] <= pv < s["last"]:
        out.append(f"돌파 {'+거래량 ' + format(vr, '.1f') + '배' if vr and vr >= cfg['vol_mult'] else '(거래량 미달)'}")
    elif pv and s["last"] > pv * (1 + cfg["chase"]):
        out.append(f"돌파선 위 {s['last'] / pv - 1:+.0%} 추격구간")
    if s["dd"] <= -cfg["drawdown"] and not s["new_low"]:
        out.append(f"52주 고점 대비 {s['dd']:+.1%}")
    if s["above_ma"] is not None and s["was_above_ma"] is not None and s["above_ma"] != s["was_above_ma"]:
        out.append(f"{cfg['ma']}일선 {'회복' if s['above_ma'] else '이탈'}")
    if abs(s["chg"]) >= cfg["day_move"]:
        out.append(f"하루 {s['chg']:+.1%} 급변")
    return out


def macro_stats(vals):
    """금리·스프레드 같은 % 수준 시계열. 변화는 bp."""
    last = vals[-1]
    yr, before = vals[-252:], vals[-252:-1]
    return {
        "last": last,
        "d1": (last - vals[-2]) * 100,
        "d20": (last - vals[-21]) * 100 if len(vals) > 20 else None,
        "hi": max(yr), "lo": min(yr),
        "pos": (last - min(yr)) / (max(yr) - min(yr)) if max(yr) > min(yr) else None,
        "new_high": last >= max(before),
        "new_low": last <= min(before),
    }


def vix_signals(s, cfg):
    out = []
    if s["last"] >= cfg["vix_level"]:
        out.append(f"VIX {s['last']:.1f} (≥{cfg['vix_level']})")
    if s["chg"] >= cfg["vix_jump"]:
        out.append(f"VIX 하루 {s['chg']:+.1%} 급등")
    return out


def diff(prev, cur):
    """브리핑 비교. 숫자를 뺀 문구(신호 종류)로 짝지어, 수치만 바뀐 건 '계속'으로 본다."""
    kind = lambda x: re.sub(r"[\d.,+%-]+", "", x)
    new, gone, kept = [], [], []
    for t in sorted(set(prev) | set(cur)):
        p = {kind(x): x for x in prev.get(t, [])}
        c = {kind(x): x for x in cur.get(t, [])}
        new += [(t, c[k]) for k in c if k not in p]
        gone += [(t, p[k]) for k in p if k not in c]
        kept += [(t, c[k]) for k in c if k in p]
    return new, gone, kept


def position(trades):
    """trades: (side, qty, price, fee) 시간순. 이동평균법 평단."""
    qty = cost = 0.0
    for side, q, p, fee in trades:
        if side == "BUY":
            cost += q * p + fee
            qty += q
        else:
            cost -= cost / qty * q
            qty -= q
    return qty, (cost / qty if qty else 0.0)


# ---------- 데이터 ----------

def currency(t):
    # shortcut: 접미사로 통화 추정, 일본·유럽 종목 넣을 땐 yfinance info['currency']로 교체
    return "KRW" if t.upper().endswith((".KS", ".KQ")) else "USD"


def fetch(t):
    """(종가, 날짜, 거래량) 리스트. 실패하면 None. 옛 데이터로 대체하지 않는다."""
    import yfinance as yf
    try:
        with warnings.catch_warnings():  # yfinance가 필터를 덮어써서 여기서 막음
            warnings.simplefilter("ignore")
            y = yf.Ticker(t)
            h = y.history(period="2y", auto_adjust=True)
            # 야후가 최신 일봉 종가를 NaN으로 줄 때가 있음(2026-11-02 실측) → 그날 마지막 1시간봉으로 채움
            if not h.empty and h["Close"].isna().iloc[-1]:
                hr = y.history(period="5d", interval="1h").dropna(subset=["Close"])
                day = h.index[-1].date()
                same = hr[[i.date() == day for i in hr.index]]
                if len(same):
                    h.loc[h.index[-1], "Close"] = float(same["Close"].iloc[-1])
    except Exception:
        return None
    h = h.dropna(subset=["Close"]) if not h.empty else h
    if len(h) < 2:
        return None
    vols = h["Volume"].fillna(0).tolist()
    return h["Close"].tolist(), [d.strftime("%Y-%m-%d") for d in h.index], (vols if any(vols[-50:]) else None)


def fetch_fred(sid):
    """FRED 공개 CSV (키 없음). (값, 날짜) 최근 약 2년. 실패하면 None."""
    import urllib.request
    try:
        rows = urllib.request.urlopen(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}", timeout=20) \
            .read().decode().strip().splitlines()[1:]
    except Exception:
        return None
    rows = [r.split(",") for r in rows[-600:]]
    rows = [(d, float(v)) for d, v in rows if v not in ("", ".")]
    if len(rows) < 2:
        return None
    return [v for _, v in rows], [d for d, _ in rows]


def names():
    return {t: n for g in CFG["watchlist"].values() for t, n in g.items()}


def fmt(x, cur):
    return f"{x:,.0f}" if cur == "KRW" else f"{x:,.2f}"


def fmt_ind(s):
    r = "-" if s["rsi"] is None else f"{s['rsi']:.0f}"
    if s["macd"] is None:
        return r, "-"
    m, sg, prev_gap = s["macd"]
    gap = m - sg
    cross = " 골든" if prev_gap <= 0 < gap else " 데드" if prev_gap >= 0 > gap else ""
    return r, ("+" if gap > 0 else "-") + cross


# ---------- DB ----------

def db():
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS trades(
        id INTEGER PRIMARY KEY, ts TEXT, ticker TEXT, side TEXT,
        qty REAL, price REAL, fee REAL, currency TEXT, price_date TEXT, note TEXT)""")
    return con


def cash(con, cur):
    spent = con.execute(
        "SELECT COALESCE(SUM(CASE side WHEN 'BUY' THEN qty*price+fee ELSE -(qty*price-fee) END),0) "
        "FROM trades WHERE currency=?", (cur,)).fetchone()[0]
    return CFG["cash"][cur] - spent


def trades_of(con, t):
    return con.execute("SELECT side, qty, price, fee FROM trades WHERE ticker=? ORDER BY id", (t,)).fetchall()


# ---------- 명령 ----------

def collect():
    """워치리스트·매크로·VIX를 전부 조회한다. scan 표와 텔레그램 브리핑(briefing/send.py)이 같이 쓴다."""
    sc = CFG["signals"]
    rows, macro, hits, fails, vix = [], [], {}, [], None
    for group, members in CFG["watchlist"].items():
        for t, name in members.items():
            r = fetch(t)
            if r is None:
                fails.append(t)
                rows.append({"group": group, "t": t, "name": name, "s": None, "sig": []})
                continue
            s = stats(r[0], sc["ma"], r[2], sc["pivot_skip"])
            sig = signals(s, sc)
            rows.append({"group": group, "t": t, "name": name, "s": s, "sig": sig, "date": r[1][-1]})
            if sig:
                hits[t] = sig
    for sid, name in CFG.get("macro", {}).items():
        r = fetch_fred(sid)
        if r is None:
            fails.append(sid)
            macro.append({"sid": sid, "name": name, "m": None, "sig": []})
            continue
        m = macro_stats(r[0])
        sig = (["52주 최고"] if m["new_high"] else []) + (["52주 최저"] if m["new_low"] else [])
        macro.append({"sid": sid, "name": name, "m": m, "sig": sig, "date": r[1][-1]})
        if sig:
            hits[sid] = sig
    r = fetch("^VIX")
    if r is None:
        fails.append("^VIX")
    else:
        v = stats(r[0], sc["ma"])
        vix = {"s": v, "sig": vix_signals(v, sc)}
        if vix["sig"]:
            hits["^VIX"] = vix["sig"]
    return {"rows": rows, "macro": macro, "vix": vix, "hits": hits, "fails": fails}


def save_and_diff(data, today, folder=BRIEFS):
    """오늘 신호를 저장하고 직전 브리핑과 비교. 직전이 없으면 None."""
    folder.mkdir(exist_ok=True)
    olds = sorted(p for p in folder.glob("*.json") if p.stem < today)
    (folder / f"{today}.json").write_text(
        json.dumps({"hits": data["hits"], "fails": data["fails"]}, ensure_ascii=False, indent=1), encoding="utf-8")
    if not olds:
        return None
    prev = json.loads(olds[-1].read_text(encoding="utf-8-sig"))  # 손으로 고친 파일의 BOM 허용
    new, gone, kept = diff(prev["hits"], data["hits"])
    return {"since": olds[-1].stem, "new": new, "gone": gone, "kept": kept,
            "skipped": sorted(set(prev["fails"]) | set(data["fails"]))}


def today_kst():
    # GitHub 러너는 UTC라 06:30 KST가 전날로 잡힘
    return datetime.now(timezone(timedelta(hours=9))).date().isoformat()


def cmd_scan():
    data = collect()
    print("52주위치: 52주 최저 0% ~ 최고 100%  /  이평: 20·50·200일선 위(+) 아래(-)  /  거래량: 50일 평균 대비")
    group = None
    for row in data["rows"]:
        if row["group"] != group:
            group = row["group"]
            print(f"\n[{group}]")
            print(f"{'티커':<11}{'이름':<14}{'최근가':>11}{'1D':>7}{'52주위치':>8}{'고점대비':>8}{'이평':>5}{'':<4}{'RSI':>4}{'거래량':>6}  신호")
        t, name, s = row["t"], row["name"], row["s"]
        if s is None:
            print(f"{t:<11}{name:<14}{'데이터 없음':>11}")
            continue
        marks, label = ma_state(s)
        pos = "-" if s["pos"] is None else f"{s['pos']:.0%}"
        rs = "-" if s["rsi"] is None else f"{s['rsi']:.0f}"
        vr = "-" if s["vol_ratio"] is None else f"{s['vol_ratio']:.1f}x"
        print(f"{t:<11}{name:<14}{fmt(s['last'], currency(t)):>11}{s['chg']:>+7.1%}{pos:>8}{s['dd']:>+8.1%}{marks:>5} {label:<3}{rs:>4}{vr:>6}  {', '.join(row['sig'])}")
    print("\n[매크로 (FRED, %)]")
    print(f"{'시리즈':<14}{'이름':<18}{'수준':>7}{'1D':>8}{'20D':>8}{'52주위치':>8}  날짜·신호")
    for row in data["macro"]:
        m = row["m"]
        if m is None:
            print(f"{row['sid']:<14}{row['name']:<18}{'데이터 없음':>7}")
            continue
        d20 = "-" if m["d20"] is None else f"{m['d20']:+.0f}bp"
        pos = "-" if m["pos"] is None else f"{m['pos']:.0%}"
        print(f"{row['sid']:<14}{row['name']:<18}{m['last']:>7.2f}{m['d1']:>+6.0f}bp{d20:>8}{pos:>8}  {row['date']} {', '.join(row['sig'])}")
    if data["vix"]:
        v = data["vix"]
        print(f"\nVIX {v['s']['last']:.1f} ({v['s']['chg']:+.1%})  {', '.join(v['sig']) or '평온'}")
    fails = data["fails"]
    print(f"\n신호 {sum(map(len, data['hits'].values()))}건" + (f" / 데이터 실패: {', '.join(fails)}" if fails else ""))
    return data


def cmd_brief():
    d = save_and_diff(cmd_scan(), today_kst())
    if d is None:
        print("\n(첫 브리핑, 비교 대상 없음)")
        return
    print(f"\n── {d['since']} 대비 ──")
    for label, key in (("새로 걸림", "new"), ("풀림", "gone"), ("계속", "kept")):
        print(f"{label} {len(d[key])}: " + ("; ".join(f"{t} {x}" for t, x in d[key]) or "-"))
    if d["skipped"]:
        print(f"(데이터 실패로 비교 불확실: {', '.join(d['skipped'])})")


def cmd_quote(t):
    r = fetch(t)
    if r is None:
        sys.exit(f"{t}: 데이터 없음")
    closes, dates, vols = r
    sc = CFG["signals"]
    s = stats(closes, sc["ma"], vols, sc["pivot_skip"])
    cur = currency(t)
    print(f"{t} {names().get(t, '')} ({dates[-1]} 종가 기준)")
    print(f"  최근가 {fmt(s['last'], cur)} {cur}  1D {s['chg']:+.2%}")
    print(f"  52주 고점 대비 {s['dd']:+.1%} / 저점 대비 {s['up']:+.1%} / 위치 {s['pos']:.0%}" if s["pos"] is not None else "")
    for n, m in ((20, s["ma20"]), (50, s["ma50"]), (sc["ma"], s["ma"])):
        if m:
            print(f"  {n}일선 {fmt(m, cur)} ({s['last'] / m - 1:+.1%})")
    print(f"  이평 배열: {' '.join(ma_state(s))}")
    if s["pivot"]:
        print(f"  돌파 기준선(최근 {sc['pivot_skip']}일 제외 52주 고점) {fmt(s['pivot'], cur)} ({s['last'] / s['pivot'] - 1:+.1%})")
    if s["vol_ratio"]:
        print(f"  거래량 50일 평균 대비 {s['vol_ratio']:.1f}배")
    if s["rsi"] is not None:
        print(f"  RSI(14) {s['rsi']:.1f}" + (" 과매수권" if s["rsi"] >= 70 else " 과매도권" if s["rsi"] <= 30 else ""))
    if s["macd"]:
        m, sg, _ = s["macd"]
        print(f"  MACD {m:.2f} / 시그널 {sg:.2f} → {'시그널 위' if m > sg else '시그널 아래'}{fmt_ind(s)[1][1:]}")
    print(f"  신호: {', '.join(signals(s, CFG['signals'])) or '없음'}")


def cmd_chart(t, days):
    r = fetch(t)
    if r is None:
        sys.exit(f"{t}: 데이터 없음")
    closes, dates, _ = r
    n = CFG["signals"]["ma"]
    ma = [None if i + 1 < n else sum(closes[i + 1 - n:i + 1]) / n for i in range(len(closes))]
    c, d, m = closes[-days:], dates[-days:], ma[-days:]
    tl = trend(c)
    print(json.dumps({"ticker": t, "name": names().get(t, ""), "currency": currency(t),
                      "trend_pct": tl[-1] / tl[0] - 1,
                      "rows": [{"d": d[i], "close": round(c[i], 2), "ma": None if m[i] is None else round(m[i], 2),
                                "trend": round(tl[i], 2)} for i in range(len(c))]}, ensure_ascii=False))


def cmd_trade(side, t, qty, note):
    qty = float(qty)
    if qty <= 0:
        sys.exit("수량은 0보다 커야 함")
    r = fetch(t)
    if r is None:
        sys.exit(f"{t}: 데이터 없음, 체결 안 함")
    price, d = r[0][-1], r[1][-1]
    cur = currency(t)
    fee = qty * price * CFG["fee_rate"]
    con = db()
    if side == "BUY" and qty * price + fee > cash(con, cur):
        sys.exit(f"현금 부족: 필요 {fmt(qty * price + fee, cur)}, 보유 {fmt(cash(con, cur), cur)} {cur}")
    if side == "SELL":
        held, _ = position(trades_of(con, t))
        if qty > held + 1e-9:
            sys.exit(f"보유 {held:g}주, {qty:g}주 매도 불가")
    con.execute("INSERT INTO trades(ts,ticker,side,qty,price,fee,currency,price_date,note) VALUES(?,?,?,?,?,?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"), t, side, qty, price, fee, cur, d, note))
    con.commit()
    print(f"{side} {t} {qty:g}주 @ {fmt(price, cur)} ({d} 가격) 수수료 {fmt(fee, cur)} → 잔여현금 {fmt(cash(con, cur), cur)} {cur}")


def cmd_pf():
    con = db()
    tickers = [r[0] for r in con.execute("SELECT DISTINCT ticker FROM trades")]
    total = {c: cash(con, c) for c in CFG["cash"]}
    print(f"{'티커':<11}{'수량':>8}{'평단':>12}{'현재가':>12}{'평가손익':>14}{'수익률':>8}")
    for t in tickers:
        qty, avg = position(trades_of(con, t))
        if qty <= 1e-9:
            continue
        cur = currency(t)
        r = fetch(t)
        if r is None:
            print(f"{t:<11}{qty:>8g}{fmt(avg, cur):>12}{'데이터 없음':>12}")
            continue
        px = r[0][-1]
        total[cur] += qty * px
        print(f"{t:<11}{qty:>8g}{fmt(avg, cur):>12}{fmt(px, cur):>12}{fmt((px - avg) * qty, cur):>14}{px / avg - 1:>+8.1%}")
    for c, v in total.items():
        print(f"\n{c} 현금 {fmt(cash(con, c), c)} / 총평가 {fmt(v, c)} / 시작 대비 {v / CFG['cash'][c] - 1:+.2%}", end="")
    print()


def cmd_log():
    for r in db().execute("SELECT ts, side, ticker, qty, price, currency, note FROM trades ORDER BY id"):
        print(f"{r[0]}  {r[1]:<4} {r[2]:<11}{r[3]:>8g} @ {fmt(r[4], r[5])} {r[5]}  {r[6] or ''}")


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        sys.exit(__doc__)
    c = a[0]
    if c == "scan":
        cmd_scan()
    elif c == "brief":
        cmd_brief()
    elif c == "quote" and len(a) >= 2:
        cmd_quote(a[1])
    elif c == "chart" and len(a) >= 2:
        cmd_chart(a[1], int(a[2]) if len(a) > 2 else 250)
    elif c in ("buy", "sell") and len(a) >= 3:
        cmd_trade(c.upper(), a[1], a[2], " ".join(a[3:]))
    elif c == "pf":
        cmd_pf()
    elif c == "log":
        cmd_log()
    else:
        sys.exit(__doc__)
