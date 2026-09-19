// 조회 API — EC2. 배치가 쌓아둔 것을 꺼내 준다.
//
//   npm install && cp .env.example .env && npm start
//
// 요구: Node 18+ (fetch 내장)

require("dotenv").config();
const express = require("express");
const mysql = require("mysql2/promise");
const cors = require("cors");

const app = express();
const PORT = process.env.PORT || 80;

// 캐시·프롬프트 입력으로 쓰이므로 상한 필요
const MAX_KEYWORD = 50;
const MAX_DAYS = 365;
const SEARCH_LIMIT = 300;
const SUMMARY_TIMEOUT_MS = 60_000;

let pool = null;

// ── DB ────────────────────────────────────────────────────────

async function connectDatabase() {
  const missing = ["DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME"].filter(
    (k) => !process.env[k],
  );
  if (missing.length) {
    console.error("환경변수 누락:", missing.join(", "));
    return null;
  }

  const created = mysql.createPool({
    host: process.env.DB_HOST,
    user: process.env.DB_USER,
    password: process.env.DB_PASSWORD,
    database: process.env.DB_NAME,
    waitForConnections: true,
    connectionLimit: 5,
    charset: "utf8mb4",
    // published_at 이 KST 저장이므로 CURDATE()/NOW() 도 KST
    timezone: "+09:00",
  });

  created.on("connection", (conn) => {
    conn.query("SET time_zone = '+09:00'");
  });

  await created.query("SELECT 1");
  console.log("DB 연결 성공");
  return created;
}

function requireDb(req, res, next) {
  if (!pool) {
    return res.status(503).json({ error: "데이터베이스를 사용할 수 없습니다" });
  }
  next();
}

// 캐시 키이자 프롬프트 입력 — 한글·영숫자·공백·점·하이픈만
function normalizeKeyword(raw) {
  return String(raw || "")
    .trim()
    .replace(/\s+/g, " ")
    .slice(0, MAX_KEYWORD)
    .replace(/[^\wㄱ-ㅎ가-힣ㅏ-ㅣ .-]/g, "")
    .trim();
}

// 발췌는 화면에 내보내지 않는다
function withoutExcerpt(rows) {
  return rows.map(({ description, ...rest }) => rest);
}

function parseDays(raw) {
  const n = parseInt(raw, 10);
  return Number.isFinite(n) && n > 0 ? Math.min(n, MAX_DAYS) : 30;
}

// tag 는 배치에서 AI 가 붙인 값 — 제목에 글자가 없어도 주제로
// description 은 요약 Lambda 입력 전용 — 응답에서는 제거
const SEARCH_SQL = `
  SELECT id, title, url, source, tag, description,
         DATE_FORMAT(published_at, '%Y-%m-%d %H:%i') AS published_at
    FROM articles
   WHERE published_at >= NOW() - INTERVAL ? DAY
     AND ( tag = ? OR title LIKE CONCAT('%', ?, '%') )
   ORDER BY published_at DESC
   LIMIT ?
`;

// ── 엔드포인트 ────────────────────────────────────────────────

app.use(cors());
app.use(express.json());

app.get("/", (req, res) => {
  res.json({
    message: "서버 실행 중",
    status: {
      database: pool ? "연결됨" : "연결 안됨",
      summary_lambda: process.env.SUMMARY_LAMBDA_URL ? "설정됨" : "설정 안됨",
    },
  });
});

// date 없으면 최근 24시간 수집분 — digests 와 같은 집합
// date 있으면 그날 발행분 (아카이브 조회)
app.get("/articles", requireDb, async (req, res) => {
  const { date, tag } = req.query;
  const where = [date ? "DATE(published_at) = ?" : "collected_at >= NOW() - INTERVAL 1 DAY"];
  const params = date ? [date] : [];

  if (tag) {
    where.push("tag = ?");
    params.push(tag);
  }

  try {
    const [rows] = await pool.query(
      `SELECT id, title, url, source, tag,
               DATE_FORMAT(published_at, '%Y-%m-%d %H:%i') AS published_at
         FROM articles
        WHERE ${where.join(" AND ")}
        ORDER BY published_at DESC
        LIMIT 500`,
      params,
    );
    res.json(rows);
  } catch (e) {
    console.error("기사 조회 실패:", e.message);
    res.status(500).json({ error: "기사 조회 실패" });
  }
});

// 평면 JOIN 결과를 이슈 단위로 묶어서 반환
app.get("/digests", requireDb, async (req, res) => {
  const { date } = req.query;
  try {
    const [rows] = await pool.query(
      `SELECT d.id, d.rank_no, d.headline, d.summary, d.tag,
              a.id AS article_id, a.title, a.url, a.source
         FROM digests d
         LEFT JOIN digest_articles da ON da.digest_id = d.id
         LEFT JOIN articles a ON a.id = da.article_id
        WHERE d.digest_date = ${date ? "?" : "(SELECT MAX(digest_date) FROM digests)"}
        ORDER BY d.rank_no, a.published_at DESC`,
      date ? [date] : [],
    );

    const byIssue = new Map();
    for (const r of rows) {
      if (!byIssue.has(r.id)) {
        byIssue.set(r.id, {
          rank_no: r.rank_no,
          headline: r.headline,
          summary: r.summary,
          tag: r.tag,
          articles: [],
        });
      }
      if (r.article_id) {
        byIssue.get(r.id).articles.push({
          id: r.article_id,
          title: r.title,
          url: r.url,
          source: r.source,
        });
      }
    }
    res.json([...byIssue.values()]);
  } catch (e) {
    console.error("이슈 조회 실패:", e.message);
    res.status(500).json({ error: "이슈 조회 실패" });
  }
});

// SQL 만 — AI 호출 없음
app.get("/search", requireDb, async (req, res) => {
  const keyword = normalizeKeyword(req.query.q);
  const days = parseDays(req.query.days);
  if (!keyword) {
    return res.status(400).json({ error: "검색어가 필요합니다" });
  }

  try {
    const [rows] = await pool.query(SEARCH_SQL, [days, keyword, keyword, SEARCH_LIMIT]);
    res.json({ keyword, days, count: rows.length, articles: withoutExcerpt(rows) });
  } catch (e) {
    console.error("검색 실패:", e.message);
    res.status(500).json({ error: "검색 실패" });
  }
});

// 캐시 → SQL 검색 → Lambda → 캐시 저장
app.post("/summary", requireDb, async (req, res) => {
  const keyword = normalizeKeyword(req.body?.keyword);
  const days = parseDays(req.body?.days);
  if (!keyword) {
    return res.status(400).json({ error: "검색어가 필요합니다" });
  }
  if (!process.env.SUMMARY_LAMBDA_URL) {
    return res.status(503).json({ error: "요약 서비스를 사용할 수 없습니다" });
  }

  try {
    // 같은 키워드·기간을 오늘 이미 만들었으면 Lambda 를 부르지 않는다
    const [cached] = await pool.query(
      `SELECT id, summary, highlights, article_ids FROM summaries
        WHERE keyword = ? AND days = ? AND cache_date = CURDATE()`,
      [keyword, days],
    );

    if (cached.length) {
      const hit = cached[0];
      await pool.query("UPDATE summaries SET hit_count = hit_count + 1 WHERE id = ?", [hit.id]);

      // 근거 기사를 id 로 되살린다 — 원문 링크 표시에 필요
      const ids = (hit.article_ids || "").split(",").filter(Boolean);
      let articles = [];
      if (ids.length) {
        [articles] = await pool.query(
          `SELECT id, title, url, source, tag,
                  DATE_FORMAT(published_at, '%Y-%m-%d %H:%i') AS published_at
             FROM articles WHERE id IN (?) ORDER BY published_at DESC`,
          [ids],
        );
      }
      return res.json({
        keyword,
        days,
        cached: true,
        summary: hit.summary,
        highlights: (hit.highlights || "").split("\n").filter(Boolean),
        articles,
      });
    }

    const [candidates] = await pool.query(SEARCH_SQL, [days, keyword, keyword, SEARCH_LIMIT]);
    if (!candidates.length) {
      return res.json({
        keyword,
        days,
        cached: false,
        summary: null,
        message: "해당 기간에 관련 기사가 없습니다",
        articles: [],
      });
    }

    const ai = await callSummaryLambda(keyword, days, candidates);
    const highlights = Array.isArray(ai.highlights) ? ai.highlights : [];
    const usedIds = (Array.isArray(ai.used) ? ai.used : [])
      .map((i) => candidates[i]?.id)
      .filter(Boolean);

    await pool.query(
      `INSERT INTO summaries (keyword, days, cache_date, summary, highlights, article_ids)
       VALUES (?, ?, CURDATE(), ?, ?, ?)
       ON DUPLICATE KEY UPDATE summary = VALUES(summary),
                               highlights = VALUES(highlights),
                               article_ids = VALUES(article_ids)`,
      [keyword, days, ai.summary || "", highlights.join("\n"), usedIds.join(",")],
    );

    res.json({
      keyword,
      days,
      cached: false,
      summary: ai.summary,
      highlights,
      articles: withoutExcerpt(candidates.filter((c) => usedIds.includes(c.id))),
      all_count: candidates.length,
    });
  } catch (e) {
    console.error("요약 실패:", e.message);
    res.status(500).json({ error: "요약 처리 실패" });
  }
});

// 아카이브 날짜 목록
app.get("/dates", requireDb, async (req, res) => {
  try {
    const [rows] = await pool.query(
      `SELECT DATE_FORMAT(published_at, '%Y-%m-%d') AS date, COUNT(*) AS count
         FROM articles
        WHERE published_at >= NOW() - INTERVAL 60 DAY
        GROUP BY date ORDER BY date DESC LIMIT 30`,
    );
    res.json(rows);
  } catch (e) {
    console.error("날짜 조회 실패:", e.message);
    res.status(500).json({ error: "날짜 조회 실패" });
  }
});

// ── Lambda 호출 ───────────────────────────────────────────────

async function callSummaryLambda(keyword, days, articles) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), SUMMARY_TIMEOUT_MS);
  try {
    const response = await fetch(process.env.SUMMARY_LAMBDA_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keyword, days, articles }),
      signal: controller.signal,
    });
    if (!response.ok) {
      throw new Error(`Lambda ${response.status}`);
    }
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

// ── 기동 ──────────────────────────────────────────────────────

process.on("unhandledRejection", (e) => {
  console.error("처리되지 않은 Promise 거부:", e);
});

(async () => {
  try {
    pool = await connectDatabase();
  } catch (e) {
    console.error("DB 연결 실패:", e.message);
  }

  app.listen(PORT, () => {
    console.log(`포트 ${PORT} · DB ${pool ? "연결됨" : "끊김"} · ` +
      `요약 Lambda ${process.env.SUMMARY_LAMBDA_URL ? "설정됨" : "미설정"}`);
  });
})();
