#!/usr/bin/env python3
"""원전·건설 데일리 뉴스 클리핑 → 텔레그램 전송.

파이프라인:
    1) 수집   Google News RSS(키워드/매체 지정) + 전문지 직접 RSS
    2) 정제   시간창 필터 → 제목 정규화 → 같은 사건끼리 클러스터링
    3) 1차    키워드 스코어링으로 20~30건 숏리스트
    4) 2차    Claude가 중요도 판단 → 최종 8건 선별 + 요약
    5) 전송   텔레그램 봇 메시지

사용:
    python clipper.py                # 전체 실행 후 텔레그램 전송
    python clipper.py --dry-run      # 전송 없이 콘솔 출력 (결과 미리보기)
    python clipper.py --no-ai        # Claude 호출 없이 스코어링 결과만
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import quote

import feedparser
import requests
import yaml
from pydantic import BaseModel, Field

UA = "Mozilla/5.0 (compatible; news-clipper/1.0)"
HTTP_TIMEOUT = 25
AXES = ["수주·계약·실적", "정책·규제", "프로젝트·기술", "리스크·사고"]


# ─────────────────────────────────────────────────────────────
# 자료구조
# ─────────────────────────────────────────────────────────────
@dataclass
class Article:
    title: str
    url: str
    outlet: str
    published: datetime
    lang: str
    group: str
    snippet: str = ""

    @property
    def key(self) -> str:
        return normalize_title(self.title)


@dataclass
class Cluster:
    """같은 사건을 다룬 기사 묶음."""

    articles: list[Article] = field(default_factory=list)
    score: float = 0.0

    @property
    def lead(self) -> Article:
        # 가장 먼저 보도한 기사를 대표로
        return min(self.articles, key=lambda a: a.published)

    @property
    def outlets(self) -> list[str]:
        seen: list[str] = []
        for a in self.articles:
            if a.outlet and a.outlet not in seen:
                seen.append(a.outlet)
        return seen


# ─────────────────────────────────────────────────────────────
# 텍스트 유틸
# ─────────────────────────────────────────────────────────────
_TAG_RE = re.compile(r"<[^>]+>")
_BRACKET_RE = re.compile(r"[\[\(<【][^\]\)>】]{1,20}[\]\)>】]")
_NONWORD_RE = re.compile(r"[^0-9A-Za-z가-힣]+")


def strip_html(s: str) -> str:
    return html.unescape(_TAG_RE.sub(" ", s or "")).strip()


def normalize_title(title: str) -> str:
    """제목을 비교용으로 정규화. 매체명 꼬리표·말머리·기호를 털어냅니다."""
    t = html.unescape(title or "")
    t = re.sub(r"\s+[-–—]\s+[^-–—]{1,40}$", "", t)  # Google News의 " - 매체명"
    t = _BRACKET_RE.sub(" ", t)  # [단독] [속보] (종합)
    t = _NONWORD_RE.sub("", t)
    return t.lower()


def bigrams(s: str) -> set[str]:
    """문자 2-gram. 한글·영문 모두에서 무난하게 동작합니다."""
    if len(s) < 2:
        return {s} if s else set()
    return {s[i : i + 2] for i in range(len(s) - 1)}


# 제목에 흔해서 변별력이 없는 단어들
STOPWORDS = {
    "있다", "없다", "대한", "위해", "통해", "오늘", "내일", "어제", "올해", "내년",
    "작년", "이번", "관련", "추진", "밝혔다", "나섰다", "기업", "사업", "회사",
    "국내", "해외", "정부", "시장", "업계", "전망", "계획", "예정", "지난해",
    "the", "and", "for", "with", "from", "that", "this", "will", "has", "have",
    "its", "new", "says", "said", "after", "over", "into", "amid", "more",
}
_TOKEN_RE = re.compile(r"[a-z0-9]{2,}|[가-힣]{2,}")


def content_tokens(title: str) -> list[str]:
    """제목에서 변별력 있는 단어만 뽑아냅니다."""
    t = html.unescape(title or "").lower()
    t = re.sub(r"\s+[-–—]\s+[^-–—]{1,40}$", "", t)
    t = _BRACKET_RE.sub(" ", t)
    return [w for w in _TOKEN_RE.findall(t) if w not in STOPWORDS]


def token_match(a: str, b: str) -> float:
    """두 단어가 같은 말인지. 한국어 조사 차이(상용화/상용화까지)를 흡수합니다."""
    if a == b:
        return 1.0
    if len(a) >= 2 and len(b) >= 2 and (a.startswith(b) or b.startswith(a)):
        return 0.8
    return 0.0


def topic_overlap(a: list[str], b: list[str]) -> float:
    """짧은 쪽 기준 단어 겹침 비율 (0~1).

    글자 단위 비교로는 같은 사건인 줄 모르는 경우 —
    "SMR 개발부터 상용화까지 속도낸다…특별법 오늘 시행" 과
    "SMR 특별법·시행령 11일부터 시행…범정부 지원체계 구체화" —
    를 공유 단어(smr, 특별법, 시행)로 잡아냅니다.

    IDF 가중치는 일부러 쓰지 않습니다. 여러 매체가 받아쓴 사건일수록 그
    사건의 핵심 단어가 코퍼스에서 흔해져, 정작 사건을 식별해주는 단어의
    가중치가 깎이는 역효과가 납니다.
    """
    sa, sb = list(dict.fromkeys(a)), list(dict.fromkeys(b))
    if not sa or not sb:
        return 0.0

    matched, used = 0.0, set()
    for ta in sa:
        best, pick = 0.0, None
        for tb in sb:
            if tb in used:
                continue
            score = token_match(ta, tb)
            if score > best:
                best, pick = score, tb
        if pick:
            matched += best
            used.add(pick)

    return matched / min(len(sa), len(sb))


def same_story(a: set[str], b: set[str], threshold: float) -> bool:
    """두 제목이 같은 사건인지 판정.

    자카드 유사도만 쓰면 길이가 크게 다른 제목(짧은 속보 vs 긴 종합기사)을
    놓칩니다. 짧은 쪽이 긴 쪽에 얼마나 포함되는지(containment)를 함께 봅니다.
    """
    if not a or not b:
        return False
    inter = len(a & b)
    if inter / len(a | b) >= threshold:
        return True
    return inter / min(len(a), len(b)) >= threshold + 0.15


# ─────────────────────────────────────────────────────────────
# 1) 수집
# ─────────────────────────────────────────────────────────────
def google_news_url(query: str, lang: str) -> str:
    locale = "hl=ko&gl=KR&ceid=KR:ko" if lang == "ko" else "hl=en-US&gl=US&ceid=US:en"
    return f"https://news.google.com/rss/search?q={quote(query)}&{locale}"


def fetch_feed(url: str) -> list[dict]:
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": UA})
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"  ! 수집 실패 {url[:70]}… : {e}", file=sys.stderr)
        return []
    return feedparser.parse(r.content).entries


def parse_entry(entry: dict, lang: str, group: str, tz: timezone) -> Article | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    published = datetime(*parsed[:6], tzinfo=timezone.utc).astimezone(tz)

    title = strip_html(entry.get("title", ""))
    if not title:
        return None

    # Google News는 <source>에 원매체명을 담아줍니다.
    outlet = ""
    src = entry.get("source")
    if isinstance(src, dict):
        outlet = strip_html(src.get("title", ""))
    if not outlet:
        m = re.search(r"\s+[-–—]\s+([^-–—]{1,40})$", title)
        outlet = m.group(1).strip() if m else group

    return Article(
        title=re.sub(r"\s+[-–—]\s+[^-–—]{1,40}$", "", title).strip(),
        url=entry.get("link", ""),
        outlet=outlet,
        published=published,
        lang=lang,
        group=group,
        snippet=strip_html(entry.get("summary", ""))[:300],
    )


def collect(cfg: dict, tz: timezone, window_days: int) -> list[Article]:
    """설정에 적힌 모든 소스를 병렬로 긁어옵니다."""
    jobs: list[tuple[str, str, str]] = []  # (url, lang, group)

    for q in cfg.get("queries") or []:
        query = q["q"].replace("{window}", f"when:{window_days}d")
        jobs.append((google_news_url(query, q["lang"]), q["lang"], q["name"]))

    for s in cfg.get("site_queries") or []:
        query = f"{s['q']} site:{s['site']} when:{window_days}d"
        jobs.append((google_news_url(query, s["lang"]), s["lang"], s["name"]))

    for f in cfg.get("feeds") or []:
        jobs.append((f["url"], f["lang"], f["name"]))

    print(f"[1/5] 수집: {len(jobs)}개 소스")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda j: fetch_feed(j[0]), jobs))

    articles: list[Article] = []
    for (_, lang, group), entries in zip(jobs, results):
        for e in entries:
            a = parse_entry(e, lang, group, tz)
            if a and a.url:
                articles.append(a)
    print(f"      원시 기사 {len(articles)}건")
    return articles


# ─────────────────────────────────────────────────────────────
# 2) 시간창 필터 + 클러스터링
# ─────────────────────────────────────────────────────────────
def within_window(articles: list[Article], start: datetime, end: datetime) -> list[Article]:
    kept = [a for a in articles if start <= a.published <= end]
    print(f"[2/5] 시간창 {start:%m/%d %H:%M} ~ {end:%m/%d %H:%M} → {len(kept)}건")
    return kept


def cluster_articles(articles: list[Article], threshold: float) -> list[Cluster]:
    """제목이 비슷한 기사를 같은 사건으로 묶습니다."""
    clusters: list[Cluster] = []
    signatures: list[set[str]] = []

    # 먼저 보도된 순으로 처리해야 대표 기사가 안정적으로 잡힙니다.
    for a in sorted(articles, key=lambda x: x.published):
        sig = bigrams(a.key)
        if not sig:
            continue
        for i, existing in enumerate(signatures):
            if same_story(sig, existing, threshold):
                clusters[i].articles.append(a)
                break
        else:
            clusters.append(Cluster(articles=[a]))
            signatures.append(sig)

    print(f"      중복 제거 → {len(clusters)}개 사건")
    return clusters


# ─────────────────────────────────────────────────────────────
# 3) 1차 스코어링
# ─────────────────────────────────────────────────────────────
def score_clusters(clusters: list[Cluster], cfg: dict, end: datetime) -> list[Cluster]:
    rules = cfg["scoring"]
    topic_words = [w.lower() for w in rules["topic"]["words"]]
    blocked = [w.lower() for w in cfg.get("blocklist") or []]
    bonus_per_outlet = cfg.get("cluster_bonus", 3)

    scored: list[Cluster] = []
    dropped = 0
    for c in clusters:
        blob = " ".join(f"{a.title} {a.snippet}" for a in c.articles).lower()
        # 차단어는 제목에서만 봅니다. 본문 요약에 우연히 섞인 단어로
        # 멀쩡한 기사가 탈락하는 것을 막기 위해서입니다.
        titles = " ".join(a.title for a in c.articles).lower()

        # 주제어가 하나도 없으면 탈락 (수집 쿼리가 넓어서 잡히는 무관 기사 제거)
        if not any(w in blob for w in topic_words):
            continue

        # 홍보성·시황 기사는 매체 수 보너스로 살아남지 못하게 즉시 제거합니다.
        if any(w in titles for w in blocked):
            dropped += 1
            continue

        score = 0.0
        for name in ("topic", "event", "player", "noise"):
            rule = rules[name]
            hits = sum(1 for w in rule["words"] if w.lower() in blob)
            if name == "topic":
                hits = min(hits, 3)  # 주제어 반복으로 점수가 튀는 것 방지
            score += hits * rule["weight"]

        # 여러 매체가 동시에 다뤘다 = 업계가 중요하게 본다
        score += (len(c.outlets) - 1) * bonus_per_outlet

        # 최신 기사 가산 (24시간에 걸쳐 0~4점)
        age_h = (end - c.lead.published).total_seconds() / 3600
        score += max(0.0, 4.0 - age_h / 6)

        c.score = score
        scored.append(c)

    scored.sort(key=lambda c: c.score, reverse=True)
    print(f"      차단어로 제외 {dropped}건 → 채점 대상 {len(scored)}건")
    return scored


def balance(scored: list[Cluster], size: int, redundancy: float, word_overlap: float) -> list[Cluster]:
    """국내/해외 균형을 맞춰 숏리스트를 구성합니다.

    클러스터링은 제목이 꽤 비슷해야 묶이므로, 같은 사건을 크게 다르게 쓴
    기사들이 숏리스트 자리를 여러 개 차지할 수 있습니다. 여기서는 이미 뽑은
    기사와 조금이라도 겹치면 건너뛰어(느슨한 임계값) 주제 다양성을 확보합니다.
    """
    picked: list[Cluster] = []
    sigs: list[tuple[set[str], list[str]]] = []
    chosen: set[int] = set()

    def take(c: Cluster) -> bool:
        if id(c) in chosen:
            return False
        sig, toks = bigrams(c.lead.key), content_tokens(c.lead.title)
        for other_sig, other_toks in sigs:
            if same_story(sig, other_sig, redundancy):
                return False
            if topic_overlap(toks, other_toks) >= word_overlap:
                return False
        picked.append(c)
        sigs.append((sig, toks))
        chosen.add(id(c))
        return True

    half = size // 2
    for lang, quota in (("ko", half), ("en", size - half)):
        taken = 0
        for c in scored:
            if taken >= quota:
                break
            if c.lead.lang == lang and take(c):
                taken += 1

    for c in scored:  # 한쪽이 모자라면 남은 자리를 채웁니다
        if len(picked) >= size:
            break
        take(c)

    picked.sort(key=lambda c: c.score, reverse=True)
    ko_n = sum(1 for c in picked if c.lead.lang == "ko")
    print(f"[3/5] 숏리스트 {len(picked)}건 (국내 {ko_n} / 해외 {len(picked) - ko_n})")
    return picked


# ─────────────────────────────────────────────────────────────
# 4) Claude 중요도 판단
# ─────────────────────────────────────────────────────────────
class Pick(BaseModel):
    id: int = Field(description="후보 목록에 붙은 번호")
    block: Literal["원전", "건설·플랜트", "해외", "리스크"] = Field(
        description="묶음. 국내 원전/원자력=원전, 국내 건설·플랜트·정비사업=건설·플랜트, "
        "해외에서 벌어진 일=해외, 사고·중단·부실·소송·규제리스크=리스크"
    )
    tag: str = Field(
        description="줄 맨 앞에 붙는 태그. 기업 뉴스면 종목명(한수원, 삼성E&A, 현대건설), "
        "아니면 테마(정책, 규제, 전력망, 기자재). 공백 없이 8자 이내."
    )
    headline: str = Field(
        description="핵심 사실 한 줄. 명사형으로 끝냅니다(수주/체결/시행/정지). "
        "존댓말·서술형 금지. 35자 이내."
    )
    detail: str = Field(
        description="headline에 없는 정보를 더하는 부연. 금액·일정·규모·의미 중 하나. "
        "40자 이내. 더할 정보가 없으면 빈 문자열."
    )


class Curation(BaseModel):
    picks: list[Pick]


SYSTEM = """\
당신은 원전·건설 섹터를 담당하는 증권사 애널리스트의 리서치 어시스턴트입니다.
전날 나온 뉴스 후보 목록을 받아, 오늘 아침 애널리스트가 반드시 알아야 할 것만 골라냅니다.

[중요도 판단 기준] — 아래 4개 축에 걸리는 뉴스를 우선합니다.
  1. 수주·계약·실적 : 계약 체결, 수주 공시, 실적 발표, 수주잔고 변동
  2. 정책·규제      : 정부 정책, 법·제도 변경, 인허가, 예산 배정
  3. 프로젝트·기술  : 개별 프로젝트의 단계 진척, 착공·준공, 기술 실증·인증
  4. 리스크·사고    : 공사 중단, 사고, 부실, 소송, 제재

[선별 원칙]
- ★ 중복 제거가 최우선입니다. 후보 목록에는 같은 사건을 다룬 기사가 국문·영문으로
  여러 번 들어 있습니다(예: 같은 수주 건의 국내 기사와 외신 기사). 같은 사건은
  반드시 하나만 고르고, 정보가 가장 구체적인 번호를 선택하십시오. 같은 사건을
  두 번 고르면 결과물이 쓸모없어집니다.
- 구체적 사실(금액·일정·주체)이 있는 기사 > 전망·해설·기획 기사
- 시장에 새로운 정보 > 이미 알려진 사실의 반복
- 여러 매체가 동시에 다뤘다면 그만큼 중요하다는 신호
- 국내와 해외를 균형 있게 담되, 억지로 맞추지는 마십시오
- 다음은 제외합니다: 단순 주가 등락·특징주, 분양 광고성 기사, 인사·동정,
  스포츠 후원·기부·견학 같은 홍보성 사회공헌 기사, 단순 행사 개최 소식
- 중요한 뉴스가 부족하면 요청 개수보다 적게 골라도 됩니다

[작성 형식] — 최종 결과물은 아래처럼 한 줄로 렌더링됩니다.
    · 삼성E&A: 사우디 비료 EPC 35억달러 수주 — 약 4.7조원, 올해 해외 수주 최대
      └tag┘  └──── headline ────┘   └──── detail ────┘

- tag : 기업 뉴스면 종목명을 그대로 씁니다(한수원, 삼성E&A, 두산에너빌리티).
        기업이 주어가 아니면 테마를 씁니다(정책, 규제, 전력망, 기자재, 대미투자).
- headline : 명사형으로 끝냅니다. "수주", "체결", "시행", "정지", "착수".
        "~했다", "~입니다", "~할 전망" 같은 서술형·존댓말은 쓰지 마십시오.
        tag에 이미 나온 회사명을 headline에서 반복하지 마십시오.
- detail : headline에 없는 정보만 더합니다. 금액·일정·규모·파급 중 하나면 충분합니다.
        headline을 바꿔 말하기만 하는 detail은 빈 문자열로 두십시오.

[사실 원칙]
- 후보 목록에 주어진 제목·요약에 있는 사실만 씁니다. 추측하거나 지어내지 마십시오.
- 숫자는 원문 그대로 옮깁니다. 외화와 원화가 둘 다 있으면 "35억달러(약 4.7조원)"처럼
  병기하고, 하나만 있으면 임의로 환산하지 마십시오.
- 정보가 제목뿐이라 불충분하면 detail을 비우십시오. 채우려고 지어내면 안 됩니다.

[블록 배정]
- 원전        : 국내 원전·원자력 산업, 정책, 기업, 기술
- 건설·플랜트 : 국내 건설사 수주, 정비사업, 플랜트, 건설 시장
- 해외        : 해외에서 벌어진 일 (국내 기업의 해외 수주는 '건설·플랜트' 또는 '원전')
- 리스크      : 사고, 공사 중단, 부실, 소송, 제재, 인허가 지연
"""


def curate(shortlist: list[Cluster], cfg: dict, target_date: str) -> Curation | None:
    import anthropic

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("  ! ANTHROPIC_API_KEY 없음 — AI 선별을 건너뜁니다.", file=sys.stderr)
        return None

    lines = []
    for i, c in enumerate(shortlist, 1):
        a = c.lead
        outlets = ", ".join(c.outlets[:4])
        lines.append(
            f"[{i}] ({'국내' if a.lang == 'ko' else '해외'}) {a.title}\n"
            f"    매체: {outlets}{' 외' if len(c.outlets) > 4 else ''} "
            f"({len(c.outlets)}곳 보도) | {a.published:%m/%d %H:%M}\n"
            f"    요약: {a.snippet[:180] or '(없음)'}"
        )
    candidates = "\n".join(lines)

    user = (
        f"{target_date} 자 원전·건설 뉴스 후보 {len(shortlist)}건입니다.\n"
        f"이 중 가장 중요한 {cfg['final_size']}건 이내를 골라 주십시오.\n\n"
        f"{candidates}"
    )

    print(f"[4/5] Claude 중요도 판단 ({cfg['model']})")
    client = anthropic.Anthropic()
    try:
        response = client.messages.parse(
            model=cfg["model"],
            max_tokens=16000,
            system=SYSTEM,
            messages=[{"role": "user", "content": user}],
            output_format=Curation,
        )
    except anthropic.APIError as e:
        print(f"  ! Claude 호출 실패: {e}", file=sys.stderr)
        return None

    result = response.parsed_output
    # 모델이 범위 밖 번호를 주는 경우를 대비해 걸러냅니다.
    result.picks = [p for p in result.picks if 1 <= p.id <= len(shortlist)]
    u = response.usage
    print(f"      선별 {len(result.picks)}건 | 토큰 in {u.input_tokens} / out {u.output_tokens}")
    return result


# ─────────────────────────────────────────────────────────────
# 5) 텔레그램 전송
# ─────────────────────────────────────────────────────────────
def esc(s: str) -> str:
    """텔레그램 HTML 모드용 본문 이스케이프."""
    return html.escape(s or "", quote=False)


def esc_attr(s: str) -> str:
    """href 속성용. 따옴표까지 이스케이프해야 태그가 깨지지 않습니다."""
    return html.escape(s or "", quote=True)


BLOCK_ORDER = ["원전", "건설·플랜트", "해외", "리스크"]


def source_link(c: Cluster) -> str:
    """'원문보기 · 매체명 외 N곳' 형태의 하이퍼링크 한 줄.

    긴 Google News URL을 그대로 노출하지 않으면서, 어느 매체 기사인지와
    몇 곳이 받아썼는지를 같이 보여줍니다.
    """
    label = f"원문보기 · {c.outlets[0]}"
    if len(c.outlets) > 1:
        label += f" 외 {len(c.outlets) - 1}곳"
    return f'<a href="{esc_attr(c.lead.url)}">{esc(label)}</a>'


def render(curation: Curation | None, shortlist: list[Cluster], title: str, subtitle: str) -> list[str]:
    """텔레그램 HTML 메시지 본문을 만듭니다."""
    out = [f"<b>■ {esc(title)}</b>", f"<i>{esc(subtitle)}</i>"]

    if curation and curation.picks:
        for block in BLOCK_ORDER:
            group = [p for p in curation.picks if p.block == block]
            if not group:
                continue
            out.append(f"\n<b>──── {esc(block)} ────</b>")
            for p in group:
                line = f"· <b>{esc(p.tag)}</b>: {esc(p.headline)}"
                if p.detail.strip():
                    line += f" — {esc(p.detail)}"
                out.append(f"{line}\n  {source_link(shortlist[p.id - 1])}")
    else:
        # AI 선별이 실패해도 빈손으로 보내지 않습니다.
        out.append("\n<i>(AI 선별 미실행 — 스코어 상위 기사)</i>")
        for c in shortlist[:10]:
            out.append(f"· {esc(c.lead.title)}\n  {source_link(c)}")

    return split_message("\n".join(out))


def split_message(text: str, limit: int = 3900) -> list[str]:
    """텔레그램 4096자 제한에 맞춰 줄 단위로 자릅니다."""
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


def send_telegram(chunks: list[str]) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("  ! TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 없음 — 전송 생략", file=sys.stderr)
        return

    print(f"[5/5] 텔레그램 전송 ({len(chunks)}개 메시지)")
    for i, chunk in enumerate(chunks, 1):
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=HTTP_TIMEOUT,
        )
        if not r.ok:
            print(f"  ! 전송 실패 ({i}/{len(chunks)}): {r.status_code} {r.text[:200]}", file=sys.stderr)
            r.raise_for_status()
    print("      완료")


# ─────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="원전·건설 데일리 뉴스 클리핑")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--dry-run", action="store_true", help="텔레그램 전송 없이 콘솔 출력")
    ap.add_argument("--no-ai", action="store_true", help="Claude 선별 없이 스코어링 결과만")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))

    # 실행 시각 기준으로 시간창을 계산합니다 (KST 고정).
    tz = timezone(timedelta(hours=9))
    now = datetime.now(tz)
    start = now - timedelta(hours=cfg["window_hours"])
    # Google News의 when:Nd 는 일 단위라 넉넉히 잡고, 정확한 필터는 아래에서 합니다.
    window_days = max(1, (cfg["window_hours"] + 23) // 24)

    articles = collect(cfg, tz, window_days)
    articles = within_window(articles, start, now)
    if not articles:
        print("수집된 기사가 없습니다.", file=sys.stderr)
        return 1

    clusters = cluster_articles(articles, cfg["dedup_threshold"])
    scored = score_clusters(clusters, cfg, now)
    shortlist = balance(
        scored,
        cfg["shortlist_size"],
        cfg["shortlist_dedup_threshold"],
        cfg["shortlist_word_overlap"],
    )

    curation = None if args.no_ai else curate(shortlist, cfg, f"{start:%Y-%m-%d}")

    weekday = "월화수목금토일"[now.weekday()]
    picked_n = len(curation.picks) if curation else min(10, len(shortlist))
    subtitle = f"{now:%Y.%m.%d} ({weekday}) | 후보 {len(shortlist)}건 → {picked_n}건"
    chunks = render(curation, shortlist, cfg["title"], subtitle)

    if args.dry_run:
        print("\n" + "=" * 60)
        print(html.unescape(re.sub(r"<[^>]+>", "", "\n\n".join(chunks))))
        print("=" * 60)
        print("\n--- 숏리스트 전체 ---")
        for i, c in enumerate(shortlist, 1):
            print(f"{i:2d}. [{c.score:5.1f}] ({len(c.outlets)}곳) {c.lead.title[:70]}")
    else:
        send_telegram(chunks)

    return 0


if __name__ == "__main__":
    sys.exit(main())
