// 用原型 news-radar.html 裡的 analyze() 產生標準答案，給 Python 版的單元測試比對。
// 執行：node tools/make_golden.js
// 輸出：tests/fixtures/analysis/golden.json
// 原型的分析規則若有更新，重跑這支並確認 python -m pytest 仍全部通過。
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "news-radar.html"), "utf8");
const cut = (from, to) => {
  const a = html.indexOf(from), b = html.indexOf(to, a);
  if (a < 0 || b < 0) throw new Error(`原型找不到程式段落：${from}`);
  return html.slice(a, b);
};
const engineSrc = cut("// ---------- 公司詞典", "const LABEL=");
const newsSrc = cut("const SOURCES =", "const hasUrl=");
const reasonSrc = cut("const esc=", "function cls(");

// 原型在瀏覽器執行；這裡補上它用到的 localStorage，並讓測試可以設定自訂詞
const proto = new Function("localStorage", `${engineSrc}
${reasonSrc}
${newsSrc}
return {STOCKS, SECTOR_KW, BASE_LEX, analyze, reason, NEWS, EXTRA, OLDER,
        setCustom: c => { custom = c }};`)({ getItem: () => null, setItem: () => {} });

// 手寫的邊界案例：每條規則至少一個
const EDGE = [
  { id: "neg-not", t: "法說會", b: "公司表示未見砍單，訂單能見度到明年。" },
  { id: "neg-reduce", t: "", b: "協議可降低產能過剩風險。" },
  { id: "neg-cut-by-comma", t: "", b: "雖然不，營收成長。" },
  { id: "not-neg-words", t: "", b: "不斷創新高，不少法人看好。無論如何營收成長。" },
  { id: "boost", t: "", b: "需求大幅成長，營收大幅衰退，明顯優於預期。" },
  { id: "boost-and-neg", t: "", b: "營收並非大幅衰退。" },
  { id: "title-weight", t: "台積電創新高", b: "台積電創新高。" },
  { id: "longest-match", t: "", b: "南亞科跌停，油價上漲，報價上漲。" },
  { id: "direct-no-sentiment", t: "台積電營收成長", b: "聯電今日召開董事會。" },
  { id: "direct-cancel-out", t: "", b: "華邦電營收成長但虧損。南亞科漲停。" },
  { id: "sector-only", t: "DRAM 報價上漲", b: "記憶體供不應求，HBM 吃緊。" },
  { id: "sector-fallback-total", t: "", b: "法人看好。CoWoS 產能。" },
  { id: "sector-below-threshold", t: "", b: "航空股跟進。" },
  { id: "sector-skip-direct", t: "", b: "AI伺服器需求強勁，鴻海受惠。" },
  { id: "multi-sector", t: "", b: "運價上漲。面板報價下跌。SCFI 創新高。" },
  { id: "empty", t: "", b: "" },
  { id: "no-match", t: "今天天氣晴", b: "這是一則與股市無關的新聞。" },
  { id: "punctuation", t: "營收創新高！", b: "毛利率下修？不過訂單滿載；EPS 上修\n明年樂觀" },
  { id: "alias", t: "台積先進封裝", b: "中華航空與虎航獲利成長。臻鼎接單。" },
];

const dump = (n, a) => ({
  total: a.total,
  label: a.label,
  hits: a.hits.map(h => ({ term: h.term, w: h.w, note: h.note })),
  matches: a.ms.map(m => ({ start: m.start, end: m.end, term: m.term, kind: m.kind })),
  negators: a.negators,
  sentences: a.sents,
  sentence_scores: a.sScore,
  impacts: a.impacts.map(v => ({
    code: v.stock.code, type: v.type, sector: v.sector || null, score: v.score,
    key_sentence: v.type === "direct" ? v.keySent : null, reason: proto.reason(v, a),
  })),
});
const run = (list, custom = {}) => {
  proto.setCustom(custom);
  return list.map(n => ({ id: n.id || n.url, title: n.t, body: n.b, custom, result: dump(n, proto.analyze(n.t, n.b)) }));
};

const tryNews = { id: "try-default", t: "航空股布局時機到了？2027年油價降 法人看好這兩檔",
  b: "法人指出第四季油價上漲，航空公司獲利面臨壓力。但預估 2027 年油價下跌約兩成，獲利可望回升，給予華航、台灣虎航買進評等，長榮航維持中立。" };
const CUSTOM = { "漲價效應": 1.5, "利空": -3, "董事會": 1 };  // 新增詞、覆寫既有詞

const out = {
  _說明: "由 tools/make_golden.js 從原型 news-radar.html 產生，請勿手動修改。",
  stocks: proto.STOCKS.map(s => ({ code: s.code, name: s.name, aliases: s.alias, sectors: s.sectors })),
  sentiment: Object.entries(proto.BASE_LEX),
  sectors: Object.entries(proto.SECTOR_KW),
  cases: [
    ...run([...proto.NEWS, ...proto.EXTRA, ...proto.OLDER, tryNews, ...EDGE]),
    ...run([{ id: "custom-1", t: "漲價效應發酵", b: "利空出盡，董事會通過配息。" }, EDGE[8]], CUSTOM),
  ],
};
const dest = path.join(root, "tests", "fixtures", "analysis", "golden.json");
fs.mkdirSync(path.dirname(dest), { recursive: true });
fs.writeFileSync(dest, JSON.stringify(out, null, 1) + "\n");
console.log(`已輸出 ${out.cases.length} 個案例到 ${path.relative(root, dest)}`);
