-- RDS 환경 진단 — 스키마를 만들기 전에 제일 먼저 돌린다.
--
-- mysql -h <엔드포인트> -u <user> -p < db/check.sql
--
-- DB_HOST 에 http:// 를 붙이지 말 것.

SELECT '===== ① 서버 환경 =====' AS '';

SELECT
    VERSION()              AS mysql_version,
    @@character_set_server AS charset_server,
    @@collation_server     AS collation_server,
    @@ngram_token_size     AS ngram_token_size,
    @@max_allowed_packet   AS max_allowed_packet;

SELECT '===== ② 시간대 =====' AS '';

-- 서버 기본 시간대
SELECT
    @@global.time_zone  AS global_tz,
    @@system_time_zone  AS system_tz,
    NOW()               AS now_default,
    CURDATE()           AS curdate_default;

-- 앱이 실제로 쓰는 조건
SET time_zone = '+09:00';

SELECT
    @@session.time_zone AS session_tz,
    NOW()               AS now_kst,
    CURDATE()           AS curdate_kst;

SELECT '===== ③ 접속 계정과 권한 =====' AS '';

SELECT CURRENT_USER() AS current_user_, USER() AS connected_as;

SHOW GRANTS;

SELECT '===== ④ 기존 데이터베이스 =====' AS '';

SHOW DATABASES;

SELECT '===== ⑤ 기존 테이블 (시스템 DB 제외) =====' AS '';

SELECT
    table_schema,
    table_name,
    engine,
    table_collation,
    table_rows
  FROM information_schema.tables
 WHERE table_schema NOT IN
       ('mysql', 'information_schema', 'performance_schema', 'sys')
 ORDER BY table_schema, table_name;

SELECT '===== 진단 끝 =====' AS '';
