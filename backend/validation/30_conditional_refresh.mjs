/**
 * 条件请求端到端：真前端函数 × 真后端进程 × 真 HTTP。
 *
 * 29 号测的是后端的 ETag 不会漏。这一份测另一半——前端**真的用上了**它。
 * 这两件事分开验有具体理由：ETag 完全正确、而前端因为跨域读不到响应头，
 * 是这个优化最可能的失效方式，且不报任何错，只是悄悄退化成每次全量下载。
 *
 * 所以这里不 mock fetch，也不重写 apiConditional：
 *   · 用 esbuild 把 App.jsx 里真正的 apiConditional 导出来
 *   · 起一个真的 uvicorn，DATABASE_URL 指向临时库
 *   · 让前端函数经 undici 真的发 HTTP 请求过去
 *
 * 跑：node backend/validation/30_conditional_refresh.mjs
 * 依赖：frontend/node_modules 里的 esbuild；后端能起 uvicorn
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
const APP = path.join(FRONTEND, "src", "App.jsx");
const OUT = path.join(NM, ".vb-cond-bundle.mjs");

const PORT = 8931;
const BASE = `http://127.0.0.1:${PORT}/api`;
const DB = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "vb-cond-")), "cond.db");

let failed = 0;
const check = (name, cond, extra = "") => {
  if (cond) console.log(`  ✅ ${name}`);
  else { console.log(`  ❌ ${name}${extra ? " —— " + extra : ""}`); failed += 1; }
};

// ── 建一个有数据的临时库 ─────────────────────────────────────
const SEED = `
import os, datetime as dt
os.environ["DATABASE_URL"] = "sqlite:///${DB}"
import sys; sys.path.insert(0, ${JSON.stringify(BACKEND)})
from app.models import SessionLocal, engine, Base, Competition, Match, Prediction, UpdateLog
Base.metadata.create_all(engine)
db = SessionLocal()
if not db.query(Competition).first():
    c = Competition(code="epl", name="EPL", name_zh="英超", data_source="x", is_active=True)
    db.add(c); db.commit()
    today = dt.date.today()
    for i in range(200):
        m = Match(competition_id=c.id, date=today - dt.timedelta(days=i+1),
                  team1=f"Team {i}", team2=f"Team {i+1}", score1=1, score2=0,
                  status="played", time_utc="19:30")
        db.add(m)
    db.commit()
    for m in db.query(Match).all():
        db.add(Prediction(match_id=m.id, prob_home=.5, prob_draw=.25, prob_away=.25,
                          predicted="win1", is_correct=True, rps=0.15))
    db.add(UpdateLog(ran_at=dt.datetime.utcnow(), status="ok"))
    db.commit()
print("seeded", db.query(Match).count())
db.close()
`;
console.log("条件请求端到端：真前端函数 × 真后端进程 × 真 HTTP\n");
console.log(execFileSync("python3", ["-c", SEED], { cwd: BACKEND, encoding: "utf8" }).trim());

// ── 打包出真正的 apiConditional ──────────────────────────────
const esbuild = await import(path.join(NM, "esbuild", "lib", "main.js"));
const SHADOW = path.join(FRONTEND, "src", ".__test_cond_entry.jsx");
fs.writeFileSync(SHADOW, fs.readFileSync(APP, "utf8") + "\nexport { apiConditional, api };\n");
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
    // 关键：把前端的 API 基址指到这次起的后端上，走真实 HTTP
    define: { "import.meta.env": JSON.stringify({ DEV: false, VITE_API_BASE: BASE }) },
    plugins: [stub],
  });
} finally { fs.unlinkSync(SHADOW); }

// ── 起真的后端 ───────────────────────────────────────────────
const srv = spawn("python3", ["-m", "uvicorn", "app.main:app", "--port", String(PORT),
                               "--host", "127.0.0.1", "--log-level", "warning"],
  { cwd: BACKEND, env: { ...process.env, DATABASE_URL: `sqlite:///${DB}` } });
let srvErr = "";
srv.stderr.on("data", d => { srvErr += d; });

async function waitUp(timeoutMs = 40000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    try {
      const r = await fetch(`${BASE}/health`);
      if (r.ok) return true;
    } catch { /* 还没起来 */ }
    await new Promise(r => setTimeout(r, 300));
  }
  return false;
}

try {
  if (!await waitUp()) throw new Error("后端起不来:\n" + srvErr);
  const { apiConditional } = await import(OUT + `?t=${Date.now()}`);

  console.log("\n【1】第一次请求：拿到数据和 ETag");
  const r1 = await apiConditional("/matches", null);
  check("回了数据", Array.isArray(r1.data) && r1.data.length === 200,
        `实际 ${Array.isArray(r1.data) ? r1.data.length : typeof r1.data}`);
  check("读得到 ETag", !!r1.etag, "跨域没暴露 ETag 的话这里就是 null，优化会静默失效");
  check("不是 304", !r1.notModified);

  console.log("\n【2】带上 ETag 再请求：应该 304，且不回数据");
  const r2 = await apiConditional("/matches", r1.etag);
  check("notModified 为真", r2.notModified === true);
  check("没有 data 字段（前端要沿用缓存）", r2.data === undefined);
  check("ETag 原样带回", r2.etag === r1.etag);

  console.log("\n【3】量一下真实省了多少");
  const t0 = Date.now(); await apiConditional("/matches", null); const tFull = Date.now() - t0;
  const t1 = Date.now(); await apiConditional("/matches", r1.etag); const t304 = Date.now() - t1;
  const bytes = (await (await fetch(`${BASE}/matches`)).text()).length;
  console.log(`     全量 ${tFull} ms / ${(bytes / 1024).toFixed(0)} KB`);
  console.log(`     304  ${t304} ms / 0 KB`);
  check("304 确实更快", t304 <= tFull, `304 ${t304}ms 反而不比全量 ${tFull}ms 快`);

  console.log("\n【4】数据变了以后必须重新拿到全量");
  execFileSync("python3", ["-c", `
import os
os.environ["DATABASE_URL"] = "sqlite:///${DB}"
import sys; sys.path.insert(0, ${JSON.stringify(BACKEND)})
import datetime as dt
from app.models import SessionLocal, Match, Competition
db = SessionLocal()
c = db.query(Competition).first()
db.add(Match(competition_id=c.id, date=dt.date.today() + dt.timedelta(days=3),
             team1="New Home", team2="New Away", status="upcoming", time_utc="19:30"))
db.commit(); db.close()
`], { cwd: BACKEND, encoding: "utf8" });
  const r3 = await apiConditional("/matches", r1.etag);
  check("不再是 304", !r3.notModified, "新增了一场比赛却还回 304 —— 用户看不到新赛程");
  check("拿到 201 场", r3.data?.length === 201, `实际 ${r3.data?.length}`);
  check("ETag 换了新的", r3.etag && r3.etag !== r1.etag);
  check("新 ETag 立刻能命中 304",
        (await apiConditional("/matches", r3.etag)).notModified === true);

  console.log("\n【5】backtest-summary 同样走通");
  const b1 = await apiConditional("/backtest-summary", null);
  check("拿到 by_competition", Array.isArray(b1.data?.by_competition));
  check("有 ETag", !!b1.etag);
  check("带 ETag 回 304",
        (await apiConditional("/backtest-summary", b1.etag)).notModified === true);

  console.log("\n【6】ETag 对不上时退回全量，不报错");
  const r4 = await apiConditional("/matches", '"garbage"');
  check("回 200 带数据", Array.isArray(r4.data) && r4.data.length === 201);

  // ── 跨域：线上真正跑的那条路 ──────────────────────────────
  // 上面几步是 node 直接请求，没有 Origin 头，CORS 中间件根本不介入，
  // 所以 res.headers.get('ETag') 一定读得到——**这恰恰测不到生产环境**。
  // 线上前端在 Vercel、后端在 Render，是跨域的：浏览器默认只把六个
  // 「简单」响应头交给 JS，ETag 不在其中。没有
  // Access-Control-Expose-Headers: ETag，前端拿到的就是 null，
  // 于是每次都当作"没有 ETag"重新下载全量——不报错，只是白做。
  console.log("\n【7】跨域时 ETag 必须被显式暴露（线上是 Vercel→Render 跨域）");
  const ORIGIN = "http://localhost:5173";     // CORS 白名单里的开发源
  const xr = await fetch(`${BASE}/matches`, { headers: { Origin: ORIGIN } });
  const expose = xr.headers.get("access-control-expose-headers") || "";
  check("响应带 Access-Control-Allow-Origin",
        !!xr.headers.get("access-control-allow-origin"),
        "这个源不在 CORS 白名单里，下面那条断言就没意义了");
  check("Access-Control-Expose-Headers 含 ETag",
        expose.toLowerCase().split(",").map(x => x.trim()).includes("etag"),
        `实际是 "${expose}" —— 浏览器会读不到 ETag，前端每次都全量下载`);

  const pre = await fetch(`${BASE}/matches`, {
    method: "OPTIONS",
    headers: { Origin: ORIGIN, "Access-Control-Request-Method": "GET",
               "Access-Control-Request-Headers": "if-none-match, authorization" },
  });
  const allowHdr = (pre.headers.get("access-control-allow-headers") || "").toLowerCase();
  check("预检放行 If-None-Match 请求头",
        pre.status < 400 && (allowHdr.includes("if-none-match") || allowHdr.includes("*")),
        `预检 ${pre.status}，allow-headers="${allowHdr}"`);
} finally {
  srv.kill("SIGTERM");
  try { fs.unlinkSync(OUT); } catch { /* 已经删了 */ }
}

console.log(failed === 0 ? "\n全部通过。" : `\n${failed} 项失败。`);
process.exit(failed === 0 ? 0 : 1);
