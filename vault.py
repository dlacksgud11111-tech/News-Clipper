"""옵시디언 볼트(GitHub 비공개 저장소 MyWiki)에 마크다운을 쓴다.

볼트는 노트북의 옵시디언이 Git 플러그인으로 당겨 가는 저장소다. 봇은 GitHub
Contents API 로 파일을 하나씩 만들거나 이어 붙인다 — git 을 깔 필요가 없고,
실행이 겹쳐도 sha 비교로 충돌을 잡는다.

필요한 환경변수 두 개가 없으면 아무것도 하지 않는다 (텔레그램 동작은 그대로).
  VAULT_REPO   예) dlacksgud11111-tech/MyWiki
  VAULT_TOKEN  그 저장소 하나에만 Contents 읽기/쓰기 권한을 준 fine-grained 토큰

news-bot 과 News-Clipper 에 같은 파일이 들어 있다. 한쪽을 고치면 다른 쪽도 맞출 것.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

log = logging.getLogger("vault")
API = "https://api.github.com"
KST = timezone(timedelta(hours=9))

# 볼트의 업무 섹터. 텔레그램 태그(#원전)와 모델이 고른 섹터가 이 중 하나로 모인다.
WORK_SECTORS = ["건설", "유틸리티", "원전", "반도체", "IT", "기타"]
# 개인 태그 → 볼트 폴더. AI 요약 없이 링크·메모만 쌓고, 밤에 Claude 가 정리한다.
PERSONAL = {"여행": "여행", "일정": "일정관리", "일정관리": "일정관리",
            "월급": "월급관리", "월급관리": "월급관리", "가계부": "월급관리"}
# 태그 별칭. 내가 손으로 칠 때 편한 말 → 볼트 섹터.
ALIAS = {"전력기기": "유틸리티", "전력": "유틸리티", "유틸": "유틸리티",
         "부동산": "건설", "반도체": "반도체", "it": "IT", "IT": "IT",
         "원자력": "원전", "smr": "원전", "SMR": "원전"}


def enabled() -> bool:
    return bool(os.environ.get("VAULT_REPO") and os.environ.get("VAULT_TOKEN"))


def now() -> datetime:
    return datetime.now(KST)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ['VAULT_TOKEN']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _url(path: str) -> str:
    return f"{API}/repos/{os.environ['VAULT_REPO']}/contents/{quote(path, safe='/')}"


def put(path: str, content: str, message: str, append: bool = False) -> bool:
    """파일을 만들거나(append=False 면 덮어씀) 끝에 이어 붙인다. 실패해도 예외를 던지지 않는다."""
    if not enabled():
        return False
    for attempt in range(4):
        try:
            sha, body = None, content
            r = requests.get(_url(path), headers=_headers(), timeout=20)
            if r.status_code == 200:
                sha = r.json()["sha"]
                if append:
                    old = base64.b64decode(r.json()["content"]).decode("utf-8")
                    body = old.rstrip("\n") + "\n" + content
            elif r.status_code != 404:
                log.warning("볼트 읽기 실패 %s: %s", r.status_code, r.text[:200])
                return False
            payload = {"message": message,
                       "content": base64.b64encode(body.encode("utf-8")).decode("ascii")}
            if sha:
                payload["sha"] = sha
            w = requests.put(_url(path), headers=_headers(), json=payload, timeout=30)
            if w.status_code in (200, 201):
                return True
            if w.status_code in (409, 422) and attempt < 3:
                # 다른 실행이 그사이 같은 파일을 고쳤다. sha 를 새로 받아 다시 시도.
                time.sleep(1 + attempt)
                continue
            log.warning("볼트 쓰기 실패 %s: %s", w.status_code, w.text[:200])
            return False
        except requests.RequestException as e:
            log.warning("볼트 접속 실패: %s", e)
            time.sleep(2)
    return False


# ---------------------------------------------------------------- 공용 도우미

_BAD = re.compile(r'[\\/:*?"<>|#^\[\]\n\r\t]')


def slug(title: str, limit: int = 50) -> str:
    s = _BAD.sub(" ", title or "").strip()
    s = re.sub(r"\s+", " ", s)
    return (s[:limit].rstrip() or "제목없음")


def norm_sector(word: str) -> str | None:
    w = (word or "").strip().lstrip("#")
    if w in WORK_SECTORS:
        return w
    return ALIAS.get(w) or ALIAS.get(w.lower())


def source_type(url: str) -> str:
    u = (url or "").lower()
    if "t.me/" in u:
        return "텔레그램"
    if "blog.naver" in u or "tistory" in u or "brunch.co.kr" in u or "blog" in u:
        return "블로그"
    if "youtube.com" in u or "youtu.be" in u:
        return "유튜브"
    return "뉴스"


def queue_line(kind: str, path: str, note: str = "") -> bool:
    """밤에 Claude 가 연결 작업을 할 목록에 한 줄 추가한다."""
    t = now()
    link = path.removesuffix(".md")
    line = f"- [ ] {t:%Y-%m-%d %H:%M} · {kind} · [[{link}]]" + (f" — {note}" if note else "")
    return put("00_Inbox/_대기열.md", line, f"queue: {kind}", append=True)
