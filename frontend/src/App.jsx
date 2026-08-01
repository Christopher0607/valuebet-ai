import { useState, useEffect, useCallback, useRef, useMemo } from "react";
import { isAuthEnabled, supabase, getToken, signIn, signUp, signOut, supabaseUrl, supabaseKeyHint } from "./auth";
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine } from "recharts";

// 打包后走同源相对路径，不写死主机名——后端本来就在托管这份前端，
// 所以页面从哪个地址打开，API 就跟着走到哪台机器。
// 这是手机能用的前提：写死 127.0.0.1 的话，手机上那个地址指的是手机自己。
// vite 开发服务器（5173）是另一个源，那时才需要显式指向后端。
// 三种部署形态，API 地址各不相同：
//   1. 本地一键启动 —— 后端自己托管前端，同源相对路径 /api
//   2. vite 开发 —— 前端在 5173、后端在 8000，是两个源
//   3. 云端 —— 前端在 Vercel、后端在 Railway，完全不同的域名，
//      由 VITE_API_BASE 指定（例如 https://valuebet.up.railway.app/api）
const API = import.meta.env.VITE_API_BASE
  ? import.meta.env.VITE_API_BASE.replace(/\/$/, "")
  : (import.meta.env.DEV ? "http://127.0.0.1:8000/api" : "/api");

async function api(path, opts) {
  // 启用认证时每个请求都要带令牌。getToken 会在令牌快过期时自动续期，
  // 所以这里不需要自己管刷新。
  const token = await getToken();
  const res = await fetch(API + path, {
    ...opts,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(opts?.headers || {}),
    },
  });
  if (res.status === 401) {
    // 令牌失效：抛一个可识别的错误，App 会切回登录页而不是显示
    // 「后端连不上」——那句话在这里是错的，后端好得很，是你没登录。
    const err = new Error("UNAUTHORIZED");
    err.unauthorized = true;
    throw err;
  }
  if (!res.ok) throw new Error(`API ${path} failed: ${res.status}`);
  return res.json();
}

// ── Formatting ─────────────────────────────────────────────
const pct  = v => v == null ? "—" : (v * 100).toFixed(1) + "%";
const fev  = v => v == null ? "—" : (v >= 0 ? "+" : "") + (v * 100).toFixed(1) + "%";
const fnum = v => v == null ? "—" : (v >= 0 ? "+" : "") + Math.round(v).toLocaleString("zh-HK");
const fod  = v => v ? (+v).toFixed(2) : "—";
const fdt  = d => new Date(d + "T12:00:00").toLocaleDateString("zh-HK", { month: "short", day: "numeric" });
const fdatetime = iso => iso ? new Date(iso).toLocaleString("zh-HK", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";

// ── Colors ─────────────────────────────────────────────────
const C = {
  bg: "#080d14", surface: "#0e1520", card: "#121c2a", border: "#1a2840",
  accent: "#00c896", accentDim: "#00c89615",
  gold: "#f0b429", goldDim: "#f0b42915",
  red: "#e8365d", redDim: "#e8365d15",
  blue: "#4e9eff", blueDim: "#4e9eff15",
  purple: "#a78bfa", purpleDim: "#a78bfa15",
  text: "#dde6f0", textDim: "#5a7a9a", muted: "#1e2d42",
};
const evc  = v => v > 0.04 ? C.accent : v > 0 ? C.gold : C.red;
const evbg = v => v > 0.04 ? C.accentDim : v > 0 ? C.goldDim : C.redDim;

// ══════════════════════════════════════════════════════════
export default function App() {
  const [tab, setTab]       = useState("upcoming");
  const [status, setStatus] = useState(null);
  const [matches, setMatches] = useState([]);
  const [backtestByComp, setBacktestByComp] = useState([]);
  const [competitions, setCompetitions] = useState([]);
  const [comp, setComp] = useState(null);        // null = 全部赛事
  const [bets, setBets]     = useState([]);
  const [realBets, setRealBets] = useState([]);
  const [bankroll, setBankroll] = useState(null);
  const [settings, setSettings] = useState(null);
  const [parlayBets, setParlayBets] = useState([]);
  const [showSett, setShowSett] = useState(false);
  const [loading, setLoading]   = useState(true);
  const [apiError, setApiError] = useState(null);
  const [updating, setUpdating] = useState(false);
  const [starting, setStarting] = useState(false);   // 后端还在启动中
  const [session, setSession] = useState(null);
  const [authReady, setAuthReady] = useState(!isAuthEnabled);
  const retryRef = useRef(0);

  const loadAll = useCallback(async () => {
    setLoading(true);
    setApiError(null);
    try {
      const [st, all, bt, vb, rb, br, se, cp, pl] = await Promise.all([
        api("/status"),
        api("/matches"),
        api("/backtest-summary"),
        api("/bets"),
        api("/real-bets"),
        api("/bankroll-summary"),
        api("/settings"),
        // 赛事名单单独取：backtest-summary 里的 by_competition 会跳过
        // 「还没有已完赛比赛」的赛事，拿它当赛事全集会漏掉新赛事
        api("/competitions"),
        // 串关注单。原来只拉 /real-bets（RealBet 表），而串关记在 ParlayBet
        // 表里，两张表从不汇合——实盘页因此看不到任何串关，尽管资金曲线
        // （/bankroll-summary）一直把串关算进去了。用户反馈的
        // 「串关的下注和回报没有记录进实盘」就是这个。
        api("/parlay-bets"),
      ]);
      setStatus(st);
      setMatches(all);
      setBacktestByComp(bt.by_competition || []);
      setCompetitions(cp || []);
      setBets(vb);
      setRealBets(rb);
      setBankroll(br);
      setSettings(se);
      setParlayBets(pl || []);
    } catch (e) {
      // 刚启动那十几秒后端可能还没就绪（uvicorn 还没绑定端口，或者首次
      // 全量更新正在写库）。直接甩「无法连接本地后端」会让人以为服务没起，
      // 其实再等几秒就好了。所以先默默重试几轮，真连不上才报错。
      if (e.unauthorized) {          // 没登录/令牌过期 —— 不是后端挂了
        setSession(null); setApiError(null); setStarting(false);
        setLoading(false);
        return;
      }
      if (retryRef.current < 6) {
        retryRef.current += 1;
        setStarting(true);
        setTimeout(() => loadAll(), 2500);
        return;                       // 不要走 finally 里的 setLoading(false)
      }
      setApiError(e.message);
      setStarting(false);
    } finally {
      setLoading(false);
    }
    retryRef.current = 0;
    setStarting(false);
  }, []);

  // 恢复已有会话，并把 authReady 置位。
  //
  // 为什么需要 authReady 这个中间态：supabase-js 把令牌存在 localStorage，
  // 但读回来是异步的。在读完之前，session 是 null——此时如果直接按
  // 「没登录」处理，每次刷新页面都会先闪一下登录页，已登录的用户也会
  // 被当成未登录。所以 authReady 为 false 时既不进主界面也不进登录页。
  //
  // 这个 effect 之前是缺的（setAuthReady 在整个文件里没有任何调用点），
  // 于是云端 authReady 恒为 false，页面永远停在那个转圈上，连登录页
  // 都到不了。本地模式看不出来：isAuthEnabled 为 false 时初始值就是 true。
  // 教训跟 04 号文档里那些一样——本地跑得好好的，只有真部署才暴露。
  useEffect(() => {
    if (!isAuthEnabled) return;
    let cancelled = false;

    // 兜底：不管 getSession 成功、失败还是卡住，都必须让 authReady 变 true，
    // 否则又回到「永远转圈」。拿不到会话就当没登录，去登录页重新登。
    const settle = (s) => {
      if (cancelled) return;
      setSession(s ?? null);
      setAuthReady(true);
    };

    supabase.auth.getSession()
      .then(({ data }) => settle(data?.session))
      .catch(() => settle(null));

    // 令牌自动续期、在别的标签页登出等情况都从这里回调
    const { data: sub } = supabase.auth.onAuthStateChange((_evt, s) => settle(s));

    return () => { cancelled = true; sub?.subscription?.unsubscribe(); };
  }, []);

  // 拉数据。云端要等「认证已就绪且已登录」，否则第一次请求必然 401。
  // 这个依赖数组也是登录成功后自动加载数据的触发点——原来依赖是空的，
  // 只在挂载时跑一次（那次还没登录），登录后界面会一直空着。
  useEffect(() => {
    if (isAuthEnabled && (!authReady || !session)) return;
    loadAll();
  }, [authReady, session, loadAll]);

  // If we land in the "first run still in flight" state (see StatusBanner),
  // poll briefly until it resolves so the page updates itself without the
  // user needing to know to click refresh. Stops after the first successful
  // update or after ~20s so it never turns into a permanent background poll.
  useEffect(() => {
    if (!status) return;
    const stillFirstRun = status.last_update == null && status.last_status == null;
    if (!stillFirstRun) return;

    let cancelled = false;
    let elapsed = 0;
    const interval = setInterval(async () => {
      elapsed += 2000;
      if (cancelled || elapsed > 20000) {
        clearInterval(interval);
        return;
      }
      const s = await api("/status").catch(() => null);
      if (s && !cancelled && (s.last_update != null || s.last_status != null)) {
        clearInterval(interval);
        loadAll();
      }
    }, 2000);

    return () => { cancelled = true; clearInterval(interval); };
  }, [status, loadAll]);

  async function triggerUpdate() {
    setUpdating(true);
    try {
      await api("/update-now", { method: "POST" });
      await loadAll();
    } catch (e) {
      setApiError(e.message);
    } finally {
      setUpdating(false);
    }
  }

  async function saveSettings(s) {
    await api("/settings", { method: "PUT", body: JSON.stringify(s) });
    setSettings(s);
    setShowSett(false);
    await loadAll();
  }

  // 比赛日期已经过去、却还挂着 upcoming 的，是抓不到比分留下的陈旧记录，
  // 不是即将赛事。实测撞到过：2025-05-31 的欧冠决赛（PSG vs 国米）因为没有
  // 比分，一直被当成「接下来 1 场」显示，而那已经是 14 个月前的事。
  // 把它们分出来单独提示，而不是混进 upcoming 里让人以为有比赛可下。
  // ── 赛事筛选 ────────────────────────────────────────────
  // comp === null 表示「全部赛事」。注意：即使选了全部，顶部统计栏也只显示
  // **单个赛事**的数字，绝不跨赛事求和或求平均——不同赛事的准确率和 RPS
  // 不可比（世界杯 67% vs 英超 52% 是赛事难度差异，不是模型好坏），
  // 平均出来是个没有意义的数。后端 /api/backtest-summary 的注释里也写了
  // 「不做任何跨赛事聚合」，前端在这里合并回去等于把那个 bug 重新引入。
  // 所以「全部」模式下统计栏取第一个赛事并**标明是哪一个**，而不是假装是总体。
  const _today = new Date().toISOString().slice(0, 10);
  const inComp = m => comp == null || m.competition_id === comp;

  const upcoming = matches.filter(m => m.status === "upcoming" && m.date >= _today && inComp(m));

  // 「日期暂定」：同一赛事同一轮的比赛全挤在同一个日期同一个时间，说明上游
  // 还没排具体日程。西甲 2026-27 第 1 轮就是 10 场全写 08-16 17:00；而英超
  // 第 1 轮是排好的，分散在 8/21-8/24、时间各不相同。
  // 不标出来的话，用户拿系统的日期去跟别处（按自己时区显示真实开球时间的
  // 网站）对，会以为我们把不相干的比赛混进了同一天——实际就是这么误会的。
  const provisionalRounds = useMemo(() => {
    const byRound = new Map();
    for (const m of matches) {
      if (m.status !== "upcoming") continue;
      const k = `${m.competition_id}|${m.round}`;
      if (!byRound.has(k)) byRound.set(k, []);
      byRound.get(k).push(m);
    }
    const out = new Set();
    for (const [k, list] of byRound) {
      // 至少 4 场才判定——两三场同一时间开球是正常的
      if (list.length >= 4 && new Set(list.map(m => `${m.date} ${m.time_utc}`)).size === 1) out.add(k);
    }
    return out;
  }, [matches]);
  const isProvisional = m => provisionalRounds.has(`${m.competition_id}|${m.round}`);
  const stale    = matches.filter(m => m.status === "upcoming" && m.date < _today && inComp(m));
  const played   = matches.filter(m => m.status === "played" && inComp(m));
  // 注单可能没有 competition_id（老数据，或后端没回填），这时不过滤掉，
  // 宁可多显示也不要让用户以为注单丢了
  const shownBets     = bets.filter(b => comp == null || b.competition_id == null || b.competition_id === comp);
  const shownRealBets = realBets.filter(b => comp == null || b.competition_id == null || b.competition_id === comp);

  // 「全部」模式下顶部统计栏显示什么。
  //
  // 原来是 backtestByComp[0]——后端按 Competition.id 升序返回，第一个是
  // 世界杯，于是选「全部」看到的其实是世界杯的准确率和 RPS。标签里虽然
  // 带了赛事名，但它挂在顶部总览的位置上，读起来就是全站数字。
  //
  // 改成按场次加权的合计：
  //   准确率   = 所有猜对的场次 / 所有场次
  //   平均 RPS = Σ(赛事均值 × 该赛事场次) / Σ场次，即全部比赛 RPS 的均值
  //
  // 跟 CLAUDE.md 里那条禁令的区别要说清楚，否则以后会有人把这段删掉：
  // 被禁的是**把不同赛事的比率直接平均**（世界杯 67% 和英超 52% 平均成
  // 59.5%，那个数哪个赛事都不代表），以及拿单个赛事冒充总体——就是这次
  // 的 bug。按场次加权的合计不属于这两种，它就是「这批比赛里猜对了多少」，
  // 是良定义的。
  //
  // 它真正的局限在别处：赛事构成一变这个数就跟着变（多抓一个联赛进来，
  // 总体准确率会朝那个联赛的难度移动），所以**不能拿它跟历史数字比长短**，
  // 也不能用它评价模型变好还是变坏。要比较就看「📊 回测」标签，那里是
  // 按赛事分开列的。
  const pooledBacktest = backtestByComp.length ? (() => {
    const total   = backtestByComp.reduce((s, b) => s + b.total, 0);
    const correct = backtestByComp.reduce((s, b) => s + b.correct, 0);
    if (!total) return null;
    return {
      total, correct,
      accuracy: correct / total,
      // 后端把每个赛事的 avg_rps 四舍五入到 4 位小数才传过来，所以这里
      // 加权还原出的总体均值有 ~1e-5 量级的误差。显示只到 3 位，无影响。
      avg_rps: backtestByComp.reduce((s, b) => s + b.avg_rps * b.total, 0) / total,
    };
  })() : null;

  const backtest = comp != null
    ? (backtestByComp.find(b => b.competition_id === comp) || null)
    : pooledBacktest;

  const backtestLabel = comp != null
    ? (backtestByComp.find(b => b.competition_id === comp)?.competition_name
       || competitions.find(c => c.id === comp)?.name_zh || "该赛事")
    : `全部${backtestByComp.length}赛事`;

  // 启用认证但还没登录 → 登录页。本地模式 authReady 一开始就是 true，
  // session 永远是 null，这个分支不会进，行为跟以前完全一样。
  if (isAuthEnabled && authReady && !session) {
    return <LoginScreen onSignedIn={s => { setSession(s); setApiError(null); }} />;
  }
  if (isAuthEnabled && !authReady) {
    return (
      <div style={{ minHeight: "100vh", background: C.bg, display: "flex", alignItems: "center", justifyContent: "center" }}>
        <div style={{ width: 28, height: 28, border: `3px solid ${C.border}`, borderTopColor: C.accent, borderRadius: "50%", animation: "spin 0.7s linear infinite" }} />
      </div>
    );
  }

  if (starting && !apiError) {
    return (
      <div style={{ minHeight: "100vh", background: C.bg, color: C.text, display: "flex", alignItems: "center", justifyContent: "center", fontFamily: "'Inter',system-ui,sans-serif", padding: 24 }}>
        <div style={{ textAlign: "center" }}>
          <div style={{ width: 30, height: 30, border: `3px solid ${C.border}`, borderTopColor: C.accent, borderRadius: "50%", animation: "spin 0.7s linear infinite", margin: "0 auto 14px" }} />
          <div style={{ fontWeight: 700, marginBottom: 6 }}>后端启动中…</div>
          <div style={{ fontSize: 12, color: C.textDim, maxWidth: 340, lineHeight: 1.7 }}>
            首次启动要抓取并计算 1700 多场比赛，通常十几秒到一分钟。
            这个页面会自动重连，不用手动刷新。
          </div>
        </div>
      </div>
    );
  }

  // ── Backend not running: clear, actionable error state ──
  if (apiError && !loading) {
    return (
      <div style={{ minHeight: "100vh", background: C.bg, color: C.text, display: "flex", alignItems: "center", justifyContent: "center", fontFamily: "'Inter',system-ui,sans-serif", padding: 24 }}>
        <div style={{ maxWidth: 480, background: C.card, border: `1px solid ${C.red}44`, borderRadius: 12, padding: 28 }}>
          <div style={{ fontSize: 32, marginBottom: 12 }}>⚠️</div>
          <div style={{ fontSize: 16, fontWeight: 800, marginBottom: 8 }}>无法连接本地后端</div>
          <div style={{ fontSize: 13, color: C.textDim, lineHeight: 1.7, marginBottom: 16 }}>
            前端正常运行，但无法访问 <code style={{ background: C.bg, padding: "2px 5px", borderRadius: 4 }}>{API}</code>。
            {/* 手机上不能提示 127.0.0.1——那指的是手机自己。这里回显真实用到的地址，
                并按「是不是从别的设备打开的」给不同的排查方向。 */}
            {typeof window !== "undefined" && !["localhost", "127.0.0.1"].includes(window.location.hostname) ? (
              <>
                <br />你是从另一台设备打开的，请检查：跑服务的电脑是否开着、
                两台设备是否连同一个 Wi-Fi、以及电脑防火墙是否放行了 8000 端口。
                服务必须以 <code style={{ background: C.bg, padding: "2px 5px", borderRadius: 4 }}>--host 0.0.0.0</code> 启动
                （用 <code style={{ background: C.bg, padding: "2px 5px", borderRadius: 4 }}>start.py</code> 的话已经是了）。
              </>
            ) : (
              <>
                请确认后端已启动：在 <code style={{ background: C.bg, padding: "2px 5px", borderRadius: 4 }}>backend/</code> 目录运行
                <code style={{ display: "block", background: C.bg, padding: "8px 10px", borderRadius: 6, marginTop: 8 }}>uvicorn app.main:app --host 0.0.0.0 --port 8000</code>
              </>
            )}
          </div>
          <button onClick={loadAll} style={{ padding: "8px 16px", borderRadius: 8, border: "none", background: C.accent, color: C.bg, fontWeight: 700, fontSize: 13 }}>
            重试连接
          </button>
        </div>
      </div>
    );
  }

  return (
    <div style={{ minHeight: "100vh", background: C.bg, color: C.text, fontFamily: "'Inter',system-ui,sans-serif", fontSize: 13 }}>
      <style>{`
        * { box-sizing: border-box; margin: 0; padding: 0; }
        button { cursor: pointer; }
        input, select { outline: none; }
        ::-webkit-scrollbar { width: 5px; }
        ::-webkit-scrollbar-track { background: ${C.bg}; }
        ::-webkit-scrollbar-thumb { background: ${C.border}; border-radius: 3px; }
        /* @keyframes spin 移到了 index.html —— 提前 return 的登录页/认证
           加载态渲染不到这个 style 标签，放这里那两处的圈不会转 */
        code { font-family: 'SF Mono', Consolas, monospace; }
      `}</style>

      {/* Status banner - honest about what "automatic" means here */}
      <StatusBanner status={status} updating={updating} onUpdateNow={triggerUpdate} />

      {/* Header */}
      <div style={{ background: C.surface, borderBottom: `1px solid ${C.border}`, padding: "11px 16px", position: "sticky", top: 0, zIndex: 30 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <div style={{ width: 32, height: 32, borderRadius: 8, background: `linear-gradient(135deg,${C.accent},${C.blue})`, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 16 }}>⚽</div>
            <div>
              <div style={{ fontWeight: 800, fontSize: 14, letterSpacing: "-0.3px" }}>ValueBet 精算系统</div>
              <div style={{ fontSize: 9, color: C.textDim, textTransform: "uppercase", letterSpacing: "0.7px" }}>{isAuthEnabled ? "云端版 · FastAPI + Postgres" : "本地版 · FastAPI + SQLite"}</div>
            </div>
          </div>
          <div style={{ display: "flex", gap: 6 }}>
            <button
              onClick={() => setShowSett(s => !s)}
              style={{ padding: "6px 12px", borderRadius: 7, border: `1px solid ${showSett ? C.purple : C.border}`, background: showSett ? C.purpleDim : "transparent", color: showSett ? C.purple : C.textDim, fontSize: 11, fontWeight: 700 }}
            >
              ⚙ 设置
            </button>
            {isAuthEnabled && session && (
              <button
                onClick={async () => { await signOut(); setSession(null); }}
                title={session.user?.email}
                style={{ padding: "6px 12px", borderRadius: 7, border: `1px solid ${C.border}`, background: "transparent", color: C.textDim, fontSize: 11, fontWeight: 700 }}
              >
                退出
              </button>
            )}
          </div>
        </div>

        <div style={{ display: "flex", gap: 4, marginTop: 10, flexWrap: "wrap" }}>
          {[["strategy", "💰 价格策略"], ["upcoming", "⚡ 预测"], ["parlay", "🎯 串关推荐"], ["backtest", "📊 回测"], ["bets", "🎲 虚拟盘"], ["realbets", "💵 实盘"], ["chart", "📈 走势"]].map(([k, l]) => (
            <button
              key={k}
              onClick={() => setTab(k)}
              style={{ padding: "5px 11px", borderRadius: 7, border: `1px solid ${tab === k ? C.accent : C.border}`, background: tab === k ? C.accentDim : "transparent", color: tab === k ? C.accent : C.textDim, fontSize: 11, fontWeight: 700 }}
            >
              {l}
            </button>
          ))}
        </div>

        {/* 赛事筛选。价格策略页不依赖赛程（手输赔率），所以那一页不显示 */}
        {competitions.length > 0 && tab !== "strategy" && (
          <div style={{ display: "flex", gap: 4, marginTop: 7, flexWrap: "wrap", alignItems: "center" }}>
            <span style={{ fontSize: 10, color: C.textDim, fontWeight: 700, marginRight: 2 }}>赛事</span>
            {[{ id: null, name_zh: "全部" }, ...competitions].map(c => {
              const on = comp === c.id;
              const n = c.id == null ? matches.length : matches.filter(m => m.competition_id === c.id).length;
              return (
                <button key={String(c.id)} onClick={() => setComp(c.id)}
                  style={{ padding: "4px 9px", borderRadius: 6, fontSize: 10, fontWeight: 700,
                           border: `1px solid ${on ? C.blue : C.border}`,
                           background: on ? C.blueDim : "transparent",
                           color: on ? C.blue : C.textDim }}>
                  {c.name_zh}<span style={{ opacity: 0.55, marginLeft: 4 }}>{n}</span>
                </button>
              );
            })}
          </div>
        )}
      </div>

      {showSett && settings && (
        <SettingsPanel settings={settings} onSave={saveSettings} onClose={() => setShowSett(false)} />
      )}

      {/* Stats bar */}
      {backtest && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(104px, 1fr))", background: C.border, gap: 1 }}>
          {[
            { v: `${backtest.correct}/${backtest.total}`, l: `${backtestLabel} · 预测正确`, c: C.blue },
            { v: pct(backtest.accuracy), l: `${backtestLabel} · 准确率`, c: backtest.accuracy > 0.6 ? C.accent : C.gold },
            { v: backtest.avg_rps?.toFixed(3), l: `${backtestLabel} · 平均RPS`, c: C.accent },
            { v: shownBets.length, l: "虚拟下注", c: C.text },
            { v: shownRealBets.length, l: "实盘下注", c: C.purple },
            { v: fnum(bankroll?.real?.total_pnl), l: "实盘盈亏", c: (bankroll?.real?.total_pnl || 0) >= 0 ? C.accent : C.red },
          ].map(({ v, l, c }) => (
            <div key={l} style={{ background: C.surface, padding: "9px 8px", textAlign: "center" }}>
              <div style={{ fontSize: 16, fontWeight: 900, color: c, lineHeight: 1 }}>{v}</div>
              <div style={{ fontSize: 9, color: C.textDim, textTransform: "uppercase", letterSpacing: "0.4px", marginTop: 3 }}>{l}</div>
            </div>
          ))}
        </div>
      )}

      <div style={{ maxWidth: 960, margin: "0 auto", padding: "14px 14px" }}>
        {loading && (
          <div style={{ textAlign: "center", padding: 60, color: C.textDim }}>
            <div style={{ width: 28, height: 28, border: `3px solid ${C.border}`, borderTopColor: C.accent, borderRadius: "50%", animation: "spin 0.7s linear infinite", margin: "0 auto 12px" }} />
            加载中...
          </div>
        )}

        {!loading && tab === "upcoming" && settings && (
          <div>
            <SL>接下来 {upcoming.length} 场 · 资金 {(+settings.bankroll_total).toLocaleString()} · {(settings.kelly_fraction * 100).toFixed(0)}% 凯利</SL>
            {upcoming.length === 0 && <NoFixtures played={played} stale={stale} />}
            {/* MatchCard 在 match.prediction 为空时直接 return null。原来这里
                没有对应的空状态，于是「有比赛但都还没算出预测」会表现成：
                标题写着「接下来 1446 场」，底下一张卡都没有，页面看起来
                像坏了。实际排查时就是这一步花了最久——界面什么都不说，
                只能去翻数据库。现在把这个状态显式说出来。 */}
            {upcoming.length > 0 && upcoming.every(m => !m.prediction) && (
              <NoPredictions count={upcoming.length} status={status}
                             updating={updating} onUpdateNow={triggerUpdate} />
            )}
            <DayGroups matches={upcoming}
              renderMatch={m => <MatchCard key={m.id} match={m} settings={settings}
                                            provisional={isProvisional(m)} onRefresh={loadAll} />} />
          </div>
        )}

        {!loading && tab === "parlay" && settings && (
          <ParlaySuggestTab upcoming={upcoming} settings={settings} onRefresh={loadAll} />
        )}

        {!loading && tab === "backtest" && (
          <div>
            {backtestByComp.length === 0 && <Empty text="还没有已完赛的比赛" />}
            {backtestByComp.filter(bc => comp == null || bc.competition_id === comp).map(bc => {
              const compPlayed = played.filter(m => m.competition_id === bc.competition_id);
              return (
                <div key={bc.competition_id} style={{ marginBottom: 26 }}>
                  <SL>{bc.competition_name} · {bc.total} 场已完赛 · 赛前纯模型预测</SL>
                  <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 10, padding: 14, marginBottom: 12, display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(76px, 1fr))", gap: 12 }}>
                    <Stat label="总场次" val={bc.total} color={C.blue} />
                    <Stat label="模型正确" val={bc.correct} color={C.accent} />
                    <Stat label="模型错误" val={bc.total - bc.correct} color={C.red} />
                    <Stat label="准确率" val={pct(bc.accuracy)} color={bc.accuracy > 0.6 ? C.accent : C.gold} />
                    <Stat label="平均RPS" val={bc.avg_rps?.toFixed(3)} color={C.blue} sub="↓越低越准" />
                    <Stat label="随机基准" val="0.245" color={C.textDim} />
                    <Stat label="相对改善" val={bc.avg_rps ? (((0.245 - bc.avg_rps) / 0.245) * 100).toFixed(1) + "%" : "—"} color={C.accent} />
                    <Stat label="评级" val={bc.avg_rps < 0.18 ? "优秀" : bc.avg_rps < 0.21 ? "良好" : "待提升"} color={bc.avg_rps < 0.18 ? C.accent : bc.avg_rps < 0.21 ? C.gold : C.red} />
                  </div>
                  <div style={{ background: C.surface, borderRadius: 10, overflow: "hidden", border: `1px solid ${C.border}` }}>
                    <div style={{ overflowX: "auto" }}>
                      <div style={{ display: "grid", gridTemplateColumns: "68px 1fr 52px 52px 52px 52px 52px 44px", minWidth: 560, padding: "7px 12px", background: C.muted, fontSize: 9, color: C.textDim, fontWeight: 700, textTransform: "uppercase", gap: 3 }}>
                        {["日期", "赛事", "主胜%", "平%", "客胜%", "比分", "RPS", "结果"].map(h => <span key={h}>{h}</span>)}
                      </div>
                      {compPlayed.map(m => {
                        const p = m.prediction;
                        if (!p) return null;
                        return (
                          <div key={m.id} style={{ display: "grid", gridTemplateColumns: "68px 1fr 52px 52px 52px 52px 52px 44px", minWidth: 560, padding: "7px 12px", borderBottom: `1px solid ${C.border}`, background: p.is_correct ? "transparent" : C.redDim, gap: 3, alignItems: "center", fontSize: 11 }}>
                            <span style={{ color: C.textDim }}>{fdt(m.date)}</span>
                            <span style={{ fontWeight: 600 }}>{m.team1} <span style={{ color: C.textDim }}>vs</span> {m.team2}</span>
                            <span style={{ textAlign: "center", color: p.prob_home > p.prob_draw && p.prob_home > p.prob_away ? C.accent : C.textDim }}>{pct(p.prob_home)}</span>
                            <span style={{ textAlign: "center", color: p.prob_draw > p.prob_home && p.prob_draw > p.prob_away ? C.accent : C.textDim }}>{pct(p.prob_draw)}</span>
                            <span style={{ textAlign: "center", color: p.prob_away > p.prob_home && p.prob_away > p.prob_draw ? C.accent : C.textDim }}>{pct(p.prob_away)}</span>
                            <span style={{ textAlign: "center", fontWeight: 700 }}>{m.score1}-{m.score2}</span>
                            <span style={{ textAlign: "center", color: C.blue }}>{p.rps != null ? p.rps.toFixed(3) : "—"}</span>
                            <span style={{ textAlign: "center" }}>{p.is_correct ? "✅" : "❌"}</span>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {!loading && tab === "bets" && settings && (
          <div>
            <SL>虚拟下注 · 起始 {(+settings.bankroll_total).toLocaleString()} 单位</SL>
            <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 10, padding: 14, marginBottom: 14, display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(110px, 1fr))", gap: 12 }}>
              <Stat label="总注数" val={shownBets.length} color={C.blue} />
              <Stat label="赢注" val={shownBets.filter(b => b.result === "win").length} color={C.accent} />
              <Stat label="待结算" val={shownBets.filter(b => b.result === "pending").length} color={C.gold} />
              <Stat label="总盈亏" val={fnum(bankroll?.virtual?.total_pnl)} color={(bankroll?.virtual?.total_pnl || 0) >= 0 ? C.accent : C.red} />
              <Stat label="ROI" val={bankroll?.virtual ? bankroll.virtual.roi_pct.toFixed(1) + "%" : "—"} color={(bankroll?.virtual?.roi_pct || 0) >= 0 ? C.accent : C.red} />
              <Stat label="胜率" val={shownBets.length ? pct(shownBets.filter(b => b.result === "win").length / shownBets.length) : "—"} color={C.blue} />
              <Stat label="" val="" color={C.textDim} />
              <Stat label="" val="" color={C.textDim} />
            </div>
            {shownBets.length === 0 && <Empty text="还没有虚拟下注。去「预测」页输入赔率，点「🎲 虚拟」。" />}
            {shownBets.length > 0 && (
              <div style={{ background: C.surface, borderRadius: 10, overflow: "hidden", border: `1px solid ${C.border}` }}>
                <div style={{ display: "grid", gridTemplateColumns: "64px 1fr 60px 52px 52px 52px 60px 40px", padding: "7px 12px", background: C.muted, fontSize: 9, color: C.textDim, fontWeight: 700, textTransform: "uppercase", gap: 3 }}>
                  {["日期", "赛事", "方向", "赔率", "本金", "EV", "盈亏", "结果"].map(h => <span key={h}>{h}</span>)}
                </div>
                {shownBets.map(b => (
                  <div key={b.id} style={{ display: "grid", gridTemplateColumns: "64px 1fr 60px 52px 52px 52px 60px 40px", padding: "7px 12px", borderBottom: `1px solid ${C.border}`, background: b.result === "win" ? C.accentDim : b.result === "loss" ? C.redDim : "transparent", gap: 3, alignItems: "center", fontSize: 11 }}>
                    <span style={{ color: C.textDim }}>{fdt(b.date)}</span>
                    <span style={{ fontWeight: 600 }}>{b.team1} vs {b.team2}</span>
                    <span style={{ color: C.textDim }}>{b.outcome === "home" ? "主胜" : b.outcome === "away" ? "客胜" : "平局"}</span>
                    <span style={{ fontWeight: 700 }}>{fod(b.odds_used)}</span>
                    <span>{b.stake}</span>
                    <span style={{ color: evc(b.ev_at_bet || 0) }}>{fev(b.ev_at_bet)}</span>
                    <span style={{ fontWeight: 700, color: (b.pnl || 0) > 0 ? C.accent : (b.pnl || 0) < 0 ? C.red : C.textDim }}>{b.pnl != null ? fnum(b.pnl) : "待定"}</span>
                    <span>{b.result === "win" ? "✅" : b.result === "loss" ? "❌" : "⏳"}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {!loading && tab === "realbets" && settings && (
          <RealBetsTab realBets={shownRealBets} bankroll={bankroll} settings={settings}
            parlays={parlayBets.filter(p => p.kind === "real")} />
        )}

        {!loading && tab === "chart" && bankroll && (
          <ChartTab bankroll={bankroll} settings={settings} />
        )}

        {!loading && tab === "strategy" && <StrategyTab />}
      </div>
    </div>
  );
}

// ── Status Banner — honest about the 12h mechanism ─────────
function LoginScreen({ onSignedIn }) {
  const [email, setEmail] = useState("");
  const [pw, setPw] = useState("");
  const [mode, setMode] = useState("in");     // 'in' 登录 | 'up' 注册
  const [err, setErr] = useState(null);
  const [msg, setMsg] = useState(null);
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setErr(null); setMsg(null); setBusy(true);
    try {
      if (mode === "in") {
        const { session } = await signIn(email.trim(), pw);
        onSignedIn(session);
      } else {
        const { needsEmailConfirm, data } = await signUp(email.trim(), pw);
        if (needsEmailConfirm) {
          // 不说清楚的话，用户会卡在「注册成功但进不去」，看起来像坏了
          setMsg("注册成功。请去邮箱点确认链接，然后回来登录。");
          setMode("in");
        } else {
          onSignedIn(data.session);
        }
      }
    } catch (e2) {
      setErr(e2.message);
    } finally { setBusy(false); }
  }

  const inp = { width: "100%", padding: "10px 12px", borderRadius: 8, marginBottom: 10,
                border: `1px solid ${C.border}`, background: C.surface, color: C.text, fontSize: 15 };

  return (
    <div style={{ minHeight: "100vh", background: C.bg, color: C.text, display: "flex",
                  alignItems: "center", justifyContent: "center", padding: 20,
                  fontFamily: "'Inter',system-ui,sans-serif" }}>
      <form onSubmit={submit} style={{ width: "100%", maxWidth: 340, background: C.card,
              border: `1px solid ${C.border}`, borderRadius: 12, padding: 22 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 9, marginBottom: 4 }}>
          <div style={{ width: 30, height: 30, borderRadius: 8, background: `linear-gradient(135deg,${C.accent},${C.blue})`,
                        display: "flex", alignItems: "center", justifyContent: "center", fontSize: 15 }}>⚽</div>
          <div style={{ fontSize: 17, fontWeight: 800 }}>ValueBet 精算系统</div>
        </div>
        <div style={{ fontSize: 11, color: C.textDim, marginBottom: 16 }}>
          {mode === "in" ? "登录后才能查看你的预测和实盘记录" : "创建账号"}
        </div>

        <input style={inp} type="email" value={email} onChange={e => setEmail(e.target.value)}
               placeholder="邮箱" autoComplete="email" required />
        <input style={inp} type="password" value={pw} onChange={e => setPw(e.target.value)}
               placeholder="密码（至少 6 位）" autoComplete={mode === "in" ? "current-password" : "new-password"}
               minLength={6} required />

        {err && <div style={{ fontSize: 12, color: C.red, marginBottom: 10, lineHeight: 1.6 }}>{err}</div>}
        {/* 「Invalid API key」是 Supabase 最没信息量的一句报错：它不说是哪个
            项目、也不说收到的是哪个 key。真正的原因几乎总是 URL 和 key 属于
            **不同的 Supabase 项目**（或者其中一个粘贴时带了空格/被截断）。
            把这个页面实际在用的两个值摆出来，直接跟后台对照就行。 */}
        {err && /api key/i.test(err) && (
          <div style={{ fontSize: 11, color: C.textDim, marginBottom: 10, lineHeight: 1.7,
                        background: C.surface, border: `1px solid ${C.border}`, borderRadius: 8, padding: "9px 10px" }}>
            这个页面连的是：
            <div style={{ color: C.text, wordBreak: "break-all", margin: "4px 0" }}>{supabaseUrl || "（未配置 VITE_SUPABASE_URL）"}</div>
            用的 key：<span style={{ color: C.text }}>{supabaseKeyHint || "（未配置）"}</span>
            <div style={{ marginTop: 6 }}>
              去 Supabase 后台确认这两个来自<b style={{ color: C.text }}>同一个项目</b>——
              地址里那串 ref 要跟后台项目的 ref 一致。改完 Vercel 的环境变量后
              必须重新部署才生效。
            </div>
          </div>
        )}
        {msg && <div style={{ fontSize: 12, color: C.accent, marginBottom: 10, lineHeight: 1.6 }}>{msg}</div>}

        <button type="submit" disabled={busy}
          style={{ width: "100%", padding: "10px", borderRadius: 8, border: "none", cursor: "pointer",
                   background: C.accent, color: C.bg, fontWeight: 800, fontSize: 14, opacity: busy ? 0.6 : 1 }}>
          {busy ? "…" : mode === "in" ? "登录" : "注册"}
        </button>
        <div style={{ textAlign: "center", marginTop: 12 }}>
          <button type="button" onClick={() => { setMode(mode === "in" ? "up" : "in"); setErr(null); setMsg(null); }}
            style={{ background: "none", border: "none", color: C.textDim, fontSize: 11.5, cursor: "pointer" }}>
            {mode === "in" ? "还没有账号？注册" : "已有账号？去登录"}
          </button>
        </div>
      </form>
    </div>
  );
}

function StatusBanner({ status, updating, onUpdateNow }) {
  if (!status) return null;

  // On a truly fresh install, the startup run may still be in flight
  // when this first renders — last_update is null for a second or two,
  // not because anything failed.
  const isFirstRun = status.last_update == null && status.last_status == null;

  return (
    <div style={{ background: C.goldDim, borderBottom: `1px solid ${C.gold}44`, padding: "7px 16px", display: "flex", alignItems: "center", justifyContent: "center", gap: 14, flexWrap: "wrap", fontSize: 11, color: C.gold }}>
      <span>
        {isFirstRun ? (
          <>{isAuthEnabled ? "☁️ 云端运行中" : "🖥️ 本地运行中"} · 首次抓取数据中，几秒后自动刷新...</>
        ) : (
          <>
            {isAuthEnabled ? "☁️ 云端运行中" : "🖥️ 本地运行中"} · 上次更新 {fdatetime(status.last_update)}
            {status.last_severity === "error" && <span style={{ color: C.red }}> · 更新失败: {status.last_detail}</span>}
            {status.last_severity === "warning" && <span style={{ color: C.gold }}> · ⚠ {status.last_status_label}{status.last_detail ? `：${status.last_detail}` : ""}</span>}
          </>
        )}
      </span>
      <button
        onClick={onUpdateNow}
        disabled={updating}
        style={{ padding: "3px 10px", borderRadius: 12, border: `1px solid ${C.gold}66`, background: "transparent", color: C.gold, fontSize: 10, fontWeight: 700 }}
      >
        {updating ? "更新中..." : "↻ 立即更新"}
      </button>
    </div>
  );
}

// ── Settings Panel ──────────────────────────────────────────
function SettingsPanel({ settings, onSave, onClose }) {
  const [d, setD] = useState(settings);
  useEffect(() => { setD(settings); }, [settings]);
  const inp = { width: "100%", background: C.card, border: `1px solid ${C.border}`, borderRadius: 6, padding: "7px 9px", color: C.text, fontSize: 13, fontWeight: 700 };

  return (
    <div style={{ background: C.purpleDim, borderBottom: `1px solid ${C.purple}44`, padding: "14px 16px" }}>
      <div style={{ maxWidth: 960, margin: "0 auto" }}>
        <div style={{ fontSize: 11, fontWeight: 700, color: C.purple, marginBottom: 10, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span>⚙ 资金与凯利设置</span>
          <span onClick={onClose} style={{ cursor: "pointer", color: C.textDim, fontSize: 16 }}>✕</span>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(110px, 1fr))", gap: 10 }}>
          <div>
            <div style={{ fontSize: 10, color: C.textDim, marginBottom: 4 }}>总资金</div>
            <input type="number" value={d.bankroll_total} onChange={e => setD(x => ({ ...x, bankroll_total: +e.target.value }))} style={inp} />
          </div>
          <div>
            <div style={{ fontSize: 10, color: C.textDim, marginBottom: 4 }}>凯利比例</div>
            <select value={d.kelly_fraction} onChange={e => setD(x => ({ ...x, kelly_fraction: +e.target.value }))} style={inp}>
              <option value={0.25}>四分之一 (0.25×)</option>
              <option value={0.5}>半凯利 (0.5×) 推荐</option>
              <option value={0.75}>3/4 (0.75×)</option>
              <option value={1.0}>全凯利 (1×) 高风险</option>
            </select>
          </div>
          <div>
            <div style={{ fontSize: 10, color: C.textDim, marginBottom: 4 }}>单注上限</div>
            <select value={d.max_bet_pct} onChange={e => setD(x => ({ ...x, max_bet_pct: +e.target.value }))} style={inp}>
              <option value={0.05}>5%</option>
              <option value={0.1}>10%</option>
              <option value={0.15}>15%（推荐）</option>
              <option value={0.2}>20%</option>
              <option value={0.3}>30%</option>
            </select>
          </div>
          <div>
            <div style={{ fontSize: 10, color: C.textDim, marginBottom: 4 }}>Value EV 门槛</div>
            <select value={d.min_ev_threshold} onChange={e => setD(x => ({ ...x, min_ev_threshold: +e.target.value }))} style={inp}>
              <option value={0.01}>1%</option>
              <option value={0.02}>2%</option>
              <option value={0.03}>3%（推荐）</option>
              <option value={0.05}>5%</option>
              <option value={0.1}>10%</option>
            </select>
          </div>
        </div>
        <button onClick={() => onSave(d)} style={{ marginTop: 12, padding: "8px 18px", borderRadius: 8, border: "none", background: C.purple, color: "#0a0510", fontWeight: 800, fontSize: 12 }}>
          保存设置
        </button>
      </div>
    </div>
  );
}

// ── Match Card ───────────────────────────────────────────────
// 按比赛日期分组折叠。
//
// 起因：新赛季接进来之后「接下来」有 1446 场，原来是一场一张卡平铺，
// 要划到最底得滚很久，而且 1446 个 MatchCard 同时挂在 DOM 上，手机端
// 明显卡。折叠之后收起的那些天一个卡片都不渲染，滚动长度从上千张卡
// 变成几十行标题。
//
// 默认只展开最近的一天——绝大多数时候要看的就是马上要踢的那批。
function DayGroups({ matches, renderMatch, selectedIds }) {
  const days = useMemo(() => {
    const map = new Map();
    for (const m of matches) {
      if (!map.has(m.date)) map.set(m.date, []);
      map.get(m.date).push(m);
    }
    return [...map.entries()].sort((a, b) => (a[0] < b[0] ? -1 : 1));
  }, [matches]);

  // 用日期串当依赖，而不是 matches 本身——matches 是每次渲染新建的过滤
  // 数组，拿它当依赖会每帧都重置，展开状态根本留不住。
  const daysKey = days.map(d => d[0]).join("|");
  const [openDays, setOpenDays] = useState(() => new Set(days.length ? [days[0][0]] : []));
  useEffect(() => {
    // 换了赛事筛选 → 天数变了 → 回到「只展开最近一天」
    setOpenDays(new Set(days.length ? [days[0][0]] : []));
  }, [daysKey]);   // eslint-disable-line react-hooks/exhaustive-deps

  if (!days.length) return null;

  const today = new Date().toISOString().slice(0, 10);
  const tomorrow = new Date(Date.now() + 86400000).toISOString().slice(0, 10);

  function toggle(date) {
    setOpenDays(s => {
      const next = new Set(s);
      next.has(date) ? next.delete(date) : next.add(date);
      return next;
    });
  }

  const allOpen = openDays.size === days.length;

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginBottom: 8 }}>
        <button
          onClick={() => setOpenDays(allOpen ? new Set() : new Set(days.map(d => d[0])))}
          style={{ background: "none", border: `1px solid ${C.border}`, color: C.textDim,
                   borderRadius: 6, padding: "4px 10px", fontSize: 11 }}>
          {allOpen ? "全部收起" : `全部展开（${days.length} 天）`}
        </button>
      </div>

      {days.map(([date, list]) => {
        const open = openDays.has(date);
        const d = new Date(date + "T12:00:00");
        const wd = d.toLocaleDateString("zh-HK", { weekday: "short" });
        const mark = date === today ? "今天" : date === tomorrow ? "明天" : null;
        // 这一天涉及哪些赛事，收起时也能看出来，不用展开才知道有没有你要的联赛
        const comps = [...new Set(list.map(m => m.competition_name).filter(Boolean))];
        // 串关页会传 selectedIds 进来：这一天选了几场要显示在标题上，
        // 否则折叠之后完全看不出自己选过什么
        const nSel = selectedIds ? list.filter(m => selectedIds.has(m.id)).length : 0;
        return (
          <div key={date} style={{ marginBottom: 8 }}>
            <button
              onClick={() => toggle(date)}
              style={{ width: "100%", display: "flex", alignItems: "center", gap: 10,
                       background: C.surface, border: `1px solid ${C.border}`,
                       borderRadius: 9, padding: "10px 12px", color: C.text,
                       textAlign: "left", fontSize: 13 }}>
              <span style={{ color: C.textDim, fontSize: 11, width: 12, flexShrink: 0 }}>
                {open ? "▾" : "▸"}
              </span>
              <span style={{ fontWeight: 800 }}>{fdt(date)}</span>
              <span style={{ color: C.textDim, fontSize: 11 }}>{wd}</span>
              {mark && (
                <span style={{ background: C.accentDim, color: C.accent, borderRadius: 5,
                               padding: "1px 6px", fontSize: 10, fontWeight: 700 }}>{mark}</span>
              )}
              <span style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 8 }}>
                <span style={{ color: C.textDim, fontSize: 10.5 }}>{comps.join(" · ")}</span>
                {nSel > 0 && (
                  <span style={{ background: C.accentDim, color: C.accent, borderRadius: 5,
                                 padding: "1px 6px", fontSize: 10, fontWeight: 700 }}>已选 {nSel}</span>
                )}
                <span style={{ background: C.muted, color: C.text, borderRadius: 5,
                               padding: "1px 7px", fontSize: 11, fontWeight: 700 }}>{list.length}</span>
              </span>
            </button>
            {open && <div style={{ marginTop: 6 }}>{list.map(renderMatch)}</div>}
          </div>
        );
      })}
    </div>
  );
}

function MatchCard({ match, settings, onRefresh, provisional }) {
  const [open, setOpen] = useState(false);
  const [oHome, setOHome] = useState(match.latest_odds?.odds_home?.toString() || "");
  const [oDraw, setODraw] = useState(match.latest_odds?.odds_draw?.toString() || "");
  const [oAway, setOAway] = useState(match.latest_odds?.odds_away?.toString() || "");
  const [calc, setCalc] = useState(null);
  const [stake, setStake] = useState(100);
  const [rStake, setRStake] = useState({});
  const [showRF, setShowRF] = useState(null);
  const [saving, setSaving] = useState(null);
  const [saved, setSaved] = useState(null);

  const mdl = match.prediction;
  if (!mdl) return null;

  async function compute() {
    const h = parseFloat(oHome), d = parseFloat(oDraw), a = parseFloat(oAway);
    if (!h || !a) return;
    try {
      const result = await api("/odds", {
        method: "POST",
        body: JSON.stringify({ match_id: match.id, odds_home: h, odds_draw: d || null, odds_away: a }),
      });
      setCalc({ h, d: d || null, a, ...result });
    } catch (e) {
      alert("计算失败: " + e.message);
    }
  }

  async function doVBet(outcome) {
    if (!calc) return;
    setSaving("v" + outcome);
    const odds = outcome === "home" ? calc.h : outcome === "away" ? calc.a : calc.d;
    const evVal = outcome === "home" ? calc.ev_home : outcome === "away" ? calc.ev_away : calc.ev_draw;
    const kPct = outcome === "home" ? calc.kelly_home : outcome === "away" ? calc.kelly_away : calc.kelly_draw;
    try {
      await api("/bets", {
        method: "POST",
        body: JSON.stringify({ match_id: match.id, outcome, stake, odds_used: odds, ev_at_bet: evVal, kelly_pct: kPct }),
      });
      setSaved("v" + outcome);
      setTimeout(() => setSaved(null), 2000);
      onRefresh();
    } finally {
      setSaving(null);
    }
  }

  async function doRBet(outcome) {
    if (!calc) return;
    const rs = parseFloat(rStake[outcome] || "");
    if (!rs || rs <= 0) return;
    setSaving("r" + outcome);
    const odds = outcome === "home" ? calc.h : outcome === "away" ? calc.a : calc.d;
    const evVal = outcome === "home" ? calc.ev_home : outcome === "away" ? calc.ev_away : calc.ev_draw;
    const kPct = outcome === "home" ? calc.kelly_home : outcome === "away" ? calc.kelly_away : calc.kelly_draw;
    const kAmt = outcome === "home" ? calc.kelly_home_amount : outcome === "away" ? calc.kelly_away_amount : calc.kelly_draw_amount;
    try {
      await api("/real-bets", {
        method: "POST",
        body: JSON.stringify({
          match_id: match.id, platform: "bk8", outcome, stake_real: rs, currency: "HKD",
          odds_used: odds, ev_at_bet: evVal, kelly_suggested_pct: kPct, kelly_suggested_amount: kAmt,
        }),
      });
      setSaved("r" + outcome);
      setShowRF(null);
      setTimeout(() => setSaved(null), 2500);
      onRefresh();
    } finally {
      setSaving(null);
    }
  }

  const threshold = +settings.min_ev_threshold;
  const maxP = Math.max(mdl.prob_home, mdl.prob_draw, mdl.prob_away);

  return (
    <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 10, marginBottom: 10, overflow: "hidden" }}>
      <div onClick={() => setOpen(o => !o)} style={{ padding: "11px 14px", display: "flex", alignItems: "center", justifyContent: "space-between", cursor: "pointer", userSelect: "none" }}>
        <div>
          <div style={{ fontWeight: 700, fontSize: 13 }}>{match.team1} <span style={{ color: C.textDim, fontWeight: 400, fontSize: 12 }}>vs</span> {match.team2}</div>
          <div style={{ fontSize: 10, color: C.textDim, marginTop: 2 }}>
            {match.competition_name && <span style={{ color: C.blue, fontWeight: 700 }}>{match.competition_name} · </span>}
            {fdt(match.date)}{match.time_utc ? ` ${match.time_utc}` : ""} · {match.round} · {match.ground}
            {provisional && (
              <span title="上游数据源还没排这一轮的具体日程，整轮先挂在一个名义日期上"
                    style={{ marginLeft: 6, background: C.goldDim, color: C.gold, borderRadius: 4,
                             padding: "1px 5px", fontSize: 9, fontWeight: 700 }}>日期暂定</span>
            )}</div>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <span style={{ fontSize: 10, color: C.blue }}>ELO {mdl.elo_home}/{mdl.elo_away}</span>
          <span style={{ color: C.textDim }}>{open ? "▲" : "▼"}</span>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(96px, 1fr))", gap: 1, background: C.border }}>
        {[
          { label: match.team1, prob: mdl.prob_home, xg: mdl.xg_home },
          { label: "平局", prob: mdl.prob_draw, xg: null },
          { label: match.team2, prob: mdl.prob_away, xg: mdl.xg_away },
        ].map((item, idx) => (
          <div key={idx} style={{ background: C.card, padding: "9px 12px" }}>
            <div style={{ fontSize: 9, color: C.textDim, textTransform: "uppercase", letterSpacing: "0.6px", marginBottom: 3 }}>{item.label}</div>
            <div style={{ fontSize: 19, fontWeight: 900, color: item.prob === maxP ? C.accent : C.text }}>{pct(item.prob)}</div>
            {item.xg !== null && <div style={{ fontSize: 10, color: C.textDim, marginTop: 1 }}>xG {item.xg}</div>}
            <div style={{ marginTop: 5, height: 2, background: C.border, borderRadius: 1 }}>
              <div style={{ width: pct(item.prob), height: "100%", background: item.prob === maxP ? C.accent : C.muted, borderRadius: 1 }} />
            </div>
          </div>
        ))}
      </div>

      {open && (
        <div style={{ borderTop: `1px solid ${C.border}`, padding: "12px 14px", background: C.surface }}>
          <div style={{ fontSize: 11, color: C.textDim, marginBottom: 8 }}>输入赔率，系统计算真实期望值：</div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(88px, 1fr))", gap: 7, alignItems: "flex-end", marginBottom: 10 }}>
            {[
              { label: match.team1, val: oHome, set: setOHome },
              { label: "平局", val: oDraw, set: setODraw },
              { label: match.team2, val: oAway, set: setOAway },
            ].map(f => (
              <div key={f.label}>
                <div style={{ fontSize: 10, color: C.textDim, marginBottom: 3 }}>{f.label}</div>
                <input type="number" step="0.01" placeholder="e.g. 2.40" value={f.val} onChange={e => f.set(e.target.value)}
                  style={{ width: "100%", background: C.card, border: `1px solid ${C.border}`, borderRadius: 6, padding: "6px 9px", color: C.text, fontSize: 13, fontWeight: 700 }} />
              </div>
            ))}
            <button onClick={compute} style={{ padding: "6px 12px", borderRadius: 7, border: "none", background: C.accent, color: C.bg, fontWeight: 800, fontSize: 12, whiteSpace: "nowrap" }}>
              计算 →
            </button>
          </div>

          {calc && (
            <div>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(88px, 1fr))", gap: 7, marginBottom: 10 }}>
                {[
                  { key: "home", label: match.team1, evVal: calc.ev_home, kPct: calc.kelly_home, kAmt: calc.kelly_home_amount, odds: calc.h },
                  ...(calc.d ? [{ key: "draw", label: "平局", evVal: calc.ev_draw, kPct: calc.kelly_draw, kAmt: calc.kelly_draw_amount, odds: calc.d }] : []),
                  { key: "away", label: match.team2, evVal: calc.ev_away, kPct: calc.kelly_away, kAmt: calc.kelly_away_amount, odds: calc.a },
                ].map(item => (
                  <div key={item.key} style={{ background: evbg(item.evVal), border: `1px solid ${evc(item.evVal)}44`, borderRadius: 8, padding: "9px 10px" }}>
                    <div style={{ fontSize: 10, color: C.textDim, marginBottom: 3 }}>{item.label} @ {fod(item.odds)}</div>
                    <div style={{ fontSize: 17, fontWeight: 900, color: evc(item.evVal) }}>EV {fev(item.evVal)}</div>
                    {item.kPct > 0 ? (
                      <div style={{ fontSize: 11, marginTop: 3 }}>建议: <strong>{Math.round(item.kAmt).toLocaleString()}</strong> <span style={{ color: C.textDim }}>({pct(item.kPct)})</span></div>
                    ) : (
                      <div style={{ fontSize: 11, marginTop: 3, color: C.textDim }}>不建议下注</div>
                    )}
                    {item.evVal > threshold && <div style={{ fontSize: 10, fontWeight: 800, color: C.accent, marginTop: 3 }}>⚡ VALUE</div>}
                    <div style={{ display: "flex", gap: 4, marginTop: 7 }}>
                      <button onClick={() => doVBet(item.key)} disabled={saving === "v" + item.key || saved === "v" + item.key}
                        style={{ flex: 1, padding: "10px 5px", borderRadius: 6, border: "none", background: item.evVal > 0 ? C.blue : C.muted, color: item.evVal > 0 ? "#fff" : C.textDim, fontWeight: 700, fontSize: 12 }}>
                        {saved === "v" + item.key ? "✅" : saving === "v" + item.key ? "..." : "🎲 虚拟"}
                      </button>
                      <button onClick={() => setShowRF(showRF === item.key ? null : item.key)}
                        style={{ flex: 1, padding: "10px 5px", borderRadius: 6, border: `1px solid ${C.purple}`, background: showRF === item.key ? C.purple : "transparent", color: showRF === item.key ? "#0a0510" : C.purple, fontWeight: 700, fontSize: 12 }}>
                        💵 实盘
                      </button>
                    </div>
                    {showRF === item.key && (
                      <div style={{ marginTop: 7, paddingTop: 7, borderTop: `1px solid ${C.border}` }}>
                        <div style={{ fontSize: 9, color: C.textDim, marginBottom: 3 }}>真实下注金额（HKD）：</div>
                        {/* 输入框和确认按钮改成上下排，不再并排。
                            并排时按钮只有 padding 5px 9px、字号 10 —— 三列布局下
                            每列约 105px，输入框占掉 flex:1，按钮实际可点区域只剩
                            三四十像素宽、二十来像素高，手机上按不中（用户反馈的
                            「手机端下注按不到确认」就是这个）。占满整行之后
                            高度约 40px，是正常的触摸目标。
                            输入框字号必须 ≥16px：iOS Safari 在小于 16px 时会自动
                            放大整个页面，一放大按钮又跑偏了。 */}
                        <input type="number" inputMode="decimal" placeholder={`建议 ${Math.round(item.kAmt || 0)}`} value={rStake[item.key] || ""}
                          onChange={e => setRStake(r => ({ ...r, [item.key]: e.target.value }))}
                          style={{ width: "100%", background: C.card, border: `1px solid ${C.purple}66`, borderRadius: 6, padding: "9px 10px", color: C.text, fontSize: 16, fontWeight: 700 }} />
                        <button onClick={() => doRBet(item.key)} disabled={saving === "r" + item.key || saved === "r" + item.key}
                          style={{ width: "100%", marginTop: 6, padding: "11px", borderRadius: 6, border: "none", background: C.purple, color: "#0a0510", fontWeight: 800, fontSize: 13 }}>
                          {saved === "r" + item.key ? "✅ 已登记" : saving === "r" + item.key ? "..." : "确认登记实盘"}
                        </button>
                      </div>
                    )}
                  </div>
                ))}
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 11 }}>
                <span style={{ color: C.textDim }}>虚拟本金：</span>
                <input type="number" value={stake} onChange={e => setStake(+e.target.value)}
                  style={{ width: 75, background: C.card, border: `1px solid ${C.border}`, borderRadius: 6, padding: "4px 7px", color: C.text, fontSize: 12 }} />
                <span style={{ color: C.textDim }}>单位</span>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Real Bets Tab ────────────────────────────────────────────
function RealBetsTab({ realBets, bankroll, settings, parlays = [] }) {
  const pending = realBets.filter(b => b.result === "pending");
  const settled = realBets.filter(b => b.result !== "pending");
  const pSettled = parlays.filter(p => p.result !== "pending");
  // 串关的本金和回报要跟单场一起计入这一页的合计，否则「实盘盈亏」这个数
  // 跟资金曲线对不上——曲线一直是把串关算进去的
  const pStake = parlays.reduce((s, p) => s + (p.stake || 0), 0);
  const pPnl = pSettled.reduce((s, p) => s + (p.pnl || 0), 0);

  return (
    <div>
      <SL>实盘记录 · 真实金钱（HKD）· 起始 {(+settings.bankroll_total).toLocaleString()}</SL>
      <div style={{ background: C.purpleDim, border: `1px solid ${C.purple}44`, borderRadius: 8, padding: "9px 13px", fontSize: 11, color: C.purple, marginBottom: 12 }}>
        💡 在「预测」页点「💵 实盘」按钮记录你真实下的注。比赛结束后系统每12小时自动结算盈亏。
      </div>

      {/* 串关是单独一张表（ParlayBet），一注对应 3-8 场比赛，塞不进上面那个
          按单场排的表格，所以单独列一段。原来这一页完全不显示串关，
          于是「串关下注和回报没进实盘」——其实记下来了，只是没地方看。 */}
      {parlays.length > 0 && (
        <div style={{ marginBottom: 14 }}>
          <div style={{ fontSize: 11, color: C.textDim, marginBottom: 7, display: "flex", justifyContent: "space-between", flexWrap: "wrap", gap: 6 }}>
            <span style={{ fontWeight: 700, color: C.text }}>🎯 串关注单 {parlays.length} 注</span>
            <span>本金合计 {Math.round(pStake).toLocaleString()} · 已结算盈亏
              <strong style={{ color: pPnl >= 0 ? C.accent : C.red, marginLeft: 4 }}>{fnum(pPnl)}</strong>
            </span>
          </div>
          {parlays.map(p => (
            <div key={p.id} style={{ background: C.card, border: `1px solid ${p.result === "win" ? C.accent + "66" : p.result === "loss" ? C.red + "66" : C.border}`,
                                     borderRadius: 9, padding: "10px 12px", marginBottom: 7 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 6, marginBottom: 6 }}>
                <span style={{ fontWeight: 800, fontSize: 12 }}>
                  {p.legs?.length || 0} 串 1 · 总赔率 {fod(p.odds_used)}
                </span>
                <span style={{ fontSize: 11 }}>
                  本金 {Math.round(p.stake).toLocaleString()} ·{" "}
                  <strong style={{ color: p.result === "win" ? C.accent : p.result === "loss" ? C.red : C.gold }}>
                    {p.result === "pending" ? "待结算" : p.result === "win" ? `赢 ${fnum(p.pnl)}` : `输 ${fnum(p.pnl)}`}
                  </strong>
                </span>
              </div>
              {(p.legs || []).map((l, i) => (
                <div key={i} style={{ fontSize: 10.5, color: C.textDim, paddingLeft: 8, lineHeight: 1.7 }}>
                  · {l.team1} vs {l.team2} — 押 {l.outcome === "home" ? l.team1 : l.outcome === "away" ? l.team2 : "平局"} @ {fod(l.odds)}
                  {/* 后端不返回逐腿的输赢，只返回比分。直接显示比分，
                      是赢是输一眼能看出来，也不用前端再判一次（判重了就
                      有两处结算逻辑，早晚会不一致）。 */}
                  {l.score && <span style={{ color: C.text, marginLeft: 5 }}>({l.score})</span>}
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
      <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 10, padding: 14, marginBottom: 14, display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(110px, 1fr))", gap: 12 }}>
        <Stat label="总注数" val={realBets.length} color={C.purple} />
        <Stat label="赢注" val={settled.filter(b => b.result === "win").length} color={C.accent} />
        <Stat label="待结算" val={pending.length} color={C.gold} />
        <Stat label="实盘盈亏" val={fnum(bankroll?.real?.total_pnl)} color={(bankroll?.real?.total_pnl || 0) >= 0 ? C.accent : C.red} />
        <Stat label="实盘ROI" val={bankroll?.real ? bankroll.real.roi_pct.toFixed(1) + "%" : "—"} color={(bankroll?.real?.roi_pct || 0) >= 0 ? C.accent : C.red} />
        <Stat label="胜率" val={settled.length ? pct(settled.filter(b => b.result === "win").length / settled.length) : "—"} color={C.blue} />
        <Stat label="" val="" color={C.textDim} />
        <Stat label="" val="" color={C.textDim} />
      </div>
      {realBets.length === 0 && <Empty text="还没有实盘记录。去「预测」页输入赔率，点击「💵 实盘」按钮。" />}
      {realBets.map(b => {
        const won = b.result === "win";
        return (
          <div key={b.id} style={{ background: C.card, border: `1px solid ${b.result === "pending" ? C.gold + "44" : won ? C.accent + "44" : C.red + "44"}`, borderRadius: 8, marginBottom: 8, padding: "10px 14px", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <div>
              <div style={{ fontWeight: 700, fontSize: 12 }}>{b.team1} vs {b.team2}</div>
              <div style={{ fontSize: 10, color: C.textDim, marginTop: 2 }}>
                押 {b.outcome === "home" ? "主队" : b.outcome === "away" ? "客队" : "平局"} · 赔率 {fod(b.odds_used)} · {b.ev_at_bet != null ? `EV ${fev(b.ev_at_bet)}` : ""}
              </div>
            </div>
            <div style={{ textAlign: "right" }}>
              <div style={{ fontWeight: 700, fontSize: 13 }}>{b.stake_real.toLocaleString()} {b.currency}</div>
              <div style={{ fontSize: 11, fontWeight: 700, color: b.result === "pending" ? C.gold : won ? C.accent : C.red }}>
                {b.result === "pending" ? "⏳ 待结算" : won ? `✅ +${(b.pnl_real || 0).toFixed(0)}` : `❌ ${(b.pnl_real || 0).toFixed(0)}`}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ── Chart Tab ────────────────────────────────────────────────
function ChartTab({ bankroll, settings }) {
  // 后端现在返回单一合并数据集（每个点同时带 virtual 和 real 两个值）。
  // 之前是两个独立数组分别喂给两条 Line，配 category 类型的 X 轴时
  // recharts 会因两边日期集合不同而画歪——这是曲线之前有问题的原因之一。
  const series = bankroll.series || [];

  return (
    <div>
      <SL>资金走势 · 虚拟盘 vs 实盘</SL>
      <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 10, padding: "16px 18px", marginBottom: 16 }}>
        <div style={{ fontSize: 11, color: C.textDim, marginBottom: 8, display: "flex", gap: 16 }}>
          <span><span style={{ display: "inline-block", width: 10, height: 10, background: C.accent, borderRadius: 2, marginRight: 5 }} />虚拟盘</span>
          <span><span style={{ display: "inline-block", width: 10, height: 10, background: C.purple, borderRadius: 2, marginRight: 5 }} />实盘</span>
        </div>
        {series.length <= 1 ? (
          <Empty text="还没有已结算注单，下注后这里显示走势" />
        ) : (
          <div style={{ height: 280 }}>
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={series}>
                <CartesianGrid strokeDasharray="3 3" stroke={C.border} />
                <XAxis dataKey="date" type="category" allowDuplicatedCategory={false} tick={{ fontSize: 10, fill: C.textDim }} tickFormatter={d => fdt(d)} />
                <YAxis tick={{ fontSize: 10, fill: C.textDim }} domain={["auto", "auto"]} />
                <Tooltip contentStyle={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 8, fontSize: 12 }} labelFormatter={fdt} formatter={v => [`${Math.round(v).toLocaleString()}`, "资金"]} />
                <ReferenceLine y={+settings.bankroll_total} stroke={C.gold} strokeDasharray="4 4" label={{ value: "起始", fill: C.gold, fontSize: 10 }} />
                <Line type="monotone" dataKey="virtual" name="虚拟盘" stroke={C.accent} strokeWidth={2} dot={{ fill: C.accent, r: 3 }} />
                <Line type="monotone" dataKey="real" name="实盘" stroke={C.purple} strokeWidth={2} dot={{ fill: C.purple, r: 3 }} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Parlay Suggest Tab ──────────────────────────────────────
function ParlaySuggestTab({ upcoming, settings, onRefresh }) {
  const [selected, setSelected] = useState({});   // { matchId: true }
  // 赔率从后端已保存的记录初始化——之前这里是空对象，赔率只活在 React state 里，
  // 一刷新就没了；单场那边（MatchCard）一直是从 match.latest_odds 读的，
  // 所以单场不受影响。现在两边用同一个数据来源。
  const [odds, setOdds] = useState(() => {
    const init = {};
    for (const m of upcoming) {
      if (m.latest_odds) {
        init[m.id] = {
          home: m.latest_odds.odds_home?.toString() || "",
          draw: m.latest_odds.odds_draw?.toString() || "",
          away: m.latest_odds.odds_away?.toString() || "",
        };
      }
    }
    return init;
  });
  const [minLegs, setMinLegs] = useState(3);
  const [maxLegs, setMaxLegs] = useState(6);
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [recording, setRecording] = useState(null);
  const [recorded, setRecorded] = useState(null);
  const [realStake, setRealStake] = useState({});
  const [realOdds, setRealOdds] = useState({});
  const [showRealForm, setShowRealForm] = useState(null);

  const selectedIds = Object.keys(selected).filter(id => selected[id]).map(Number);

  // 凯利比例的显示名，跟设置里的实际值走
  const kellyLabel = {
    0.25: "四分之一凯利", 0.5: "半凯利",
    0.75: "3/4凯利", 1.0: "全凯利",
  }[+settings.kelly_fraction] || `${(+settings.kelly_fraction * 100).toFixed(0)}%凯利`;

  function toggleMatch(id) {
    setSelected(s => ({ ...s, [id]: !s[id] }));
  }

  function setOddsField(id, field, value) {
    setOdds(o => ({ ...o, [id]: { ...o[id], [field]: value } }));
  }

  async function generate() {
    if (selectedIds.length < minLegs) {
      setError(`已选 ${selectedIds.length} 场比赛，至少需要选够 ${minLegs} 场才能搜索`);
      return;
    }
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const matches = selectedIds.map(id => ({
        match_id: id,
        odds_home: odds[id]?.home ? +odds[id].home : null,
        odds_draw: odds[id]?.draw ? +odds[id].draw : null,
        odds_away: odds[id]?.away ? +odds[id].away : null,
      }));

      // 把赔率存回后端，这样刷新页面后还在（之前只存在 React state 里，一刷新就丢）
      await Promise.all(matches
        .filter(m => m.odds_home && m.odds_away)
        .map(m => api("/odds", {
          method: "POST",
          body: JSON.stringify({
            match_id: m.match_id, odds_home: m.odds_home,
            odds_draw: m.odds_draw, odds_away: m.odds_away,
          }),
        }).catch(() => null))   // 存赔率失败不该挡住搜索结果
      );

      const r = await api("/parlay/suggest", {
        method: "POST",
        body: JSON.stringify({ matches, min_legs: minLegs, max_legs: maxLegs }),
      });
      setResult(r);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  async function recordParlay(combo, comboIdx, kind) {
    const key = `${kind}-${comboIdx}`;
    setRecording(key);
    try {
      const stake = kind === "real"
        ? parseFloat(realStake[comboIdx] || "")
        : Math.round(combo.kelly_amount || 0);
      if (!stake || stake <= 0) {
        setError(kind === "real" ? "请填写真实下注金额" : "凯利建议金额为0，不适合下注");
        setRecording(null);
        return;
      }
      const oddsOverride = kind === "real" && realOdds[comboIdx]
        ? parseFloat(realOdds[comboIdx])
        : combo.combined_odds;

      await api("/parlay-bets", {
        method: "POST",
        body: JSON.stringify({
          kind,
          stake,
          odds_used: oddsOverride,
          joint_probability: combo.joint_probability,
          ev_at_bet: combo.ev,
          kelly_pct: combo.kelly_pct,
          legs: combo.legs.map(l => ({
            match_id: l.match_id, outcome: l.outcome,
            odds: l.odds, prob: l.prob,
          })),
        }),
      });
      setRecorded(key);
      setShowRealForm(null);
      setTimeout(() => setRecorded(null), 2500);
      if (onRefresh) onRefresh();
    } catch (e) {
      setError(e.message);
    } finally {
      setRecording(null);
    }
  }

  return (
    <div>
      <SL>串关推荐 · 只支持独立比赛的1X2组合，{minLegs}-{maxLegs}腿</SL>

      <div style={{ background: C.goldDim, border: `1px solid ${C.gold}44`, borderRadius: 8, padding: "10px 13px", fontSize: 11, color: C.gold, marginBottom: 12, lineHeight: 1.6 }}>
        💡 只有单腿本身是正EV的选项才会进入候选池——赔率再高，如果模型概率算下来是负EV，
        不会被推荐。热门强队的赔率经常被市场压得低于其真实胜率对应的公平赔率，串起来只会让负EV被放大，
        不会凭空创造价值。
      </div>

      <div style={{ display: "flex", gap: 12, alignItems: "center", marginBottom: 12, flexWrap: "wrap" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ fontSize: 11, color: C.textDim }}>最少腿数:</span>
          <select value={minLegs} onChange={e => setMinLegs(+e.target.value)}
            style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 6, padding: "5px 8px", color: C.text, fontSize: 12 }}>
            {[2, 3, 4, 5].map(n => <option key={n} value={n}>{n}</option>)}
          </select>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ fontSize: 11, color: C.textDim }}>最多腿数:</span>
          <select value={maxLegs} onChange={e => setMaxLegs(+e.target.value)}
            style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 6, padding: "5px 8px", color: C.text, fontSize: 12 }}>
            {[3, 4, 5, 6, 7, 8].map(n => <option key={n} value={n}>{n}</option>)}
          </select>
        </div>
        <span style={{ fontSize: 11, color: C.textDim }}>已选 {selectedIds.length} 场</span>
      </div>

      {upcoming.length === 0 && <Empty text="暂无即将赛事可供选择" />}

      <DayGroups matches={upcoming} selectedIds={new Set(selectedIds)} renderMatch={m => {
        const isSel = !!selected[m.id];
        const p = m.prediction;
        return (
          <div key={m.id} style={{ background: C.card, border: `1px solid ${isSel ? C.accent : C.border}`, borderRadius: 10, marginBottom: 8, overflow: "hidden" }}>
            <div onClick={() => toggleMatch(m.id)} style={{ padding: "10px 14px", display: "flex", alignItems: "center", justifyContent: "space-between", cursor: "pointer", userSelect: "none" }}>
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <div style={{ width: 16, height: 16, borderRadius: 4, border: `2px solid ${isSel ? C.accent : C.border}`, background: isSel ? C.accent : "transparent", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 10, color: C.bg, fontWeight: 900 }}>
                  {isSel ? "✓" : ""}
                </div>
                <div>
                  <div style={{ fontWeight: 700, fontSize: 13 }}>{m.team1} <span style={{ color: C.textDim, fontWeight: 400, fontSize: 12 }}>vs</span> {m.team2}</div>
                  <div style={{ fontSize: 10, color: C.textDim, marginTop: 2 }}>{fdt(m.date)} · {m.round}</div>
                </div>
              </div>
              {p && <span style={{ fontSize: 10, color: C.textDim }}>主{pct(p.prob_home)} 平{pct(p.prob_draw)} 客{pct(p.prob_away)}</span>}
            </div>
            {isSel && (
              <div style={{ padding: "8px 14px 12px", borderTop: `1px solid ${C.border}`, display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(90px, 1fr))", gap: 7 }}>
                {[
                  { label: `${m.team1} 赔率`, field: "home" },
                  { label: "平局 赔率", field: "draw" },
                  { label: `${m.team2} 赔率`, field: "away" },
                ].map(f => (
                  <div key={f.field}>
                    <div style={{ fontSize: 9, color: C.textDim, marginBottom: 3 }}>{f.label}</div>
                    <input
                      type="number" step="0.01" inputMode="decimal" placeholder="e.g. 2.10"
                      value={odds[m.id]?.[f.field] || ""}
                      onChange={e => setOddsField(m.id, f.field, e.target.value)}
                      style={{ width: "100%", background: C.bg, border: `1px solid ${C.border}`, borderRadius: 6, padding: "7px 8px", color: C.text, fontSize: 14, fontWeight: 700 }}
                    />
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      }} />

      <button onClick={generate} disabled={loading || selectedIds.length < 2}
        style={{ width: "100%", padding: "11px", borderRadius: 8, border: "none", background: selectedIds.length >= 2 ? C.accent : C.muted, color: selectedIds.length >= 2 ? C.bg : C.textDim, fontWeight: 800, fontSize: 13, marginTop: 8, marginBottom: 14 }}>
        {loading ? "搜索中..." : `🎯 生成推荐组合（已选${selectedIds.length}场）`}
      </button>

      {error && (
        <div style={{ background: C.redDim, border: `1px solid ${C.red}44`, borderRadius: 8, padding: "10px 13px", fontSize: 12, color: C.red, marginBottom: 12 }}>
          {error}
        </div>
      )}

      {result && result.status !== "ok" && (
        <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 8, padding: "14px", fontSize: 12, color: C.textDim, lineHeight: 1.6 }}>
          {result.detail}
        </div>
      )}

      {result && result.status === "ok" && (
        <div>
          <SL>推荐组合（共评估 {result.n_combinations_evaluated} 种组合，{result.n_candidates} 条候选正EV腿）</SL>
          {result.combinations.map((combo, i) => (
            <div key={i} style={{ background: C.card, border: `1px solid ${combo.tag ? C.accent + "66" : C.border}`, borderRadius: 10, marginBottom: 10, padding: "12px 14px" }}>
              {combo.tag && (
                <div style={{ fontSize: 11, fontWeight: 800, color: C.accent, marginBottom: 8 }}>{combo.tag}</div>
              )}
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(90px, 1fr))", gap: 10, marginBottom: 10 }}>
                <MiniStat label="联合概率" val={pct(combo.joint_probability)} color={C.blue} />
                <MiniStat label="联合赔率" val={combo.combined_odds.toFixed(2)} color={C.text} />
                <MiniStat label="EV" val={fev(combo.ev)} color={evc(combo.ev)} />
                {/* 标签跟着设置里的凯利比例走。之前这里写死成「半凯利建议」，
                    但金额其实一直是按设置算的——所以你把设置改成四分之一凯利时，
                    数字变了、标签没变，看起来像「只会推荐半凯利」。 */}
                <MiniStat label={`${kellyLabel} 建议`} val={combo.kelly_amount ? combo.kelly_amount.toLocaleString() : "0"} color={C.purple} sub={pct(combo.kelly_pct)} />
              </div>
              <div style={{ fontSize: 10, color: C.textDim, marginBottom: 8 }}>
                相对最弱一腿（{combo.weakest_leg_label} {pct(combo.weakest_leg_prob)}）命中率打了 {(combo.risk_ratio_vs_weakest_leg * 10).toFixed(1)} 折
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 4, marginBottom: 10 }}>
                {combo.legs.map((leg, j) => (
                  <div key={j} style={{ display: "flex", justifyContent: "space-between", fontSize: 11, background: C.bg, borderRadius: 6, padding: "6px 9px" }}>
                    <span>{leg.label}</span>
                    <span style={{ color: C.textDim }}>@{leg.odds} · {pct(leg.prob)}</span>
                  </div>
                ))}
              </div>

              <div style={{ display: "flex", gap: 6, borderTop: `1px solid ${C.border}`, paddingTop: 10 }}>
                <button
                  onClick={() => recordParlay(combo, i, "virtual")}
                  disabled={recording === `virtual-${i}` || recorded === `virtual-${i}`}
                  style={{ flex: 1, padding: "9px 4px", borderRadius: 6, border: "none", background: C.blue, color: "#fff", fontWeight: 700, fontSize: 12 }}
                >
                  {recorded === `virtual-${i}` ? "✅ 已记录" : recording === `virtual-${i}` ? "..." : `🎲 记虚拟盘（${Math.round(combo.kelly_amount || 0).toLocaleString()}）`}
                </button>
                <button
                  onClick={() => setShowRealForm(showRealForm === i ? null : i)}
                  style={{ flex: 1, padding: "9px 4px", borderRadius: 6, border: `1px solid ${C.purple}`, background: showRealForm === i ? C.purple : "transparent", color: showRealForm === i ? "#0a0510" : C.purple, fontWeight: 700, fontSize: 12 }}
                >
                  💵 记实盘
                </button>
              </div>

              {showRealForm === i && (
                <div style={{ marginTop: 10, paddingTop: 10, borderTop: `1px solid ${C.border}` }}>
                  <div style={{ fontSize: 10, color: C.textDim, marginBottom: 6 }}>
                    填你在平台实际下的金额和拿到的总赔率。总赔率不填就用各腿相乘（{combo.combined_odds.toFixed(2)}）——
                    但平台的串关定价不一定等于乘积，填真实值账面才准。
                  </div>
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr auto", gap: 6 }}>
                    <input
                      type="number" inputMode="decimal" placeholder={`金额（建议 ${Math.round(combo.kelly_amount || 0)}）`}
                      value={realStake[i] || ""}
                      onChange={e => setRealStake(s => ({ ...s, [i]: e.target.value }))}
                      style={{ background: C.bg, border: `1px solid ${C.purple}66`, borderRadius: 6, padding: "9px 8px", color: C.text, fontSize: 16 }}
                    />
                    <input
                      type="number" step="0.01" inputMode="decimal" placeholder={`总赔率 ${combo.combined_odds.toFixed(2)}`}
                      value={realOdds[i] || ""}
                      onChange={e => setRealOdds(s => ({ ...s, [i]: e.target.value }))}
                      style={{ background: C.bg, border: `1px solid ${C.purple}66`, borderRadius: 6, padding: "9px 8px", color: C.text, fontSize: 16 }}
                    />
                    <button
                      onClick={() => recordParlay(combo, i, "real")}
                      disabled={recording === `real-${i}` || recorded === `real-${i}`}
                      style={{ padding: "9px 14px", borderRadius: 6, border: "none", background: C.purple, color: "#0a0510", fontWeight: 800, fontSize: 12, whiteSpace: "nowrap" }}
                    >
                      {recorded === `real-${i}` ? "✅" : recording === `real-${i}` ? "..." : "确认"}
                    </button>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ══════════════════════════════════════════════════════════
// 价格策略页
// ══════════════════════════════════════════════════════════
// 这一页跟「预测」页是两套独立的东西，刻意不显示任何模型概率。
// handoff/09 用 141,287 场证明模型对市场价格的增量信息为零（t=-0.2），
// 所以这里的判断只看两件事：赔率落在偏差的哪一侧、你拿到的价有多好。
// 把模型概率摆在旁边只会让人以为它参与了决策。

const LEVEL_COLOR = {
  ok: C.accent, bad_price: C.red, avoid: C.red,
  need_price_check: C.gold, none: C.textDim, unknown: C.textDim,
};

function StrategyTab() {
  const [odds, setOdds] = useState("");
  const [avg, setAvg] = useState("");
  const [best, setBest] = useState("");
  const [res, setRes] = useState(null);
  const [legs, setLegs] = useState([]);
  const [parlay, setParlay] = useState(null);
  const [margin, setMargin] = useState(0);
  const [log, setLog] = useState(null);
  const [desc, setDesc] = useState("");
  const [saving, setSaving] = useState(false);
  // 默认走「同场三个赔率」——它只需要你自己平台的报价，不依赖任何行情数据
  const [mode, setMode] = useState("vig");
  const [drawOdds, setDrawOdds] = useState("");
  const [awayOdds, setAwayOdds] = useState("");
  const [pick, setPick] = useState(null);

  const loadLog = useCallback(async () => {
    try { setLog(await api("/price-log")); } catch { /* 后端没起时静默 */ }
  }, []);
  useEffect(() => { loadLog(); }, [loadLog]);

  // 赔率一变就重算，不需要按按钮。
  //
  // 两条判断路径，取决于你手上有什么：
  //   A「同场三个赔率」——只要你自己平台的主/平/客，就能算出这家的抽水，
  //     而抽水才是真正吃掉优势的量。多数人只有这个，所以它是默认路径。
  //   B「跨平台比价」——填市场平均价和最高价，算价格捕获率。更精细，
  //     但要求你能查到行情。
  // 两条路是同一批实测数据的两种坐标表达，结论一致。
  useEffect(() => {
    const o = parseFloat(odds);
    const a = parseFloat(avg), b = parseFloat(best);
    const d = parseFloat(drawOdds), w = parseFloat(awayOdds);
    let cancelled = false;

    if (mode === "vig") {
      if (!(o > 1 && d > 1 && w > 1)) { setRes(null); return; }
      api("/strategy/evaluate-simple", {
        method: "POST",
        body: JSON.stringify({ odds_home: o, odds_draw: d, odds_away: w, pick }),
      }).then(r => { if (!cancelled) setRes(r); })
        .catch(() => { if (!cancelled) setRes(null); });
    } else {
      if (!o || o <= 1) { setRes(null); return; }
      const body = { odds: o };
      if (a > 1 && b > 1) { body.market_avg = a; body.market_best = b; }
      api("/strategy/evaluate", { method: "POST", body: JSON.stringify(body) })
        .then(r => { if (!cancelled) setRes(r); })
        .catch(() => { if (!cancelled) setRes(null); });
    }
    return () => { cancelled = true; };
  }, [mode, odds, avg, best, drawOdds, awayOdds, pick]);

  useEffect(() => {
    if (!legs.length) { setParlay(null); return; }
    api("/strategy/parlay", {
      method: "POST",
      body: JSON.stringify({ leg_edges: legs.map(l => l.edge), margin_per_leg: margin }),
    }).then(setParlay).catch(() => setParlay(null));
  }, [legs, margin]);

  const canAddLeg = res && res.expected_roi != null;

  async function saveLog() {
    const o = parseFloat(odds), a = parseFloat(avg), b = parseFloat(best);
    if (!(o > 1 && a > 1 && b > 1)) return;
    setSaving(true);
    try {
      await api("/price-log", {
        method: "POST",
        body: JSON.stringify({ match_desc: desc || null, my_odds: o, market_avg: a, market_best: b }),
      });
      await loadLog();
      setDesc("");
    } finally { setSaving(false); }
  }

  const inp = { width: "100%", padding: "8px 10px", borderRadius: 7, border: `1px solid ${C.border}`,
                background: C.surface, color: C.text, fontSize: 14, fontWeight: 700 };
  const lbl = { fontSize: 10, color: C.textDim, marginBottom: 4, fontWeight: 700 };

  return (
    <div style={{ display: "grid", gap: 12 }}>
      <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 10, padding: 14 }}>
        <SL>输入赔率</SL>
        <div style={{ display: "flex", gap: 6, marginTop: 8, marginBottom: 10, flexWrap: "wrap" }}>
          {[["vig", "同场三个赔率"], ["capture", "跨平台比价"]].map(([k, l]) => (
            <button key={k} onClick={() => { setMode(k); setRes(null); }}
              style={{ padding: "6px 12px", borderRadius: 7, fontSize: 11, fontWeight: 700,
                       border: `1px solid ${mode === k ? C.accent : C.border}`,
                       background: mode === k ? C.accentDim : "transparent",
                       color: mode === k ? C.accent : C.textDim }}>{l}</button>
          ))}
        </div>

        {mode === "vig" ? (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(110px, 1fr))", gap: 10 }}>
              <div><div style={lbl}>主胜 *</div>
                <input style={inp} value={odds} onChange={e => setOdds(e.target.value)} placeholder="1.85" inputMode="decimal" /></div>
              <div><div style={lbl}>平局 *</div>
                <input style={inp} value={drawOdds} onChange={e => setDrawOdds(e.target.value)} placeholder="3.50" inputMode="decimal" /></div>
              <div><div style={lbl}>客胜 *</div>
                <input style={inp} value={awayOdds} onChange={e => setAwayOdds(e.target.value)} placeholder="4.20" inputMode="decimal" /></div>
            </div>
            <div style={{ display: "flex", gap: 6, marginTop: 9, flexWrap: "wrap", alignItems: "center" }}>
              <span style={{ ...lbl, marginBottom: 0 }}>押哪边</span>
              {[[null, "自动（热门侧）"], ["home", "主胜"], ["draw", "平局"], ["away", "客胜"]].map(([k, l]) => (
                <button key={String(k)} onClick={() => setPick(k)}
                  style={{ padding: "4px 9px", borderRadius: 6, fontSize: 10, fontWeight: 700,
                           border: `1px solid ${pick === k ? C.accent : C.border}`,
                           background: pick === k ? C.accentDim : "transparent",
                           color: pick === k ? C.accent : C.textDim }}>{l}</button>
              ))}
            </div>
            <div style={{ fontSize: 10, color: C.textDim, marginTop: 9, lineHeight: 1.6 }}>
              <strong style={{ color: C.text }}>只要你自己平台的三个赔率就够了</strong>，不用查行情。
              系统从这三个数算出这家的抽水，而抽水才是真正吃掉优势的量：
              抽水 <strong style={{ color: C.text }}>低于 1.95%</strong> 押热门才有得赚，
              6.4% 的话是 -3.28%。多数软盘 1X2 抽水 5-7%，只有锐盘和交易所能到 2% 附近。
            </div>
          </>
        ) : (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 10 }}>
              <div><div style={lbl}>你的平台赔率 *</div>
                <input style={inp} value={odds} onChange={e => setOdds(e.target.value)} placeholder="1.45" inputMode="decimal" /></div>
              <div><div style={lbl}>市场平均赔率</div>
                <input style={inp} value={avg} onChange={e => setAvg(e.target.value)} placeholder="1.42" inputMode="decimal" /></div>
              <div><div style={lbl}>全市场最高赔率</div>
                <input style={inp} value={best} onChange={e => setBest(e.target.value)} placeholder="1.50" inputMode="decimal" /></div>
            </div>
            <div style={{ fontSize: 10, color: C.textDim, marginTop: 8, lineHeight: 1.6 }}>
              这条路更精细，但要求你能查到行情。查不到就用「同场三个赔率」——
              两者是同一批实测数据的两种坐标表达，结论一致。
            </div>
          </>
        )}
      </div>

      {res && (
        <div style={{ background: C.card, border: `1px solid ${LEVEL_COLOR[res.level]}55`, borderRadius: 10, padding: 14 }}>
          <div style={{ display: "flex", gap: 14, flexWrap: "wrap", marginBottom: 10 }}>
            <MiniStat label="赔率档位" val={res.band || "—"} color={C.text} />
            <MiniStat label="该档净超额" val={res.net_edge_best_price != null ? fev(res.net_edge_best_price) : "—"}
                      color={evc(res.net_edge_best_price)} sub={res.n_bets ? `${res.n_bets.toLocaleString()} 注实测` : null} />
            {res.price_capture != null &&
              <MiniStat label="价格捕获率" val={pct(res.price_capture)}
                        color={res.price_capture >= 0.8 ? C.accent : res.price_capture >= 0.6 ? C.gold : C.red} />}
            {/* 抽水口径下显示的是这家平台的抽水，不是捕获率——两者互斥 */}
            {res.vig != null &&
              <MiniStat label="这家的抽水" val={pct(res.vig)}
                        color={res.vig <= (res.breakeven_vig ?? 0.0195) ? C.accent : C.red}
                        sub={`平衡线 ${pct(res.breakeven_vig ?? 0.0195)}`} />}
            {res.expected_roi != null &&
              <MiniStat label="预期 ROI" val={fev(res.expected_roi)} color={evc(res.expected_roi)} />}
          </div>
          <div style={{ fontSize: 12, color: C.text, lineHeight: 1.7, background: C.surface,
                        borderRadius: 7, padding: "9px 11px" }}>
            {res.text}
          </div>

          <div style={{ display: "flex", gap: 8, marginTop: 10, flexWrap: "wrap" }}>
            <button
              disabled={!canAddLeg}
              onClick={() => { setLegs(l => [...l, { odds: parseFloat(odds), edge: res.expected_roi }]); }}
              style={{ padding: "7px 13px", borderRadius: 7, fontSize: 11, fontWeight: 800,
                       border: `1px solid ${canAddLeg ? C.accent : C.border}`,
                       background: canAddLeg ? C.accentDim : "transparent",
                       color: canAddLeg ? C.accent : C.textDim }}>
              ＋ 加入串关
            </button>
            <input value={desc} onChange={e => setDesc(e.target.value)} placeholder="比赛备注（选填）"
                   style={{ ...inp, flex: "1 1 140px", minWidth: 0, fontSize: 11, fontWeight: 600 }} />
            <button disabled={saving || !(parseFloat(avg) > 1 && parseFloat(best) > 1)} onClick={saveLog}
                    style={{ padding: "7px 13px", borderRadius: 7, fontSize: 11, fontWeight: 800,
                             border: `1px solid ${C.blue}`, background: C.blueDim, color: C.blue }}>
              {saving ? "…" : "记录这次价格"}
            </button>
          </div>
          {!canAddLeg && (
            <div style={{ fontSize: 10, color: C.textDim, marginTop: 6 }}>
              要加入串关得先填市场平均价和最高价——串关会把价格好坏一起放大，
              没有捕获率就算不出这条腿到底是正是负。
            </div>
          )}
        </div>
      )}

      {legs.length > 0 && (
        <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 10, padding: 14 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <SL>串关（{legs.length} 腿）</SL>
            <button onClick={() => setLegs([])} style={{ fontSize: 10, color: C.textDim, background: "none", border: "none" }}>清空</button>
          </div>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", margin: "8px 0" }}>
            {legs.map((l, i) => (
              <span key={i} style={{ fontSize: 11, padding: "4px 9px", borderRadius: 6,
                                     background: C.surface, border: `1px solid ${C.border}`, color: C.text }}>
                @{fod(l.odds)} <span style={{ color: evc(l.edge) }}>{fev(l.edge)}</span>
                <button onClick={() => setLegs(x => x.filter((_, j) => j !== i))}
                        style={{ marginLeft: 6, background: "none", border: "none", color: C.textDim }}>×</button>
              </span>
            ))}
          </div>
          <div style={{ ...lbl, marginTop: 6 }}>平台串关抽水（每腿）</div>
          <select value={margin} onChange={e => setMargin(+e.target.value)} style={{ ...inp, maxWidth: 300, fontSize: 12 }}>
            <option value={0}>0%（串关赔率 = 各腿相乘）</option>
            <option value={0.01}>1%</option>
            <option value={0.02}>2%（多数平台）</option>
            <option value={0.03}>3%</option>
          </select>
          <div style={{ fontSize: 10, color: C.textDim, marginTop: 5, lineHeight: 1.6 }}>
            实测方法：把各腿赔率乘起来，跟平台给的串关总赔率对一下，差多少就是抽水。
            这个数很关键——单腿优势撑不住每腿 2% 的话，串关反而不如分开下。
          </div>
          {parlay && (
            <div style={{ marginTop: 11, fontSize: 12, color: C.text, lineHeight: 1.7,
                          background: C.surface, borderRadius: 7, padding: "9px 11px",
                          border: `1px solid ${LEVEL_COLOR[parlay.level]}44` }}>
              <div style={{ fontSize: 18, fontWeight: 900, color: evc(parlay.net_edge), marginBottom: 5 }}>
                {fev(parlay.net_edge)}
              </div>
              {parlay.text}
            </div>
          )}
        </div>
      )}

      {log && (
        <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 10, padding: 14 }}>
          <SL>你的平台价格捕获率（{log.summary.n} 条观测）</SL>
          <div style={{ fontSize: 13, color: log.summary.enough ? C.text : C.textDim,
                        marginTop: 8, lineHeight: 1.7 }}>
            {log.summary.mean_capture != null && (
              <span style={{ fontSize: 24, fontWeight: 900,
                             color: log.summary.mean_capture >= 0.6 ? C.accent
                                  : log.summary.mean_capture <= 0.2 ? C.red : C.gold,
                             marginRight: 10 }}>
                f = {pct(log.summary.mean_capture)}
              </span>
            )}
            {log.summary.verdict}
          </div>
          {log.rows.length > 0 && (
            <div style={{ marginTop: 10, maxHeight: 200, overflowY: "auto" }}>
              {log.rows.slice(0, 30).map(r => (
                <div key={r.id} style={{ display: "flex", justifyContent: "space-between",
                                         fontSize: 11, padding: "5px 0", borderBottom: `1px solid ${C.muted}` }}>
                  <span style={{ color: C.textDim }}>{r.match_desc || fdatetime(r.logged_at)}</span>
                  <span style={{ color: C.text }}>
                    {fod(r.my_odds)} <span style={{ color: C.textDim }}>(均{fod(r.market_avg)} / 高{fod(r.market_best)})</span>
                    <strong style={{ marginLeft: 8, color: r.capture >= 0.6 ? C.accent : r.capture <= 0.2 ? C.red : C.gold }}>
                      {r.capture != null ? pct(r.capture) : "—"}
                    </strong>
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {res?.reality_check && (
        <div style={{ background: C.redDim, border: `1px solid ${C.red}44`, borderRadius: 10, padding: 13 }}>
          <div style={{ fontSize: 11, fontWeight: 800, color: C.red, marginBottom: 7 }}>⚠ 三个不该被正数字盖掉的约束</div>
          {Object.values(res.reality_check).map((t, i) => (
            <div key={i} style={{ fontSize: 11, color: C.text, lineHeight: 1.7, marginBottom: 5 }}>· {t}</div>
          ))}
        </div>
      )}
    </div>
  );
}

function MiniStat({ label, val, color, sub }) {
  return (
    <div>
      <div style={{ fontSize: 9, color: C.textDim, marginBottom: 2 }}>{label}</div>
      <div style={{ fontSize: 15, fontWeight: 900, color }}>{val}</div>
      {sub && <div style={{ fontSize: 9, color: C.textDim }}>{sub}</div>}
    </div>
  );
}

// ── Small components ──────────────────────────────────────────
function SL({ children }) {
  return (
    <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "1px", color: C.textDim, marginBottom: 10, marginTop: 16, display: "flex", alignItems: "center", gap: 8 }}>
      <span style={{ width: 3, height: 12, background: C.accent, borderRadius: 2, display: "inline-block" }} />
      {children}
    </div>
  );
}
function Stat({ label, val, color, sub }) {
  if (!label) return <div />;
  return (
    <div style={{ textAlign: "center" }}>
      <div style={{ fontSize: 18, fontWeight: 900, color, letterSpacing: "-0.5px" }}>{val}</div>
      <div style={{ fontSize: 9, color: C.textDim, marginTop: 3 }}>{label}</div>
      {sub && <div style={{ fontSize: 8, color: C.muted }}>{sub}</div>}
    </div>
  );
}
// 没有即将开赛的比赛时，先分清是「抓取坏了」还是「现在真的没有比赛」。
// 原来那句「或数据还未抓取——点顶部『立即更新』试试」在休赛期是误导：
// 点了也没用，上游 openfootball 根本还没发布新赛季的赛程（实测 404）。
// 库里有完赛数据就说明抓取是通的，那就该说清楚是休赛期，并告诉用户会自动接上。
function NoFixtures({ played, stale = [] }) {
  if (!played.length) {
    return <Empty text="还没有任何比赛数据——点顶部「立即更新」抓一次" />;
  }
  const last = played.reduce((a, b) => (a.date > b.date ? a : b));
  const days = Math.round((Date.now() - new Date(last.date + "T12:00:00")) / 86400000);
  return (
    <div style={{ textAlign: "center", padding: "34px 20px", color: C.textDim, lineHeight: 1.8 }}>
      <div style={{ fontSize: 28, opacity: 0.35, marginBottom: 10 }}>🌱</div>
      <div style={{ color: C.text, fontWeight: 700, marginBottom: 6 }}>休赛期，暂时没有比赛</div>
      <div style={{ fontSize: 12, maxWidth: 420, margin: "0 auto" }}>
        库里已有 <strong style={{ color: C.text }}>{played.length}</strong> 场完赛数据，
        最后一场是 <strong style={{ color: C.text }}>{last.date}</strong>（{days} 天前），
        所以抓取本身是正常的。新赛季的赛程数据源还没发布，
        发布后系统会在下次更新时<strong style={{ color: C.text }}>自动接上</strong>，不需要改任何东西。
      </div>
      <div style={{ fontSize: 12, marginTop: 12, color: C.accent }}>
        这期间「💰 价格策略」页照常可用——它只需要你手输赔率，不依赖赛程。
      </div>
      {stale.length > 0 && (
        <div style={{ fontSize: 11, marginTop: 14, color: C.textDim, maxWidth: 420, margin: "14px auto 0",
                      background: C.goldDim, border: `1px solid ${C.gold}33`, borderRadius: 8, padding: "9px 11px" }}>
          另有 <strong style={{ color: C.gold }}>{stale.length}</strong> 场记录日期已过却没抓到比分
          （最早 {stale.reduce((a, b) => (a.date < b.date ? a : b)).date}），
          已从「接下来」里排除。它们多半是数据源缺了这场的比分，不影响其他功能。
        </div>
      )}
    </div>
  );
}

// 有赛程、但一场都还没算出预测。
//
// 这个状态原来是完全不可见的：MatchCard 在没有预测时 return null，所以
// 标题写着「接下来 1446 场」、底下空空如也，看起来像功能坏了。真实排查
// 时也确实只能去翻数据库才知道是缺预测而不是缺赛程。
//
// 把后端最近一次更新的状态一并显示出来——预测生成是更新流程的最后一步，
// 更新失败或者中途被打断，最先没有的就是它。
function NoPredictions({ count, status, updating, onUpdateNow }) {
  const sev = status?.last_severity;
  return (
    <div style={{ textAlign: "center", padding: "30px 20px", color: C.textDim, lineHeight: 1.8 }}>
      <div style={{ fontSize: 28, opacity: 0.35, marginBottom: 10 }}>⏳</div>
      <div style={{ color: C.text, fontWeight: 700, marginBottom: 6 }}>赛程已就位，模型还没算出预测</div>
      <div style={{ fontSize: 12, maxWidth: 440, margin: "0 auto" }}>
        库里有 <strong style={{ color: C.text }}>{count}</strong> 场即将进行的比赛，但它们还没有对应的预测，
        所以这里暂时没有可展示的卡片。预测是更新流程的最后一步，
        比赛数量多的时候要跑上一会儿。
      </div>
      <div style={{ fontSize: 12, marginTop: 12, maxWidth: 440, margin: "12px auto 0",
                    background: C.surface, border: `1px solid ${C.border}`, borderRadius: 8, padding: "10px 12px" }}>
        后端最近一次更新：
        <strong style={{ color: sev === "error" ? C.red : sev === "warning" ? C.gold : C.accent }}>
          {status?.last_status_label || "还没有记录"}
        </strong>
        {status?.last_detail && <div style={{ marginTop: 4, color: C.textDim }}>{status.last_detail}</div>}
        {status?.last_update && <div style={{ marginTop: 4, fontSize: 11 }}>{fdatetime(status.last_update)}</div>}
      </div>
      <button onClick={onUpdateNow} disabled={updating}
        style={{ marginTop: 14, padding: "8px 18px", borderRadius: 8, border: "none",
                 background: C.accent, color: C.bg, fontWeight: 700, fontSize: 13,
                 opacity: updating ? 0.6 : 1 }}>
        {updating ? "更新中…" : "立即更新"}
      </button>
    </div>
  );
}

function Empty({ text }) {
  return (
    <div style={{ textAlign: "center", padding: "36px 20px", color: C.textDim }}>
      <div style={{ fontSize: 28, opacity: 0.3, marginBottom: 10 }}>📭</div>
      <div>{text}</div>
    </div>
  );
}
