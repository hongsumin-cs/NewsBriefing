"""키워드 종합 요약 — 온디맨드.

Express 가 기사 배열을 POST 하면 요약만 돌려준다. DB 에 붙지 않는다 —
캐시 확인·기간 검색·캐시 저장은 Express 담당.

배포 zip 루트:
    lambda_function.py · keyword_summary.md

의존성 없음 (boto3 는 Lambda 런타임 내장) → Layer 불필요
환경변수: MODEL_ID · BEDROCK_REGION
"""

from __future__ import annotations

import json
import os
import re

import boto3
from botocore.exceptions import ClientError

HERE = os.path.dirname(__file__)
PROMPT_PATH = os.path.join(HERE, "keyword_summary.md")

REGION = os.environ.get("BEDROCK_REGION", "us-east-1")
# 리전 간 추론 프로파일 ID
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-6")

MAX_ARTICLES = 300
MAX_KEYWORD = 50
MAX_HIGHLIGHTS = 5
EXCERPT_LEN = 200


def parse_event(event) -> dict:
    if isinstance(event, dict) and "body" in event:
        body = event["body"]
        return json.loads(body) if isinstance(body, str) else (body or {})
    return event or {}


def load_prompt() -> str:
    with open(PROMPT_PATH, encoding="utf-8") as f:
        return f.read()


def build_message(keyword: str, days: int, articles: list[dict]) -> str:
    lines = [f"키워드: {keyword}", f"기간: 최근 {days}일 · 기사 {len(articles)}건", ""]
    for i, a in enumerate(articles):
        date = str(a.get("published_at") or "")[:10]
        lines.append(f"{i}. [{a.get('source') or '?'}] {date} {a.get('title') or ''}")
        excerpt = a.get("description")
        if excerpt:
            lines.append(f"   {excerpt[:EXCERPT_LEN]}")
    return "\n".join(lines)


def parse_response(text: str):
    body = re.sub(r"^\s*```[a-zA-Z]*\s*", "", text)
    body = re.sub(r"\s*```\s*$", "", body).strip()

    try:
        return json.loads(body)
    except json.JSONDecodeError:
        pass

    start, end = body.find("{"), body.rfind("}")
    if 0 <= start < end:
        return json.loads(body[start : end + 1])
    raise ValueError(f"JSON 을 꺼내지 못함 ({len(text)}자)")


def call_bedrock(bedrock, system_prompt: str, message: str) -> str:
    response = bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": system_prompt}],
        # 키워드는 사용자 입력 — system 이 아니라 user 에만 넣는다
        messages=[{"role": "user", "content": [{"text": message}]}],
        inferenceConfig={"maxTokens": 2000, "temperature": 0.3},
    )
    return response["output"]["message"]["content"][0]["text"]


def normalize(raw: str, count: int) -> dict:
    """범위 밖 번호 제거 · 중복 제거"""
    data = parse_response(raw)
    if not isinstance(data, dict):
        raise ValueError("객체가 아님")

    summary = str(data.get("summary") or "").strip()
    if not summary:
        raise ValueError("summary 가 비어 있음")

    raw_highlights = data.get("highlights")
    highlights = [
        str(h).strip()
        for h in (raw_highlights if isinstance(raw_highlights, list) else [])
        if str(h).strip()
    ][:MAX_HIGHLIGHTS]

    used = []
    for v in data.get("used") or []:
        try:
            i = int(v)
        except (TypeError, ValueError):
            continue
        if 0 <= i < count and i not in used:
            used.append(i)

    return {"summary": summary, "highlights": highlights, "used": used}


def lambda_handler(event, context):
    try:
        payload = parse_event(event)
    except json.JSONDecodeError as e:
        print(f"요청 파싱 실패: {e}")
        return {"statusCode": 400, "body": "Invalid JSON"}

    keyword = str(payload.get("keyword") or "").strip()[:MAX_KEYWORD]
    days = int(payload.get("days") or 30)
    articles = (payload.get("articles") or [])[:MAX_ARTICLES]

    if not keyword or not articles:
        print("keyword 또는 articles 누락")
        return {"statusCode": 400, "body": "No keyword or articles"}

    print(f"요약 요청 — '{keyword}' 최근 {days}일 · 기사 {len(articles)}건")
    bedrock = boto3.client("bedrock-runtime", region_name=REGION)
    system_prompt = load_prompt()
    message = build_message(keyword, days, articles)

    for attempt in (1, 2):
        raw = ""
        try:
            raw = call_bedrock(bedrock, system_prompt, message)
            result = normalize(raw, len(articles))
            print(f"요약 완료 — 근거 {len(result['used'])}건")
            return result
        except (ValueError, json.JSONDecodeError, KeyError, IndexError) as e:
            print(f"응답 처리 실패 (시도 {attempt}): {type(e).__name__}: {e}")
            print(f"  응답 앞 200자: {raw[:200]!r}")
        except ClientError as e:
            print(f"Bedrock 호출 실패: {e}")
            raise

    # 빈 요약을 돌려주고 Express 가 처리
    print("재시도 후에도 실패")
    return {"summary": "", "highlights": [], "used": [], "error": "요약 생성 실패"}
