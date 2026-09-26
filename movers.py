#!/usr/bin/env python3
"""해외 급등락 알림 → 텔레그램 전송.

    1) 시세   yfinance 로 추적 종목의 전 거래일·당일 정규장 종가
    2) 판정   |당일 종가 ÷ 전 거래일 종가 - 1| 이 임계값 이상인 종목만
    3) 원인   Claude 웹서치로 사건 단위로 묶어 한국어 설명
    4) 전송   메시지 2통 (주가 / 원인)
    5) 기록   movers/YYYY-MM.csv 에 한 줄씩

데일리 클리핑(clipper.py)과 코드·설정·워크플로가 전부 분리돼 있습니다.
이쪽이 죽어도 아침 뉴스는 그대로 나갑니다.

사용:
    python movers.py --dry-run          # 전송 없이 콘솔 출력
    python movers.py --no-ai            # 원인 탐색 없이 주가만
    python movers.py --send-at 07:30    # 준비를 끝내고 정각에 전송
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
RECORD_DIR = ROOT / "movers"
STATE_PATH = RECORD_DIR / "state.json"
KST = timezone(timedelta(hours=9), "KST")
HTTP_TIMEOUT = 25

# 화면에 통화를 어떻게 붙일지. 없는 통화는 'CODE 123.45' 로 떨어집니다.
CURRENCY = {"USD": ("$", ""), "EUR": ("€", ""), "GBP": ("£", ""), "CHF": ("CHF ", "")}


def env(name: str) -> str:
    """환경변수를 읽고 앞뒤 공백·줄바꿈을 제거합니다.

    GitHub Secrets 에 키를 붙여넣을 때 끝에 줄바꿈이 딸려오는 일이 흔한데,
    그대로 HTTP 헤더를 만들면 원인을 찾기 어려운 접속 오류로 나타납니다.
    """
    return (os.environ.get(name) or "").strip()


def now_kst() -> datetime:
    return datetime.now(KST)


def load_dotenv(path: Path) -> int:
    """로컬 테스트용 .env 로더.

    GitHub Actions 에서는 Secrets 가 이미 환경변수로 들어오므로 이미 설정된
    값은 절대 덮어쓰지 않습니다. (.env 는 .gitignore 에 들어 있습니다)
    """
    if not path.exists():
        return 0
    loaded = 0
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value and not os.environ.get(key):
            os.environ[key] = value
            loaded += 1
    return loaded


def force_utf8_output() -> None:
    """콘솔 출력을 UTF-8 로 고정합니다.

    윈도우 기본 콘솔 코드페이지는 cp949 라서 메시지에 든 이모지를 찍는 순간
    UnicodeEncodeError 로 죽습니다. 텔레그램 전송은 멀쩡한데 --dry-run 만
    터지므로 원인을 엉뚱한 데서 찾게 됩니다. GitHub Actions 는 UTF-8 이라
    거기서는 겪지 않습니다.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass  # 파이프로 넘긴 경우 등. 못 바꿔도 진행합니다.


# ─────────────────────────────────────────────────────────────
# 자료구조
# ─────────────────────────────────────────────────────────────
@dataclass
class Quote:
    ticker: str
    name: str
    sector: str
    currency: str = ""
    prev_close: float | None = None
    prev_date: str = ""
    close: float | None = None
    close_date: str = ""
    pct: float | None = None
    # 종가를 일봉이 아니라 시간봉 마지막 값으로 채웠다는 뜻. 유럽 종목에서
    # 일상적으로 일어납니다(아래 fetch_eu 주석 참고). 실측 오차 평균 0.18%p.
    approx: bool = False
    ext_price: float | None = None
    ext_pct: float | None = None
    ext_time: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.pct is not None


@dataclass
class Event:
    """급등락 하나를 설명하는 사건. 기업이 아니라 사건이 단위입니다."""

    headline: str
    tickers: list[str]
    explanation: str
    url: str = ""
    confidence: str = "추정"  # 확인 | 추정
    body_checked: bool = True
    sources: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────
# 1) 시세
# ─────────────────────────────────────────────────────────────
def is_us(ticker: str) -> bool:
    """미국 상장인가. yfinance 는 미국 종목에만 접미사를 붙이지 않습니다."""
    return "." not in ticker


def fetch_us(y, q: Quote) -> None:
    """미국 종목: 일봉으로 종가를 잡고, 1분봉(prepost)으로 시간외를 덧붙입니다."""
    d = y.history(period="10d", interval="1d", auto_adjust=False).dropna(subset=["Close"])
    if len(d) < 2:
        q.error = "일봉 부족"
        return
    q.close = float(d["Close"].iloc[-1])
    q.close_date = str(d.index[-1].date())
    q.prev_close = float(d["Close"].iloc[-2])
    q.prev_date = str(d.index[-2].date())
    q.pct = (q.close / q.prev_close - 1) * 100

    # 시간외. 마지막 1분봉이 정규장 종가 봉이면 ext_pct 가 0 에 수렴해
    # 어차피 꼬리표 기준(extended_min_pct)에 걸리지 않으므로 따로 걸러내지 않습니다.
    try:
        p = y.history(period="1d", interval="1m", prepost=True).dropna(subset=["Close"])
        if len(p):
            q.ext_price = float(p["Close"].iloc[-1])
            q.ext_pct = (q.ext_price / q.close - 1) * 100
            q.ext_time = p.index[-1].strftime("%m-%d %H:%M %Z")
    except Exception:
        pass  # 시간외는 없어도 되는 정보라 조용히 넘어갑니다


def fetch_eu(y, q: Quote) -> None:
    """유럽 종목: 일봉이 하루 늦게 들어오므로 시간봉으로 당일을 채웁니다.

    실측(2026-09-18): ENR.DE·RWE.DE·ABBN.SW·SU.PA·PRY.MI·NEX.PA·NKT.CO·NG.L
    여덟 종목 전부, 현지 마감 21시간이 지나도 일봉의 최신 행이 비어 있고
    확정값은 전 거래일까지만 있었습니다. 07:30 발송 시점에는 확실히 없습니다.

    시간봉에는 당일 값이 있으므로 거래일별 마지막 시간봉을 종가 대용으로 씁니다.
    종가 단일가(클로징 옥션)가 시간봉에 안 잡혀서 오차가 생기는데, 32건을
    확정 일봉과 대조한 결과 평균 0.179%p·최대 0.914%p 였습니다. 이 값으로
    채운 종목은 approx 로 표시해 메시지에도 그렇게 적습니다.

    일봉이 제때 들어오는 날에는 시간봉이 더 새로운 날짜를 갖지 않으므로
    아무것도 덧붙이지 않고 approx 도 서지 않습니다. 저절로 맞아 들어갑니다.
    """
    d = y.history(period="1mo", interval="1d", auto_adjust=False).dropna(subset=["Close"])
    if not len(d):
        q.error = "일봉 없음"
        return
    closes: list[tuple[date, float, bool]] = [
        (i.date(), float(c), False) for i, c in zip(d.index, d["Close"])
    ]

    try:
        h = y.history(period="5d", interval="1h", auto_adjust=False).dropna(subset=["Close"])
        if len(h):
            newest = closes[-1][0]
            for day, val in h.groupby(h.index.date)["Close"].last().items():
                if day > newest:
                    closes.append((day, float(val), True))
    except Exception:
        pass

    if len(closes) < 2:
        q.error = "비교할 거래일 부족"
        return
    (pd_, pv, _), (cd, cv, approx) = closes[-2], closes[-1]
    q.prev_date, q.prev_close = str(pd_), pv
    q.close_date, q.close, q.approx = str(cd), cv, approx
    q.pct = (cv / pv - 1) * 100


def fetch_one(spec: dict, sector: str) -> Quote:
    import yfinance as yf

    q = Quote(ticker=spec["ticker"], name=spec["name"], sector=sector)
    try:
        y = yf.Ticker(q.ticker)
        (fetch_us if is_us(q.ticker) else fetch_eu)(y, q)
        if q.ok:
            try:
                q.currency = y.fast_info.get("currency") or ""
            except Exception:
                pass
    except Exception as e:  # 한 종목이 죽어도 나머지는 나갑니다
        q.error = f"{type(e).__name__}: {e}"[:80]
    return q


def fetch_all(cfg: dict) -> list[Quote]:
    jobs = [(s, t) for s in cfg["sectors"] for t in s["tickers"]]
    print(f"  · {len(jobs)}종목 조회 중…")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=6) as pool:
        quotes = list(pool.map(lambda j: fetch_one(j[1], j[0]["name"]), jobs))
    bad = [q.ticker for q in quotes if not q.ok]
    print(f"  · {time.time() - t0:.1f}초 · 성공 {len(quotes) - len(bad)}/{len(quotes)}"
          + (f" · 실패 {', '.join(bad)}" if bad else ""))
    return quotes


# ─────────────────────────────────────────────────────────────
# 2) 판정
# ─────────────────────────────────────────────────────────────
def session_date(quotes: list[Quote]) -> str:
    """이번 회차의 기준 거래일. 가장 최근 거래일을 씁니다.

    휴장일 달력을 따로 두지 않습니다. '지난번에 보낸 거래일과 같으면 건너뛴다'
    한 줄이면 주말·미국 공휴일·유럽 공휴일이 전부 해결됩니다. 결과적으로
    화~토 발송이 되고, 모든 시장이 쉰 날은 저절로 조용해집니다.
    """
    days = [q.close_date for q in quotes if q.ok]
    return max(days) if days else ""


def pick_movers(quotes: list[Quote], cfg: dict) -> list[Quote]:
    th = float(cfg.get("threshold_pct", 5.0))
    return [q for q in quotes if q.ok and abs(q.pct) >= th]


# ─────────────────────────────────────────────────────────────
# 3) 렌더링
# ─────────────────────────────────────────────────────────────
def esc(s) -> str:
    return html.escape(str(s or "").strip(), quote=False)


def fmt_price(value: float, currency: str) -> str:
    """통화를 붙여 가격 한 조각을 만듭니다.

    National Grid(NG.L)의 통화는 GBp — 파운드가 아니라 펜스입니다.
    1134.0 은 £11.34 입니다. 그대로 찍으면 주가가 100배로 보입니다.
    """
    if currency == "GBp":
        return f"£{value / 100:,.2f}"
    pre, post = CURRENCY.get(currency, (f"{currency} " if currency else "", ""))
    digits = 0 if value >= 1000 and currency in ("DKK", "JPY") else 2
    return f"{pre}{value:,.{digits}f}{post}"


def weekday_ko(d: date) -> str:
    return "월화수목금토일"[d.weekday()]


def render_prices(quotes: list[Quote], movers: list[Quote], cfg: dict, sess: str) -> str:
    ext_min = float(cfg.get("extended_min_pct", 3.0))
    sanity = float(cfg.get("sanity_max_pct", 25.0))
    sd = date.fromisoformat(sess)
    now = now_kst()

    lines = [
        f"<b>📊 해외 장전 시황 · {now.month}/{now.day}({weekday_ko(now.date())})</b>",
        f"<i>정규장 {sd.month}/{sd.day} 종가 · 조회 {now:%H:%M} KST</i>",
    ]

    for sec in cfg["sectors"]:
        sec_name = sec["name"]
        got = [q for q in quotes if q.sector == sec_name and q.ok]
        if not got:
            continue
        avg = sum(q.pct for q in got) / len(got)
        up = sum(1 for q in got if q.pct > 0)
        lines += ["", f"──── {sec.get('emoji', '')} {sec_name} ────",
                  f"평균 {avg:+.1f}%  ·  {len(got)}종목 중 {up}개 상승"]

        rows = [q for q in movers if q.sector == sec_name]
        # 상승률 높은 순 → 하락폭 큰 순
        rows.sort(key=lambda q: (-q.pct) if q.pct > 0 else (1e9 + q.pct))
        if not rows:
            lines.append("<i>기준 충족 종목 없음</i>")
            continue
        for q in rows:
            mark = "▲" if q.pct > 0 else "▼"
            tail = []
            if q.ext_pct is not None and abs(q.ext_pct) >= ext_min:
                tail.append(f"시간외 {q.ext_pct:+.1f}%")
            if q.approx:
                tail.append("장중 마지막 체결 기준")
            if abs(q.pct) >= sanity:
                tail.append("⚠️ 검증 필요")
            suffix = ("  · " + " · ".join(tail)) if tail else ""
            lines.append(
                f"<code>{mark} {q.pct:+6.1f}%</code>  {esc(q.name)} "
                f"({esc(q.ticker)}) {esc(fmt_price(q.close, q.currency))}{esc(suffix)}"
            )

    failed = [q for q in quotes if not q.ok]
    if failed:
        lines += ["", f"⚠️ 조회 실패 {len(failed)}건: " + esc(", ".join(q.ticker for q in failed))]
    return "\n".join(lines)


def render_causes(events: list[Event], movers: list[Quote], unexplained: list[str]) -> str:
    now = now_kst()
    by_ticker = {q.ticker: q for q in movers}
    lines = [f"<b>🔎 왜 움직였나 · {now.month}/{now.day}({weekday_ko(now.date())})</b>"]

    n = 0
    for ev in events:
        n += 1
        tags = [ev.confidence]
        if not ev.body_checked:
            tags.append("본문 미확인")
        lines += ["", f"<b>{n}. {esc(ev.headline)}</b>  ·  {esc(' · '.join(tags))}",
                  esc(ev.explanation)]
        moved = []
        for t in ev.tickers:
            q = by_ticker.get(t)
            if q:
                moved.append(f"{q.name} {q.pct:+.1f}%")
        if moved:
            lines.append("↳ " + esc(" · ".join(moved)))
        if ev.url:
            lines.append(f'🔗 <a href="{esc(ev.url)}">링크</a>')

    for t in unexplained:
        q = by_ticker.get(t)
        if not q:
            continue
        n += 1
        lines += ["", f"<b>{n}. {esc(q.name)} {q.pct:+.1f}%</b>  ·  원인 미확인",
                  "해당 종목의 뉴스·공시를 찾지 못했습니다."]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# 4) 원인 탐색 — Claude 웹서치
# ─────────────────────────────────────────────────────────────
SYSTEM = """당신은 한국 증권사 애널리스트를 위해 해외 주식의 급등락 원인을 조사합니다.

원칙:
1. 기업별이 아니라 **사건 단위**로 묶으십시오. 정책 뉴스 하나로 원전주 네 곳이
   올랐다면 사건은 하나이고 관련 종목이 넷입니다. 같은 설명을 종목마다 반복하지 마십시오.
2. 원인이 서로 다르면 별도 사건으로 나누십시오.
3. **찾지 못한 것은 찾지 못했다고 하십시오.** 그럴듯한 추측으로 빈칸을 채우지
   마십시오. 근거가 없으면 unexplained 에 넣으십시오. 이것이 이 일에서 가장 중요합니다.
4. 확인된 사실과 추정을 구분하십시오.
   - confidence "확인": 공시·보도자료·복수 매체 보도로 사실이 확인됨
   - confidence "추정": 정황은 맞아떨어지지만 그 기업의 주가 움직임과 직접 연결한
     보도는 없음. 연결 근거를 설명에 반드시 밝히십시오.
   - **개별 재료를 찾지 못해 매크로·섹터·업종 동반 상승으로 설명한다면 무조건 "추정"입니다.**
     사건 자체(금리 하락, 유가 급락 등)가 사실로 확인되더라도, 그것이 이 종목을 그만큼
     움직였다는 것은 확인된 사실이 아닙니다. 설명에 "개별 재료는 확인되지 않았다"고
     써 놓고 confidence 를 "확인"으로 다는 것은 모순입니다.
   - 전일 낙폭 회복·되돌림으로 설명하는 경우도 "추정"입니다.
5. 기사 본문까지 읽었으면 body_checked true, 제목·검색 스니펫까지만 봤으면 false.
   읽지 않은 본문의 내용을 읽은 것처럼 요약하지 마십시오.
6. 실적 발표가 원인이면 EPS·컨센서스·가이던스 등 핵심 숫자를 넣으십시오.
   ("EPS $2.14로 컨센서스 $1.87 상회, 내년 가이던스 상향" 수준)
7. 설명은 한국어로 **사건당 2~3문장, 300자 이내**. 아침에 훑는 알림이라 길면
   읽히지 않습니다. 조사 과정에서 알게 된 배경을 다 쏟지 말고, 주가가 그날 그만큼
   움직인 이유만 남기십시오. 추정일 때 근거가 약한 이유는 한 문장으로만 밝히십시오.
   기업명은 한국어 표기가 익숙하면 한국어로 쓰되, tickers 배열에는 반드시 주어진
   티커를 그대로 쓰십시오.
8. url 은 그 사건의 대표 출처 하나. 회사 IR·보도자료·공시를 언론 기사보다 우선합니다.
   X(트위터) 등 SNS 는 공식 계정이라도 보도자료·공시·기사가 있으면 그쪽을 쓰십시오.

출력은 아래 형태의 JSON 하나만. 설명 문장이나 코드펜스 없이 JSON 만 쓰십시오.

{
  "events": [
    {
      "headline": "사건 제목 (한국어, 한 줄)",
      "tickers": ["OKLO", "SMR"],
      "explanation": "한국어 2~3문장",
      "url": "https://...",
      "confidence": "확인",
      "body_checked": true
    }
  ],
  "unexplained": ["FLNC"]
}

주어진 종목은 events 의 tickers 와 unexplained 에 빠짐없이 한 번씩 등장해야 합니다."""


def find_causes(movers: list[Quote], cfg: dict, sess: str) -> tuple[list[Event], list[str]]:
    """급등락 종목들의 원인을 웹서치로 찾아 사건 단위로 묶습니다."""
    ai = cfg.get("ai") or {}
    api_key = env("ANTHROPIC_API_KEY")
    if not api_key:
        print("  ! ANTHROPIC_API_KEY 없음 — 원인 탐색을 건너뜁니다", file=sys.stderr)
        return [], [q.ticker for q in movers]

    import anthropic

    rows = []
    for q in sorted(movers, key=lambda x: -abs(x.pct)):
        ext = f", 시간외 {q.ext_pct:+.1f}%" if q.ext_pct is not None and abs(q.ext_pct) >= 1 else ""
        rows.append(f"- {q.name} ({q.ticker}, {q.sector}): {q.pct:+.2f}%{ext}")

    user = (
        f"{sess} 미국·유럽 정규장 종가 기준으로 크게 움직인 종목입니다.\n"
        f"각각 왜 움직였는지 웹에서 찾아 사건 단위로 묶어 주십시오.\n"
        f"오늘 날짜는 {now_kst():%Y-%m-%d} 입니다. {sess} 전후의 뉴스·공시를 보십시오.\n\n"
        + "\n".join(rows)
    )

    client = anthropic.Anthropic(api_key=api_key)
    tools = [{
        "type": "web_search_20260209",
        "name": "web_search",
        "max_uses": int(ai.get("max_searches", 12)),
    }]
    messages = [{"role": "user", "content": user}]

    print(f"  · {len(movers)}종목의 원인을 Claude 웹서치로 조사 중…")
    text = ""
    for _ in range(4):  # pause_turn 으로 끊기면 이어서 재개합니다
        try:
            with client.messages.stream(
                model=ai.get("model", "claude-opus-5"),
                max_tokens=int(ai.get("max_tokens", 16000)),
                system=SYSTEM,
                thinking={"type": "adaptive"},
                output_config={"effort": ai.get("effort", "high")},
                tools=tools,
                messages=messages,
            ) as stream:
                msg = stream.get_final_message()
        except anthropic.APIError as e:
            print(f"  ! Claude 호출 실패: {type(e).__name__}: {e}", file=sys.stderr)
            return [], [q.ticker for q in movers]

        text = "".join(b.text for b in msg.content if b.type == "text")
        if msg.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": msg.content})

    return parse_causes(text, movers)


def parse_causes(text: str, movers: list[Quote]) -> tuple[list[Event], list[str]]:
    """모델 응답에서 JSON 을 꺼냅니다. 실패하면 전부 '미확인'으로 떨어집니다.

    거짓 설명을 내보내느니 미확인이 낫습니다.
    """
    known = {q.ticker for q in movers}
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        print("  ! 응답에서 JSON 을 찾지 못했습니다 — 전부 미확인 처리", file=sys.stderr)
        return [], sorted(known)
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        print(f"  ! JSON 파싱 실패({e}) — 전부 미확인 처리", file=sys.stderr)
        return [], sorted(known)

    events, covered = [], set()
    for e in data.get("events") or []:
        tickers = [t for t in (e.get("tickers") or []) if t in known]
        if not tickers or not (e.get("headline") or "").strip():
            continue  # 추적하지 않는 종목이나 빈 사건은 버립니다
        events.append(Event(
            headline=str(e.get("headline", "")).strip(),
            tickers=tickers,
            explanation=str(e.get("explanation", "")).strip(),
            url=str(e.get("url", "")).strip(),
            confidence="확인" if str(e.get("confidence")) == "확인" else "추정",
            body_checked=bool(e.get("body_checked", False)),
        ))
        covered |= set(tickers)

    # 모델이 빠뜨린 종목도 미확인으로 반드시 내보냅니다. 조용히 사라지면
    # '원인을 못 찾은 것'과 '종목이 목록에 없던 것'을 구분할 수 없습니다.
    unexplained = sorted((set(data.get("unexplained") or []) & known) | (known - covered))
    return events, unexplained


# ─────────────────────────────────────────────────────────────
# 5) 전송 · 기록
# ─────────────────────────────────────────────────────────────
def split_message(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks


def send_telegram(text: str, chat_id_env: str) -> bool:
    import requests

    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env(chat_id_env) if chat_id_env else ""
    if not chat_id:
        chat_id = env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("  ! TELEGRAM_BOT_TOKEN / chat_id 없음 — 전송 생략", file=sys.stderr)
        return False

    for chunk in split_message(text):
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": chunk, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=HTTP_TIMEOUT,
        )
        if not r.ok:
            print(f"  ! 전송 실패: {r.status_code} {r.text[:200]}", file=sys.stderr)
            return False
    return True


FIELDS = ["거래일", "조회시각", "섹터", "기업명", "티커", "통화", "전일종가", "종가",
          "변동률", "종가출처", "시간외가격", "시간외변동률", "시간외시각",
          "상태", "사건", "설명", "링크", "발송"]


def write_records(quotes: list[Quote], movers: list[Quote], events: list[Event],
                  unexplained: list[str], sess: str, sent: bool) -> Path:
    """기준을 충족한 종목을 월별 CSV 에 append 합니다.

    조회 실패 종목도 남깁니다. '기준 충족 종목 없음'과 '조회 실패'를
    나중에 구분할 수 있어야 합니다.
    """
    RECORD_DIR.mkdir(exist_ok=True)
    path = RECORD_DIR / f"{sess[:7]}.csv"
    cause = {}
    for ev in events:
        for t in ev.tickers:
            cause[t] = ev
    stamp = now_kst().strftime("%Y-%m-%d %H:%M:%S%z")

    rows = []
    for q in movers:
        ev = cause.get(q.ticker)
        if ev:
            state = ev.confidence + ("" if ev.body_checked else "·본문 미확인")
        else:
            state = "미확인"
        rows.append({
            "거래일": sess, "조회시각": stamp, "섹터": q.sector, "기업명": q.name,
            "티커": q.ticker, "통화": q.currency,
            "전일종가": f"{q.prev_close:.4f}", "종가": f"{q.close:.4f}",
            "변동률": f"{q.pct:.2f}",
            "종가출처": "시간봉(장중 마지막 체결)" if q.approx else "일봉 종가",
            "시간외가격": f"{q.ext_price:.4f}" if q.ext_price is not None else "",
            "시간외변동률": f"{q.ext_pct:.2f}" if q.ext_pct is not None else "",
            "시간외시각": q.ext_time, "상태": state,
            "사건": ev.headline if ev else "", "설명": ev.explanation if ev else "",
            "링크": ev.url if ev else "", "발송": "성공" if sent else "실패",
        })
    for q in (x for x in quotes if not x.ok):
        rows.append({"거래일": sess, "조회시각": stamp, "섹터": q.sector, "기업명": q.name,
                     "티커": q.ticker, "상태": f"조회 실패: {q.error}",
                     "발송": "성공" if sent else "실패"})

    fresh = not path.exists()
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        if fresh:
            w.writeheader()
        w.writerows(rows)
    return path


def read_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(sess: str, n_movers: int) -> None:
    RECORD_DIR.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(
        {"last_session": sess, "last_run": now_kst().isoformat(timespec="seconds"),
         "last_movers": n_movers}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def wait_until(hhmm: str) -> None:
    """준비를 끝낸 채로 목표 시각까지 기다립니다.

    GitHub 쪽 대기는 실행을 붙잡아 두는 용도라 초 단위가 맞지 않습니다.
    시세 조회와 원인 탐색을 먼저 끝내고 여기서 마지막 몇 분을 기다려야
    도착 시각이 정확해집니다. 준비가 목표를 넘겨 끝났으면 바로 보냅니다.
    """
    if not hhmm:
        return
    hh, mm = (int(x) for x in hhmm.split(":"))
    now = now_kst()
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    delay = (target - now).total_seconds()
    if delay <= 0:
        print(f"  · 목표 {hhmm} 을 이미 지나 바로 보냅니다 (현재 {now:%H:%M:%S} KST)")
        return
    print(f"  · 준비 완료 ({now:%H:%M:%S} KST). {hhmm}:00 까지 {delay:.0f}초 대기")
    time.sleep(delay)


# ─────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="해외 급등락 알림")
    ap.add_argument("--config", default=str(ROOT / "movers.yaml"))
    ap.add_argument("--dry-run", action="store_true", help="전송 없이 콘솔 출력")
    ap.add_argument("--no-ai", action="store_true", help="원인 탐색 없이 주가만")
    ap.add_argument("--threshold", type=float, default=None, metavar="PCT",
                    help="임계값을 이번 실행만 바꿉니다 (설정값 대신)")
    ap.add_argument("--send-at", default="", metavar="HH:MM",
                    help="준비를 끝내고 이 시각(KST) 정각에 전송합니다")
    ap.add_argument("--marker", default="", metavar="파일",
                    help="실제로 보냈을 때만 이 파일을 만듭니다 (워크플로 중복 방지용)")
    ap.add_argument("--force", action="store_true",
                    help="이미 보낸 거래일이어도 다시 보냅니다")
    args = ap.parse_args()
    force_utf8_output()
    n = load_dotenv(ROOT / ".env")
    if n:
        print(f"  · .env 에서 {n}개 값을 읽었습니다")

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    if args.threshold is not None:
        cfg["threshold_pct"] = args.threshold

    quotes = fetch_all(cfg)
    sess = session_date(quotes)
    if not sess:
        print("  ! 시세를 한 건도 받지 못했습니다. 아무것도 보내지 않습니다.", file=sys.stderr)
        return 1

    # 휴장이라 새 거래 결과가 없는 날. 어제 등락률을 오늘 것처럼 다시 보내지 않습니다.
    last = read_state().get("last_session")
    if last == sess and not args.force:
        print(f"  · 기준 거래일 {sess} 은 이미 보냈습니다 (휴장/주말). 종료합니다.")
        return 0

    movers = pick_movers(quotes, cfg)
    print(f"  · 기준 거래일 {sess} · 기준 충족 {len(movers)}종목")

    events, unexplained = [], []
    if movers and not args.no_ai and (cfg.get("ai") or {}).get("enabled", True):
        events, unexplained = find_causes(movers, cfg, sess)
    elif movers:
        unexplained = sorted(q.ticker for q in movers)

    price_msg = render_prices(quotes, movers, cfg, sess)
    cause_msg = render_causes(events, movers, unexplained) if movers else ""

    wait_until(args.send_at)

    if args.dry_run:
        print("\n" + "=" * 64 + "\n" + price_msg)
        if cause_msg:
            print("\n" + "=" * 64 + "\n" + cause_msg)
        print("\n(연습 실행 — 전송·기록·상태 저장을 하지 않습니다)")
        return 0

    chan = cfg.get("chat_id_env", "")
    sent = send_telegram(price_msg, chan)
    if sent and cause_msg:
        sent = send_telegram(cause_msg, chan)

    path = write_records(quotes, movers, events, unexplained, sess, sent)
    print(f"  · 기록 {path.relative_to(ROOT)}")
    if sent:
        # 실제로 나갔을 때만 도장을 찍습니다. 전송이 실패한 실행은 표시를
        # 남기지 않으므로 뒤따르는 예약이 다시 시도합니다.
        write_state(sess, len(movers))
        if args.marker:
            Path(args.marker).write_text(f"{sess}\n", encoding="utf-8")
    return 0 if sent else 1


if __name__ == "__main__":
    raise SystemExit(main())
