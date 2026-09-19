"""태그가 없는 기사에 태그를 붙인다.

배포 zip 루트:
    lambda_function.py · tagging.md · sources.json

    zip -j summarizer.zip \
      lambdas/summarizer/lambda_function.py prompts/tagging.md config/sources.json

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
PROMPT_PATH = os.path.join(HERE, "tagging.md")

REGION = os.environ.get("BEDROCK_REGION", "us-east-1")
# 리전 간 추론 프로파일 ID
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-6")

# 분류에는 발췌 앞부분
EXCERPT_LEN = 200

# 한 번에 보낼 기사 수 상한
BATCH_LIMIT = 300

# 한 번의 Bedrock 호출에 담을 기사 수
CHUNK_SIZE = 50

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


def load_prompt(tags: list[str]) -> str:
    with open(PROMPT_PATH, encoding="utf-8") as f:
        template = f.read()
    return template.replace("{tags}", ", ".join(tags))


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
    """(태그 목록, 모델이 채우지 못한 개수) 를 돌려준다."""
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
            # 원인을 보려면 실제 응답이 필요하다. 앞뒤만 남긴다.
            print(f"  응답 앞 200자: {raw[:200]!r}")
            print(f"  응답 뒤 100자: {raw[-100:]!r}")
        except ClientError as e:
            print(f"Bedrock 호출 실패: {e}")
            raise
    return None


def save_tags(db, rows: list[tuple], tags: list[str]) -> int:
    params = [(tags[i], rows[i][0]) for i in range(len(rows))]
    with db.cursor() as cursor:
        cursor.executemany("UPDATE articles SET tag = %s WHERE id = %s", params)
    db.commit()
    return len(params)


def lambda_handler(event, context):
    now = datetime.now(KST)
    print(f"태깅 시작 — {now:%Y-%m-%d %H:%M:%S} KST")

    tags_allowed = load_tags()
    system_prompt = load_prompt(tags_allowed)

    db = connect_db()
    try:
        rows = load_untagged(db)
        if not rows:
            print("태그 없는 기사가 없습니다.")
            return {"date": now.strftime("%Y-%m-%d"), "articles": 0, "tagged": 0}

        print(f"태그 없는 기사 {len(rows)}건 · {CHUNK_SIZE}건씩 나눠 호출")
        bedrock = boto3.client("bedrock-runtime", region_name=REGION)
        allowed = set(tags_allowed)

        # 묶음별로 따로 호출
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
    finally:
        db.close()

    distribution = {}
    for tag in done_tags:
        distribution[tag] = distribution.get(tag, 0) + 1
    print(f"태그 {tagged}건 반영 · 실패 {failed}건 — {distribution}")

    return {
        "date": now.strftime("%Y-%m-%d"),
        "articles": len(rows),
        "tagged": tagged,
        "failed": failed,
        "distribution": distribution,
    }
