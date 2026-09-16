"""RSS 수집 → RDS 적재.

스케줄로 하루 한 번 돈다. 파싱은 parser.py 가 하고, 여기서는 적재만 한다.

배포 zip 루트에 세 파일이 함께 들어간다:
    lambda_function.py · parser.py · sources.json

    zip -j fetcher.zip \
      lambdas/fetcher/lambda_function.py collector/parser.py config/sources.json

의존성: pymysql (Layer)
환경변수: DB_HOST · DB_USER · DB_PASSWORD · DB_NAME
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

import pymysql

import parser

KST = timezone(timedelta(hours=9))

# 여기서 읽는 sources.json 은 배포 zip 에 동봉된 사본
SOURCES_PATH = os.path.join(os.path.dirname(__file__), "sources.json")

INSERT_SQL = """
    INSERT IGNORE INTO articles
        (url_hash, title, url, source, feed, description, tag, published_at)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
"""


def connect_db():
    return pymysql.connect(
        host=os.environ["DB_HOST"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.Cursor,
        connect_timeout=15,
        init_command="SET time_zone = '+09:00'",
    )


def load_sources():
    with open(SOURCES_PATH, encoding="utf-8") as f:
        return json.load(f)["sources"]


def count_articles(db) -> int:
    with db.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM articles")
        return cursor.fetchone()[0]


def to_row(article: dict) -> tuple:
    """기사 dict 를 INSERT 파라미터로. url_hash 를 여기서 만든다."""
    published = article["published_at"]
    return (
        hashlib.md5(article["url"].encode("utf-8")).hexdigest(),
        article["title"],
        article["url"],
        article["source"],
        article["feed"],
        article["description"] or None,
        article["tag"],
        published.replace(tzinfo=None) if published else None,
    )


def save(db, articles: list[dict]) -> int:
    if not articles:
        return 0
    with db.cursor() as cursor:
        cursor.executemany(INSERT_SQL, [to_row(a) for a in articles])
        inserted = cursor.rowcount
    db.commit()
    return inserted


def lambda_handler(event, context):
    now = datetime.now(KST)
    print(f"수집 시작 — {now:%Y-%m-%d %H:%M:%S} KST")

    sources = load_sources()
    articles, failures = parser.collect(sources)

    for f in failures:
        print(f"피드 실패 — {f['name']}: {f['error']}")
    print(f"수집 {len(articles)}건 · 실패 {len(failures)}개")

    db = connect_db()
    try:
        inserted = save(db, articles)
        total = count_articles(db)
    finally:
        db.close()

    print(f"신규 {inserted}건 적재 · 누적 {total}건")

    return {
        "date": now.strftime("%Y-%m-%d"),
        "fetched": len(articles),
        "inserted": inserted,
        "total": total,
        "failures": failures,
    }
