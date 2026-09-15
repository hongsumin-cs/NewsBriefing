"""RSS 피드 파싱 — 수집 경로 전체가 공유하는 단 하나의 파서.

로컬 실행과 Lambda 가 같은 함수를 쓴다. 출력 대상(파일 / RDS)만 호출하는
쪽에서 정하고, 파싱 로직은 여기 한 곳에만 둔다.

의존성 없음 — 표준 라이브러리만 사용한다. Lambda 에 zip 하나로 올라가야 하므로
이 제약은 유지할 것.

2026-09-15 기준 config/sources.json 의 12개 피드는 전부 RSS 2.0 이다.
Atom 은 지원하지 않는다 — 검증할 피드가 없어 죽은 코드가 되기 때문. 필요해지면
아래 폴백 체인에 항목을 추가하는 것으로 대응한다 (링크가 href 속성인 점에 주의).
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

# 일부 피드는 기본 User-Agent 를 막는다.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

TIMEOUT = 25

# 저장 상한. 초과분은 잘라낸다.
MAX_TITLE = 500
MAX_URL = 1000
MAX_SOURCE = 100

# description 상한. 지디넷은 발췌에 본문을 거의 다 넣어 보내서(1,600자대),
# 상한이 없으면 이 한 피드가 전체 텍스트의 45%를 차지한다. 그러면 AI 가
# 이슈를 고를 때 기사의 중요도가 아니라 피드의 후함에 끌려간다.
# 600자면 나머지 피드(250~300자)는 그대로고 지디넷만 잘린다.
MAX_DESCRIPTION = 600

CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
_WS_RE = re.compile(r"\s+")

# 문장 끝. 뒤에 공백이나 끝이 와야 하므로 "1.5" 나 "aitimes.com" 은 걸리지 않는다.
_SENT_END_RE = re.compile(r"[.!?](?=\s|$)")

# 타임존이 없는 피드용. 블로터·디지털투데이·AI타임스가 이 형식으로 준다.
NAIVE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S")


# ── 텍스트 정리 ──────────────────────────────────────────────


def clean_text(raw: str) -> str:
    """HTML 태그를 걷어내고 엔티티를 풀어 한 줄 평문으로 만든다.

    description 에는 <p>, <img>, &nbsp; 가 섞여 들어온다. XML 파서가 CDATA 는
    풀어주지만 그 안의 HTML 은 그대로 남는다.
    """
    if not raw:
        return ""
    text = _SCRIPT_RE.sub(" ", raw)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def truncate(text: str, limit: int) -> str:
    """문장 경계에서 자른다.

    그냥 잘라내면 "...고객 유치 흐름을 다시" 처럼 문장 중간에서 끊긴다.
    AI 입력으로 들어가는 값이라 잘린 조각이 사실을 오해하게 만들 수 있다.

    상한 안의 마지막 문장 끝을 찾되, 너무 앞이면(60% 미만) 내용이 과하게
    날아가므로 그냥 자르고 말줄임표를 붙인다.
    """
    if len(text) <= limit:
        return text

    head = text[:limit]
    ends = [m.end() for m in _SENT_END_RE.finditer(head)]
    if ends and ends[-1] >= limit * 0.6:
        return head[: ends[-1]].rstrip()
    return head.rstrip() + "…"


def norm_title(title: str) -> str:
    """중복 판정용 키. 공백과 기호를 없앤 소문자.

    같은 기사가 여러 피드로 들어오는 경우를 잡는다. 표현이 다른 같은 사건은
    못 잡는다 — 그건 summarizer 의 이슈 묶기가 담당한다.
    """
    return re.sub(r"[^0-9a-z가-힣]", "", title.lower())


# ── 발행시각 ─────────────────────────────────────────────────


def parse_date(raw: str) -> datetime | None:
    """발행시각을 KST aware datetime 으로 정규화한다.

    확인된 형식 2종:
      RFC822   Tue, 15 Sep 2026 15:00:00 +0900   (9개 피드)
      타임존 없음  2026-09-15 15:28:56            (3개 피드)

    타임존이 없으면 KST 로 간주한다. UTC 로 읽으면 9시간 미래가 되어
    '오늘 기사' 필터에서 통째로 빠진다.
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
    """naive 면 KST 로 간주하고, aware 면 KST 로 변환한다."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=KST)
    return dt.astimezone(KST)


# ── 수집 ─────────────────────────────────────────────────────


def encode_url(url: str) -> str:
    """경로·쿼리의 한글을 퍼센트 인코딩한다.

    구글뉴스 키워드 피드는 쿼리에 한글이 들어간다. 그대로 urlopen 하면
    UnicodeEncodeError: 'ascii' codec 으로 죽는다.
    """
    p = urlsplit(url)
    encoded = f"{p.scheme}://{p.netloc}{quote(p.path)}"
    if p.query:
        encoded += "?" + quote(p.query, safe="=&?+")
    return encoded


def fetch(url: str, timeout: int = TIMEOUT) -> bytes:
    """피드를 bytes 로 받아온다.

    문자열로 디코딩하지 않는 것이 중요하다. ElementTree 가 XML 선언의
    인코딩을 보고 스스로 처리하므로, EUC-KR 피드도 그대로 넘기면 된다.
    """
    request = urllib.request.Request(encode_url(url), headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


# ── 파싱 ─────────────────────────────────────────────────────


def _first_text(item: ET.Element, *tags: str) -> str:
    """폴백 체인 — 앞에서부터 찾아 내용이 있는 첫 태그를 쓴다."""
    for tag in tags:
        el = item.find(tag)
        if el is not None and el.text and el.text.strip():
            return el.text.strip()
    return ""


def parse_item(item: ET.Element, source: dict) -> dict | None:
    """<item> 하나를 기사 dict 로. 제목이나 링크가 없으면 None."""
    title = clean_text(_first_text(item, "title"))
    url = _first_text(item, "link", "guid")
    if not title or not url:
        return None

    # 출처: 구글뉴스는 <source> 로 언론사를 따로 준다. 제목을 쪼갤 필요가 없다.
    # 나머지 피드는 매체가 곧 출처이므로 피드 이름을 쓴다.
    publisher = _first_text(item, "source") or source["name"]

    # 구글뉴스 제목은 "기사 제목 - 한국경제" 형태다. <source> 와 같은 꼬리표를
    # 달고 있으므로 정확히 일치할 때만 떼어낸다.
    suffix = f" - {publisher}"
    if title.endswith(suffix):
        title = title[: -len(suffix)].strip()

    description = ""
    if source.get("use_description", True):
        # description(발췌)만 쓴다. content:encoded 는 기사 본문 전체라
        # 일부러 읽지 않는다 — 발행자가 RSS 로 주더라도 본문을 통째로 보관하는
        # 성격이 되기 때문.
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
    """피드 하나를 기사 목록으로. max_items 로 자른다."""
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
    """활성 피드를 전부 수집한다. (기사 목록, 실패 목록) 반환.

    피드별로 예외를 가둔다 — 하나가 죽어도 나머지는 수집된다. 아침 배치에서
    피드 하나 때문에 브리핑이 통째로 비는 것을 막는다.

    제목 기준 중복은 여기서 거른다. URL 중복은 DB 의 UNIQUE 제약이 잡는다.
    """
    articles: list[dict] = []
    failures: list[dict] = []
    seen: set[str] = set()

    for source in sources:
        if not source.get("enabled", True):
            continue
        try:
            parsed = parse_feed(fetch(source["url"]), source)
        except Exception as e:  # 네트워크·XML·인코딩 무엇이든
            failures.append({"name": source["name"], "error": f"{type(e).__name__}: {e}"})
            continue

        for article in parsed:
            key = norm_title(article["title"])
            if not key or key in seen:
                continue
            seen.add(key)
            articles.append(article)

    return articles, failures
