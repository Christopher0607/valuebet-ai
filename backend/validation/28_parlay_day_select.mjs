/**
 * 串关页「按天全选」的真实交互回归。
 *
 * 不是重写一遍逻辑再断言（那只验证了我的复制粘贴）——这里把 App.jsx 里真正的
 * ParlaySuggestTab 用 esbuild 打包出来，挂到 jsdom 上真的点，读 DOM 上的
 * 「已选 N」来判断状态。改坏 toggleDay 或 DayGroups 的 stopPropagation，
 * 这个脚本会红。
 *
 * 跑：node backend/validation/28_parlay_day_select.mjs
 * 依赖：frontend/node_modules 里的 react / react-dom / esbuild，外加 jsdom
 *       （不在 package.json 里，跑之前 cd frontend && npm i --no-save jsdom）
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND = path.resolve(HERE, "..", "..", "frontend");
const APP = path.join(FRONTEND, "src", "App.jsx");
const NM = path.join(FRONTEND, "node_modules");
const OUT = path.join(NM, ".vb-test-bundle.mjs");

const esbuild = await import(path.join(NM, "esbuild", "lib", "main.js"));
const { JSDOM } = await import(path.join(NM, "jsdom", "lib", "api.js"));

// ── 1. 把 App.jsx 打成一个可以在 node 里 import 的包 ──────────────
// App.jsx 没有 export ParlaySuggestTab（它是模块内部的函数），所以造一个
// 影子入口：读源码 + 追加一行 export。源码本身一个字都不改。
const src = fs.readFileSync(APP, "utf8");
const SHADOW = path.join(FRONTEND, "src", ".__test_app_entry.jsx");
fs.writeFileSync(SHADOW, src + "\nexport { ParlaySuggestTab, DayGroups, StatusBanner };\n");

// auth / recharts 在 node 里跑不起来也用不到，用桩替掉。
const stub = {
  name: "stub",
  setup(build) {
    build.onResolve({ filter: /^\.\/auth$|^recharts$/ }, a => ({ path: a.path, namespace: "stub" }));
    build.onLoad({ filter: /.*/, namespace: "stub" }, a => ({
      contents: a.path === "recharts"
        ? "export const LineChart=()=>null,Line=()=>null,XAxis=()=>null,YAxis=()=>null," +
          "CartesianGrid=()=>null,Tooltip=()=>null,ResponsiveContainer=()=>null,ReferenceLine=()=>null;"
        : "export const isAuthEnabled=false,supabase=null,supabaseUrl='',supabaseKeyHint='';" +
          "export const getToken=async()=>null,signIn=async()=>({}),signUp=async()=>({}),signOut=async()=>({});",
      loader: "js",
    }));
  },
};

try {
  await esbuild.build({
    entryPoints: [SHADOW],
    bundle: true, format: "esm", outfile: OUT, logLevel: "silent",
    jsx: "automatic", loader: { ".js": "jsx", ".jsx": "jsx" },
    external: ["react", "react-dom", "react/jsx-runtime", "react-dom/client"],
    define: { "import.meta.env": JSON.stringify({ DEV: false, VITE_API_BASE: "" }) },
    plugins: [stub],
  });
} finally {
  fs.unlinkSync(SHADOW);
}

// ── 2. jsdom + react-dom 真的渲染 ────────────────────────────────
const dom = new JSDOM("<!doctype html><div id=root></div>", { url: "http://localhost/", pretendToBeVisual: true });
for (const k of ["window", "document", "navigator", "HTMLElement", "Node", "Event", "MouseEvent", "getComputedStyle"]) {
  // node 22 起 globalThis.navigator 是只读的 getter，直接赋值会抛；
  // 用 defineProperty 覆盖掉。
  Object.defineProperty(globalThis, k, {
    value: k === "window" ? dom.window : dom.window[k],
    writable: true, configurable: true,
  });
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
globalThis.fetch = async () => { throw new Error("测试里不该发请求"); };
globalThis.localStorage = dom.window.localStorage;

const React = (await import(path.join(NM, "react", "index.js"))).default;
const { createRoot } = await import(path.join(NM, "react-dom", "client.js"));
const { act } = React;   // react-dom/test-utils 的 act 在 18.3 起已弃用
const { ParlaySuggestTab, StatusBanner } = await import(OUT);

// 三天的赛程，每天场次不同；全部带 market_odds，所以全都进 withOdds 候选池。
const DAYS = ["2026-08-15", "2026-08-16", "2026-08-17"];
const COUNTS = { "2026-08-15": 3, "2026-08-16": 5, "2026-08-17": 2 };
let id = 0;
const upcoming = DAYS.flatMap(d =>
  Array.from({ length: COUNTS[d] }, () => {
    id += 1;
    return {
      id, date: d, time_utc: "19:00", status: "upcoming",
      team1: `Home ${id}`, team2: `Away ${id}`,
      competition_id: 1, competition_name: "英超", round: "1",
      market_odds: { avg_home: 2.1, avg_draw: 3.3, avg_away: 3.5 },
      prediction: { prob_home: 0.45, prob_draw: 0.28, prob_away: 0.27 },
    };
  })
);
const TOTAL = upcoming.length;   // 10

const container = dom.window.document.getElementById("root");
const root = createRoot(container);
act(() => {
  root.render(React.createElement(ParlaySuggestTab, {
    upcoming, settings: { bankroll_total: 10000, kelly_fraction: 0.5, max_bet_pct: 0.15 },
    parlayBets: [], realBets: [], onRefresh: () => {},
  }));
});

// ── 3. 从 DOM 读状态 ────────────────────────────────────────────
const $$ = sel => [...container.querySelectorAll(sel)];
const text = () => container.textContent;
const headers = () => $$("button").filter(b => /^\s*[▾▸]/.test(b.textContent));

/** 顶部「已选 N / M 场（已定价）」里的 N —— 全局选中数 */
function selectedCount() {
  const m = text().match(/已选 (\d+) \/ (\d+) 场/);
  if (!m) throw new Error("找不到顶部计数，界面结构变了？");
  if (+m[2] !== TOTAL) throw new Error(`候选池应为 ${TOTAL}，实际 ${m[2]}`);
  return +m[1];
}

/** 每个日期标题上的「全选/取消」小按钮，按天顺序 */
function dayChips() {
  const chips = $$('span[role="button"]').filter(s => s.textContent === "全选" || s.textContent === "取消");
  if (chips.length !== DAYS.length) {
    throw new Error(`应有 ${DAYS.length} 个按天全选按钮，实际 ${chips.length}`);
  }
  return chips;
}

/** 某天标题上的「已选 n」徽章；没有徽章说明这天一场没选 */
function daySelectedBadge(i) {
  const m = headers()[i].textContent.match(/已选 (\d+)/);
  return m ? +m[1] : 0;
}

/** 这一天是不是展开着 */
const dayOpen = i => headers()[i].textContent.trimStart().startsWith("▾");

function click(el) {
  act(() => { el.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true })); });
}

let failed = 0;
function check(name, cond, extra = "") {
  if (cond) console.log(`  ✅ ${name}`);
  else { console.log(`  ❌ ${name}${extra ? " —— " + extra : ""}`); failed += 1; }
}

console.log("串关「按天全选」交互回归\n");
console.log(`赛程：${DAYS.length} 天 / ${TOTAL} 场（${DAYS.map(d => COUNTS[d]).join(" + ")}）\n`);

check("初始一场未选", selectedCount() === 0, `实际 ${selectedCount()}`);
check("每天标题都有全选按钮", dayChips().length === 3);
check("默认全部收起", DAYS.every((_, i) => !dayOpen(i)));

// —— 全选第二天（5 场）——
click(dayChips()[1]);
check("全选第 2 天后总数 = 5", selectedCount() === 5, `实际 ${selectedCount()}`);
check("第 2 天徽章 = 5", daySelectedBadge(1) === 5, `实际 ${daySelectedBadge(1)}`);
check("第 1 天没被牵连", daySelectedBadge(0) === 0);
check("第 3 天没被牵连", daySelectedBadge(2) === 0);
check("按钮变成「取消」", dayChips()[1].textContent === "取消");
// 这条是 stopPropagation 的回归：不拦事件冒泡的话，点全选会顺手折叠这一天，
// 刚选好的比赛立刻从眼前消失。
check("点全选不会把这天折叠掉", !dayOpen(1), "被 toggle(date) 顺带触发了");

// —— 再全选第一天（3 场），两天应该叠加 ——
click(dayChips()[0]);
check("再全选第 1 天后总数 = 8", selectedCount() === 8, `实际 ${selectedCount()}`);
check("第 2 天仍是 5", daySelectedBadge(1) === 5, `实际 ${daySelectedBadge(1)}`);

// —— 取消第二天，只掉这一天的 5 场 ——
click(dayChips()[1]);
check("取消第 2 天后总数 = 3", selectedCount() === 3, `实际 ${selectedCount()}`);
check("第 1 天仍是 3", daySelectedBadge(0) === 3, `实际 ${daySelectedBadge(0)}`);
check("第 2 天归零", daySelectedBadge(1) === 0, `实际 ${daySelectedBadge(1)}`);
check("按钮变回「全选」", dayChips()[1].textContent === "全选");

// —— 单场取消后，那天的按钮要退回「全选」 ——
click(headers()[0]);                        // 展开第 1 天
check("点标题能展开", dayOpen(0));
const firstCard = $$("div").find(d =>
  /Home 1\b/.test(d.textContent) && (d.getAttribute("style") || "").includes("cursor: pointer"));
check("找得到第一场的卡片", !!firstCard);
click(firstCard);                           // 取消其中一场
check("单场取消后该天 = 2", daySelectedBadge(0) === 2, `实际 ${daySelectedBadge(0)}`);
check("此时按钮应是「全选」", dayChips()[0].textContent === "全选");

// —— 顶部「全选」跟按天全选共存 ——
const topAll = $$("button").find(b => b.textContent === "全选" || b.textContent === "取消全选");
click(topAll);
check("顶部全选 = 全部 10 场", selectedCount() === TOTAL, `实际 ${selectedCount()}`);
check("每天按钮都变「取消」", dayChips().every(c => c.textContent === "取消"));
click(dayChips()[2]);
check("取消第 3 天后 = 8", selectedCount() === 8, `实际 ${selectedCount()}`);


// ── 3b. 推荐卡片里改单腿赔率 ─────────────────────────────────────
// 31 号验的是"算得对不对"，这里验的是"接得上没有"：
// 输入框敲进去的价，有没有真的走到界面上那三个数字、以及走到记账的请求体里。
// 记账那一条尤其重要——界面显示改后的价、请求体却发原价，是那种
// 界面上完全看不出来、只有对账时才发现的错。
console.log("\n推荐卡片：改单腿赔率\n");

const COMBO = {
  legs: [
    { label: "A win", outcome: "home", odds: 2.0, prob: 0.55, match_id: upcoming[0].id },
    { label: "B win", outcome: "away", odds: 3.0, prob: 0.40, match_id: upcoming[1].id },
  ],
  n_legs: 2,
  joint_probability: 0.22,
  combined_odds: 6.0,
  ev: 0.32,                       // 0.22 × 6.0 - 1
  kelly_pct: 0.032,
  kelly_amount: 320,
  weakest_leg_label: "B win", weakest_leg_match_id: upcoming[1].id,
  weakest_leg_outcome: "away", weakest_leg_prob: 0.40,
  risk_ratio_vs_weakest_leg: 0.55,
};
const SUGGEST = {
  status: "ok", n_combinations_evaluated: 12, n_candidates: 4, combinations: [COMBO],
};

const posted = [];
const savedBack = [];
globalThis.fetch = async (url, opts = {}) => {
  const u = String(url);
  if (u.endsWith("/parlay/suggest")) {
    return { ok: true, status: 200, headers: new dom.window.Headers(),
             json: async () => SUGGEST };
  }
  if (u.endsWith("/parlay-bets")) {
    posted.push(JSON.parse(opts.body));
    return { ok: true, status: 200, headers: new dom.window.Headers(),
             json: async () => ({ id: 1 }) };
  }
  if (u.endsWith("/odds/bulk")) {
    savedBack.push(JSON.parse(opts.body));
    return { ok: true, status: 200, headers: new dom.window.Headers(),
             json: async () => ({ saved: JSON.parse(opts.body).items.length, skipped: 0 }) };
  }
  throw new Error("测试里没预期到的请求: " + u);
};

const flush = async () => { await act(async () => { await new Promise(r => setTimeout(r, 0)); }); };

// 选两场，跑一次搜索
click(dayChips()[0]);
const genBtn = $$("button").find(b => /生成推荐组合|搜索/.test(b.textContent));
check("找得到搜索按钮", !!genBtn);
await flush();
act(() => { genBtn.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true })); });
await flush();
check("推荐结果渲染出来了", /推荐组合/.test(text()));

/** 卡片上的四个统计值 */
function stats() {
  const t = text();
  const g = re => (t.match(re) || [])[1];
  return { odds: g(/联合赔率([\d.]+)/), ev: g(/EV([+\-][\d.]+%)/),
           kelly: g(/建议([\d,]+)/) };
}
const before = stats();
check(`初始联合赔率 6.00（实际 ${before.odds}）`, before.odds === "6.00");
check(`初始 EV +32.0%（实际 ${before.ev}）`, before.ev === "+32.0%");

// 把第一腿从 2.0 改成 1.50 → 联合赔率 4.50，EV = 0.22×4.5-1 = -0.01
const legInputs = $$('input[aria-label="单腿赔率"]');
check(`两条腿各有一个赔率输入框（实际 ${legInputs.length} 个）`, legInputs.length === 2);
check("输入框预填的是原价", legInputs[0].value === "2" || legInputs[0].value === "2.0");
act(() => {
  const setter = Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, "value").set;
  setter.call(legInputs[0], "1.50");
  legInputs[0].dispatchEvent(new dom.window.Event("input", { bubbles: true }));
});
const after = stats();
check(`联合赔率跟着变成 4.50（实际 ${after.odds}）`, after.odds === "4.50");
check(`EV 跟着变成 -1.0%（实际 ${after.ev}）`, after.ev === "-1.0%");
check("EV 转负后有明确提示", /负 EV/.test(text()));
check("显示了原值供对照", /原 6\.00/.test(text()));
// 不从统计格里正则取凯利值：那一格的主值和副值是相邻的两个 DOM 节点，
// textContent 会把它们拼成 "00.0% · 原 320"，正则取到的是 "00"。
// 「记虚拟盘（N）」按钮上的金额是同一个数，且没有歧义。
const vAmt = ($$("button").find(b => /记虚拟盘/.test(b.textContent))?.textContent || "")
  .match(/记虚拟盘（([\d,]+)）/)?.[1];
check(`凯利建议归零（负 EV 不该推荐下注），按钮显示 ${vAmt}`, vAmt === "0");

// 记一笔虚拟盘，检查请求体里是改后的价
const vbtn = $$("button").find(b => /记虚拟盘/.test(b.textContent));
await act(async () => {
  vbtn.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  await new Promise(r => setTimeout(r, 0));
});
// EV 已经是负的、凯利为 0，按设计这一注根本记不下去——这本身就是要的行为
check("负 EV 时点记虚拟盘不会真的记进去", posted.length === 0,
      `却发出了 ${posted.length} 个请求：${JSON.stringify(posted[0] || null).slice(0, 200)}`);

// 改成一个更高的价，让它重新变成正 EV，再记一次
act(() => {
  const setter = Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, "value").set;
  setter.call(legInputs[0], "2.50");
  legInputs[0].dispatchEvent(new dom.window.Event("input", { bubbles: true }));
});
check(`抬价后联合赔率 7.50（实际 ${stats().odds}）`, stats().odds === "7.50");
const vbtn2 = $$("button").find(b => /记虚拟盘/.test(b.textContent));
await act(async () => {
  vbtn2.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  await new Promise(r => setTimeout(r, 0));
});
check("这次记进去了", posted.length === 1, `实际 ${posted.length} 个请求`);
if (posted.length === 1) {
  const body = posted[0];
  check("请求体里第一腿是改后的 2.5，不是原来的 2.0",
        body.legs[0].odds === 2.5, `实际 ${body.legs[0].odds}`);
  check("没改的第二腿保持 3.0", body.legs[1].odds === 3.0, `实际 ${body.legs[1].odds}`);
  check("总赔率是改后的乘积 7.5", body.odds_used === 7.5, `实际 ${body.odds_used}`);
  check("ev_at_bet 是重算后的 0.65（0.22×7.5-1）",
        Math.abs(body.ev_at_bet - 0.65) < 1e-9, `实际 ${body.ev_at_bet}`);
  check("联合概率原样透传，没被赔率带着变",
        body.joint_probability === COMBO.joint_probability, `实际 ${body.joint_probability}`);
}

// 存回系统：只覆盖改的那一路，同场另外两个保留
const saveBtn = $$("button").find(b => b.textContent === "存回系统");
check("有「存回系统」按钮", !!saveBtn);
if (saveBtn) {
  await act(async () => {
    saveBtn.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
    await new Promise(r => setTimeout(r, 0));
  });
  check("发出了一次 /odds/bulk", savedBack.length === 1, `实际 ${savedBack.length} 次`);
  const it = savedBack[0]?.items?.[0];
  check("只带了改过的那一场", savedBack[0]?.items?.length === 1,
        JSON.stringify(savedBack[0]));
  // 第一腿是 outcome=home，改成了 2.50；这一场的市场平均价是 2.1/3.3/3.5
  check("主队那一路被覆盖成 2.5", it?.odds_home === 2.5, JSON.stringify(it));
  check("平局价保留原来的 3.3", it?.odds_draw === 3.3, JSON.stringify(it));
  check("客队价保留原来的 3.5", it?.odds_away === 3.5, JSON.stringify(it));
  check("按钮给出已存反馈", /已存/.test(text()));
}

// 复原
const undo = $$("button").find(b => b.textContent === "复原");
check("有「复原」按钮", !!undo);
if (undo) {
  click(undo);
  check(`复原后回到 6.00（实际 ${stats().odds}）`, stats().odds === "6.00");
  check("复原后不再显示「原 …」对照", !/原 6\.00/.test(text()));
}

// ── 4. 状态条：更新失败的详情不再挂在首页上 ──────────────────────
// 用户的原话是「不影响我下注，但影响我美观」。信息本身没删——
// GET /api/status 和 UpdateLog 表里都还在，只是不往界面上顶。
console.log("\n状态条详情已下架\n");
const sbHost = dom.window.document.createElement("div");
dom.window.document.body.appendChild(sbHost);
const sbRoot = createRoot(sbHost);
const DETAIL = "英乙 3 场赛程的队名不在参数表里：Barnet, Bromley";
for (const sev of ["error", "warning"]) {
  act(() => {
    sbRoot.render(React.createElement(StatusBanner, {
      status: {
        last_update: "2026-08-12T10:00:00", last_status: "partial",
        last_severity: sev, last_status_label: "部分赛事未更新", last_detail: DETAIL,
      },
      updating: false, onUpdateNow: () => {}, refreshing: false, refreshFailed: false,
    }));
  });
  const t = sbHost.textContent;
  check(`severity=${sev} 时详情不出现在状态条`, !t.includes(DETAIL), `实际渲染出了：${t}`);
  check(`severity=${sev} 时也不出现「更新失败」字样`, !t.includes("更新失败"), `实际：${t}`);
  check(`severity=${sev} 时「上次更新」还在`, t.includes("上次更新"), `实际：${t}`);
  check(`severity=${sev} 时「立即更新」按钮还在`, t.includes("立即更新"), `实际：${t}`);
}

fs.unlinkSync(OUT);
console.log(failed === 0 ? "\n全部通过" : `\n${failed} 项失败`);
process.exit(failed === 0 ? 0 : 1);
