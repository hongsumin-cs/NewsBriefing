"""RSS 파싱 — 로컬·Lambda 공용. 출력 대상은 호출하는 쪽에서 결정.

의존성 없음 (표준 라이브러리만) — Lambda zip 단독 배포 조건.
Atom 미지원 — 현재 12개 피드가 전부 RSS 2.0. 필요 시 폴백 체인에 추가
(링크가 href 속성인 점 주의).
"""

from __future__ import annotations

import html
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree as ET

KST = timezone(timedelta(hours=9))

# 기본 User-Agent 를 막는 피드 있음
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

TIMEOUT = 25

# 저장 상한 — 초과분 절단
MAX_TITLE = 500
MAX_URL = 1000
MAX_SOURCE = 100

# 발췌 상한 — 한 피드가 AI 입력을 독점하는 것 방지.
# 상한 없으면 지디넷(1,600자대) 한 곳이 전체 텍스트의 45%.
MAX_DESCRIPTION = 600

CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
_WS_RE = re.compile(r"\s+")

# 문장 끝 — 뒤에 공백 필요. "1.5", "aitimes.com" 제외됨
_SENT_END_RE = re.compile(r"[.!?](?=\s|$)")

# 타임존 없는 피드용 (블로터·디지털투데이·AI타임스)
NAIVE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S")


# ── 텍스트 정리 ──────────────────────────────────────────────


def clean_text(raw: str) -> str:
    """HTML 태그·엔티티 제거 → 한 줄 평문. CDATA 안의 HTML 은 파서가 안 풀어줌"""
    if not raw:
        return ""
    text = _SCRIPT_RE.sub(" ", raw)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def truncate(text: str, limit: int) -> str:
    """문장 경계 절단 — AI 입력이라 중간에 끊기면 오해 소지.
    문장 끝이 60% 미만 위치면 그냥 자르고 말줄임표."""
    if len(text) <= limit:
        return text

    head = text[:limit]
    ends = [m.end() for m in _SENT_END_RE.finditer(head)]
    if ends and ends[-1] >= limit * 0.6:
        return head[: ends[-1]].rstrip()
    # 말줄임표 포함해 limit 이내
    return text[: limit - 1].rstrip() + "…"


def norm_title(title: str) -> str:
    """중복 판정 키 — 공백·기호 제거 후 소문자.
    같은 제목만 잡음. 표현이 다른 같은 사건은 summarizer 담당."""
    return re.sub(r"[^0-9a-z가-힣]", "", title.lower())


# ── 발행시각 ─────────────────────────────────────────────────


def parse_date(raw: str) -> datetime | None:
    """→ KST aware datetime.

    RFC822 (9개 피드) · 타임존 없음 (3개 피드) 두 형식.
    타임존 없으면 KST 간주 — UTC 로 읽으면 9시간 미래가 되어 날짜 필터에서 누락.
    """
    if not raw:
        return None
    raw = raw.strip()

    # RFC822 — +0900, GMT, +0000 모두 처리된다
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            return _to_kst(dt)
    except (TypeError, ValueError):
        pass

    # ISO 8601 (2026-09-15T04:57:48Z 포함)
    try:
        return _to_kst(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        pass

    for fmt in NAIVE_FORMATS:
        try:
            return _to_kst(datetime.strptime(raw, fmt))
        except ValueError:
            continue

    return None


def _to_kst(dt: datetime) -> datetime:
    """naive → KST 간주, aware → KST 변환"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=KST)
    return dt.astimezone(KST)


# ── 수집 ─────────────────────────────────────────────────────


def encode_url(url: str) -> str:
    """경로·쿼리 한글 퍼센트 인코딩 — 구글뉴스 키워드 피드용.
    미인코딩 시 urlopen 에서 UnicodeEncodeError."""
    p = urlsplit(url)
    encoded = f"{p.scheme}://{p.netloc}{quote(p.path)}"
    if p.query:
        encoded += "?" + quote(p.query, safe="=&?+")
    return encoded


def fetch(url: str, timeout: int = TIMEOUT) -> bytes:
    """→ bytes. 디코딩하지 않음 — ElementTree 가 XML 선언의 인코딩을 따라감"""
    request = urllib.request.Request(encode_url(url), headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


# ── 파싱 ─────────────────────────────────────────────────────


def _first_text(item: ET.Element, *tags: str) -> str:
    """폴백 체인 — 내용이 있는 첫 태그"""
    for tag in tags:
        el = item.find(tag)
        if el is not None and el.text and el.text.strip():
            return el.text.strip()
    return ""


def parse_item(item: ET.Element, source: dict) -> dict | None:
    """<item> → 기사 dict. 제목·링크 없으면 None"""
    title = clean_text(_first_text(item, "title"))
    url = _first_text(item, "link", "guid")
    if not title or not url:
        return None

    # 출처 — 구글뉴스는 <source> 제공, 나머지는 피드명이 곧 출처
    publisher = _first_text(item, "source") or source["name"]

    # 구글뉴스 제목의 " - 언론사" 꼬리표 제거 — <source> 와 일치할 때만
    suffix = f" - {publisher}"
    if title.endswith(suffix):
        title = title[: -len(suffix)].strip()

    description = ""
    if source.get("use_description", True):
        # 발췌만 사용. content:encoded(본문 전체)는 의도적으로 제외
        description = truncate(
            clean_text(_first_text(item, "description")), MAX_DESCRIPTION
        )

    categories = [
        clean_text(c.text) for c in item.findall("category") if c is not None and c.text
    ]

    return {
        "title": title[:MAX_TITLE],
        "url": url[:MAX_URL],
        "source": publisher[:MAX_SOURCE],
        "feed": source["name"][:MAX_SOURCE],
        "description": description,
        "published_at": parse_date(_first_text(item, "pubDate", "date")),
        "categories": categories,
        "tag": source.get("tag"),
    }


def parse_feed(raw: bytes, source: dict) -> list[dict]:
    """피드 하나 → 기사 목록. max_items 로 절단"""
    items = ET.fromstring(raw).findall(".//item")
    limit = source.get("max_items")

    articles = []
    for item in items:
        article = parse_item(item, source)
        if article is not None:
            articles.append(article)
        if limit and len(articles) >= limit:
            break
    return articles


def collect(sources: list[dict]) -> tuple[list[dict], list[dict]]:
    """활성 피드 전체 수집 → (기사 목록, 실패 목록).

    피드별 예외 격리 — 하나가 죽어도 나머지는 수집.
    제목 중복은 여기서, URL 중복은 DB UNIQUE 제약이 처리.
    """
    articles: list[dict] = []
    failures: list[dict] = []
    seen: set[str] = set()

    for source in sources:
        if not source.get("enabled", True):
            continue
        try:
            parsed = parse_feed(fetch(source["url"]), source)
        except Exception as e:  # 네트워크·XML·인코딩 전부
            failures.append({"name": source["name"], "error": f"{type(e).__name__}: {e}"})
            continue

        for article in parsed:
            key = norm_title(article["title"])
            if not key or key in seen:
                continue
            seen.add(key)
            articles.append(article)

    return articles, failures
