"""태깅 + 이슈 묶기.

① 태그 없는 기사에 태그 부여
② 최근 24시간 수집분에서 오늘의 이슈 추출

배포 zip 루트:
    lambda_function.py · tagging.md · daily_digest.md · sources.json

의존성: pymysql (Layer)
환경변수: DB_HOST · DB_USER · DB_PASSWORD · DB_NAME · MODEL_ID · BEDROCK_REGION
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone

import boto3
import pymysql
from botocore.exceptions import ClientError

KST = timezone(timedelta(hours=9))

HERE = os.path.dirname(__file__)
SOURCES_PATH = os.path.join(HERE, "sources.json")
TAGGING_PROMPT = os.path.join(HERE, "tagging.md")
DIGEST_PROMPT = os.path.join(HERE, "daily_digest.md")

REGION = os.environ.get("BEDROCK_REGION", "us-east-1")
# 리전 간 추론 프로파일 ID
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-6")

# 분류에는 발췌 앞부분
EXCERPT_LEN = 200

# 한 번에 보낼 기사 수 상한
BATCH_LIMIT = 300

# 한 번의 Bedrock 호출에 담을 기사 수
CHUNK_SIZE = 50

# 하루에 뽑을 이슈 수
MAX_DIGESTS = 5

FALLBACK_TAG = "기타"


def connect_db():
    return pymysql.connect(
        host=os.environ["DB_HOST"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"],
        charset="utf8mb4",
        connect_timeout=15,
        init_command="SET time_zone = '+09:00'",
    )


def load_tags() -> list[str]:
    with open(SOURCES_PATH, encoding="utf-8") as f:
        return json.load(f)["tags"]


def load_prompt(path: str, **fields) -> str:
    with open(path, encoding="utf-8") as f:
        template = f.read()
    for key, value in fields.items():
        template = template.replace("{" + key + "}", str(value))
    return template


def load_untagged(db) -> list[tuple]:
    sql = """
        SELECT id, title, source, description
          FROM articles
         WHERE tag IS NULL
         ORDER BY published_at DESC
         LIMIT %s
    """
    with db.cursor() as cursor:
        cursor.execute(sql, (BATCH_LIMIT,))
        return cursor.fetchall()


def build_message(rows: list[tuple]) -> str:
    lines = [f"기사 {len(rows)}건", ""]
    for i, (_, title, source, description) in enumerate(rows):
        lines.append(f"{i}. [{source}] {title}")
        if description:
            lines.append(f"   {description[:EXCERPT_LEN]}")
    return "\n".join(lines)


def parse_response(text: str):
    body = re.sub(r"^\s*```[a-zA-Z]*\s*", "", text)
    body = re.sub(r"\s*```\s*$", "", body).strip()

    try:
        return json.loads(body)
    except json.JSONDecodeError:
        pass

    # 앞뒤에 설명이 붙은 경우 — 배열이든 객체든 바깥쪽을 잘라냄
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = body.find(opener), body.rfind(closer)
        if 0 <= start < end:
            try:
                return json.loads(body[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"JSON 을 꺼내지 못함 ({len(text)}자)")


def to_index_map(value) -> dict[int, str]:
    if isinstance(value, dict) and "tags" in value:
        value = value["tags"]

    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            try:
                out[int(str(k).strip())] = "" if v is None else str(v).strip()
            except ValueError:
                continue
        return out

    if isinstance(value, list):
        # 배열로 오면 순서를 번호로
        out = {}
        for i, item in enumerate(value):
            if isinstance(item, dict):
                inner = item.get("tags") or item.get("tag")
                item = inner[0] if isinstance(inner, list) and inner else inner
            out[i] = "" if item is None else str(item).strip()
        return out

    raise ValueError(f"객체도 배열도 아님: {type(value).__name__}")


def call_bedrock(bedrock, system_prompt: str, message: str) -> str:
    response = bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": [{"text": message}]}],
        inferenceConfig={"maxTokens": 4000, "temperature": 0},
    )
    return response["output"]["message"]["content"][0]["text"]


def normalize(raw: str, count: int, allowed: set[str]) -> tuple[list[str], int]:
    """→ (태그 목록, 미수신 개수)"""
    mapping = to_index_map(parse_response(raw))
    tags = [
        mapping[i] if mapping.get(i) in allowed else FALLBACK_TAG
        for i in range(count)
    ]
    missing = sum(1 for i in range(count) if mapping.get(i) not in allowed)
    return tags, missing


def tag_chunk(bedrock, system_prompt: str, rows: list[tuple],
              allowed: set[str]) -> list[str] | None:
    message = build_message(rows)
    for attempt in (1, 2):
        raw = ""
        try:
            raw = call_bedrock(bedrock, system_prompt, message)
            tags, missing = normalize(raw, len(rows), allowed)
            if missing:
                print(f"  번호 {missing}개를 못 받아 기타로 채움 ({len(rows)}건 중)")
            return tags
        except (ValueError, json.JSONDecodeError, KeyError, IndexError) as e:
            print(f"  실패 (시도 {attempt}): {type(e).__name__}: {e}")
            # 원인 파악용 — 응답 앞뒤만
            print(f"  응답 앞 200자: {raw[:200]!r}")
            print(f"  응답 뒤 100자: {raw[-100:]!r}")
        except ClientError as e:
            print(f"Bedrock 호출 실패: {e}")
            raise
    return None


def load_for_digest(db) -> list[tuple]:
    """수집일 기준 최근 24시간. 발행일이 아닌 이유는 배치 주기와 맞추기 위해,
    자정이 아닌 이유는 경계 누락 회피."""
    sql = """
        SELECT id, title, source, tag, description
          FROM articles
         WHERE collected_at >= NOW() - INTERVAL 1 DAY
         ORDER BY published_at DESC
    """
    with db.cursor() as cursor:
        cursor.execute(sql)
        return cursor.fetchall()


def build_digest_message(rows: list[tuple]) -> str:
    lines = [f"기사 {len(rows)}건", ""]
    for i, (_, title, source, tag, description) in enumerate(rows):
        lines.append(f"{i}. [{source}][{tag or '미분류'}] {title}")
        if description:
            lines.append(f"   {description[:EXCERPT_LEN]}")
    return "\n".join(lines)


def normalize_digests(raw: str, count: int, allowed: set[str]) -> list[dict]:
    """이슈 목록 검증 — 범위 밖 번호·중복 배정 제거"""
    data = parse_response(raw)
    items = data.get("digests") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("digests 가 목록이 아님")

    digests, used = [], set()
    for item in items:
        if not isinstance(item, dict):
            continue
        headline = str(item.get("headline") or "").strip()[:300]
        if not headline:
            continue

        # 기사 중복 배정 방지
        indexes = []
        for v in item.get("articles") or []:
            try:
                i = int(v)
            except (TypeError, ValueError):
                continue
            if 0 <= i < count and i not in used:
                indexes.append(i)
                used.add(i)

        tag = str(item.get("tag") or "").strip()
        digests.append({
            "headline": headline,
            "summary": str(item.get("summary") or "").strip(),
            "tag": tag if tag in allowed else FALLBACK_TAG,
            "articles": indexes,
        })

    if not digests:
        raise ValueError("쓸 만한 이슈가 없음")
    return digests[:MAX_DIGESTS]


def save_digests(db, date_str: str, rows: list[tuple], digests: list[dict]) -> int:
    """그날 이슈 전체 교체. digest_articles 는 FK CASCADE 로 정리됨"""
    with db.cursor() as cursor:
        cursor.execute("DELETE FROM digests WHERE digest_date = %s", (date_str,))
        for rank, d in enumerate(digests, start=1):
            cursor.execute(
                """INSERT INTO digests (digest_date, rank_no, headline, summary, tag)
                   VALUES (%s, %s, %s, %s, %s)""",
                (date_str, rank, d["headline"], d["summary"], d["tag"]),
            )
            digest_id = cursor.lastrowid
            if d["articles"]:
                cursor.executemany(
                    "INSERT IGNORE INTO digest_articles (digest_id, article_id) VALUES (%s, %s)",
                    [(digest_id, rows[i][0]) for i in d["articles"]],
                )
    db.commit()
    return len(digests)


def save_tags(db, rows: list[tuple], tags: list[str]) -> int:
    params = [(tags[i], rows[i][0]) for i in range(len(rows))]
    with db.cursor() as cursor:
        cursor.executemany("UPDATE articles SET tag = %s WHERE id = %s", params)
    db.commit()
    return len(params)


def run_tagging(db, bedrock, allowed: set[str]) -> dict:
    system_prompt = load_prompt(TAGGING_PROMPT, tags=", ".join(sorted(allowed)))
    rows = load_untagged(db)
    if not rows:
        print("태그 없는 기사가 없습니다.")
        return {"articles": 0, "tagged": 0, "failed": 0, "distribution": {}}

    print(f"태그 없는 기사 {len(rows)}건 · {CHUNK_SIZE}건씩 나눠 호출")
    done_rows, done_tags, failed = [], [], 0
    for start in range(0, len(rows), CHUNK_SIZE):
        chunk = rows[start : start + CHUNK_SIZE]
        print(f"[{start}-{start + len(chunk) - 1}] {len(chunk)}건")
        tags = tag_chunk(bedrock, system_prompt, chunk, allowed)
        if tags is None:
            failed += len(chunk)
            continue
        done_rows.extend(chunk)
        done_tags.extend(tags)

    tagged = save_tags(db, done_rows, done_tags) if done_rows else 0
    distribution = {}
    for tag in done_tags:
        distribution[tag] = distribution.get(tag, 0) + 1
    print(f"태그 {tagged}건 반영 · 실패 {failed}건 — {distribution}")
    return {"articles": len(rows), "tagged": tagged,
            "failed": failed, "distribution": distribution}


def run_digests(db, bedrock, allowed: set[str], date_str: str) -> dict:
    system_prompt = load_prompt(DIGEST_PROMPT, count=MAX_DIGESTS)
    rows = load_for_digest(db)
    if not rows:
        print("오늘 수집된 기사가 없습니다.")
        return {"candidates": 0, "digests": 0}

    print(f"이슈 묶기 — 오늘 기사 {len(rows)}건")
    message = build_digest_message(rows)
    for attempt in (1, 2):
        raw = ""
        try:
            raw = call_bedrock(bedrock, system_prompt, message)
            digests = normalize_digests(raw, len(rows), allowed)
            saved = save_digests(db, date_str, rows, digests)
            for d in digests:
                print(f"  [{d['tag']}] {d['headline']} ({len(d['articles'])}건)")
            return {"candidates": len(rows), "digests": saved}
        except (ValueError, json.JSONDecodeError, KeyError, IndexError) as e:
            print(f"  실패 (시도 {attempt}): {type(e).__name__}: {e}")
            print(f"  응답 앞 200자: {raw[:200]!r}")
        except ClientError as e:
            print(f"Bedrock 호출 실패: {e}")
            raise

    print("이슈 묶기 실패. 태그는 이미 저장됐습니다.")
    return {"candidates": len(rows), "digests": 0, "error": "이슈 묶기 실패"}


def lambda_handler(event, context):
    now = datetime.now(KST)
    date_str = now.strftime("%Y-%m-%d")
    print(f"시작 — {now:%Y-%m-%d %H:%M:%S} KST")

    allowed = set(load_tags())
    bedrock = boto3.client("bedrock-runtime", region_name=REGION)

    db = connect_db()
    try:
        # 태깅 먼저 — 이슈 묶기의 입력
        tagging = run_tagging(db, bedrock, allowed)
        digests = run_digests(db, bedrock, allowed, date_str)
    finally:
        db.close()

    return {"date": date_str, "tagging": tagging, "digests": digests}
