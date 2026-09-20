import { useCallback, useEffect, useState } from "react";

// API 주소는 빌드에 박지 않고 런타임에 읽는다 — EC2 IP 가 바뀌어도 재빌드 불필요
async function resolveApiBase() {
  try {
    const res = await fetch("./config.json", { cache: "no-store" });
    const { apiUrl } = await res.json();
    if (apiUrl) return apiUrl.replace(/\/$/, "");
  } catch {
    // config.json 이 없으면 아래 기본값
  }
  // 개발 중에는 로컬 Express
  if (["localhost", "127.0.0.1"].includes(window.location.hostname)) {
    return "http://localhost:3000";
  }
  return window.location.origin;
}

const DAY_OPTIONS = [7, 30, 90];

export default function App() {
  const [api, setApi] = useState(null);

  const [articles, setArticles] = useState([]);
  const [digests, setDigests] = useState([]);
  const [dates, setDates] = useState([]);
  const [date, setDate] = useState("");

  const [activeTag, setActiveTag] = useState(null);
  const [filter, setFilter] = useState("");

  const [keyword, setKeyword] = useState("");
  const [days, setDays] = useState(30);
  const [summary, setSummary] = useState(null);
  const [summaryLoading, setSummaryLoading] = useState(false);

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(
    async (base, targetDate) => {
      setLoading(true);
      setError(null);
      try {
        const q = targetDate ? `?date=${targetDate}` : "";
        const [a, d] = await Promise.all([
          fetch(`${base}/articles${q}`).then((r) => r.json()),
          fetch(`${base}/digests${q}`).then((r) => r.json()),
        ]);
        setArticles(Array.isArray(a) ? a : []);
        setDigests(Array.isArray(d) ? d : []);
        setActiveTag(null);
      } catch {
        setError("서버에 연결할 수 없습니다");
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    (async () => {
      const base = await resolveApiBase();
      setApi(base);
      try {
        const list = await fetch(`${base}/dates`).then((r) => r.json());
        setDates(Array.isArray(list) ? list : []);
      } catch {
        // 날짜 목록은 없어도 본문은 보여준다
      }
      load(base, "");
    })();
  }, [load]);

  async function requestSummary() {
    const q = keyword.trim();
    if (!q || summaryLoading) return;
    setSummaryLoading(true);
    setSummary(null);
    try {
      const res = await fetch(`${api}/summary`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ keyword: q, days }),
      });
      setSummary(await res.json());
    } catch {
      setSummary({ error: "요약을 가져오지 못했습니다" });
    } finally {
      setSummaryLoading(false);
    }
  }

  function changeDate(next) {
    setDate(next);
    setFilter("");
    load(api, next);
  }

  // 태그·제목 필터는 받아온 배열 안에서 — 서버에 다시 묻지 않는다
  const tagCounts = articles.reduce((acc, a) => {
    const t = a.tag || "미분류";
    acc[t] = (acc[t] || 0) + 1;
    return acc;
  }, {});

  const visible = articles.filter((a) => {
    if (activeTag && (a.tag || "미분류") !== activeTag) return false;
    if (filter && !a.title.toLowerCase().includes(filter.toLowerCase())) return false;
    return true;
  });

  // 목록은 published_at DESC — 양 끝이 곧 기간
  const span = (() => {
    if (visible.length === 0) return "";
    const day = (a) => (a.published_at || "").slice(0, 10);
    const newest = day(visible[0]);
    const oldest = day(visible[visible.length - 1]);
    if (!newest) return "";
    return newest === oldest ? newest : `${oldest} ~ ${newest}`;
  })();

  return (
    <div className="page">
      <header>
        <h1>국내 IT 뉴스 브리핑</h1>
        <p className="sub">
          {date ? `${date} 아카이브` : "최근 수집분"} · 기사 {articles.length}건
        </p>
      </header>

      {error && <div className="error">{error}</div>}

      <section className="card">
        <h2>키워드 종합 요약</h2>
        <p className="hint">한 주제가 그동안 어떻게 흘러왔는지 한 문단으로 정리합니다.</p>
        <div className="row">
          <input
            className="text"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && requestSummary()}
            placeholder="예: 반도체, 클라우드"
          />
          <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
            {DAY_OPTIONS.map((d) => (
              <option key={d} value={d}>
                최근 {d}일
              </option>
            ))}
          </select>
          <button onClick={requestSummary} disabled={summaryLoading || !keyword.trim()}>
            {summaryLoading ? "요약 중…" : "요약"}
          </button>
        </div>

        {summary && (
          <div className="result">
            {summary.error && <div className="error">{summary.error}</div>}
            {summary.message && <div className="empty">{summary.message}</div>}

            {summary.summary && (
              <>
                <div className="meta">
                  <strong>{summary.keyword}</strong> · 최근 {summary.days}일
                  {summary.cached && <span className="badge">캐시</span>}
                </div>
                <p className="para">{summary.summary}</p>

                {summary.highlights?.length > 0 && (
                  <ul className="highlights">
                    {summary.highlights.map((h, i) => (
                      <li key={i}>{h}</li>
                    ))}
                  </ul>
                )}

                {summary.articles?.length > 0 && (
                  <>
                    <div className="label">근거 기사 {summary.articles.length}건</div>
                    <ul className="refs">
                      {summary.articles.map((a) => (
                        <li key={a.id}>
                          <a href={a.url} target="_blank" rel="noreferrer">
                            {a.title}
                          </a>
                          <span className="src">{a.source}</span>
                        </li>
                      ))}
                    </ul>
                  </>
                )}
              </>
            )}
          </div>
        )}
      </section>

      <section>
        <h2>오늘의 이슈</h2>
        {loading ? (
          <div className="empty">불러오는 중…</div>
        ) : digests.length === 0 ? (
          <div className="empty">이슈가 아직 만들어지지 않았습니다.</div>
        ) : (
          digests.map((d) => (
            <article key={d.rank_no} className="issue">
              <div className="issue-head">
                <span className="rank">{d.rank_no}.</span>
                <h3>{d.headline}</h3>
                <span className="chip">{d.tag}</span>
              </div>
              <p className="para">{d.summary}</p>
              {d.articles?.length > 0 && (
                <ul className="refs">
                  {d.articles.map((a) => (
                    <li key={a.id}>
                      <a href={a.url} target="_blank" rel="noreferrer">
                        {a.title}
                      </a>
                      <span className="src">{a.source}</span>
                    </li>
                  ))}
                </ul>
              )}
            </article>
          ))
        )}
      </section>

      <section>
        <h2>
          전체 기사 <span className="count">{visible.length}건</span>
          {span && <span className="span">{span}</span>}
        </h2>

        <div className="tags">
          <button
            className={activeTag === null ? "tag on" : "tag"}
            onClick={() => setActiveTag(null)}
          >
            전체 {articles.length}
          </button>
          {Object.entries(tagCounts)
            .sort((a, b) => b[1] - a[1])
            .map(([tag, n]) => (
              <button
                key={tag}
                className={activeTag === tag ? "tag on" : "tag"}
                onClick={() => setActiveTag(activeTag === tag ? null : tag)}
              >
                {tag} {n}
              </button>
            ))}
        </div>

        <input
          className="text full"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="제목으로 거르기"
        />

        {visible.length === 0 ? (
          <div className="empty">해당하는 기사가 없습니다.</div>
        ) : (
          <ul className="list">
            {visible.map((a) => (
              <li key={a.id}>
                <a href={a.url} target="_blank" rel="noreferrer">
                  {a.title}
                </a>
                {a.tag && <span className="chip small">{a.tag}</span>}
                <span className="src">{a.source}</span>
                <span className="time">{(a.published_at || "").slice(11)}</span>
              </li>
            ))}
          </ul>
        )}
      </section>

      {dates.length > 0 && (
        <section>
          <h2>아카이브</h2>
          <div className="dates">
            <button className={date === "" ? "tag on" : "tag"} onClick={() => changeDate("")}>
              최근
            </button>
            {dates.map((d) => (
              <button
                key={d.date}
                className={date === d.date ? "tag on" : "tag"}
                onClick={() => changeDate(d.date)}
              >
                {d.date.slice(5)} <span className="count">{d.count}</span>
              </button>
            ))}
          </div>
        </section>
      )}

      <footer>
        제목·출처·링크만 수집합니다. 본문은 각 언론사 원문에서 확인하세요.
      </footer>
    </div>
  );
}
