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
import time
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
    coverage: list[str] = field(default_factory=list)  # 제목에 등장한 커버리지 종목
    sector: str = ""  # 원전 / 건설 / 유틸리티 / 전력기기
    industry: bool = False  # 산업·정책 쿼리에서 나온 기사인가

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


def env(name: str) -> str:
    """환경변수를 읽고 앞뒤 공백·줄바꿈을 제거합니다.

    키를 메모장이나 GitHub Secrets에 붙여넣을 때 끝에 줄바꿈이 딸려오는 일이
    흔합니다. 그 상태로 HTTP 헤더를 만들면 헤더에 공백 문자를 넣을 수 없어
    요청을 만들다가 터지는데, SDK는 이를 APIConnectionError("Connection error")로
    감싸서 내보냅니다. 네트워크 장애처럼 보이지만 실제로는 키 모양 문제라
    원인을 찾기가 매우 어렵습니다. 여기서 미리 털어냅니다.
    """
    return (os.environ.get(name) or "").strip()


def strip_html(s: str) -> str:
    return html.unescape(_TAG_RE.sub(" ", s or "")).strip()


_OUTLET_TAIL_RE = re.compile(r"\s+[-–—]\s+[^-–—]{1,40}$")
_LEAD_BRACKET_RE = re.compile(r"^\s*[\[【(]([^\]】)]{1,20})[\]】)]\s*")

# 기사 가치를 담은 말머리는 남깁니다. 나머지(코너명·기사 유형)는 떼어냅니다.
KEEP_BRACKETS = ("단독", "속보", "긴급", "특징주")


def strip_lead_brackets(t: str) -> str:
    """제목 앞의 [해설] [기획취재] [증권言言] 같은 코너명을 떼어냅니다.

    [단독], [속보] 처럼 기사 가치를 알려주는 말머리는 남깁니다.
    말머리가 두 개 붙은 제목도 있어 반복해서 벗겁니다.
    """
    while True:
        m = _LEAD_BRACKET_RE.match(t)
        if not m:
            return t
        inner = m.group(1).strip()
        if any(k in inner for k in KEEP_BRACKETS):
            return t
        stripped = t[m.end():].strip()
        if not stripped:  # 제목이 통째로 대괄호뿐이면 그대로 둡니다
            return t
        t = stripped


def clean_title(title: str, outlet: str = "") -> str:
    """화면에 그대로 나갈 기사 제목을 다듬습니다.

    Google News는 제목 끝에 " - 매체명"을 붙입니다. <source>에서 받은 매체명을
    통째로 떼어내는 방식이 가장 확실합니다 — 정규식만 쓰면 매체명 자체에
    하이픈이 든 경우(g-enews.com)를 놓쳐서 제목에 그대로 남습니다.

    말줄임표도 함께 통일합니다. 매체마다 "...", "…", "… " 가 뒤섞여 있어
    붙은 것과 띄어진 것이 한 메시지에 같이 보이면 지저분합니다.
    """
    t = (title or "").strip()

    if outlet:
        for dash in ("-", "–", "—"):
            suffix = f" {dash} {outlet}"
            if t.endswith(suffix):
                t = t[: -len(suffix)].strip()
                break
        else:
            t = _OUTLET_TAIL_RE.sub("", t).strip()
    else:
        t = _OUTLET_TAIL_RE.sub("", t).strip()

    # "...", "···", "‥", "⋯" 이 매체마다 뒤섞여 들어옵니다. 가운뎃점은 한 개일 때
    # "수지삼성4·수영1" 처럼 구분자로 쓰이므로 두 개 이상일 때만 말줄임표로 봅니다.
    t = re.sub(r"[.·‥⋯]{2,}", "…", t)
    t = re.sub(r"…+", "…", t)
    t = re.sub(r"\s*…\s*", "…", t)  # 말줄임표 앞뒤 공백 제거
    t = strip_lead_brackets(t)
    return re.sub(r"\s{2,}", " ", t).strip()


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
        title=clean_title(title, outlet),
        url=entry.get("link", ""),
        outlet=outlet,
        published=published,
        lang=lang,
        group=group,
        snippet=strip_html(entry.get("summary", ""))[:300],
    )


def coverage_groups(cfg: dict) -> list[tuple[str, str, list[str]]]:
    """커버리지 설정을 (섹터, 언어, 종목명들) 목록으로 폅니다."""
    return [
        (g["sector"], g["lang"], list(g.get("names") or []))
        for g in (cfg.get("coverage") or [])
    ]


def coverage_sectors(cfg: dict) -> dict[str, str]:
    """종목명 → 섹터. 섹터 판정에서 가장 강한 신호입니다."""
    out: dict[str, str] = {}
    for sector, _, names in coverage_groups(cfg):
        for n in names:
            out.setdefault(n.lower(), sector)
    return out


def infer_sector(cluster: Cluster, sector_words: dict[str, list[str]],
                 cov_sector: dict[str, str]) -> str:
    """기사를 네 섹터 중 하나로 배정합니다.

    커버리지 종목명이 제목에 있으면 그 종목의 섹터를 강하게 밀어주고(5점),
    섹터 키워드는 보조 신호로 씁니다(1점). 둘 다 없으면 빈 문자열이고,
    그런 기사는 어느 발송에도 들어가지 않습니다.
    """
    titles = " ".join(a.title for a in cluster.articles).lower()
    blob = f"{titles} {' '.join(a.snippet for a in cluster.articles)}".lower()

    points = {s: 0.0 for s in sector_words}
    for name, sector in cov_sector.items():
        if name in titles and sector in points:
            points[sector] += 5
    for sector, words in sector_words.items():
        points[sector] += sum(1 for w in words if w.lower() in blob)

    best = max(points, key=lambda s: points[s])
    return best if points[best] > 0 else ""


def coverage_names(cfg: dict) -> list[str]:
    """중복 없이 전체 커버리지 종목명. 스코어링에 씁니다."""
    seen: list[str] = []
    for _, _, names in coverage_groups(cfg):
        for n in names:
            if n not in seen:
                seen.append(n)
    return seen


def coverage_queries(cfg: dict, window_days: int, batch: int = 6) -> list[tuple[str, str, str]]:
    """커버리지 종목명으로 검색 쿼리를 자동 생성합니다.

    이름을 하나씩 검색하면 소스가 60개를 넘어 느려지므로, OR로 묶어 batch개씩
    한 쿼리에 담습니다. Google News는 쿼리가 너무 길면 결과가 부실해져서
    6개 정도가 적당합니다.
    """
    jobs = []
    for sector, lang, names in coverage_groups(cfg):
        for i in range(0, len(names), batch):
            chunk = names[i : i + batch]
            terms = " OR ".join(f'"{n}"' if " " in n else n for n in chunk)
            query = f"({terms}) when:{window_days}d"
            label = f"커버리지-{sector}-{lang}-{i // batch + 1}"
            jobs.append((google_news_url(query, lang), lang, label))
    return jobs


def collect(cfg: dict, tz: timezone, window_days: int) -> list[Article]:
    """설정에 적힌 모든 소스를 병렬로 긁어옵니다."""
    jobs: list[tuple[str, str, str]] = []  # (url, lang, group)

    for q in cfg.get("queries") or []:
        query = q["q"].replace("{window}", f"when:{window_days}d")
        jobs.append((google_news_url(query, q["lang"]), q["lang"], q["name"]))

    jobs += coverage_queries(cfg, window_days)

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
    covered = [n.lower() for n in coverage_names(cfg)]
    coverage_weight = cfg.get("coverage_weight", 8)
    sector_words = {s: list(w) for s, w in (cfg.get("sectors") or {}).items()}
    cov_sector = coverage_sectors(cfg)
    industry_markers = list(cfg.get("industry_query_markers") or [])
    industry_weight = cfg.get("industry_weight", 6)

    scored: list[Cluster] = []
    dropped = 0
    with_coverage = 0
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

        # 커버리지 종목이 제목에 있으면 1순위. 본문이 아니라 제목만 봅니다 —
        # 본문에 스쳐 지나가듯 언급된 기사까지 끌어올리면 오히려 지저분해집니다.
        hits = [n for n in covered if n in titles]
        if hits:
            c.coverage = hits[:3]
            with_coverage += 1
            score += min(len(hits), 2) * coverage_weight

        # 여러 매체가 동시에 다뤘다 = 업계가 중요하게 본다
        score += (len(c.outlets) - 1) * bonus_per_outlet

        # 최신 기사 가산 (24시간에 걸쳐 0~4점)
        age_h = (end - c.lead.published).total_seconds() / 3600
        score += max(0.0, 4.0 - age_h / 6)

        # 어떤 쿼리가 이 기사를 찾았는지가 "산업·정책 기사"의 가장 정확한
        # 신호입니다. 제목에 종목명이 없다는 것만으로는 재건축 시공사 선정
        # 같은 개별 프로젝트 기사까지 산업 기사로 잡히기 때문입니다.
        c.industry = any(
            m in a.group for a in c.articles for m in industry_markers
        )
        # 정책·업황 기사는 커버리지 가산점(+8~16)을 받을 수 없어 구조적으로
        # 점수가 낮습니다. 그대로 두면 기업 뉴스에 항상 밀리므로 보정합니다.
        if c.industry:
            score += industry_weight

        c.score = score
        c.sector = infer_sector(c, sector_words, cov_sector)
        scored.append(c)

    scored.sort(key=lambda c: c.score, reverse=True)
    by_sector = {s: sum(1 for c in scored if c.sector == s) for s in sector_words}
    unassigned = sum(1 for c in scored if not c.sector)
    print(f"      차단어로 제외 {dropped}건 → 채점 대상 {len(scored)}건 (커버리지 {with_coverage}건)")
    print("      섹터 배정: " + " / ".join(f"{s} {n}" for s, n in by_sector.items())
          + f" / 미배정 {unassigned}")
    return scored


class Picker:
    """같은 사건을 두 번 뽑지 않도록 걸러주는 선별기.

    클러스터링은 제목이 꽤 비슷해야 묶이므로, 같은 사건을 크게 다르게 쓴
    기사들이 자리를 여러 개 차지할 수 있습니다. 이미 뽑은 것과 조금이라도
    겹치면(느슨한 임계값) 건너뛰어 주제 다양성을 확보합니다.
    """

    def __init__(self, redundancy: float, word_overlap: float):
        self.redundancy = redundancy
        self.word_overlap = word_overlap
        self.picked: list[Cluster] = []
        self._sigs: list[tuple[set[str], list[str]]] = []
        self._ids: set[int] = set()

    def take(self, c: Cluster) -> bool:
        if id(c) in self._ids:
            return False
        sig, toks = bigrams(c.lead.key), content_tokens(c.lead.title)
        for other_sig, other_toks in self._sigs:
            if same_story(sig, other_sig, self.redundancy):
                return False
            if topic_overlap(toks, other_toks) >= self.word_overlap:
                return False
        self.picked.append(c)
        self._sigs.append((sig, toks))
        self._ids.add(id(c))
        return True


def shortlist_for(sector: str, pool: int, scored: list[Cluster], cfg: dict) -> list[Cluster]:
    """한 섹터의 후보를 뽑습니다.

    두 가지를 여기서 보장합니다.

    1) 산업·정책 기사 몫  — 커버리지 종목명이 제목에 있으면 가산점이 커서,
       그냥 점수순으로 뽑으면 후보가 개별 기업 뉴스로만 채워집니다. 부동산
       대책·중대재해처벌법·전력수급기본계획처럼 종목명이 없지만 섹터 전체에
       영향을 주는 기사 자리를 먼저 떼어둡니다.
    2) 국내/해외 비율    — 해외 쿼리는 범위가 넓어 커버리지와 무관한 기사가
       많이 걸립니다. 반반으로 두면 저품질 외신이 자리를 절반이나 먹습니다.
    """
    picker = Picker(cfg["shortlist_dedup_threshold"], cfg["shortlist_word_overlap"])
    mine = [c for c in scored if c.sector == sector]

    # 1) 산업·정책 몫: 산업·정책 쿼리에서 나온 기사로 먼저 채웁니다.
    #    단 점수 문턱을 둡니다 — 정책 쿼리는 검색어가 넓어 공공기관 홍보나
    #    사진기사까지 끌고 오는데, 그런 걸 자리만 채우려고 넣으면 손해입니다.
    industry_quota = round(pool * cfg.get("shortlist_industry_ratio", 0))
    floor = cfg.get("industry_min_score", 0)
    if industry_quota:
        taken = 0
        for c in mine:
            if taken >= industry_quota:
                break
            if c.industry and c.score >= floor and picker.take(c):
                taken += 1

    # 2) 나머지는 점수순으로, 국내/해외 비율을 맞춰 채웁니다.
    ko_quota = round(pool * cfg["shortlist_ko_ratio"])
    for lang, quota in (("ko", ko_quota), ("en", pool - ko_quota)):
        taken = sum(1 for c in picker.picked if c.lead.lang == lang)
        for c in mine:
            if taken >= quota:
                break
            if c.lead.lang == lang and picker.take(c):
                taken += 1

    for c in mine:  # 한쪽이 모자라면 남은 자리를 채웁니다
        if len(picker.picked) >= pool:
            break
        picker.take(c)

    picker.picked.sort(key=lambda c: c.score, reverse=True)
    return picker.picked


# ─────────────────────────────────────────────────────────────
# 4) Claude 중요도 판단
# ─────────────────────────────────────────────────────────────
class Pick(BaseModel):
    id: int = Field(description="후보 목록에 붙은 번호")
    tags: list[str] = Field(
        description="해시태그 2~3개. 첫 번째는 주체(종목명 또는 정부부처), "
        "나머지는 사건의 핵심어. 각 태그는 공백·특수문자 없이 붙여 쓴 한 단어, 12자 이내. "
        "# 기호는 붙이지 마십시오."
    )
    title_ko: str = Field(
        description="영문 기사일 때만 채웁니다. 영문 제목을 한국어로 옮긴 것. "
        "요약하지 말고 제목에 있는 내용을 그대로 옮기십시오. "
        "국문 기사는 반드시 빈 문자열로 두십시오."
    )


class Curation(BaseModel):
    picks: list[Pick]


SYSTEM = """\
당신은 증권사 리서치 어시스턴트입니다. 지금 만드는 것은 **{sector}** 섹터의
데일리 뉴스 한 장입니다. 받는 사람은 이 섹터를 담당하는 애널리스트입니다.

전날 나온 {sector} 후보 목록에서, 오늘 아침 반드시 알아야 할 것만 골라냅니다.

[중요도 우선순위] — 위에서부터 우선합니다.
  1순위. 커버리지 종목의 실적·수주·공시에 직접 영향을 주는 뉴스
         (계약 체결, 수주 공시, 실적 발표, 수주잔고, 대규모 투자 결정)
  2순위. 섹터 전체의 수요·가격·정책이 바뀌는 뉴스
         (전기·가스 요금, 원전 정책과 인허가, 건설 규제, 변압기 업황, 연료비)
  3순위. 경쟁사·전방산업 동향 (해외 포함)
         (Westinghouse·EDF 수주, GE Vernova 실적, 미국 데이터센터 증설)
  제외.  주가 시황·특징주, 홍보성 기사, 단순 행사 소식

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

[작성 형식] — 최종 결과물은 아래처럼 렌더링됩니다. 한 항목은 딱 세 줄입니다.

    #삼성EA #사우디 #비료플랜트
    삼성E&A, 35억달러 규모 사우디 비료 프로젝트 수주     ← 기사 제목 (굵게)
    🔗 원문 보기

    #GEVernova #수주잔고 #가스터빈
    가스터빈·전력망 수주잔고가 GE Vernova 실적 견인      ← 영문 기사는 title_ko
    🔗 원문 보기

★ 국문 기사의 제목은 당신이 쓰지 않습니다. 후보 목록의 제목이 그대로 들어갑니다.
   당신이 만드는 것은 tags 와 title_ko(영문 기사만) 두 가지뿐입니다.
   요약문·부연 설명을 따로 쓰지 마십시오. 화면에 나갈 자리가 없습니다.

★ 제목이 곧 전부이므로, 같은 사건을 다룬 후보가 여럿이면 **제목만 읽고도 무슨
   일인지 가장 잘 알 수 있는 기사**를 고르십시오. 금액·수치·주체가 제목에
   들어 있는 기사가 유리합니다.

- tags : 2~3개. 첫 번째는 주체(종목명·정부부처), 나머지는 사건의 핵심어입니다.
        각 태그는 붙여 쓴 한 단어여야 합니다 — 공백·점·괄호·& 를 넣지 마십시오.
        "삼성E&A"는 "삼성EA", "LS전선"은 그대로, "AI 데이터센터"는 "AI데이터센터".
        # 기호는 붙이지 마십시오. 렌더링할 때 자동으로 붙습니다.
        이 메시지 전체가 이미 {sector} 섹터이므로 "{sector}"를 태그로 쓰지 마십시오.
        같은 종목이 여러 날 반복돼도 태그 표기는 항상 똑같이 써야 나중에 검색됩니다.
- title_ko : 후보가 (해외) 표시된 영문 기사일 때만 채웁니다. 영문 제목을 한국어로
        옮기되 **요약하지 말고** 제목에 담긴 내용을 그대로 옮기십시오. 기업명·수치·
        국가명은 살립니다. 예: "Gas Turbine And Grid Backlog Powers GE Vernova"
        → "가스터빈·전력망 수주잔고가 GE Vernova 실적 견인".
        (국내) 표시된 국문 기사는 **반드시 빈 문자열**로 두십시오. 국문 제목은
        원문 그대로 나가야 합니다.

[사실 원칙]
- 후보 목록에 주어진 제목·요약에 있는 사실만 씁니다. 추측하거나 지어내지 마십시오.
- 숫자는 원문 그대로 옮깁니다. 외화와 원화가 둘 다 있으면 "35억달러(약 4.7조원)"처럼
  병기하고, 하나만 있으면 임의로 환산하지 마십시오.
- 정보가 제목뿐이라 불충분하면 detail을 비우십시오. 채우려고 지어내면 안 됩니다.

[섹터 적합성]
후보 목록은 기계적으로 걸러낸 것이라 {sector}와 거리가 먼 기사가 섞여 있습니다.
{sector} 담당자가 볼 이유가 없는 기사는 점수가 높아도 빼십시오.
반대로 다른 섹터 담당자에게 더 어울리는 기사도 빼십시오 — 그쪽은 별도로 발송됩니다.
"""


def curate(shortlist: list[Cluster], cfg: dict, sector: str, size: int,
           target_date: str) -> Curation | None:
    import anthropic

    api_key = env("ANTHROPIC_API_KEY")
    if not api_key:
        print("  ! ANTHROPIC_API_KEY 없음 — AI 선별을 건너뜁니다.", file=sys.stderr)
        return None

    lines = []
    for i, c in enumerate(shortlist, 1):
        a = c.lead
        outlets = ", ".join(c.outlets[:4])
        # 제목에서 찾은 커버리지 종목을 붙여주면 1순위 판단이 훨씬 정확해집니다.
        mark = f"  ★커버리지: {', '.join(c.coverage)}" if c.coverage else ""
        lines.append(
            f"[{i}] ({'국내' if a.lang == 'ko' else '해외'}) {a.title}{mark}\n"
            f"    매체: {outlets}{' 외' if len(c.outlets) > 4 else ''} "
            f"({len(c.outlets)}곳 보도) | {a.published:%m/%d %H:%M}\n"
            f"    요약: {a.snippet[:180] or '(없음)'}"
        )
    candidates = "\n".join(lines)

    # 커버리지 목록을 시스템 프롬프트에 붙여 1순위 기준을 구체적으로 알려줍니다.
    # 담당 섹터를 먼저 보여주고, 다른 섹터는 참고용으로 뒤에 둡니다.
    groups = [(s, lg, n) for s, lg, n in coverage_groups(cfg) if n]
    mine = [g for g in groups if g[0] == sector]
    others = [g for g in groups if g[0] != sector]
    roster = "\n".join(
        f"  {s} ({'국내' if lg == 'ko' else '해외'}) : {', '.join(n)}" for s, lg, n in mine + others
    )
    system = SYSTEM.format(sector=sector) + (
        f"\n[커버리지 종목] — 1순위 판단의 기준입니다. 맨 위가 이번 담당 섹터입니다.\n{roster}\n\n"
        "후보 목록에서 ★커버리지 표시가 붙은 항목은 담당 종목이 제목에 등장한다는 뜻입니다.\n"
        "같은 값이면 표시가 붙은 쪽을 우선하되, 표시가 붙었다고 중요하지 않은 기사까지\n"
        "억지로 올리지는 마십시오. 표시가 없어도 섹터 전체를 흔드는 뉴스면 당연히 고릅니다.\n"
    )

    user = (
        f"{target_date} 자 {sector} 뉴스 후보 {len(shortlist)}건입니다.\n"
        f"이 중 가장 중요한 {size}건 이내를 골라 주십시오.\n\n"
        f"{candidates}"
    )

    print(f"  · {sector}: 후보 {len(shortlist)}건 → Claude 판단 중")
    client = anthropic.Anthropic(api_key=api_key)

    # 한 섹터만 조용히 실패해서 폴백으로 나가는 일이 있었습니다. 원인을 알 수
    # 있도록 예외 종류를 구분해 찍고, 일시적 실패는 한 번 더 시도합니다.
    response = None
    for attempt in (1, 2):
        try:
            response = client.messages.parse(
                model=cfg["model"],
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=Curation,
            )
            break
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            kind = type(e).__name__
            if attempt == 1:
                print(f"    {kind} — 10초 후 재시도", file=sys.stderr)
                time.sleep(10)
                continue
            print(f"  ! {sector} 실패: {kind} (재시도도 실패) — {e}", file=sys.stderr)
        except anthropic.APIStatusError as e:
            # 400/401/404 등은 다시 시도해도 같은 결과이므로 바로 포기합니다.
            print(f"  ! {sector} 실패: HTTP {e.status_code} {type(e).__name__} — {e}", file=sys.stderr)
        except Exception as e:
            # 스키마 검증 실패(응답이 잘렸을 때 등)도 여기로 들어옵니다.
            print(f"  ! {sector} 실패: {type(e).__name__} — {e}", file=sys.stderr)
        return None

    if response.stop_reason == "refusal":
        detail = getattr(response.stop_details, "category", None)
        print(f"  ! {sector} 실패: 모델이 응답을 거부했습니다 (category={detail})", file=sys.stderr)
        return None
    if response.stop_reason == "max_tokens":
        print(f"  ! {sector} 경고: 응답이 max_tokens 에서 잘렸습니다", file=sys.stderr)

    result = response.parsed_output
    # 모델이 범위 밖 번호를 주거나 같은 기사를 두 번 고르는 경우를 대비합니다.
    seen: set[int] = set()
    clean = []
    for p in result.picks:
        if 1 <= p.id <= len(shortlist) and p.id not in seen:
            seen.add(p.id)
            clean.append(p)
    result.picks = clean[:size]
    u = response.usage
    print(f"    선별 {len(result.picks)}건 | 토큰 in {u.input_tokens} / out {u.output_tokens}")
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


_TAG_STRIP_RE = re.compile(r"[^0-9A-Za-z가-힣]")


def hashtag(word: str) -> str:
    """해시태그로 쓸 수 있게 다듬습니다.

    텔레그램 해시태그는 공백·점·괄호가 들어가면 거기서 끊깁니다.
    "삼성E&A" → "삼성EA", "HD현대일렉트릭" → 그대로.
    숫자로만 이뤄진 태그는 텔레그램이 태그로 인식하지 않아 버립니다.
    """
    w = _TAG_STRIP_RE.sub("", word or "")
    return "" if not w or w.isdigit() else w[:20]


def source_link(c: Cluster) -> str:
    """'🔗 원문 보기' 하이퍼링크 한 줄.

    긴 Google News URL은 앵커 뒤에 숨고 화면에는 이 문구만 보입니다.
    매체명과 보도 매체 수는 화면에 싣지 않습니다 — 선별에는 쓰지만
    읽을 때는 군더더기라서요.
    """
    return f'<a href="{esc_attr(c.lead.url)}">🔗 원문 보기</a>'


def render(curation: Curation | None, shortlist: list[Cluster], title: str, subtitle: str,
           emoji: str = "") -> list[str]:
    """텔레그램 HTML 메시지 본문을 만듭니다."""
    head = f"{emoji} {title}".strip() if emoji else title
    out = [f"<b>{esc(head)}</b>", f"<i>{esc(subtitle)}</i>"]

    if curation and curation.picks:
        for p in curation.picks:
            c = shortlist[p.id - 1]
            # 해시태그는 맨 위에 두되 굵게 처리하지 않습니다. 파란 글씨라
            # 그냥 두면 눈에 먼저 들어와 정작 제목이 묻히기 때문입니다.
            # 굵은 것은 제목 하나뿐이어야 시선이 제목에 먼저 닿습니다.
            tags = " ".join(f"#{hashtag(t)}" for t in p.tags[:3] if hashtag(t))
            out.append(f"\n{esc(tags)}" if tags else "")
            # (태그가 없으면 위에서 빈 문자열이 들어가 빈 줄 하나로 구분됩니다)

            # 국문은 원문 제목 그대로, 영문은 한국어로 옮긴 제목을 씁니다.
            # 굵게 처리하지 않습니다 — 제목이 줄을 넘길 때 뒷부분이 따로
            # 떨어져 보이는 문제가 있어서, 본문 서식을 아예 비웠습니다.
            title = p.title_ko.strip() or c.lead.title
            out.append(esc(title))
            out.append(source_link(c))
    else:
        # AI 선별이 실패해도 빈손으로 보내지 않습니다.
        out.append("\n<i>(AI 선별 미실행 — 스코어 상위 기사)</i>")
        for c in shortlist[:8]:
            out.append(f"\n{esc(c.lead.title)}")
            out.append(source_link(c))

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


def send_telegram(chunks: list[str], chat_id_env: str = "") -> bool:
    """한 섹터의 메시지를 보냅니다.

    chat_id_env 로 섹터 전용 채널을 지정할 수 있고, 그 값이 없으면
    공용 TELEGRAM_CHAT_ID 로 보냅니다. 채널을 아직 안 만들었어도
    전부 한 곳으로 떨어지게 하기 위한 장치입니다.
    """
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env(chat_id_env) if chat_id_env else ""
    if not chat_id:
        chat_id = env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("  ! TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 없음 — 전송 생략", file=sys.stderr)
        return False

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
    return True


# ─────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="원전·건설 데일리 뉴스 클리핑")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--dry-run", action="store_true", help="텔레그램 전송 없이 콘솔 출력")
    ap.add_argument("--no-ai", action="store_true", help="Claude 선별 없이 스코어링 결과만")
    ap.add_argument("--sector", default="", help="한 섹터만 실행 (예: 원전). 테스트용")
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

    digests = [d for d in (cfg.get("digests") or []) if not args.sector or d["sector"] == args.sector]
    if not digests:
        print(f"보낼 섹터가 없습니다 (--sector {args.sector}).", file=sys.stderr)
        return 1

    weekday = "월화수목금토일"[now.weekday()]
    print(f"[4/5] 섹터별 선별 ({len(digests)}개)")

    sent = 0
    for d in digests:
        sector, size = d["sector"], d["size"]
        shortlist = shortlist_for(sector, d["pool"], scored, cfg)
        if not shortlist:
            print(f"  · {sector}: 후보 없음 — 건너뜁니다")
            continue

        curation = None if args.no_ai else curate(shortlist, cfg, sector, size, f"{start:%Y-%m-%d}")
        chunks = render(curation, shortlist, d["title"],
                        f"{now:%Y.%m.%d} ({weekday})", d.get("emoji", ""))

        if args.dry_run:
            print("\n" + "=" * 60)
            print(html.unescape(re.sub(r"<[^>]+>", "", "\n\n".join(chunks))))
            print("=" * 60)
            for i, c in enumerate(shortlist, 1):
                star = "★" if c.coverage else " "
                print(f"  {star}{i:2d}. [{c.score:5.1f}] ({len(c.outlets)}곳) {c.lead.title[:64]}")
        elif send_telegram(chunks, d.get("chat_id_env", "")):
            sent += 1

    if not args.dry_run:
        print(f"[5/5] 텔레그램 전송 완료 — {sent}/{len(digests)}개 섹터")
    return 0


if __name__ == "__main__":
    sys.exit(main())
