/**
 * 串关推荐里改单腿赔率 —— 前端重算必须跟后端算的一模一样。
 *
 * 这个功能的坑不在"能不能输入"，在**两套实现漂移**：
 * 联合赔率/EV/凯利本来是后端 model.py 算的，现在前端要在你改价时当场重算
 * 一遍。两边只要有一点不一致，就会出现"什么都没改，一点开输入框数字自己
 * 跳一下"，或者更糟——记进账的 EV 跟界面上显示的不是一个数。
 *
 * 所以这里不各写一遍公式对答案，而是：
 *   · 起真的后端，让它真的搜一批串关出来
 *   · 把 App.jsx 里真正的 recomputeCombo 用 esbuild 导出来
 *   · 未改价时，要求前端重算的四个数跟后端返回的**逐位相等**
 *   · 改价后，用后端自己的 expected_value / kelly_pct 算出期望值，再比
 *
 * 另外守住 CLAUDE.md 那条硬约束：EV = 模型概率 × 市场原始赔率 - 1。
 * 改赔率只准动"赔率"那一半，概率必须原样透传，不能被赔率反推出来。
 *
 * 跑：node backend/validation/31_leg_odds_override.mjs
 */
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { spawn, execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..", "..");
const BACKEND = path.join(ROOT, "backend");
const FRONTEND = path.join(ROOT, "frontend");
const NM = path.join(FRONTEND, "node_modules");
const OUT = path.join(NM, ".vb-leg-bundle.mjs");
const PORT = 8934;
const BASE = `http://127.0.0.1:${PORT}/api`;
const DB = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "vb-leg-")), "leg.db");

let failed = 0;
const check = (name, cond, extra = "") => {
  if (cond) console.log(`  ✅ ${name}`);
  else { console.log(`  ❌ ${name}${extra ? " —— " + extra : ""}`); failed += 1; }
};

console.log("串关推荐改单腿赔率：前端重算 vs 后端公式\n");

// ── 造一批有预测的未来比赛 ───────────────────────────────────
execFileSync("python3", ["-c", `
import os, datetime as dt, random
os.environ["DATABASE_URL"] = "sqlite:///${DB}"
import sys; sys.path.insert(0, ${JSON.stringify(BACKEND)})
from app.models import (SessionLocal, engine, Base, Competition, Match,
                        Prediction, UpdateLog, UserSettings)
Base.metadata.create_all(engine)
db = SessionLocal(); random.seed(11)
c = Competition(code="epl", name="EPL", name_zh="英超", data_source="x", is_active=True)
db.add(c); db.commit()
today = dt.date.today()
for i in range(12):
    db.add(Match(competition_id=c.id, date=today + dt.timedelta(days=i % 5 + 1),
                 team1=f"Home {i}", team2=f"Away {i}", status="upcoming", time_utc="19:30"))
db.commit()
for m in db.query(Match).all():
    h = random.uniform(0.34, 0.62)
    d = random.uniform(0.18, 0.28)
    db.add(Prediction(match_id=m.id, prob_home=h, prob_draw=d, prob_away=1 - h - d,
                      predicted="win1"))
db.add(UpdateLog(ran_at=dt.datetime.utcnow(), status="ok"))
db.commit()
s = db.query(UserSettings).filter_by(setting_key="local").first()
if not s:
    s = UserSettings(setting_key="local"); db.add(s)
s.bankroll_total = 10000; s.kelly_fraction = 0.5; s.max_bet_pct = 0.15
db.commit()
print("seeded", db.query(Match).count(), "matches")
db.close()
`], { cwd: BACKEND, encoding: "utf8", stdio: ["pipe", "inherit", "inherit"] });

// ── 把真正的 recomputeCombo 打包出来 ────────────────────────
const esbuild = await import(path.join(NM, "esbuild", "lib", "main.js"));
const SHADOW = path.join(FRONTEND, "src", ".__test_leg_entry.jsx");
fs.writeFileSync(SHADOW,
  fs.readFileSync(path.join(FRONTEND, "src", "App.jsx"), "utf8")
  + "\nexport { recomputeCombo, parlayKellyPct, buildLegOddsWriteback };\n");
const stub = {
  name: "stub",
  setup(b) {
    b.onResolve({ filter: /^\.\/auth$|^recharts$/ }, a => ({ path: a.path, namespace: "stub" }));
    b.onLoad({ filter: /.*/, namespace: "stub" }, a => ({
      contents: a.path === "recharts"
        ? "export const LineChart=()=>null,Line=()=>null,XAxis=()=>null,YAxis=()=>null,CartesianGrid=()=>null,Tooltip=()=>null,ResponsiveContainer=()=>null,ReferenceLine=()=>null;"
        : "export const isAuthEnabled=false,supabase=null,supabaseUrl='',supabaseKeyHint='';"
          + "export const getToken=async()=>null,signIn=async()=>({}),signUp=async()=>({}),signOut=async()=>({});",
      loader: "js",
    }));
  },
};
try {
  await esbuild.build({
    entryPoints: [SHADOW], bundle: true, format: "esm", outfile: OUT, logLevel: "silent",
    jsx: "automatic", loader: { ".js": "jsx", ".jsx": "jsx" },
    external: ["react", "react-dom", "react/jsx-runtime", "react-dom/client"],
    define: { "import.meta.env": JSON.stringify({ DEV: false, VITE_API_BASE: BASE }) },
    plugins: [stub],
  });
} finally { fs.unlinkSync(SHADOW); }

const srv = spawn("python3", ["-m", "uvicorn", "app.main:app", "--port", String(PORT),
                               "--host", "127.0.0.1", "--log-level", "warning"],
  { cwd: BACKEND, env: { ...process.env, DATABASE_URL: `sqlite:///${DB}` } });
let srvErr = ""; srv.stderr.on("data", d => { srvErr += d; });

try {
  const t0 = Date.now();
  let up = false;
  while (Date.now() - t0 < 40000) {
    try { if ((await fetch(`${BASE}/health`)).ok) { up = true; break; } } catch { /* 还没起 */ }
    await new Promise(r => setTimeout(r, 300));
  }
  if (!up) throw new Error("后端起不来:\n" + srvErr);

  const { recomputeCombo, buildLegOddsWriteback } = await import(OUT + `?t=${Date.now()}`);
  const settings = await (await fetch(`${BASE}/settings`)).json();

  const matches = (await (await fetch(`${BASE}/matches?status_filter=upcoming`)).json());
  const payload = {
    min_legs: 3, max_legs: 5,
    matches: matches.slice(0, 10).map((m, k) => ({
      match_id: m.id,
      odds_home: 2.0 + (k % 5) * 0.25,
      odds_draw: 3.2 + (k % 3) * 0.2,
      odds_away: 3.0 + (k % 4) * 0.3,
    })),
  };
  const res = await (await fetch(`${BASE}/parlay/suggest`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })).json();

  if (res.status !== "ok" || !res.combinations?.length) {
    throw new Error("后端没搜出组合，测不下去: " + JSON.stringify(res).slice(0, 300));
  }
  console.log(`后端搜出 ${res.combinations.length} 个组合，每个 ${res.combinations[0].legs.length} 腿起\n`);

  console.log("【1】没改价时，前端重算必须跟后端返回的逐位相等");
  let allSame = true, firstBad = "";
  for (const [i, combo] of res.combinations.entries()) {
    const r = recomputeCombo(combo, undefined, settings);
    const same = r.combinedOdds === combo.combined_odds
              && r.ev === combo.ev
              && r.kellyPct === combo.kelly_pct
              && r.kellyAmount === combo.kelly_amount
              && r.edited === false;
    if (!same && !firstBad) {
      firstBad = `第 ${i} 个：前端 ${JSON.stringify({ o: r.combinedOdds, ev: r.ev, k: r.kellyPct, a: r.kellyAmount })}`
               + ` vs 后端 ${JSON.stringify({ o: combo.combined_odds, ev: combo.ev, k: combo.kelly_pct, a: combo.kelly_amount })}`;
    }
    allSame = allSame && same;
  }
  check(`${res.combinations.length} 个组合全部逐位一致`, allSame, firstBad);
  check("未改价时 edited 为 false（不会误标成已改）",
        res.combinations.every(c => recomputeCombo(c, {}, settings).edited === false));
  check("空串/undefined 当作没改",
        recomputeCombo(res.combinations[0], { 0: "" }, settings).edited === false);

  console.log("\n【2】改价后，跟后端自己的公式对答案");
  const combo = res.combinations[0];
  const cases = [
    { 0: "1.50" },                       // 砍一腿
    { 0: "9.00" },                       // 抬一腿
    { 0: "1.80", 1: "2.60" },            // 改两腿
    Object.fromEntries(combo.legs.map((_, j) => [j, (1.4 + j * 0.37).toFixed(2)])),  // 全改
  ];
  const jsOut = cases.map(c => {
    const r = recomputeCombo(combo, c, settings);
    return { odds: r.list, combinedOdds: r.combinedOdds, ev: r.ev,
             kellyPct: r.kellyPct, kellyAmount: r.kellyAmount };
  });
  const pyOut = JSON.parse(execFileSync("python3", ["-c", `
import json, sys
sys.path.insert(0, ${JSON.stringify(BACKEND)})
from app.model import expected_value, kelly_pct
cases = json.loads(sys.argv[1]); p = ${combo.joint_probability}
frac, cap, bank = ${settings.kelly_fraction}, ${settings.max_bet_pct}, ${settings.bankroll_total}
out = []
for odds in cases:
    co = round(eval("*".join(repr(o) for o in odds)), 3)
    k = round(kelly_pct(p, co, frac, cap), 4)
    out.append({"combinedOdds": co, "ev": round(expected_value(p, co), 4),
                "kellyPct": k, "kellyAmount": round(k * bank, 2)})
print(json.dumps(out))
`, JSON.stringify(jsOut.map(x => x.odds))], { cwd: BACKEND, encoding: "utf8" }));

  for (const [i, js] of jsOut.entries()) {
    const py = pyOut[i];
    check(`用例 ${i + 1} @${js.odds.join("×")} → 联合赔率/EV/凯利 三项全对`,
          js.combinedOdds === py.combinedOdds && js.ev === py.ev
          && js.kellyPct === py.kellyPct && js.kellyAmount === py.kellyAmount,
          `前端 ${JSON.stringify(js)} vs 后端 ${JSON.stringify(py)}`);
  }

  console.log("\n【3】CLAUDE.md 硬约束：EV = 模型概率 × 市场原始赔率 - 1");
  // 概率必须原样透传。反过来用赔率去推概率（p = 1/赔率，去抽水后归一）
  // 算出来的 EV 恒等于负的抽水，是循环论证——这里明确验它**没有**这么做。
  const p = combo.joint_probability;
  // 从两个差很远的赔率各反解一次概率，都必须解回同一个 p——如果前端偷偷
  // 用赔率去推概率，这两次解出来的会跟着赔率跑，不可能相等。
  // ev 是舍到 4 位的，所以反解有 5e-5/赔率 的固有误差，容差按它给。
  for (const o of ["1.50", "5.00", "12.00"]) {
    const rr = recomputeCombo(combo, { 0: o }, settings);
    const solved = (rr.ev + 1) / rr.combinedOdds;
    check(`第一腿改成 ${o} 时，反解出的概率仍是模型那个 ${p}`,
          Math.abs(solved - p) < 5e-5 / rr.combinedOdds + 1e-12,
          `反解得 ${solved}`);
  }
  // 循环用法（p = 去抽水后的市场概率）算出来的 EV 恒等于负的抽水，
  // 跟赔率高低无关。这里验它**没有**退化成那个。
  const r5 = recomputeCombo(combo, { 0: "5.00" }, settings);
  check("EV 不是那个循环解（1/赔率 × 赔率 - 1 恒为 0）",
        Math.abs(r5.ev - 0) > 1e-6, `EV=${r5.ev}`);

  console.log("\n【4】乱填的输入不能把卡片算成 NaN");
  for (const bad of [".", "-", "abc", "0", "1", "-3", "1.0"]) {
    const rb = recomputeCombo(combo, { 0: bad }, settings);
    const ok = Number.isFinite(rb.combinedOdds) && Number.isFinite(rb.ev)
            && Number.isFinite(rb.kellyAmount) && rb.list[0] === combo.legs[0].odds;
    check(`输入 "${bad}" → 退回原价，数字仍然有效`, ok, JSON.stringify(rb.list));
  }
  check("赔率 > 1 才算有效改动（2.50 被接受）",
        recomputeCombo(combo, { 0: "2.50" }, settings).list[0] === 2.5);

  console.log("\n【5】单腿转负时要能被识别出来（界面上要红字提示）");
  const low = recomputeCombo(combo, { 0: "1.01" }, settings);
  check("1.01 的腿被标成负 EV", low.negativeLegs.some(x => x.j === 0),
        JSON.stringify(low.negativeLegs.map(x => x.j)));
  check("原样时一条负 EV 腿都没有（推荐本来就只收正 EV 腿）",
        recomputeCombo(combo, undefined, settings).negativeLegs.length === 0);

  console.log("\n【6】凯利在负 EV 时必须归零，不能推荐下注");
  // 全部腿都压到 1.01：联合赔率约 1.05，而联合概率只有几个百分点，
  // EV 必然深度为负。只压两腿的话，5 腿串关剩下的高赔率还能把整体拉回正，
  // 那就测不到这一条（第一版就是这么写的，EV 还是 +0.0968）。
  const neg = recomputeCombo(
    combo, Object.fromEntries(combo.legs.map((_, j) => [j, "1.01"])), settings);
  check("EV 为负", neg.ev < 0, `实际 ${neg.ev}`);
  check("凯利建议金额为 0", neg.kellyAmount === 0, `实际 ${neg.kellyAmount}`);
  // ── 存回系统：只覆盖改的那一个，另外两个保留 ──────────────
  console.log("\n【7】把改过的价存回系统");
  // 界面上那三个输入框的现值。串关页就是拿它初始化的：自己填过的价优先，
  // 没填过用市场平均价兜底。存回时另外两个结果的价从这里取。
  const oddsState = {};
  for (const leg of combo.legs) {
    oddsState[leg.match_id] = { home: "1.11", draw: "2.22", away: "3.33" };
  }
  const leg0 = combo.legs[0];
  const FIELD = { home: "odds_home", draw: "odds_draw", away: "odds_away" };
  const ORIG = { odds_home: 1.11, odds_draw: 2.22, odds_away: 3.33 };
  const f0 = FIELD[leg0.outcome];
  const keep0 = ["odds_home", "odds_draw", "odds_away"].filter(x => x !== f0);

  const w1 = buildLegOddsWriteback(combo, { 0: "4.44" }, oddsState, settings);
  check("只有改过的那一条腿被收进来", w1.items.length === 1, `实际 ${w1.items.length} 条`);
  check("没有缺价被跳过的", w1.missing.length === 0);
  if (w1.items.length === 1) {
    const it = w1.items[0];
    check(`改的那一路（${leg0.outcome}）被覆盖成 4.44`, it[f0] === 4.44, JSON.stringify(it));
    check(`另外两路（${keep0.join("/")}）原样保留`,
          keep0.every(f => it[f] === ORIG[f]), JSON.stringify(it));
    check("match_id 对得上", it.match_id === leg0.match_id);
  }

  const w2 = buildLegOddsWriteback(combo, { 0: "4.44", 2: "5.55" }, oddsState, settings);
  check("改两条腿就出两条（分属两场比赛）", w2.items.length === 2, `实际 ${w2.items.length}`);
  check("两条的 match_id 不同", w2.items[0]?.match_id !== w2.items[1]?.match_id);
  check("没改的腿一条都没混进来",
        !w2.items.some(it => it.match_id === combo.legs[1].match_id));
  check("什么都没改时不产生任何条目",
        buildLegOddsWriteback(combo, undefined, oddsState, settings).items.length === 0);

  console.log("\n【8】同场另一边缺价时宁可跳过，也不瞎填");
  const holed = { [leg0.match_id]: { home: "", draw: "", away: "" } };
  const w3 = buildLegOddsWriteback(combo, { 0: "4.44" }, holed, settings);
  check("被放进 missing 而不是硬存", w3.items.length === 0 && w3.missing.length === 1,
        `items=${w3.items.length} missing=${w3.missing.length}`);
  // 平局价缺是允许的：有人只填主客
  const noDraw = { [leg0.match_id]: { home: "1.11", draw: "", away: "3.33" } };
  const w4 = buildLegOddsWriteback(combo, { 0: "4.44" }, noDraw, settings);
  check("只缺平局价时照存，平局记为 null",
        w4.items.length === 1 && (leg0.outcome === "draw" || w4.items[0].odds_draw === null),
        JSON.stringify(w4.items[0]));

  console.log("\n【9】真的存进去，再从 /api/matches 读回来核对");
  const before = await (await fetch(`${BASE}/matches?status_filter=upcoming`)).json();
  const b0 = before.find(m => m.id === leg0.match_id);
  check("存之前这场没有 latest_odds（干净起点）", !b0.latest_odds, JSON.stringify(b0.latest_odds));

  const saveRes = await (await fetch(`${BASE}/odds/bulk`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items: w1.items }),
  })).json();
  check("后端确认存了 1 条", saveRes.saved === 1, JSON.stringify(saveRes));

  const after = await (await fetch(`${BASE}/matches?status_filter=upcoming`)).json();
  const a0 = after.find(m => m.id === leg0.match_id);
  check("现在读得到 latest_odds", !!a0.latest_odds, JSON.stringify(a0.latest_odds));
  if (a0.latest_odds) {
    check("读回来改的那一路是 4.44", a0.latest_odds[f0] === 4.44, JSON.stringify(a0.latest_odds));
    check("读回来另外两路还是原值",
          keep0.every(x => a0.latest_odds[x] === ORIG[x]), JSON.stringify(a0.latest_odds));
  }
  check("别的比赛没被牵连",
        after.filter(m => m.latest_odds).length === 1,
        `有 ${after.filter(m => m.latest_odds).length} 场有 latest_odds，应该只有 1 场`);

  // 再存一次不同的价 —— Odds 是追加表，读的时候取最新那条
  const w5 = buildLegOddsWriteback(combo, { 0: "6.66" }, oddsState, settings);
  await fetch(`${BASE}/odds/bulk`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items: w5.items }),
  });
  const after2 = await (await fetch(`${BASE}/matches?status_filter=upcoming`)).json();
  const a2 = after2.find(m => m.id === leg0.match_id);
  check("存第二次后读到的是最新那条 6.66（存错了可以再存一次改回来）",
        a2.latest_odds?.[f0] === 6.66, JSON.stringify(a2.latest_odds));
} finally {
  srv.kill("SIGTERM");
  try { fs.unlinkSync(OUT); } catch { /* 已删 */ }
}

console.log(failed === 0 ? "\n全部通过。" : `\n${failed} 项失败。`);
process.exit(failed === 0 ? 0 : 1);
