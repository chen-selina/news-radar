// 台股新聞雷達網頁：所有資料都向後端 API 取得（radar/api/app.py），畫面沿用原型 news-radar.html。
"use strict";

// ---------- 共用 ----------
const LABEL = {pos: "利多", neg: "利空", neu: "中性"};
const strength = s => { const a = Math.abs(s); return a >= 4 ? "高" : a >= 2 ? "中" : "低"; };
const fmt = n => (n > 0 ? "+" : "") + n.toFixed(1);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const cls = s => s > 0.01 ? "up" : s < -0.01 ? "down" : "flat";
const pct = (x, d = 1) => x == null || isNaN(x) ? "—" : (x > 0 ? "+" : "") + (x * 100).toFixed(d) + "%";
const fr = x => x == null ? "—" : (x * 100).toFixed(0) + "%";
const $ = id => document.getElementById(id);
const isUrl = u => /^https?:\/\/\S+$/.test(u || "");
const ymd = d => d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
// ISO 時間（台北）→「10/27 09:30」
const when = iso => iso ? iso.slice(5, 10).replace("-", "/") + " " + iso.slice(11, 16) : "";
const PREVIEW = 160;  // 列表上的內文只顯示前面幾個字

let META = null;

async function api(path, opts = {}) {
  const init = {...opts, headers: {"content-type": "application/json", ...(opts.headers || {})}};
  if (init.body && typeof init.body !== "string") init.body = JSON.stringify(init.body);
  let r;
  try { r = await fetch(path, init); }
  catch (e) { throw new Error("連不上網站後端，請確認 python -m radar.api 還在執行。"); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { const err = new Error(data.detail || `伺服器錯誤（HTTP ${r.status}）`); err.status = r.status; throw err; }
  return data;
}

function showApiError(e) {
  const b = $("apiErr"); b.hidden = false; b.textContent = e.message;
}

// 在原文上標出關鍵詞。offset：這段文字在「標題＋換行＋內文」裡的起點；limit：只顯示前幾個字
function highlight(text, offset, hl, limit) {
  const shown = limit && text.length > limit ? text.slice(0, limit) : text;
  if (!hl) return esc(shown) + (shown.length < text.length ? "…" : "");
  const end = offset + shown.length; let out = "", p = offset;
  const all = [...hl.matches.map(m => ({...m, cls: "k-" + m.kind})), ...hl.negators.map(n => ({...n, cls: "k-negator"}))]
    .filter(m => m.start >= offset && m.end <= end).sort((a, b) => a.start - b.start);
  for (const m of all) {
    if (m.start < p) continue;
    out += esc(shown.slice(p - offset, m.start - offset)) + `<mark class="${m.cls}">${esc(shown.slice(m.start - offset, m.end - offset))}</mark>`;
    p = m.end;
  }
  return out + esc(shown.slice(p - offset)) + (shown.length < text.length ? "…" : "");
}
// impacts：[{code, name, score, type, sector, reason}]
function impactsHTML(impacts) {
  if (!impacts.length) return `<div class="note">沒有比對到上市櫃公司或產業關鍵字。</div>`;
  return `<div class="impacts">${impacts.map(v => `<div class="imp"><span class="nm"><b>${esc(v.name)}</b> <span class="code">${esc(v.code)}</span></span><span class="num ${cls(v.score)}">${fmt(v.score)}・${strength(v.score)}</span><span class="reason">${esc(v.reason)}</span></div>`).join("")}</div>`;
}
function hitsHTML(hl) {
  if (!hl) return "";
  if (!hl.hits.length) return "命中詞：無";
  return "命中詞：" + hl.hits.map(h => `<span class="${h.w > 0 ? "up" : "down"}">${esc(h.term)} ${fmt(h.w)}${h.note ? "（" + esc(h.note) + "）" : ""}</span>`).join("、");
}
const srcLabel = n => (n.source_name || "未知來源") + (n.via_outlet ? `・轉載自 ${n.via_outlet}` : "");
const linkTitle = (url, inner) => isUrl(url) ? `<a class="orig" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${inner}</a>` : inner;

// ---------- 最新新聞 ----------
const feed = {label: "", source: "", q: "", stock: null, stockName: "", period: "7", items: [], total: 0, fresh: new Set()};
const PAGE = 30;

function sinceDate() {
  if (!feed.period) return "";
  const d = new Date(); d.setDate(d.getDate() - (+feed.period - 1)); return ymd(d);
}
function feedParams(extra = {}) {
  const p = new URLSearchParams();
  const since = sinceDate();
  if (feed.label) p.set("label", feed.label);
  if (feed.source) p.set("source", feed.source);
  if (feed.q) p.set("q", feed.q);
  if (feed.stock) p.set("stock", feed.stock);
  if (since) p.set("since", since);
  for (const [k, v] of Object.entries(extra)) p.set(k, v);
  return p;
}

let feedSeq = 0;
async function loadFeed(append = false) {
  const seq = ++feedSeq;
  const p = feedParams({highlight: "true", limit: PAGE, offset: append ? feed.items.length : 0});
  try {
    const d = await api("/api/news?" + p);
    if (seq !== feedSeq) return;  // 使用者又改了篩選條件，這次結果作廢
    feed.items = append ? [...feed.items, ...d.items] : d.items;
    feed.total = d.total;
    renderFeed();
  } catch (e) { showApiError(e); }
}

function newsHTML(it) {
  const p = it.prediction, hl = it.highlight, tl = it.title.length;
  const label = p ? p.label : "neu";
  const meta = p
    ? `<span class="pill ${label}">${LABEL[label]}</span><span class="score ${cls(p.total)}">${fmt(p.total)}</span><span>影響程度 ${strength(p.total)}</span>`
    : `<span class="pill neu">尚未分析</span>`;
  const partial = it.body_from === "summary" ? `<span title="讀不到全文，只用網頁摘要分析">只有摘要</span>` :
                  it.body_from === "title" ? `<span title="讀不到內文，只用標題分析">只有標題</span>` : "";
  const stocks = p ? p.stocks : [];
  const body = it.body || "";
  return `<article class="news ${label}${feed.fresh.has(it.id) ? " fresh" : ""}"><div class="stripe"></div><div class="nbody">
    <div class="meta">${meta}<span>${esc(srcLabel(it))}</span><span>${when(it.published_at || it.fetched_at)}</span>${partial}${it.paywalled ? "<span>付費文章</span>" : ""}${isUrl(it.url) ? `<a class="origlink" href="${esc(it.url)}" target="_blank" rel="noopener noreferrer">閱讀原文 ↗</a>` : `<span class="err">缺少原文連結</span>`}</div>
    <h3>${linkTitle(it.url, highlight(it.title, 0, hl))}</h3>
    <p class="text">${highlight(body, tl + 1, hl, PREVIEW)}</p>
    ${body.length > PREVIEW ? `<details class="fulltext"><summary>展開全文擷取（${body.length} 字）</summary><p class="text">${highlight(body, tl + 1, hl)}</p></details>` : ""}
    <div class="tags">${stocks.slice(0, 6).map(v => `<span class="stag"><span class="${cls(v.score)}">${v.score > 0 ? "▲" : v.score < 0 ? "▼" : "–"}</span>${esc(v.name)}<span class="code">${esc(v.code)}</span></span>`).join("")}</div>
    ${p ? `<details class="why"><summary>為什麼這樣判斷</summary>${impactsHTML(stocks)}<div class="hits">${hitsHTML(hl)}</div>
      <div class="note" style="margin-top:6px">預測建立於 ${when(p.created_at)}，使用詞典第 ${p.lexicon_version} 版，已鎖定。</div></details>` : ""}
  </div></article>`;
}

function renderFeed() {
  const fn = $("filtnote");
  if (feed.stock) {
    fn.hidden = false;
    fn.innerHTML = `只顯示影響 <b>${esc(feed.stockName)} ${esc(feed.stock)}</b> 的新聞 · <button id="clr">清除</button>`;
    $("clr").onclick = () => { feed.stock = null; refreshFeed(); };
  } else fn.hidden = true;
  $("feed").innerHTML = feed.items.length ? feed.items.map(newsHTML).join("")
    : `<div class="empty">${META && !META.counts.news ? "資料庫還沒有新聞。按右上角「重新爬取新聞」，或在命令列執行 python -m radar.crawl。" : "沒有符合條件的新聞（可以把期間改長一點）"}</div>`;
  const more = $("more");
  more.hidden = feed.items.length >= feed.total;
  more.textContent = `載入更多（還有 ${feed.total - feed.items.length} 則）`;
}

async function loadSide() {
  const p = new URLSearchParams();
  const since = sinceDate();
  if (feed.source) p.set("source", feed.source);
  if (since) p.set("since", since);
  try {
    const d = await api("/api/news/summary?" + p);
    const c = d.counts, n = d.total || 1;
    $("distH").textContent = "情緒分布・" + $("period").selectedOptions[0].textContent;
    $("dist").innerHTML = `<span style="width:${c.pos / n * 100}%;background:var(--up)"></span><span style="width:${(c.neu + c.none) / n * 100}%;background:var(--flat)"></span><span style="width:${c.neg / n * 100}%;background:var(--down)"></span>`;
    $("distnums").innerHTML = `<span class="up"><b>${c.pos}</b>利多</span><span class="flat"><b>${c.neu + c.none}</b>中性</span><span class="down"><b>${c.neg}</b>利空</span>`;
    const max = Math.max(1, ...[...d.up, ...d.down].map(g => Math.abs(g.score)));
    const row = g => `<li><button aria-pressed="${feed.stock === g.code}" data-code="${esc(g.code)}" data-name="${esc(g.name)}"><span class="code">${esc(g.code)}</span><span>${esc(g.name)} <span class="code">${g.n} 則</span></span><span class="bar"><span style="width:${Math.abs(g.score) / max * 100}%;background:var(--${g.score > 0 ? "up" : "down"})"></span></span><span class="num ${cls(g.score)}">${fmt(g.score)}</span></button></li>`;
    $("rankUp").innerHTML = d.up.map(row).join("") || `<li class="note">無</li>`;
    $("rankDown").innerHTML = d.down.map(row).join("") || `<li class="note">無</li>`;
    document.querySelectorAll(".rank button").forEach(b => b.onclick = () => {
      feed.stock = feed.stock === b.dataset.code ? null : b.dataset.code;
      feed.stockName = b.dataset.name;
      refreshFeed();
    });
  } catch (e) { showApiError(e); }
}

function refreshFeed() { loadFeed(); loadSide(); }

let qTimer = null;
$("q").oninput = e => { clearTimeout(qTimer); qTimer = setTimeout(() => { feed.q = e.target.value.trim(); loadFeed(); }, 300); };
$("srcSel").onchange = e => { feed.source = e.target.value; refreshFeed(); };
$("period").onchange = e => { feed.period = e.target.value; refreshFeed(); };
$("more").onclick = () => loadFeed(true);
document.querySelectorAll(".toolbar .chip").forEach(b => b.onclick = () => {
  feed.label = b.dataset.f === "all" ? "" : b.dataset.f;
  document.querySelectorAll(".toolbar .chip").forEach(x => x.setAttribute("aria-pressed", x === b));
  loadFeed();
});

// ---------- 重新爬取（開發用） ----------
$("crawl").onclick = async () => {
  const btn = $("crawl"), log = $("log");
  btn.disabled = true; log.hidden = false;
  log.innerHTML = "<div>爬取中，依來源數量約需 1～5 分鐘，請不要關閉頁面…</div>";
  try {
    const d = await api("/api/crawl", {method: "POST", body: {}});
    log.innerHTML = [...d.notes.map(n => "注意：" + n), ...d.log, `完成，共新收錄 ${d.saved} 則` + (d.backlog ? `，補建 ${d.backlog} 筆預測` : "")]
      .map(s => `<div>${esc(s)}</div>`).join("");
    const before = new Set(feed.items.map(i => i.id));
    await loadMeta();
    await loadFeed();
    feed.fresh = new Set(feed.items.filter(i => !before.has(i.id)).map(i => i.id));
    renderFeed(); loadSide();
  } catch (e) {
    log.innerHTML = `<div class="err">${esc(e.message)}</div>`;
  } finally { btn.disabled = false; }
};

// ---------- 貼連結分析 ----------
let tryMeta = {url: "", published_at: null, saved_id: null};

function setStat(msg, kind = "") { const el = $("tStat"); el.className = "fetchstat " + kind; el.innerHTML = msg; }
function showSrc(url, name) {
  const el = $("tSrc");
  if (!isUrl(url)) { el.innerHTML = ""; return; }
  const label = name || "（讀取後判斷）";
  el.innerHTML = `來源：<b>${esc(label)}</b>${name === "其他網站" ? "（不在五個指定來源內，仍可分析）" : ""} · <a class="origlink" href="${esc(url)}" target="_blank" rel="noopener noreferrer">開啟原文 ↗</a>`;
}

function renderTry(a) {
  const t = $("tTitle").value.trim(), b = $("tBody").value.trim();
  const url = isUrl(tryMeta.url) ? tryMeta.url : "";
  $("tOut").innerHTML = `<article class="news ${a.label}"><div class="stripe"></div><div class="nbody">
    <div class="meta"><span class="pill ${a.label}">${LABEL[a.label]}</span><span class="score ${cls(a.total)}">${fmt(a.total)}</span><span>影響程度 ${strength(a.total)}</span><span>詞典第 ${a.lexicon_version} 版</span></div>
    <h3>${linkTitle(url, highlight(t, 0, a))}</h3><p class="text">${highlight(b, t.length + 1, a)}</p>
    <h2 style="margin-top:14px">受影響個股</h2>${impactsHTML(a.impacts)}<div class="hits">${hitsHTML(a)}</div>
    <div class="row" style="margin-top:12px">${url
      ? (tryMeta.saved_id ? `<span class="note">這則新聞已經在列表裡。</span>` : `<button class="btn ghost" id="tAdd">加入新聞列表</button><span class="note" id="addMsg">加入後會存進資料庫並鎖定預測</span>`)
      : `<span class="note">要加入新聞列表，請在上方填入原文網址。</span>`}</div></div></article>`;
  const ad = $("tAdd"); if (ad) ad.onclick = addToFeed;
}

async function runTry() {
  const title = $("tTitle").value.trim(), body = $("tBody").value.trim();
  if (!title && !body) { $("tOut").innerHTML = ""; return; }
  try { renderTry(await api("/api/analyze-text", {method: "POST", body: {title, body}})); }
  catch (e) { $("tOut").innerHTML = `<div class="empty err">${esc(e.message)}</div>`; }
}

async function fetchAndAnalyze() {
  const btn = $("tFetch"), url = $("tUrl").value.trim();
  if (!isUrl(url)) { setStat("請貼上以 http:// 或 https:// 開頭的完整網址。", "bad"); return; }
  btn.disabled = true; setStat("伺服器讀取網頁中…"); showSrc(url);
  try {
    const d = await api("/api/analyze-url", {method: "POST", body: {url}});
    tryMeta = {url: d.url, published_at: d.published_at, saved_id: d.saved_id};
    $("tUrl").value = d.url;
    showSrc(d.url, d.source_name);
    if (!d.ok) {
      setStat(esc(d.reason) + "。", "bad");
      $("tTitle").value = ""; $("tBody").value = ""; $("tOut").innerHTML = "";
      $("tManual").open = true; $("tTitle").focus();
      return;
    }
    $("tTitle").value = d.title; $("tBody").value = d.body;
    setStat(`已讀取：標題 ${d.title.length} 字、內文 ${d.body.length} 字${d.published_at ? "，發布於 " + when(d.published_at) : ""}。` +
            (d.reason ? `<br>${esc(d.reason)}。` : "下方可修改後重新分析。"), d.reason ? "bad" : "ok");
    renderTry({...d.analysis, lexicon_version: d.lexicon_version});
  } catch (e) { setStat(esc(e.message), "bad"); }
  finally { btn.disabled = false; }
}

async function addToFeed() {
  const title = $("tTitle").value.trim(), body = $("tBody").value.trim(), msg = $("addMsg");
  try {
    const item = await api("/api/news", {method: "POST", body: {url: tryMeta.url, title, body, published_at: tryMeta.published_at}});
    tryMeta.saved_id = item.id;
    $("tAdd").remove();
    msg.textContent = `已加入「最新新聞」並鎖定預測（${LABEL[item.prediction.label]} ${fmt(item.prediction.total)}）。`;
    feed.fresh = new Set([item.id]);
    loadMeta(); refreshFeed();
  } catch (e) { msg.innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}

$("tFetch").onclick = fetchAndAnalyze;
$("tUrl").addEventListener("keydown", e => { if (e.key === "Enter") fetchAndAnalyze(); });
$("tUrl").addEventListener("input", e => {
  // 改了網址就是另一則新聞：之前讀到的發布時間、已存檔狀態都不算
  tryMeta = {url: e.target.value.trim(), published_at: null, saved_id: null};
  showSrc(tryMeta.url);
});
$("tRun").onclick = runTry;

// ---------- 情緒詞典 ----------
async function loadLex(d) {
  try {
    d = d || await api("/api/lexicon");
    const chip = t => `<span class="${t.weight > 0 ? "p" : "n"}">${esc(t.term)}<small>${fmt(t.weight)}</small><button data-del="${esc(t.term)}" aria-label="刪除 ${esc(t.term)}" title="刪除">×</button></span>`;
    const ent = d.sentiment.filter(t => t.weight !== 0);
    $("lexPos").innerHTML = ent.filter(t => t.weight > 0).sort((a, b) => b.weight - a.weight).map(chip).join("");
    $("lexNeg").innerHTML = ent.filter(t => t.weight < 0).sort((a, b) => a.weight - b.weight).map(chip).join("");
    $("lexSec").innerHTML = d.sectors.map(s => `<span class="c">${esc(s.keyword)} → ${esc(s.sector)}</span>`).join("");
    $("lexVers").innerHTML = d.versions.map(v => `<li><b>第 ${v.version} 版</b>${when(v.created_at)}　${esc(v.note || "")}</li>`).join("");
    $("lexv").textContent = d.version;
    document.querySelectorAll("[data-del]").forEach(b => b.onclick = async () => {
      const term = b.dataset.del;
      if (!confirm(`確定從詞典刪除「${term}」？會建立新的詞典版本。`)) return;
      try {
        const r = await api("/api/lexicon?term=" + encodeURIComponent(term), {method: "DELETE"});
        $("nwMsg").textContent = `已刪除「${term}」，建立第 ${r.version} 版詞典。`;
        loadLex(r);
      } catch (e) { $("nwMsg").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
    });
  } catch (e) { showApiError(e); }
}

$("nwAdd").onclick = async () => {
  const term = $("nw").value.trim(), w = parseFloat($("nwt").value), msg = $("nwMsg");
  if (!term) { msg.textContent = "請先輸入詞彙。"; return; }
  if (!isFinite(w) || w === 0 || w < -5 || w > 5) { msg.textContent = "分數必須在 −5 到 5 之間，且不能是 0。"; return; }
  try {
    const r = await api("/api/lexicon", {method: "POST", body: {term, weight: w}});
    $("nw").value = "";
    msg.textContent = `已存入「${term}」（${fmt(w)}），建立第 ${r.version} 版詞典。新抓到的新聞會用新版詞典；要看對舊新聞的效果，到「預測驗證」按「用目前詞典重算」。`;
    loadLex(r);
    if ($("tTitle").value.trim() || $("tBody").value.trim()) runTry();
  } catch (e) { msg.innerHTML = `<span class="err">${esc(e.message)}</span>`; }
};

// ---------- 預測驗證 ----------
const val = {h: 3, benchmark: "excess", scope: "all", rowFilt: "all", lex: "", data: null};

function chartSVG(up, down, h) {
  const W = 620, H = 260, L = 48, R = 16, T = 14, B = 34;
  const ys = [...up, ...down].map(p => p.y).filter(y => y != null);
  let mx = Math.max(0.01, ...ys.map(Math.abs)); mx = Math.ceil(mx * 100) / 100;
  const x = k => L + (k + 3) / 13 * (W - L - R), y = v => T + (mx - v) / (2 * mx) * (H - T - B);
  let g = "";
  const step = mx > 0.04 ? 0.02 : 0.01;
  for (let v = -mx; v <= mx + 1e-9; v += step) { const yy = y(v); g += `<line x1="${L}" x2="${W - R}" y1="${yy}" y2="${yy}" stroke="var(--line)" stroke-width="${Math.abs(v) < 1e-9 ? 1.5 : 1}"/><text x="${L - 6}" y="${yy + 4}" text-anchor="end">${(v * 100).toFixed(0)}%</text>`; }
  for (let k = -3; k <= 10; k++) g += `<text x="${x(k)}" y="${H - B + 16}" text-anchor="middle">${k === 0 ? "基準" : k > 0 ? "+" + k : k}</text>`;
  g += `<line x1="${x(0)}" x2="${x(0)}" y1="${T}" y2="${H - B}" stroke="var(--muted)" stroke-dasharray="3 3"/>`;
  g += `<rect x="${x(0)}" y="${T}" width="${x(h) - x(0)}" height="${H - T - B}" fill="var(--accent-soft)" opacity=".6"/>`;
  const line = (pts, col, name) => {
    const p = pts.filter(q => q.y != null); if (!p.length) return "";
    const d = p.map((q, i) => (i ? "L" : "M") + x(q.k).toFixed(1) + " " + y(q.y).toFixed(1)).join(" "); const last = p[p.length - 1];
    return `<path d="${d}" fill="none" stroke="${col}" stroke-width="2.2"/><circle cx="${x(last.k)}" cy="${y(last.y)}" r="3.5" fill="${col}"/><text x="${x(last.k) - 6}" y="${y(last.y) + (last.y >= 0 ? -8 : 16)}" text-anchor="end" style="fill:${col};font-weight:600">${name} ${pct(last.y)}</text>`;
  };
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="新聞前後平均累積報酬">${g}${line(up, "var(--up)", "利多組")}${line(down, "var(--down)", "利空組")}<text x="${W - R}" y="${H - 4}" text-anchor="end">交易日（相對基準日）</text></svg>`;
}

let valSeq = 0;
async function loadVal() {
  const seq = ++valSeq;
  const thr = (parseFloat($("vThr").value) || 0) / 100;
  const p = new URLSearchParams({horizon: val.h, benchmark: val.benchmark, threshold: Math.min(0.5, Math.max(0, thr)), scope: val.scope});
  if (val.lex) p.set("lexicon_version", val.lex);
  try {
    const d = await api("/api/validation?" + p);
    if (seq !== valSeq) return;
    val.data = d; renderVal();
  } catch (e) { $("vWarn").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}

function renderVal() {
  const d = val.data, s = d.summary, ex = d.params.benchmark === "excess", h = d.params.horizon;
  const range = d.data_range.first ? `${d.data_range.first} 至 ${d.data_range.last}` : "尚無資料";
  $("vWarn").innerHTML = (val.lex ? `目前顯示<b>第 ${val.lex} 版詞典的重算結果</b>（不是當時鎖定的預測），用來比較詞典修改的效果。`
                                  : "目前顯示<b>鎖定的原始預測</b>。") +
    ` 收盤價資料：${range}。` + (s.n < 30 ? ` <b>有方向的預測只有 ${s.n} 筆，命中率參考價值有限。</b>` : "") +
    d.notes.map(n => ` <b>${esc(n)}</b>`).join("");
  const tile = (k, v, sub, c = "") => `<div class="tile"><div class="k">${k}</div><div class="v ${c}">${v}</div><div class="s">${sub}</div></div>`;
  $("vTiles").innerHTML =
    tile("整體命中率", fr(s.hit_rate), `${s.hits} / ${s.n} 筆有方向的預測`) +
    tile("利多命中率", fr(s.up_hit_rate), `利多組平均 ${pct(s.up_avg)}`, "up") +
    tile("利空命中率", fr(s.down_hit_rate), `利空組平均 ${pct(s.down_avg)}`, "down") +
    tile("多空報酬差", pct(s.spread), "差距越大，分數越有鑑別力") +
    tile("待驗證", s.waiting, `還沒滿 ${h} 個交易日`);
  $("vChartNote").textContent = `${ex ? "扣除大盤後的" : "個股"}累積報酬，基準日 = 新聞前最後一個收盤。藍底區間為目前的觀察期間（${h} 日）。`;
  $("vChart").innerHTML = chartSVG(d.event_curve.up, d.event_curve.down, h);
  $("vStr").innerHTML = d.by_strength.map(b => `<div class="sbar"><span>${b.strength}影響</span><span class="track"><span style="width:${(b.hit_rate || 0) * 100}%"></span><i></i></span><span class="num">${fr(b.hit_rate)}・${b.n} 筆</span></div>`).join("") + `<div class="note">虛線為 50%。</div>`;
  $("vRetH").textContent = ex ? "超額報酬" : "漲跌幅";
  const DIR = {up: "▲ 漲", down: "▼ 跌", flat: "– 持平"};
  const waiting = r => r.status === "wait" || r.status === "wait_base";
  const f = val.rowFilt;
  const list = d.rows.filter(r => r.status !== "nodata")
    .filter(r => f === "all" || (f === "hit" && r.hit === true) || (f === "miss" && r.hit === false) || (f === "wait" && waiting(r)));
  $("vRows").innerHTML = list.map(r => {
    const res = r.status === "wait" ? `<span class="res wait">等待 ${r.left} 日</span>`
      : r.status === "wait_base" ? `<span class="res wait">等待基準價</span>`
      : r.pred === "flat" ? `<span class="res wait">中性・不計</span>` : r.hit ? `<span class="res ok">命中</span>` : `<span class="res bad">未命中</span>`;
    const done = r.status === "done";
    return `<tr><td class="n">${r.date.slice(5).replace("-", "/")} ${r.time || ""}</td><td class="ttl">${linkTitle(r.url, esc(r.title))}<div class="code">${esc(srcLabel({source_name: (META && META.sources[r.source]) || r.source, via_outlet: r.via_outlet}))}</div></td><td><b>${esc(r.name)}</b> <span class="code">${esc(r.code)}</span>${r.type === "sector" ? `<div class="code">產業連動</div>` : ""}</td>
      <td class="${cls(r.score)}">${r.pred === "up" ? "利多" : r.pred === "down" ? "利空" : "中性"} <span class="code">${fmt(r.score)}</span></td>
      <td class="n">${done ? `${r.p0} → ${r.p1}<div class="code">${r.base_date.slice(5)} → ${r.end_date.slice(5)}</div>` : "—"}</td><td class="n ${r.ex != null ? cls(r.ex) : ""}">${done ? pct(r.ex, 2) + `<div class="code">${DIR[r.act]}</div>` : "—"}</td><td>${res}</td></tr>`;
  }).join("") || `<tr><td colspan="7" class="note" style="text-align:center;padding:20px">沒有符合條件的資料</td></tr>`;
  if (s.nodata) $("vRows").insertAdjacentHTML("beforeend", `<tr><td colspan="7" class="note">另有 ${s.nodata} 筆因缺少該股價格（停牌、新上市）或新聞早於價格資料而略過。</td></tr>`);
}

function seg(id, cb) {
  document.querySelectorAll(`#${id} button`).forEach(b => b.onclick = () => {
    document.querySelectorAll(`#${id} button`).forEach(x => x.setAttribute("aria-pressed", x === b)); cb(b.dataset.v);
  });
}
seg("vH", v => { val.h = +v; loadVal(); });
seg("vB", v => { val.benchmark = v; loadVal(); });
seg("vS", v => { val.scope = v; loadVal(); });
document.querySelectorAll("#vRowF .chip").forEach(b => b.onclick = () => {
  val.rowFilt = b.dataset.f;
  document.querySelectorAll("#vRowF .chip").forEach(x => x.setAttribute("aria-pressed", x === b));
  if (val.data) renderVal();
});
let thrTimer = null;
$("vThr").oninput = () => { clearTimeout(thrTimer); thrTimer = setTimeout(loadVal, 300); };
$("vLex").onchange = e => { val.lex = e.target.value; loadVal(); };

function fillLexSelect() {
  const sel = $("vLex");
  sel.innerHTML = `<option value="">原始預測（鎖定）</option>` +
    META.recalc_versions.map(v => `<option value="${v}">第 ${v} 版詞典重算</option>`).join("");
  sel.value = META.recalc_versions.includes(+val.lex) ? val.lex : "";
}

$("vRecalc").onclick = async () => {
  const btn = $("vRecalc"); btn.disabled = true;
  try {
    const r = await api("/api/recalc", {method: "POST"});
    await loadMeta();
    val.lex = String(r.lexicon_version); fillLexSelect();
    $("pMsg").textContent = "";
    await loadVal();
  } catch (e) { $("vWarn").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
  finally { btn.disabled = false; }
};

function renderPriceRange() {
  $("pRange").textContent = META.prices.first ? `目前有 ${META.prices.first} 至 ${META.prices.last} 的交易日與收盤價。` : "還沒有收盤價資料。";
}
$("pUpdate").onclick = async () => {
  const btn = $("pUpdate"), msg = $("pMsg"); btn.disabled = true;
  msg.textContent = "抓取中…證交所要求請求間隔，第一次可能需要幾分鐘。";
  try {
    const r = await api("/api/prices/update", {method: "POST", body: {}});
    msg.innerHTML = `完成：更新 ${r.months} 個月的日曆、${r.days} 個交易日、${r.rows} 筆收盤價。` +
      r.errors.map(e => `<br><span class="err">注意：${esc(e)}</span>`).join("");
    await loadMeta(); loadVal();
  } catch (e) { msg.innerHTML = `<span class="err">${esc(e.message)}</span>`; }
  finally { btn.disabled = false; }
};
$("pFile").onchange = e => {
  const f = e.target.files[0]; if (!f) return;
  const rd = new FileReader(); rd.onload = () => { $("pText").value = rd.result; }; rd.readAsText(f, "utf-8");
};
$("pImport").onclick = async () => {
  const msg = $("pMsg"), csv = $("pText").value;
  if (!csv.trim()) { msg.innerHTML = `<span class="err">請先選擇檔案或貼上 CSV 內容。</span>`; return; }
  try {
    const r = await api("/api/prices/import", {method: "POST", body: {csv}});
    msg.innerHTML = `已匯入 ${r.rows} 筆、${r.days} 天（其中 ${r.taiex_days} 天有大盤）。` +
      (r.errors.length ? `<span class="err"> 略過 ${r.errors.length} 列：${esc(r.errors.slice(0, 3).join("、"))}${r.errors.length > 3 ? "…" : ""}</span>` : "");
    await loadMeta(); loadVal();
  } catch (e) { msg.innerHTML = `<span class="err">${esc(e.message)}</span>`; }
};

// ---------- 分頁、啟動 ----------
document.querySelectorAll("nav.tabs button").forEach(b => b.onclick = () => {
  document.querySelectorAll("nav.tabs button").forEach(x => x.setAttribute("aria-selected", x === b));
  ["feed", "try", "lex", "val", "arch"].forEach(t => $("tab-" + t).hidden = t !== b.dataset.tab);
  if (b.dataset.tab === "val" && !val.data) loadVal();
});

async function loadMeta() {
  try {
    META = await api("/api/meta");
    $("count").textContent = META.counts.news;
    $("updated").textContent = META.last_fetched ? META.last_fetched.slice(0, 16).replace("T", " ").replaceAll("-", "/") : "尚未爬取";
    $("lexv").textContent = META.lexicon_version ?? "—";
    const job = META.last_job, JOB = {running: "執行中", ok: "成功", partial: "部分完成", error: "失敗"};
    $("lastjob").textContent = job ? `${when(job.started_at)} ${JOB[job.status]}` : "尚未執行";
    $("lastjob").title = job ? job.summary : "排程設定見 README「排程」";
    $("lastjob").className = job && job.status !== "ok" && job.status !== "running" ? "err" : "";
    const sel = $("srcSel"), cur = sel.value;
    sel.innerHTML = `<option value="">全部來源</option>` + Object.entries(META.sources).map(([k, v]) => `<option value="${k}">${esc(v)}${k in META.enabled && !META.enabled[k] ? "（暫停）" : ""}</option>`).join("");
    sel.value = cur;
    $("srcStatus").textContent = "目前爬取的來源：" + Object.entries(META.enabled).map(([k, on]) => `${META.sources[k]}${on ? "" : "（暫停）"}`).join("、") +
      "。開關在 config/crawl.json；Google新聞因 robots.txt 不允許、天下雜誌因網站拒絕程式讀取而暫停。";
    renderPriceRange(); fillLexSelect();
    if (!META.counts.stocks) showApiError(new Error("資料庫沒有公司資料，分析功能無法使用。請先在命令列執行 python -m radar.stocks.load。"));
  } catch (e) { showApiError(e); }
}

(async () => {
  await loadMeta();
  refreshFeed();
  loadLex();
})();
