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
from datetime import datetime, timedelta, timezone

import boto3
import pymysql
from botocore.exceptions import ClientError

KST = timezone(timedelta(hours=9))

HERE = os.path.dirname(__file__)
SOURCES_PATH = os.path.join(HERE, "sources.json")
PROMPT_PATH = os.path.join(HERE, "tagging.md")

REGION = os.environ.get("BEDROCK_REGION", "us-east-1")
MODEL_ID = os.environ.get("MODEL_ID", "amazon.nova-lite-v1:0")

# 분류에는 발췌 앞부분이면 충분하다. 저장된 600자를 그대로 보내면 입력만 커진다.
EXCERPT_LEN = 200

# 한 번에 보낼 기사 수 상한. 남은 것은 다음 실행이 가져간다.
BATCH_LIMIT = 300

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
    # 프롬프트에 JSON 예시가 들어 있어 str.format 은 쓸 수 없다.
    return template.replace("{tags}", ", ".join(tags))


def load_untagged(db) -> list[tuple]:
    """아직 태그가 없는 기사를 가져온다.

    발행일이 아니라 태그 유무로 고른다. 배치는 아침에 도는데 피드에는 어젯밤
    기사가 그때 들어오므로, 날짜로 자르면 그 기사들이 매일 빠진다.
    상태를 기준으로 하면 몇 번을 돌려도 이미 태그가 있는 기사는 건너뛴다.
    """
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


def extract_json(text: str) -> str:
    """JSON 만 달라고 해도 앞뒤에 설명이 붙는 일이 있다."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("응답에서 JSON 을 찾을 수 없음")
    return text[start : end + 1]


def call_bedrock(bedrock, system_prompt: str, message: str) -> str:
    response = bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": [{"text": message}]}],
        # 분류는 매번 같은 답이 나오는 편이 낫다.
        inferenceConfig={"maxTokens": 4000, "temperature": 0},
    )
    return response["output"]["message"]["content"][0]["text"]


def normalize(raw: str, count: int, allowed: set[str]) -> list[str]:
    """응답을 기사 수와 같은 길이의 태그 목록으로 만든다.

    개수가 어긋나면 기사와 태그가 통째로 밀리므로, 모자라면 채우고 넘치면 자른다.
    """
    values = json.loads(extract_json(raw)).get("tags") or []
    if not isinstance(values, list):
        raise ValueError("tags 가 배열이 아님")

    tags = []
    for i in range(count):
        tag = str(values[i]).strip() if i < len(values) else ""
        tags.append(tag if tag in allowed else FALLBACK_TAG)
    return tags


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

        print(f"태그 없는 기사 {len(rows)}건")
        bedrock = boto3.client("bedrock-runtime", region_name=REGION)
        message = build_message(rows)

        # 파싱 실패 시 1회 재시도. 그래도 안 되면 태그 없이 끝낸다 —
        # 기사 목록은 이미 DB 에 있으므로 화면은 동작한다.
        tags = None
        for attempt in (1, 2):
            try:
                raw = call_bedrock(bedrock, system_prompt, message)
                tags = normalize(raw, len(rows), set(tags_allowed))
                break
            except (ValueError, json.JSONDecodeError, KeyError, IndexError) as e:
                print(f"응답 처리 실패 (시도 {attempt}): {type(e).__name__}: {e}")
            except ClientError as e:
                # AccessDeniedException 이면 Bedrock 모델 접근이 열려 있지 않다.
                print(f"Bedrock 호출 실패: {e}")
                raise

        if tags is None:
            print("재시도 후에도 실패. 태그 없이 종료합니다.")
            return {"date": now.strftime("%Y-%m-%d"), "articles": len(rows),
                    "tagged": 0, "error": "AI 응답 파싱 실패"}

        tagged = save_tags(db, rows, tags)
    finally:
        db.close()

    distribution = {}
    for tag in tags:
        distribution[tag] = distribution.get(tag, 0) + 1
    print(f"태그 {tagged}건 반영 — {distribution}")

    return {
        "date": now.strftime("%Y-%m-%d"),
        "articles": len(rows),
        "tagged": tagged,
        "distribution": distribution,
    }
