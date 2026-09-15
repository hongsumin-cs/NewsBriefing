-- 국내 IT 뉴스 브리핑 — 테이블 정의

CREATE DATABASE IF NOT EXISTS briefing_v2
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE briefing_v2;

-- ── 기사 ──────────────────────────────────────────────────────
-- 수집 데이터. 매일 쌓아 만드는 아카이브.
-- 저장 범위는 제목·출처·링크·발행시각 + RSS 가 스스로 배포하는 발췌까지다.
-- 기사 페이지를 크롤링하지 않고, content:encoded(본문 전체)도 읽지 않는다.
CREATE TABLE IF NOT EXISTS articles (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  url_hash      CHAR(32)      NOT NULL,       -- md5(url). 중복 수집 차단용
  title         VARCHAR(500)  NOT NULL,
  url           VARCHAR(1000) NOT NULL,
  source        VARCHAR(100)  NOT NULL,       -- 언론사. 구글뉴스는 <source> 에서
  feed          VARCHAR(100)  DEFAULT NULL,   -- 수집 피드 이름
  description   VARCHAR(600)  DEFAULT NULL,   -- RSS 발췌. 600자 상한(문장 경계)
  tag           VARCHAR(30)   DEFAULT NULL,   -- 배치에서 AI 가 부여
  published_at  DATETIME      DEFAULT NULL,   -- KST 로 정규화해 저장
  collected_at  DATETIME      DEFAULT CURRENT_TIMESTAMP,

  UNIQUE KEY uk_url_hash (url_hash),

  INDEX idx_pub (published_at),               -- 날짜별 조회·정렬
  INDEX idx_tag_pub (tag, published_at)       -- 의미 검색: WHERE tag=? ORDER BY ...
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS digests (
  id           BIGINT AUTO_INCREMENT PRIMARY KEY,
  digest_date  DATE          NOT NULL,
  rank_no      TINYINT       NOT NULL,        -- 1부터. 기사 수가 많은 순
  headline     VARCHAR(300)  NOT NULL,
  summary      TEXT,
  tag          VARCHAR(30)   DEFAULT NULL,
  created_at   DATETIME      DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_date_rank (digest_date, rank_no),
  INDEX idx_date (digest_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS digest_articles (
  digest_id  BIGINT NOT NULL,
  article_id BIGINT NOT NULL,
  PRIMARY KEY (digest_id, article_id),
  FOREIGN KEY (digest_id)  REFERENCES digests(id)  ON DELETE CASCADE,
  FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS summaries (
  id           BIGINT AUTO_INCREMENT PRIMARY KEY,
  keyword      VARCHAR(100) NOT NULL,         -- 정규화된 검색어
  days         SMALLINT     NOT NULL,         -- 조회 기간 (일)
  cache_date   DATE         NOT NULL,         -- 생성 날짜 = TTL 기준
  summary      TEXT,                          -- 한 문단
  highlights   TEXT,                          -- 핵심 흐름 (줄바꿈 구분)
  article_ids  TEXT,                          -- 근거 기사 id (쉼표 구분)
  hit_count    INT          DEFAULT 0,        -- 재사용 횟수
  created_at   DATETIME     DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_cache (keyword, days, cache_date),
  INDEX idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

SELECT table_name, engine, table_collation
  FROM information_schema.tables
 WHERE table_schema = 'briefing_v2'
 ORDER BY table_name;
