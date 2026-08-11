import { useState, useEffect, useCallback, useRef, useMemo } from "react";
import { isAuthEnabled, supabase, getToken, signIn, signUp, signOut, supabaseUrl, supabaseKeyHint } from "./auth";
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine } from "recharts";
import { TeamLangProvider, useZhTeam, useTeamLang } from "./teamNames";

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

// 请求超时。fetch 默认**没有超时**，这不是理论问题——线上真的踩到了：
// 后端没有报错、只是挂着不回应（免费档实例内存吃紧、或者正在写库），
// 于是 loadAll 里那个 Promise.all 永远不 settle，loading 一直是 true，
// 界面就是一个转不完的圈，而且**一句报错都没有**，完全没法判断是后端慢、
// 后端挂了、还是前端自己的 bug。
//
// 45 秒是按最坏情况定的：/matches 现在有九千多场，免费档 + 远端 Postgres
// 上冷启动后的第一次请求实测要十几秒，给到三倍余量。串关搜索单独放宽到
// 120 秒——它本来就要评估二十多万个组合，免费档 CPU 慢，正常也要几十秒。
const DEFAULT_TIMEOUT_MS = 45000;
const SLOW_PATHS = { "/parlay/suggest": 120000, "/update-now": 120000 };

async function api(path, opts) {
  // 启用认证时每个请求都要带令牌。getToken 会在令牌快过期时自动续期，
  // 所以这里不需要自己管刷新。
  const token = await getToken();
  const limit = SLOW_PATHS[path] || DEFAULT_TIMEOUT_MS;
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), limit);
  let res;
  try {
    res = await fetch(API + path, {
      ...opts,
      signal: ctl.signal,
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(opts?.headers || {}),
      },
    });
  } catch (e) {
    if (e.name === "AbortError") {
      // 说清楚是"超时"而不是"连不上"——两者的排查方向完全不同：
      // 连不上是后端没起/地址错/CORS，超时是后端起着但太慢或卡死。
      const err = new Error(`${path} 超过 ${limit / 1000} 秒没有响应（后端在跑，但没回应）`);
      err.timeout = true;
      throw err;
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
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

// 实盘盈亏和 ROI。传进来的注单已经按联赛筛过了，所以这两个数会跟着
// 顶部的联赛按钮走——这正是它跟后端 /api/bankroll-summary 的唯一区别。
//
// 为什么不直接用后端那两个数：bankroll_summary 是全局接口，没有联赛概念。
// 筛到某个联赛时，同一块面板里其余六个数都只算该联赛，盈亏和 ROI 却仍然
// 是所有联赛的合计，于是「西甲 4 战全负、胜率 0%」可以跟「ROI +140%」并排
// 显示——一个联赛的亏损被另一个联赛的大串关盖住，方向都是反的。
//
// 口径跟后端保持一致，只算已结算的注单（待结算还没有输赢，计进去会让 ROI
// 在比赛开打前就被本金稀释），单场和串关一起算，提款不计入——提款是把赢到
// 的钱转走，不是一笔新的输赢。所以筛「全部」时这里算出来的应该跟后端完全
// 相等，有测试盯着这一条。
function settledPnlRoi(bets, parlays, stakeField, pnlField) {
  const sb = bets.filter(b => b.result !== "pending");
  const sp = parlays.filter(p => p.result !== "pending");
  const pnl = sb.reduce((s, b) => s + (b[pnlField] || 0), 0) +
              sp.reduce((s, p) => s + (p.pnl || 0), 0);
  const staked = sb.reduce((s, b) => s + (b[stakeField] || 0), 0) +
                 sp.reduce((s, p) => s + (p.stake || 0), 0);
  return { pnl, roi: staked ? (pnl / staked) * 100 : null, staked };
}
// 界面上只用两个词，不要再引入第三种叫法（"毛盈亏"这个词用户明确不要）：
//   盈亏   ＝ 本金 × 赔率（赢了就是这一注的总回款，输了是 0）
//   净盈亏 ＝ 盈亏 － 本金（也就是上面 settledPnlRoi 算出来的 pnl）
// 单场注单：赢时 pnl = stake*odds - stake，所以 盈亏 = pnl + stake；
// 输时 pnl = -stake，盈亏 = pnl + stake = 0，两种情况这一个公式都对，
// 不需要按赢/输分别处理。汇总多笔时同理：Σ盈亏 = Σ(pnl_i + stake_i)
// = Σpnl_i + Σstake_i = 总净盈亏 + 总本金——所以不需要单独重新算一遍
// 每笔的盈亏，直接拿 settledPnlRoi 已经算出来的 {pnl, staked} 相加即可。
const grossPnl = (pnl, staked) => pnl + staked;
// 盈亏永远 ≥ 0（它是回款金额，不是涨跌），所以不加 +/- 号——用 fnum 会
// 显示成"+92"，看着像"赚了92"，实际是"下注23拿回92"，容易看反。
// 净盈亏才是有方向的数，那个继续用 fnum 带符号显示。
const fgross = v => Math.round(v || 0).toLocaleString("zh-HK");
const realPnlRoi = (bets, parlays) => settledPnlRoi(bets, parlays, "stake_real", "pnl_real");
// 虚拟盘的单场注单字段名跟实盘不一样（stake/pnl，不是 stake_real/pnl_real）
// ——Bet 和 RealBet 是两张不同的表。虚拟盘页目前还没有串关列表（虚拟串关
// 存在，但没有 UI 展示），所以这里固定传空数组，口径先只到单场，跟这一页
// 「总注数」等其余统计目前的范围一致。
// parlays 可选——虚拟盘页自己那张卡片目前还没有虚拟串关列表 UI，沿用
// 老口径不传；总览栏这边算总盈亏时手上已经有 virtualParlays 了，传进来
// 才不会漏掉虚拟串关的盈亏，口径跟「实盘盈亏」（含真实串关）对齐。
const virtualPnlRoi = (bets, parlays = []) => settledPnlRoi(bets, parlays, "stake", "pnl");

// 资金曲线按天拆出"哪天赚了/亏了多少"。bankroll_summary 的 series 是按
// 资金事件（每笔结算）挂点的，不是按天——同一天可能有好几个点，也可能
// 好几天之间完全没有点（那几天没有任何注单结算）。想要的是"这一天收盘
// 的余额比上一个真正有数据的日子变化了多少"，所以先按日期去重只留每天
// 最后一个点，再对相邻的两个不同日期作差——这样同一天内的多笔结算会被
// 自然合并成一个净值，中间空档的日子也不会被当成"变化为 0"单独列出来。
function dailyPnl(series, key) {
  if (!series.length) return [];
  // series[0] 就是"下注发生之前"那个基准点（起始资金），不管它被标了哪个
  // 日期，数值上都代表 0 号点。第一个真正有事件的日期也要跟它比出一个
  // 差值来，不能因为"它是数组第一项、没有再前一项"就整天不显示——
  // 那样第一天真实发生的盈亏会凭空从列表里消失。
  const base = series[0][key];
  const lastOfDay = new Map();
  for (const pt of series) lastOfDay.set(pt.date, pt[key]);
  const rows = [];
  let prev = base;
  for (const date of lastOfDay.keys()) {
    const val = lastOfDay.get(date);
    const delta = val - prev;
    if (Math.abs(delta) > 0.005) rows.push({ date, delta });
    prev = val;
  }
  return rows.reverse();  // 最近的日期排在最前面
}

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

// 顶栏两个功能按钮（免责声明/设置/退出）原来是带文字的整块药丸按钮，
// 手机上三个挤在一起会跟标题抢地方，逼得整行换行。改成图标+悬浮提示，
// 每个按钮固定 30×30，头部能稳定收在一行。用 SVG 不用 emoji——
// emoji 图标在不同系统/字体下渲染差异大，线框图标风格统一，也更克制。
const Icon = {
  warn: (p) => <svg {...p} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M12 3l9 16H3z" /><path d="M12 9v4M12 16.5v.5" /></svg>,
  gear: (p) => <svg {...p} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"><circle cx="12" cy="12" r="3" /><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1" /></svg>,
  logout: (p) => <svg {...p} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" /><path d="M16 17l5-5-5-5" /><path d="M21 12H9" /></svg>,
  // 品牌标——原来是 ⚽ emoji，换成线框球图标，跟其他 SVG 图标统一风格，
  // 也不受系统/字体影响渲染成完全不同的样子。
  ball: (p) => <svg {...p} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"><circle cx="12" cy="12" r="9" /><path d="M12 7.5l3.5 2.5-1.3 4h-4.4L8.5 10z" /><path d="M12 3v4.5M4.5 8.7l3.4 1.5M4.7 15.5l3.6-1.3M19.5 8.7l-3.4 1.5M19.3 15.5l-3.6-1.3M12 16.5V21" /></svg>,
};
function IconBtn({ onClick, title, active, color = C.textDim, children }) {
  return (
    <button onClick={onClick} title={title} aria-label={title}
      style={{ width: 30, height: 30, flex: "0 0 auto", display: "flex", alignItems: "center", justifyContent: "center",
               borderRadius: 7, border: `1px solid ${active ? C.purple : C.border}`,
               background: active ? C.purpleDim : "transparent", color: active ? C.purple : color }}>
      {children}
    </button>
  );
}

// 上一次成功加载的数据，存在浏览器本地。
//
// 为什么要有它：每次打开页面都要等 8 个接口全部回来才有东西看，其中
// /api/matches 是三千多场比赛。本地还好，云端要连远端 Postgres，
// 每次进来都白等几秒——用户反馈的「每次进网页都会加载一次，很消耗时间」。
//
// 改成先把上次的数据画出来、同时在后台重新拉。数据可能旧几秒，但赛程和
// 预测本来就是十二小时更新一次的东西，旧几秒没有任何影响；而「立刻有东西看」
// 的差别很大。
//
// 按用户隔离：云端换账号登录时，绝不能读到上一个账号的实盘记录。
// 退出登录时直接清掉。
const CACHE_KEY = "vb_cache_v2";
const DISCLAIMER_SEEN_KEY = "vb_disclaimer_seen_v1";

function readCache(userKey) {
  try {
    const c = JSON.parse(localStorage.getItem(CACHE_KEY) || "null");
    return c && c.user === userKey ? c : null;
  } catch {
    return null;              // 存的东西坏了就当没有，不要让它把整个页面拖挂
  }
}

function writeCache(userKey, payload) {
  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify({ user: userKey, at: Date.now(), ...payload }));
  } catch {
    // 配额满了（这份数据接近 1MB）就放弃缓存。它是纯优化，失败不该影响功能。
  }
}

// ══════════════════════════════════════════════════════════
export default function App() {
  return (
    <TeamLangProvider>
      <AppInner />
    </TeamLangProvider>
  );
}

function AppInner() {
  const zhTeam = useZhTeam();
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
  const [withdrawals, setWithdrawals] = useState([]);
  const [refreshing, setRefreshing] = useState(false);   // 用缓存先画出来、后台正在刷新
  const [refreshFailed, setRefreshFailed] = useState(false);
  const [showSett, setShowSett] = useState(false);
  const [showDisclaimer, setShowDisclaimer] = useState(false);
  const [loading, setLoading]   = useState(true);
  const [apiError, setApiError] = useState(null);
  const [updating, setUpdating] = useState(false);
  const [starting, setStarting] = useState(false);   // 后端还在启动中
  const [session, setSession] = useState(null);
  const [authReady, setAuthReady] = useState(!isAuthEnabled);
  const retryRef = useRef(0);

  // silent：已经用缓存把界面画出来了，这一轮是后台刷新。
  // 不能再翻回加载态——否则缓存刚画出来的内容立刻被 spinner 盖掉，
  // 缓存等于白做（第一版就是这样，实测第二次打开仍然是空白页）。
  const loadAll = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    setApiError(null);
    try {
      const [st, all, bt, vb, rb, br, se, cp, pl, wd] = await Promise.all([
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
        api("/withdrawals"),
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
      setWithdrawals(wd || []);
      setRefreshFailed(false);          // 这一轮成功了，清掉上一轮的失败标记
      writeCache(session?.user?.id || "local", {
        status: st, matches: all, backtest: bt.by_competition || [], competitions: cp || [],
        bets: vb, realBets: rb, bankroll: br, settings: se, parlayBets: pl || [], withdrawals: wd || [],
      });
    } catch (e) {
      // 刚启动那十几秒后端可能还没就绪（uvicorn 还没绑定端口，或者首次
      // 全量更新正在写库）。直接甩「无法连接本地后端」会让人以为服务没起，
      // 其实再等几秒就好了。所以先默默重试几轮，真连不上才报错。
      if (e.unauthorized) {          // 没登录/令牌过期 —— 不是后端挂了
        setSession(null); setApiError(null); setStarting(false);
        setLoading(false);
        return;
      }
      // 超时**不重试**。上面那套重试是为"后端还没就绪"设计的——那种情况
      // 连接会立刻被拒，重试几轮很快就成了。超时是另一回事：后端在跑，只是
      // 慢或者卡住了，再打六次只会往一台已经吃力的机器上继续堆请求，而且
      // 6×45 秒等于四分半钟不给用户任何反馈。直接把超时报出来。
      if (e.timeout) {
        if (silent) setRefreshFailed(true);
        else { setApiError(e.message); setStarting(false); }
        return;
      }
      if (retryRef.current < 6) {
        retryRef.current += 1;
        if (!silent) setStarting(true);
        setTimeout(() => loadAll(silent), 2500);
        return;
      }
      // 后台刷新失败时不要把缓存内容换成错误页——旧数据比什么都没有有用。
      // 只在横幅上说明一句，让用户知道看到的不是最新的。
      if (silent) setRefreshFailed(true);
      else { setApiError(e.message); setStarting(false); }
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
    retryRef.current = 0;
    setStarting(false);
  }, [session]);

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
    // 有缓存就先画出来，页面立刻可用；同时照常去后台拉最新的。
    const cached = readCache(session?.user?.id || "local");
    if (cached) {
      setStatus(cached.status);
      setMatches(cached.matches || []);
      setBacktestByComp(cached.backtest || []);
      setCompetitions(cached.competitions || []);
      setBets(cached.bets || []);
      setRealBets(cached.realBets || []);
      setBankroll(cached.bankroll);
      setSettings(cached.settings);
      setParlayBets(cached.parlayBets || []);
      setWithdrawals(cached.withdrawals || []);
      setLoading(false);
      setRefreshing(true);
    }
    loadAll(!!cached);
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

  // 免责声明只自动弹一次——本地存个标记，下次进来不会一直弹。
  // 顶部一直留着「⚠️ 免责声明」的入口，想再看随时点得到，
  // 不需要靠自动弹窗才能找到它。
  useEffect(() => {
    try {
      if (!localStorage.getItem(DISCLAIMER_SEEN_KEY)) setShowDisclaimer(true);
    } catch { /* localStorage 不可用（隐私模式等）就不自动弹，不影响使用 */ }
  }, []);

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

  // 取消一笔还没结算的下注（单场虚拟/单场实盘/串关，用同一个函数是因为
  // 后端三个端点的删除语义完全一样：只能删 pending 的，成功后都要刷新）。
  // path 是 "/bets/{id}" | "/real-bets/{id}" | "/parlay-bets/{id}"。
  // confirm() 是刻意加的——这是不可逆操作，比"点错了会刷新页面"更需要
  // 一次明确的二次确认，而不是靠 UI 布局去防误触。
  const [cancelling, setCancelling] = useState(null);
  async function cancelBet(path, label) {
    if (!window.confirm(`确定取消这笔${label}吗？取消后无法恢复。`)) return;
    setCancelling(path);
    try {
      await api(path, { method: "DELETE" });
      await loadAll(true);
    } catch (e) {
      alert("取消失败: " + e.message);
    } finally {
      setCancelling(null);
    }
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
  // 串关按「有任意一条腿属于该联赛」来筛。串关跨联赛是常态，没法归给
  // 单一联赛，任一腿命中就算它属于这个联赛是唯一说得通的口径。
  // 腿上没有 competition_id 时同样不过滤掉，跟上面单场注单的处理一致。
  const shownParlays  = parlayBets.filter(p => comp == null ||
    (p.legs || []).some(l => l.competition_id == null || l.competition_id === comp));

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
          <div style={{ color: C.red, marginBottom: 12 }}><Icon.warn width={28} height={28} /></div>
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
        /* 标签栏/联赛栏横向滚动，不显示滚动条——手感上更像原生的分段
           控件，露出滚动条会让人以为是没排好版，而不是"划一下还有更多"。 */
        .hscroll { scrollbar-width: none; -ms-overflow-style: none; }
        .hscroll::-webkit-scrollbar { display: none; }
      `}</style>

      {/* Status banner - honest about what "automatic" means here */}
      <StatusBanner status={status} updating={updating} onUpdateNow={triggerUpdate}
                    refreshing={refreshing} refreshFailed={refreshFailed} />

      {/* Header —— 单行头部 + 图标按钮 + 横向滚动的标签/联赛栏，不再靠
          换行把导航挤成两三行。原来「⚠️ 免责声明」「⚙ 设置」「退出」
          三个带字按钮跟标题抢空间，手机上必定折行，开屏一大半被导航吃掉。
          图标+悬浮提示能固定宽度，头部稳定收在一行。
          试过顶栏单独换白色背景、内容区保持深色的分层方案，用户看完还是
          想要跟内容区一致的深色，这里维持统一的深色主题，不分层。 */}
      <div style={{ background: C.surface, borderBottom: `1px solid ${C.border}`, padding: "10px 14px", position: "sticky", top: 0, zIndex: 30 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 9, minWidth: 0 }}>
            <div style={{ width: 28, height: 28, flex: "0 0 auto", borderRadius: 7, background: `linear-gradient(135deg,${C.accent},${C.blue})`, display: "flex", alignItems: "center", justifyContent: "center", color: "#fff" }}>
              <Icon.ball width={16} height={16} />
            </div>
            <div style={{ fontWeight: 800, fontSize: 13.5, letterSpacing: "-0.2px", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>ValueBet 精算系统</div>
          </div>
          <div style={{ display: "flex", gap: 4, flex: "0 0 auto" }}>
            <IconBtn onClick={() => setShowDisclaimer(true)} title="免责声明">
              <Icon.warn width={15} height={15} />
            </IconBtn>
            <IconBtn onClick={() => setShowSett(s => !s)} title="资金与凯利设置" active={showSett}>
              <Icon.gear width={15} height={15} />
            </IconBtn>
            {isAuthEnabled && session && (
              <IconBtn
                onClick={async () => {
                  // 缓存里有实盘记录和资金数字，退出时必须清掉——
                  // 否则换个人在同一台设备上登录，先看到的是上一个人的数据
                  try { localStorage.removeItem(CACHE_KEY); } catch {}
                  await signOut(); setSession(null);
                }}
                title={`退出登录（${session.user?.email}）`}
              >
                <Icon.logout width={15} height={15} />
              </IconBtn>
            )}
          </div>
        </div>

        {/* 标签栏：emoji 换成纯文字——emoji 当图标在不同系统下渲染差异大，
            给别人用会显得随意；横向滚动代替换行，六个标签固定一行，不占
            第二行空间。className="hscroll" 的滚动条隐藏规则见下面全局 style。 */}
        <div className="hscroll" style={{ display: "flex", gap: 4, marginTop: 9, overflowX: "auto" }}>
          {[["upcoming", "预测"], ["parlay", "串关推荐"], ["backtest", "回测"], ["bets", "虚拟盘"], ["realbets", "实盘"], ["chart", "走势"]].map(([k, l]) => (
            <button
              key={k}
              onClick={() => setTab(k)}
              style={{ padding: "5px 11px", borderRadius: 7, border: `1px solid ${tab === k ? C.accent : C.border}`, background: tab === k ? C.accentDim : "transparent", color: tab === k ? C.accent : C.textDim, fontSize: 11, fontWeight: 700, whiteSpace: "nowrap", flex: "0 0 auto" }}
            >
              {l}
            </button>
          ))}
        </div>

        {competitions.length > 0 && (
          <div className="hscroll" style={{ display: "flex", gap: 4, marginTop: 7, overflowX: "auto", alignItems: "center" }}>
            <span style={{ fontSize: 10, color: C.textDim, fontWeight: 700, marginRight: 2, flex: "0 0 auto" }}>赛事</span>
            {[{ id: null, name_zh: "全部" }, ...competitions].map(c => {
              const on = comp === c.id;
              const n = c.id == null ? matches.length : matches.filter(m => m.competition_id === c.id).length;
              return (
                <button key={String(c.id)} onClick={() => setComp(c.id)}
                  style={{ padding: "4px 9px", borderRadius: 6, fontSize: 10, fontWeight: 700, whiteSpace: "nowrap", flex: "0 0 auto",
                           border: `1px solid ${on ? C.blue : C.border}`,
                           background: on ? C.blueDim : "transparent",
                           color: on ? C.blue : C.textDim,
                           fontVariantNumeric: "tabular-nums" }}>
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

      {showDisclaimer && (
        <DisclaimerModal onClose={() => {
          try { localStorage.setItem(DISCLAIMER_SEEN_KEY, "1"); } catch {}
          setShowDisclaimer(false);
        }} />
      )}

      {/* 串关（实盘）算进顶部这条总览栏，跟 RealBetsTab 用同一份过滤。
          串关记在另一张表（ParlayBet），早先这一栏的注数只数 RealBet、
          从来没把串关计进来，用户反馈"数字比实际少"就是这个，现在两边都算。
          这一栏只放实盘相关的数字（虚拟盘的注数/盈亏在「虚拟盘」页单独看），
          盈亏/净盈亏两个词的定义见 grossPnl 上方。 */}
      {/* Stats bar */}
      {backtest && (() => {
        const realParlays = shownParlays.filter(p => p.kind === "real");
        // 待结算下注 / 投注金额：现在还压着多少注、多少钱没结算——真实下注
        // 加真实串关里 result === "pending" 的部分。两个数刻意用同一份过滤
        // 结果算，保证"几注"和"多少钱"永远说的是同一批注单，不会一个算了
        // 串关另一个没算。
        const pendingRealBets = shownRealBets.filter(b => b.result === "pending");
        const pendingRealParlays = realParlays.filter(p => p.result === "pending");
        const pendingCount = pendingRealBets.length + pendingRealParlays.length;
        const pendingStake =
          pendingRealBets.reduce((s, b) => s + (b.stake_real || 0), 0) +
          pendingRealParlays.reduce((s, p) => s + (p.stake || 0), 0);
        // 累计流水：从有记录以来一共下过多少钱，不分输赢还是待结算——
        // 跟 RealBetsTab 里的「累计流水」是同一个口径，这里在总览栏里再显示一次
        const turnover =
          shownRealBets.reduce((s, b) => s + (b.stake_real || 0), 0) +
          realParlays.reduce((s, p) => s + (p.stake || 0), 0);
        // 盈亏/净盈亏/ROI 三个都跟着联赛筛选走，跟这一栏其余几个数口径一致。
        // 不再读 bankroll.real.total_pnl / roi_pct——那两个是全局的，混在这排
        // 筛过的数字里会把某个联赛的亏损用另一个联赛的盈利盖掉。
        //
        // 盈亏＝本金×赔率（赢的话是总回款，输的话是0）＝净盈亏＋本金，
        // 见 grossPnl 的推导。ROI 用的是已结算本金做分母（settledPnlRoi
        // 内部算的 staked），跟实盘页那个「实盘ROI」完全同一个函数、
        // 同一个口径，两处不会对不上。
        const { pnl: realPnl, roi: realRoi, staked: realStakedSettled } = realPnlRoi(shownRealBets, realParlays);
        const realGrossPnl = grossPnl(realPnl, realStakedSettled);
        return (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(104px, 1fr))", background: C.border, gap: 1 }}>
          {[
            { v: `${backtest.correct}/${backtest.total}`, l: `${backtestLabel} · 预测正确`, c: C.blue },
            { v: pct(backtest.accuracy), l: `${backtestLabel} · 准确率`, c: backtest.accuracy > 0.6 ? C.accent : C.gold },
            { v: backtest.avg_rps?.toFixed(3), l: `${backtestLabel} · 平均RPS`, c: C.accent },
            { v: fgross(realGrossPnl), l: "盈亏", c: C.gold },
            { v: fnum(realPnl), l: "净盈亏", c: realPnl >= 0 ? C.accent : C.red },
            { v: realRoi == null ? "—" : realRoi.toFixed(1) + "%", l: "ROI%", c: (realRoi || 0) >= 0 ? C.accent : C.red },
            { v: pendingCount, l: "待结算下注", c: C.purple },
            { v: Math.round(pendingStake).toLocaleString(), l: "投注金额", c: C.gold },
            { v: Math.round(turnover).toLocaleString(), l: "累计流水", c: C.blue },
          ].map(({ v, l, c }) => (
            <div key={l} style={{ background: C.surface, padding: "9px 8px", textAlign: "center" }}>
              <div style={{ fontSize: 16, fontWeight: 900, color: c, lineHeight: 1, fontVariantNumeric: "tabular-nums" }}>{v}</div>
              <div style={{ fontSize: 9, color: C.textDim, textTransform: "uppercase", letterSpacing: "0.4px", marginTop: 3 }}>{l}</div>
            </div>
          ))}
        </div>
        );
      })()}

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
                                            provisional={isProvisional(m)} onRefresh={() => loadAll(true)} />} />
          </div>
        )}

        {!loading && tab === "parlay" && settings && (
          <ParlaySuggestTab upcoming={upcoming} settings={settings} parlayBets={parlayBets}
            realBets={realBets} onRefresh={() => loadAll(true)} />
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
                            <span style={{ fontWeight: 600 }}>{zhTeam(m.team1)} <span style={{ color: C.textDim }}>vs</span> {zhTeam(m.team2)}</span>
                            <span style={{ textAlign: "center", color: p.prob_home > p.prob_draw && p.prob_home > p.prob_away ? C.accent : C.textDim }}>{pct(p.prob_home)}</span>
                            <span style={{ textAlign: "center", color: p.prob_draw > p.prob_home && p.prob_draw > p.prob_away ? C.accent : C.textDim }}>{pct(p.prob_draw)}</span>
                            <span style={{ textAlign: "center", color: p.prob_away > p.prob_home && p.prob_away > p.prob_draw ? C.accent : C.textDim }}>{pct(p.prob_away)}</span>
                            <span style={{ textAlign: "center", fontWeight: 700 }}>{m.score1}-{m.score2}</span>
                            <span style={{ textAlign: "center", color: C.blue }}>{p.rps != null ? p.rps.toFixed(3) : "—"}</span>
                            <span style={{ textAlign: "center", fontWeight: 700, color: p.is_correct ? C.accent : C.red }}>{p.is_correct ? "✓" : "✕"}</span>
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

        {!loading && tab === "bets" && settings && (() => {
          // 跟实盘那边同一个 bug：总盈亏/ROI 原来读全局 bankroll.virtual，
          // 不跟着联赛筛选走，而这块面板里其余四个数都是筛过的
          // shownBets——筛到某个联赛时会出现同样的「一个联赛的亏损被另一个
          // 联赛盖住」。改成用筛过的 shownBets 自己算。
          const { pnl: vPnl, roi: vRoi } = virtualPnlRoi(shownBets);
          return (
          <div>
            <SL>虚拟下注 · 起始 {(+settings.bankroll_total).toLocaleString()} 单位</SL>
            <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 10, padding: 14, marginBottom: 14, display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(110px, 1fr))", gap: 12 }}>
              <Stat label="总注数" val={shownBets.length} color={C.blue} />
              <Stat label="赢注" val={shownBets.filter(b => b.result === "win").length} color={C.accent} />
              <Stat label="待结算" val={shownBets.filter(b => b.result === "pending").length} color={C.gold} />
              <Stat label="总盈亏" val={fnum(vPnl)} color={vPnl >= 0 ? C.accent : C.red} />
              <Stat label="ROI" val={vRoi == null ? "—" : vRoi.toFixed(1) + "%"} color={(vRoi || 0) >= 0 ? C.accent : C.red} />
              <Stat label="胜率" val={shownBets.length ? pct(shownBets.filter(b => b.result === "win").length / shownBets.length) : "—"} color={C.blue} />
              <Stat label="" val="" color={C.textDim} />
              <Stat label="" val="" color={C.textDim} />
            </div>
            {shownBets.length === 0 && <Empty text="还没有虚拟下注。去「预测」页输入赔率，点「虚拟」。" />}
            {shownBets.length > 0 && (
              <div style={{ background: C.surface, borderRadius: 10, overflow: "hidden", border: `1px solid ${C.border}` }}>
                <div style={{ display: "grid", gridTemplateColumns: "64px 1fr 60px 52px 52px 52px 60px 40px 44px", padding: "7px 12px", background: C.muted, fontSize: 9, color: C.textDim, fontWeight: 700, textTransform: "uppercase", gap: 3 }}>
                  {["日期", "赛事", "方向", "赔率", "本金", "EV", "盈亏", "结果", ""].map(h => <span key={h}>{h}</span>)}
                </div>
                {shownBets.map(b => (
                  <div key={b.id} style={{ display: "grid", gridTemplateColumns: "64px 1fr 60px 52px 52px 52px 60px 40px 44px", padding: "7px 12px", borderBottom: `1px solid ${C.border}`, background: b.result === "win" ? C.accentDim : b.result === "loss" ? C.redDim : "transparent", gap: 3, alignItems: "center", fontSize: 11 }}>
                    <span style={{ color: C.textDim }}>{fdt(b.date)}</span>
                    <span style={{ fontWeight: 600 }}>{zhTeam(b.team1)} vs {zhTeam(b.team2)}</span>
                    <span style={{ color: C.textDim }}>{b.outcome === "home" ? "主胜" : b.outcome === "away" ? "客胜" : "平局"}</span>
                    <span style={{ fontWeight: 700 }}>{fod(b.odds_used)}</span>
                    <span>{b.stake}</span>
                    <span style={{ color: evc(b.ev_at_bet || 0) }}>{fev(b.ev_at_bet)}</span>
                    <span style={{ fontWeight: 700, color: (b.pnl || 0) > 0 ? C.accent : (b.pnl || 0) < 0 ? C.red : C.textDim }}>{b.pnl != null ? fnum(b.pnl) : "待定"}</span>
                    <span style={{ fontWeight: 700, color: b.result === "win" ? C.accent : b.result === "loss" ? C.red : C.gold }}>{b.result === "win" ? "✓" : b.result === "loss" ? "✕" : "…"}</span>
                    {/* 只有待结算的能取消——已结算的删了会悄悄改掉历史战绩，
                        后端本来就拒绝，这里提前不给按钮，省得点了才看到报错 */}
                    <span>
                      {b.result === "pending" && (
                        <button onClick={() => cancelBet(`/bets/${b.id}`, "虚拟下注")}
                          disabled={cancelling === `/bets/${b.id}`}
                          style={{ background: "none", border: `1px solid ${C.red}66`, color: C.red, borderRadius: 5, padding: "3px 6px", fontSize: 10, fontWeight: 700 }}>
                          {cancelling === `/bets/${b.id}` ? "…" : "✕"}
                        </button>
                      )}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
          );
        })()}

        {!loading && tab === "realbets" && settings && (
          <RealBetsTab realBets={shownRealBets} bankroll={bankroll} settings={settings}
            parlays={shownParlays.filter(p => p.kind === "real")} withdrawals={withdrawals}
            onCancel={cancelBet} cancelling={cancelling} onRefresh={() => loadAll(true)} />
        )}

        {!loading && tab === "chart" && bankroll && (
          <ChartTab bankroll={bankroll} settings={settings}
            bets={shownBets} realBets={shownRealBets} parlayBets={shownParlays}
            competitions={competitions} />
        )}

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
                        display: "flex", alignItems: "center", justifyContent: "center", color: "#fff" }}>
            <Icon.ball width={17} height={17} />
          </div>
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

function StatusBanner({ status, updating, onUpdateNow, refreshing, refreshFailed }) {
  if (!status) return null;

  // On a truly fresh install, the startup run may still be in flight
  // when this first renders — last_update is null for a second or two,
  // not because anything failed.
  const isFirstRun = status.last_update == null && status.last_status == null;

  return (
    <div style={{ background: C.goldDim, borderBottom: `1px solid ${C.gold}44`, padding: "7px 16px", display: "flex", alignItems: "center", justifyContent: "center", gap: 14, flexWrap: "wrap", fontSize: 11, color: C.gold }}>
      {(refreshing || refreshFailed) && (
        <span style={{ color: refreshFailed ? C.red : C.textDim }}>
          {refreshFailed
            ? "后台更新失败，显示的是上次的数据"
            : "显示的是上次的数据，正在后台更新…"}
        </span>
      )}
      <span>
        {isFirstRun ? (
          <>{isAuthEnabled ? "云端运行中" : "本地运行中"} · 首次抓取数据中，几秒后自动刷新...</>
        ) : (
          <>
            {isAuthEnabled ? "云端运行中" : "本地运行中"} · 上次更新 {fdatetime(status.last_update)}
            {status.last_severity === "error" && <span style={{ color: C.red }}> · 更新失败: {status.last_detail}</span>}
            {status.last_severity === "warning" && <span style={{ color: C.gold }}> · {status.last_status_label}{status.last_detail ? `：${status.last_detail}` : ""}</span>}
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
  // 队名语言是纯前端展示偏好，存在独立的 localStorage key（teamNames.jsx
  // 里的 TeamLangProvider），跟这个面板其余项走的后端 settings 接口
  // 是两套机制——不用等点"保存设置"就立刻生效，也不会因为没点保存
  // 而丢失。
  const { lang, setLang } = useTeamLang();

  return (
    <div style={{ background: C.purpleDim, borderBottom: `1px solid ${C.purple}44`, padding: "14px 16px" }}>
      <div style={{ maxWidth: 960, margin: "0 auto" }}>
        <div style={{ fontSize: 11, fontWeight: 700, color: C.purple, marginBottom: 10, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span style={{ display: "flex", alignItems: "center", gap: 6 }}><Icon.gear width={13} height={13} />资金与凯利设置</span>
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

        <div style={{ marginTop: 14 }}>
          <div style={{ fontSize: 10, color: C.textDim, marginBottom: 4 }}>队伍名字语言</div>
          <div style={{ display: "flex", gap: 6 }}>
            {[["zh", "中文"], ["en", "English"]].map(([code, label]) => (
              <button key={code} onClick={() => setLang(code)}
                style={{ padding: "6px 14px", borderRadius: 6, border: `1px solid ${lang === code ? C.purple : C.border}`,
                         background: lang === code ? C.purple : "transparent",
                         color: lang === code ? "#0a0510" : C.textDim, fontWeight: 700, fontSize: 12, cursor: "pointer" }}>
                {label}
              </button>
            ))}
          </div>
        </div>

        <button onClick={() => onSave(d)} style={{ marginTop: 12, padding: "8px 18px", borderRadius: 8, border: "none", background: C.purple, color: "#0a0510", fontWeight: 800, fontSize: 12 }}>
          保存设置
        </button>
      </div>
    </div>
  );
}

// ── 免责声明 ─────────────────────────────────────────────────
// 内容参照马来西亚的博彩相关法规写，但这里只是一般性说明，不是法律意见
// ——具体情况请咨询执业律师。文案基调跟项目其他地方一致：精算式的诚实，
// 不回避"模型没有信息优势"这个已经用 14 万场走查证实的结论，也不含糊
// "不做自动下注"这条硬性约束。
function DisclaimerModal({ onClose }) {
  const Section = ({ title, children }) => (
    <div style={{ marginBottom: 16 }}>
      <div style={{ fontWeight: 800, fontSize: 13, color: C.text, marginBottom: 5 }}>{title}</div>
      <div style={{ fontSize: 12, color: C.textDim, lineHeight: 1.8 }}>{children}</div>
    </div>
  );

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)", zIndex: 100, display: "flex", alignItems: "center", justifyContent: "center", padding: 16 }}
         onClick={onClose}>
      <div onClick={e => e.stopPropagation()}
           style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 14, maxWidth: 560, width: "100%", maxHeight: "85vh", display: "flex", flexDirection: "column" }}>
        <div style={{ padding: "16px 20px", borderBottom: `1px solid ${C.border}`, display: "flex", justifyContent: "space-between", alignItems: "center", flexShrink: 0 }}>
          <div style={{ fontWeight: 800, fontSize: 15, display: "flex", alignItems: "center", gap: 7 }}><Icon.warn width={14} height={14} />免责声明</div>
          <span onClick={onClose} style={{ cursor: "pointer", color: C.textDim, fontSize: 18, lineHeight: 1 }}>✕</span>
        </div>

        <div style={{ padding: "16px 20px", overflowY: "auto" }}>
          <div style={{ fontSize: 11, color: C.gold, background: C.goldDim, border: `1px solid ${C.gold}33`, borderRadius: 8, padding: "9px 11px", marginBottom: 16, lineHeight: 1.7 }}>
            以下为一般性说明，不构成法律意见。如需就你所在地区的博彩合法性、
            税务或个人具体情况获得确定性结论，请咨询执业律师或相关专业人士。
          </div>

          <Section title="1. 这是什么">
            ValueBet 精算系统是一个概率计算和个人记账工具，用来估算比赛结果的
            模型概率、计算期望值（EV）和凯利仓位建议，并让你手动登记自己在
            博彩平台上下的注、追踪盈亏。它不是博彩平台，不代客下单，不处理
            任何真实资金，也不会替你登录任何博彩账户或自动下注——这是项目
            一开始就定下的硬性规则，没有例外。
          </Section>

          <Section title="2. 预测的局限">
            模型给出的是数学估计，不是确定的结果，不保证准确。项目自己做过
            大规模走查（超过 14 万场比赛，覆盖 2013–2025 年），结论是这套
            模型对市场赔率没有可利用的信息优势——市场定价已经把能拿到的
            信息基本吃干净了。任何历史回测数字、准确率、RPS，都只描述过去，
            不构成对未来结果的保证。依据本应用的任何数字做出的下注决定，
            风险和后果完全由你自己承担。
          </Section>

          <Section title="3. 不构成投资或博彩建议">
            本应用提供的所有数字（概率、EV、凯利仓位建议等）仅供参考和个人
            研究用途，不构成投资建议、博彩建议或任何形式的专业意见。是否
            下注、下注多少，是你自己的决定，开发者和运营者不对因此产生的
            任何盈亏承担责任。
          </Section>

          <Section title="4. 法律合规——请自行确认">
            马来西亚的博彩活动受《1953年赌博法令》（Betting Act 1953）、
            《1953年common gaming houses法令》等法规约束，未经许可经营或
            参与特定形式的博彩可能触犯法律；根据伊斯兰教法（Syariah），
            穆斯林参与任何形式的赌博均被禁止，各州属有各自的执法条文。
            本应用不对你使用的第三方博彩平台（包括离岸平台）在你所在
            司法管辖区是否合法作任何保证——这是你自己需要核实和承担的事。
            使用涉及真实资金的功能前，请确认自己已达到当地法定年龄，
            且清楚了解相关法律风险。
          </Section>

          <Section title="5. 赌博风险提示">
            赌博本质上是负期望的活动，存在造成财务损失的风险，长期参与也
            可能带来成瘾问题。如果你或身边的人对博彩感到难以控制，建议
            尽早寻求专业心理咨询或医疗帮助。
          </Section>

          <Section title="6. 责任限制">
            在法律允许的最大范围内，本应用的开发者和运营者不对因使用本
            应用（包括依据其预测、计算结果做出的决定）而产生的任何直接
            或间接损失承担责任。
          </Section>
        </div>

        <div style={{ padding: "12px 20px", borderTop: `1px solid ${C.border}`, flexShrink: 0 }}>
          <button onClick={onClose}
            style={{ width: "100%", padding: "11px", borderRadius: 8, border: "none", background: C.accent, color: C.bg, fontWeight: 800, fontSize: 13 }}>
            我已阅读，知道了
          </button>
        </div>
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
// 折叠面板默认全收起，只有用户自己点了才展开——不自动帮用户点开第一天。
function DayGroups({ matches, renderMatch, selectedIds }) {
  const days = useMemo(() => {
    const map = new Map();
    for (const m of matches) {
      if (!map.has(m.date)) map.set(m.date, []);
      map.get(m.date).push(m);
    }
    return [...map.entries()].sort((a, b) => (a[0] < b[0] ? -1 : 1));
  }, [matches]);

  const [openDays, setOpenDays] = useState(() => new Set());

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
  const zhTeam = useZhTeam();
  const [open, setOpen] = useState(false);
  // 优先用自己填过的价；没填过就拿市场**平均价**预填。
  // 用平均价不用最优价是刻意的：最优价是全市场某一家的最高报价，你在 BK8
  // 大概率拿不到，拿它预填会让 EV 看起来虚高，等于给自己发假信号。平均价
  // 更接近你实际能成交的水平。两个价都在下面的对比条里显示，要用哪个
  // 自己按。
  const mkt = match.market_odds;
  const [oHome, setOHome] = useState(
    (match.latest_odds?.odds_home ?? mkt?.avg_home)?.toString() || "");
  const [oDraw, setODraw] = useState(
    (match.latest_odds?.odds_draw ?? mkt?.avg_draw)?.toString() || "");
  const [oAway, setOAway] = useState(
    (match.latest_odds?.odds_away ?? mkt?.avg_away)?.toString() || "");
  const [calc, setCalc] = useState(null);
  const [stake, setStake] = useState(100);
  const [rStake, setRStake] = useState({});
  const [showRF, setShowRF] = useState(null);
  const [saving, setSaving] = useState(null);
  const [saved, setSaved] = useState(null);

  const mdl = match.prediction;
  if (!mdl) return null;

  // 队名中文简称——纯展示层替换，match.team1/team2 本身不变，下面所有
  // API 调用（compute/doVBet/doRBet）用的都还是 match.id，不涉及队名，
  // 换成中文不影响任何一次请求的数据。查不到译名的队伍照原样显示英文，
  // 是覆盖范围的自然边界，不是 bug。
  const t1 = zhTeam(match.team1), t2 = zhTeam(match.team2);

  useEffect(() => {
    const h = parseFloat(oHome), d = parseFloat(oDraw), a = parseFloat(oAway);
    if (!h || !a) { setCalc(null); return; }
    let cancelled = false;
    const t = setTimeout(async () => {
      try {
        // 值还等于市场预填时只算 EV、不落库（save:false）。照存的话，用户
        // 根本没碰过的市场价会变成"他自己填的价"，而且 latest_odds 会永久
        // 盖过 market_odds——那场比赛显示的价从此冻在展开卡片的那一刻，
        // 再也不跟着市场更新。改过了才存。
        const same = (x, y) => (x == null && y == null) || Math.abs((x || 0) - (y || 0)) < 1e-9;
        const untouched = !!mkt && same(h, mkt.avg_home) && same(d || null, mkt.avg_draw)
                          && same(a, mkt.avg_away);
        const result = await api("/odds", {
          method: "POST",
          body: JSON.stringify({ match_id: match.id, odds_home: h, odds_draw: d || null,
                                 odds_away: a, save: !untouched }),
        });
        if (!cancelled) setCalc({ h, d: d || null, a, ...result });
      } catch (e) {
        if (!cancelled) setCalc(null);
      }
    }, 350);
    return () => { cancelled = true; clearTimeout(t); };
  }, [oHome, oDraw, oAway, match.id]);

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
      <div onClick={() => setOpen(o => !o)} style={{ padding: "11px 14px", display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, cursor: "pointer", userSelect: "none" }}>
        {/* minWidth:0 —— flex 子项默认 min-width:auto，内容再长也不肯收缩，
            于是这一栏会把右边的 ELO 挤出去、徽标被从中间断成两行。 */}
        <div style={{ minWidth: 0 }}>
          <div style={{ fontWeight: 700, fontSize: 13 }}>{t1} <span style={{ color: C.textDim, fontWeight: 400, fontSize: 12 }}>vs</span> {t2}</div>
          <div style={{ fontSize: 10, color: C.textDim, marginTop: 2 }}>
            {match.competition_name && <span style={{ color: C.blue, fontWeight: 700 }}>{match.competition_name} · </span>}
            {fdt(match.date)}{match.time_utc ? ` ${match.time_utc}` : ""} · {match.round} · {match.ground}
            {provisional && (
              <span title="上游数据源还没排这一轮的具体日程，整轮先挂在一个名义日期上"
                    style={{ marginLeft: 6, background: C.goldDim, color: C.gold, borderRadius: 4,
                             padding: "1px 5px", fontSize: 9, fontWeight: 700,
                             whiteSpace: "nowrap", display: "inline-block" }}>日期暂定</span>
            )}
            {/* 这条预测背后有多少真实数据。联赛杯这类淘汰赛里大量球队一季
                只踢 1-2 场，够不上单独拟合参数的门槛，模型会静默退回"联赛
                平均水平"——不标出来的话，一个凭空算出来的百分比看起来跟
                真实预测一模一样。后端算好三档传过来，见 main.py 的 _backing()。*/}
            {mdl.data_backing === "none" && (
              <span title="参赛球队在本系统里没有任何历史比赛记录，模型只能用联赛平均水平顶替——下面这三个百分比没有实际依据，不要当成预测使用"
                    style={{ marginLeft: 6, background: C.redDim, color: C.red, borderRadius: 4,
                             padding: "1px 5px", fontSize: 9, fontWeight: 700,
                             whiteSpace: "nowrap", display: "inline-block" }}>无数据支撑</span>
            )}
            {mdl.data_backing === "thin" && (
              <span title="至少一方球队没有足够场次单独拟合参数，只靠少量真实比赛做过贝叶斯修正——有依据但比其他比赛薄，概率的不确定性更大"
                    style={{ marginLeft: 6, background: C.goldDim, color: C.gold, borderRadius: 4,
                             padding: "1px 5px", fontSize: 9, fontWeight: 700,
                             whiteSpace: "nowrap", display: "inline-block" }}>样本偏少</span>
            )}</div>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flex: "0 0 auto" }}>
          <span style={{ fontSize: 10, color: C.blue, whiteSpace: "nowrap" }}>ELO {mdl.elo_home}/{mdl.elo_away}</span>
          <span style={{ color: C.textDim }}>{open ? "▲" : "▼"}</span>
        </div>
      </div>

      {(() => {
        const items = [
          { label: t1, prob: mdl.prob_home, xg: mdl.xg_home },
          { label: "平局", prob: mdl.prob_draw, xg: null },
          { label: t2, prob: mdl.prob_away, xg: mdl.xg_away },
        ];
        // 每栏一条各自的进度条，宽度是"这个结果自己的概率占这一栏的比例"
        // （0-100%），不是三栏共享一条按比例分段的通栏色条。
        //
        // 之前是通栏版本：一条 flex 容器横跨三栏，每段宽度=各自概率，三段
        // 概率加总接近 100% 所以三段能拼成一条完整的条，但分段的边界是
        // 按"概率累积到多少"决定的，跟上面三栏"各占 1/3 等宽"的边界完全
        // 是两套坐标系——只要三个概率不是刚好 33.3/33.3/33.3，色条的分段线
        // 就不会落在栏与栏的分界线上，平局那一段经常悬空对不上自己那一栏。
        // 现在改回每栏自己的条，条的宽度只跟自己栏内的百分比有关，
        // 天然跟上面的数字对齐。
        const order = [0, 1, 2].sort((a, b) => items[b].prob - items[a].prob);
        const rank = []; order.forEach((idx, pos) => { rank[idx] = pos; });
        const tone = idx => rank[idx] === 0 ? C.accent : rank[idx] === 1 ? C.textDim : C.muted;
        return (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(96px, 1fr))", gap: 1, background: C.border }}>
            {items.map((item, idx) => (
              <div key={idx} style={{ background: C.card, padding: "9px 12px", minWidth: 0 }}>
                <div style={{ fontSize: 9, color: C.textDim, textTransform: "uppercase", letterSpacing: "0.6px", marginBottom: 3, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{item.label}</div>
                <div style={{ fontSize: 19, fontWeight: 900, color: item.prob === maxP ? C.accent : C.text, fontVariantNumeric: "tabular-nums" }}>{pct(item.prob)}</div>
                {item.xg !== null && <div style={{ fontSize: 10, color: C.textDim, marginTop: 1, fontVariantNumeric: "tabular-nums" }}>xG {item.xg}</div>}
                <div style={{ marginTop: 5, height: 3, background: C.border, borderRadius: 2 }}>
                  <div style={{ width: pct(item.prob), height: "100%", background: tone(idx), borderRadius: 2 }} />
                </div>
              </div>
            ))}
          </div>
        );
      })()}

      {open && (
        <div style={{ borderTop: `1px solid ${C.border}`, padding: "12px 14px", background: C.surface }}>
          <div style={{ fontSize: 11, color: C.textDim, marginBottom: 8 }}>输入赔率，自动计算真实期望值：</div>

          {/* 市场公开报价（The Odds API 自动抓的，不是 BK8 的价）。
              两行都给：平均价约等于你实际能成交的水平，最优价是全市场某一家
              的最高报价。项目走查的结论是「盈亏完全取决于价格执行」——
              成交价 = 平均价 + f×(最优价−平均价)，f=0 时 ROI −3.54%、
              f=0.8 才盈亏平衡，所以这两个数的差距本身就是要看的信息。
              点任一行直接填进输入框，省掉手打。 */}
          {mkt && (
            <div style={{ marginBottom: 10, border: `1px solid ${C.border}`, borderRadius: 8, overflow: "hidden" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center",
                            padding: "5px 9px", background: C.muted, fontSize: 10, color: C.textDim }}>
                <span>市场报价 · {mkt.n_books} 家博彩公司</span>
                <span>非 BK8 报价，仅供预填与比价</span>
              </div>
              {[
                { tag: "平均价", h: mkt.avg_home, d: mkt.avg_draw, a: mkt.avg_away,
                  note: "接近实际成交" },
                { tag: "最优价", h: mkt.best_home, d: mkt.best_draw, a: mkt.best_away,
                  note: "全市场最高，未必拿得到" },
              ].map(row => (
                <button key={row.tag} type="button"
                  onClick={() => { setOHome(row.h?.toString() || ""); setODraw(row.d?.toString() || ""); setOAway(row.a?.toString() || ""); }}
                  style={{ display: "grid", gridTemplateColumns: "58px 1fr 1fr 1fr 96px", gap: 6, width: "100%",
                           alignItems: "center", padding: "7px 9px", background: "transparent", cursor: "pointer",
                           border: "none", borderTop: `1px solid ${C.border}`, color: C.text, fontSize: 12, textAlign: "left" }}>
                  <span style={{ color: C.textDim, fontSize: 10 }}>{row.tag}</span>
                  <span style={{ fontWeight: 700 }}>{fod(row.h)}</span>
                  <span style={{ fontWeight: 700 }}>{row.d ? fod(row.d) : "—"}</span>
                  <span style={{ fontWeight: 700 }}>{fod(row.a)}</span>
                  <span style={{ color: C.textDim, fontSize: 9, textAlign: "right" }}>{row.note}</span>
                </button>
              ))}
            </div>
          )}

          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(88px, 1fr))", gap: 7, alignItems: "flex-end", marginBottom: 10 }}>
            {[
              { label: t1, val: oHome, set: setOHome },
              { label: "平局", val: oDraw, set: setODraw },
              { label: t2, val: oAway, set: setOAway },
            ].map(f => (
              <div key={f.label}>
                <div style={{ fontSize: 10, color: C.textDim, marginBottom: 3 }}>{f.label}</div>
                <input type="number" step="0.01" placeholder="e.g. 2.40" value={f.val} onChange={e => f.set(e.target.value)}
                  style={{ width: "100%", background: C.card, border: `1px solid ${C.border}`, borderRadius: 6, padding: "6px 9px", color: C.text, fontSize: 13, fontWeight: 700 }} />
              </div>
            ))}
          </div>

          {calc && (
            <div>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(88px, 1fr))", gap: 7, marginBottom: 10 }}>
                {[
                  { key: "home", label: t1, evVal: calc.ev_home, kPct: calc.kelly_home, kAmt: calc.kelly_home_amount, odds: calc.h },
                  ...(calc.d ? [{ key: "draw", label: "平局", evVal: calc.ev_draw, kPct: calc.kelly_draw, kAmt: calc.kelly_draw_amount, odds: calc.d }] : []),
                  { key: "away", label: t2, evVal: calc.ev_away, kPct: calc.kelly_away, kAmt: calc.kelly_away_amount, odds: calc.a },
                ].map(item => (
                  <div key={item.key} style={{ background: evbg(item.evVal), border: `1px solid ${evc(item.evVal)}44`, borderRadius: 8, padding: "9px 10px" }}>
                    <div style={{ fontSize: 10, color: C.textDim, marginBottom: 3 }}>{item.label} @ {fod(item.odds)}</div>
                    <div style={{ fontSize: 17, fontWeight: 900, color: evc(item.evVal) }}>EV {fev(item.evVal)}</div>
                    {item.kPct > 0 ? (
                      <div style={{ fontSize: 11, marginTop: 3 }}>建议: <strong>{Math.round(item.kAmt).toLocaleString()}</strong> <span style={{ color: C.textDim }}>({pct(item.kPct)})</span></div>
                    ) : (
                      <div style={{ fontSize: 11, marginTop: 3, color: C.textDim }}>不建议下注</div>
                    )}
                    {item.evVal > threshold && <div style={{ fontSize: 10, fontWeight: 800, color: C.accent, marginTop: 3 }}>VALUE</div>}
                    <div style={{ display: "flex", gap: 4, marginTop: 7 }}>
                      <button onClick={() => doVBet(item.key)} disabled={saving === "v" + item.key || saved === "v" + item.key}
                        style={{ flex: 1, padding: "10px 5px", borderRadius: 6, border: "none", background: item.evVal > 0 ? C.blue : C.muted, color: item.evVal > 0 ? "#fff" : C.textDim, fontWeight: 700, fontSize: 12 }}>
                        {saved === "v" + item.key ? "✓" : saving === "v" + item.key ? "..." : "虚拟"}
                      </button>
                      <button onClick={() => setShowRF(showRF === item.key ? null : item.key)}
                        style={{ flex: 1, padding: "10px 5px", borderRadius: 6, border: `1px solid ${C.purple}`, background: showRF === item.key ? C.purple : "transparent", color: showRF === item.key ? "#0a0510" : C.purple, fontWeight: 700, fontSize: 12 }}>
                        实盘
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
                          {saved === "r" + item.key ? "✓ 已登记" : saving === "r" + item.key ? "..." : "确认登记实盘"}
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
function RealBetsTab({ realBets, bankroll, settings, parlays = [], withdrawals = [], onCancel, cancelling, onRefresh }) {
  const zhTeam = useZhTeam();
  const pending = realBets.filter(b => b.result === "pending");
  const settled = realBets.filter(b => b.result !== "pending");
  const pSettled = parlays.filter(p => p.result !== "pending");
  const pPending = parlays.filter(p => p.result === "pending");
  // 串关和单场用的是同一套 result 取值（win/loss/pending，见 updater.py），
  // 所以赢注、待结算、胜率可以直接把两边加起来。
  //
  // 这三个数此前只统计单场，但「总注数」是算了串关的，结果这块面板自己
  // 跟自己对不上——总注数 14、待结算 8、赢注 0，剩下 6 注凭空消失。
  // 用户就是看到这个才发现的。
  const wins = settled.filter(b => b.result === "win").length +
               pSettled.filter(p => p.result === "win").length;
  const settledCount = settled.length + pSettled.length;
  const pendingCount = pending.length + pPending.length;
  // 串关的本金和回报要跟单场一起计入这一页的合计，否则「实盘盈亏」这个数
  // 跟资金曲线对不上——曲线一直是把串关算进去的
  const pStake = parlays.reduce((s, p) => s + (p.stake || 0), 0);
  const pPnl = pSettled.reduce((s, p) => s + (p.pnl || 0), 0);
  // 累计流水：单场实盘 + 串关实盘的本金合计，不分是否已结算——
  // 「投了多少钱」这个数不该因为比赛还没开打就不算数。跟顶部总览栏
  // 「累计流水」是同一个口径，这里再显示一次方便在实盘页单独查看。
  const turnover = realBets.reduce((s, b) => s + (b.stake_real || 0), 0) + pStake;
  // 投注金额：现在还压着多少钱没结算，跟顶部总览栏那个「投注金额」
  // 同一个口径——累计流水是"一共下过多少"，这个是"现在还悬着多少"。
  const pendingStake =
    pending.reduce((s, b) => s + (b.stake_real || 0), 0) +
    parlays.filter(p => p.result === "pending").reduce((s, p) => s + (p.stake || 0), 0);
  // realBets 和 parlays 传进来时已经按联赛筛过，所以这两个数跟着筛选走，
  // 跟这块面板里其余六个数一致。详见 realPnlRoi() 上方的说明。
  const { pnl: realPnl, roi: realRoi, staked: realStakedSettled } = realPnlRoi(realBets, parlays);
  const realGrossPnl = grossPnl(realPnl, realStakedSettled);

  return (
    <div>
      <SL>实盘记录 · 真实金钱（HKD）· 起始 {(+settings.bankroll_total).toLocaleString()}</SL>
      <div style={{ background: C.purpleDim, border: `1px solid ${C.purple}44`, borderRadius: 8, padding: "9px 13px", fontSize: 11, color: C.purple, marginBottom: 12 }}>
        在「预测」页点「实盘」按钮记录你真实下的注。比赛结束后系统每12小时自动结算盈亏。
      </div>

      <WithdrawSection bankroll={bankroll} withdrawals={withdrawals} onRefresh={onRefresh} />

      {/* 串关是单独一张表（ParlayBet），一注对应 3-8 场比赛，塞不进上面那个
          按单场排的表格，所以单独列一段。原来这一页完全不显示串关，
          于是「串关下注和回报没进实盘」——其实记下来了，只是没地方看。 */}
      {parlays.length > 0 && (
        <div style={{ marginBottom: 14 }}>
          <div style={{ fontSize: 11, color: C.textDim, marginBottom: 7, display: "flex", justifyContent: "space-between", flexWrap: "wrap", gap: 6 }}>
            <span style={{ fontWeight: 700, color: C.text }}>串关注单 {parlays.length} 注</span>
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
                <span style={{ fontSize: 11, display: "flex", alignItems: "center", gap: 8 }}>
                  本金 {Math.round(p.stake).toLocaleString()} ·{" "}
                  <strong style={{ color: p.result === "win" ? C.accent : p.result === "loss" ? C.red : C.gold }}>
                    {p.result === "pending" ? "待结算" : `净盈亏 ${fnum(p.pnl)}`}
                  </strong>
                  {/* 净盈亏之外再显示盈亏（本金×总赔率，输的话是0），
                      口径跟单场那边一致，见 grossPnl 的推导 */}
                  {p.result !== "pending" && (
                    <span style={{ color: C.textDim }}>· 盈亏 {fgross(grossPnl(p.pnl || 0, p.stake || 0))}</span>
                  )}
                  {/* 只有待结算的能取消——已结算的后端会拒绝，这里提前不给按钮 */}
                  {p.result === "pending" && onCancel && (
                    <button onClick={() => onCancel(`/parlay-bets/${p.id}`, "串关")}
                      disabled={cancelling === `/parlay-bets/${p.id}`}
                      style={{ background: "none", border: `1px solid ${C.red}66`, color: C.red, borderRadius: 5, padding: "3px 7px", fontSize: 10, fontWeight: 700 }}>
                      {cancelling === `/parlay-bets/${p.id}` ? "…" : "✕ 取消"}
                    </button>
                  )}
                </span>
              </div>
              {(p.legs || []).map((l, i) => (
                <div key={i} style={{ fontSize: 10.5, color: C.textDim, paddingLeft: 8, lineHeight: 1.7 }}>
                  · {zhTeam(l.team1)} vs {zhTeam(l.team2)} — 押 {l.outcome === "home" ? zhTeam(l.team1) : l.outcome === "away" ? zhTeam(l.team2) : "平局"} @ {fod(l.odds)}
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
        {/* 总注数、累计流水、投注金额都把单场实盘和串关实盘合起来算——
            串关此前完全不计入这一页的任何统计，用户反馈「串关记录没进
            实盘」，上面已经把串关列出来了，这几个数也要跟着算，不然
            列表里看得到串关、统计栏却当它不存在，看起来还是缺了一块。

            累计流水 vs 投注金额：前者是"一共下过多少"（不分输赢/待结算），
            后者是"现在还悬着多少"（只算 pending）——两个是不同的问题，
            都跟顶部总览栏那两个同名统计口径一致。 */}
        <Stat label="总注数" val={realBets.length + parlays.length} color={C.purple} />
        <Stat label="累计流水" val={Math.round(turnover).toLocaleString()} color={C.text} />
        <Stat label="投注金额" val={Math.round(pendingStake).toLocaleString()} color={C.gold} />
        <Stat label="赢注" val={wins} color={C.accent} />
        <Stat label="待结算" val={pendingCount} color={C.gold} />
        <Stat label="净盈亏" val={fnum(realPnl)} color={realPnl >= 0 ? C.accent : C.red} />
        <Stat label="盈亏" val={fgross(realGrossPnl)} color={C.gold} />
        <Stat label="实盘ROI" val={realRoi == null ? "—" : realRoi.toFixed(1) + "%"} color={(realRoi || 0) >= 0 ? C.accent : C.red} />
        <Stat label="胜率" val={settledCount ? pct(wins / settledCount) : "—"} color={C.blue} />
      </div>
      {realBets.length === 0 && <Empty text="还没有实盘记录。去「预测」页输入赔率，点击「实盘」按钮。" />}
      {realBets.map(b => {
        const won = b.result === "win";
        return (
          <div key={b.id} style={{ background: C.card, border: `1px solid ${b.result === "pending" ? C.gold + "44" : won ? C.accent + "44" : C.red + "44"}`, borderRadius: 8, marginBottom: 8, padding: "10px 14px", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <div>
              <div style={{ fontWeight: 700, fontSize: 12 }}>{zhTeam(b.team1)} vs {zhTeam(b.team2)}</div>
              <div style={{ fontSize: 10, color: C.textDim, marginTop: 2 }}>
                押 {b.outcome === "home" ? "主队" : b.outcome === "away" ? "客队" : "平局"} · 赔率 {fod(b.odds_used)} · {b.ev_at_bet != null ? `EV ${fev(b.ev_at_bet)}` : ""}
              </div>
            </div>
            <div style={{ textAlign: "right" }}>
              <div style={{ fontWeight: 700, fontSize: 13 }}>{b.stake_real.toLocaleString()} {b.currency}</div>
              <div style={{ fontSize: 11, fontWeight: 700, color: b.result === "pending" ? C.gold : won ? C.accent : C.red, display: "flex", alignItems: "center", gap: 6, justifyContent: "flex-end" }}>
                {b.result === "pending" ? "待结算" : `净盈亏 ${fnum(b.pnl_real || 0)}`}
                {b.result === "pending" && onCancel && (
                  <button onClick={() => onCancel(`/real-bets/${b.id}`, "实盘下注")}
                    disabled={cancelling === `/real-bets/${b.id}`}
                    style={{ background: "none", border: `1px solid ${C.red}66`, color: C.red, borderRadius: 5, padding: "3px 7px", fontSize: 10, fontWeight: 700 }}>
                    {cancelling === `/real-bets/${b.id}` ? "…" : "✕ 取消"}
                  </button>
                )}
              </div>
              {/* 净盈亏＝盈亏－本金（上面那行）。盈亏单独再显示一行——
                  赢的话是本金×赔率的总回款，输的话是0。用户要求这两个数
                  分开看，而不是只有一个不知道算没算本金的数字。 */}
              {b.result !== "pending" && (
                <div style={{ fontSize: 10, color: C.textDim, marginTop: 1 }}>
                  盈亏 {fgross(grossPnl(b.pnl_real || 0, b.stake_real || 0))}
                </div>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ── 提款 ─────────────────────────────────────────────────────
// 把在 BK8 等平台赢到的钱转去自己银行账户之后，回来这里登记一笔，
// 让追踪的实盘资金曲线跟真实情况对得上。只作用于实盘——虚拟盘是
// 测试模型用的假钱，没有"从假账户提现"这回事，所以这个区块只挂在
// RealBetsTab 里，不需要在虚拟盘页出现。
function WithdrawSection({ bankroll, withdrawals, onRefresh }) {
  const [open, setOpen] = useState(false);
  const [amount, setAmount] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [cancellingId, setCancellingId] = useState(null);

  const currentBalance = bankroll?.real?.current_balance;
  const totalWithdrawn = bankroll?.real?.total_withdrawn || 0;

  async function submit(e) {
    e.preventDefault();
    const amt = parseFloat(amount);
    if (!amt || amt <= 0) { setErr("请输入大于 0 的金额"); return; }
    setErr(null); setBusy(true);
    try {
      await api("/withdrawals", { method: "POST", body: JSON.stringify({ amount: amt, note: note || null }) });
      setAmount(""); setNote(""); setOpen(false);
      await onRefresh?.();
    } catch (e2) {
      setErr(e2.message);
    } finally {
      setBusy(false);
    }
  }

  async function undo(id) {
    if (!window.confirm("撤销这笔提款记录吗？这只是改记账，不会真的把钱转回来。")) return;
    setCancellingId(id);
    try {
      await api(`/withdrawals/${id}`, { method: "DELETE" });
      await onRefresh?.();
    } catch (e) {
      alert("撤销失败: " + e.message);
    } finally {
      setCancellingId(null);
    }
  }

  return (
    <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 10, padding: 14, marginBottom: 14 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 10 }}>
        <div>
          <div style={{ fontSize: 10, color: C.textDim, textTransform: "uppercase", letterSpacing: "0.5px" }}>实盘可提余额</div>
          <div style={{ fontSize: 20, fontWeight: 900, marginTop: 2 }}>
            {currentBalance != null ? currentBalance.toLocaleString() : "—"}
          </div>
          {totalWithdrawn > 0 && (
            <div style={{ fontSize: 10, color: C.textDim, marginTop: 2 }}>累计已提款 {totalWithdrawn.toLocaleString()}</div>
          )}
        </div>
        <button onClick={() => setOpen(o => !o)}
          style={{ padding: "9px 16px", borderRadius: 8, border: "none", background: C.purple, color: "#0a0510", fontWeight: 800, fontSize: 13 }}>
          {open ? "取消" : "提款"}
        </button>
      </div>

      {open && (
        <form onSubmit={submit} style={{ marginTop: 12, paddingTop: 12, borderTop: `1px solid ${C.border}` }}>
          <div style={{ fontSize: 9, color: C.textDim, marginBottom: 4 }}>提款金额（HKD）</div>
          {/* fontSize 16 是刻意的——iOS Safari 在输入框字号小于 16px 时会
              自动放大整个页面，一放大按钮就更难点中，之前实盘登记表单
              踩过这个坑。 */}
          <input type="number" inputMode="decimal" step="0.01" autoFocus
            value={amount} onChange={e => setAmount(e.target.value)}
            placeholder={currentBalance != null ? `可提 ${currentBalance.toLocaleString()}` : "e.g. 500"}
            style={{ width: "100%", background: C.card, border: `1px solid ${C.purple}66`, borderRadius: 6, padding: "9px 10px", color: C.text, fontSize: 16, fontWeight: 700, marginBottom: 8 }} />
          <div style={{ fontSize: 9, color: C.textDim, marginBottom: 4 }}>备注（可选）</div>
          <input type="text" value={note} onChange={e => setNote(e.target.value)}
            placeholder="例如：转去银行账户"
            style={{ width: "100%", background: C.card, border: `1px solid ${C.border}`, borderRadius: 6, padding: "9px 10px", color: C.text, fontSize: 13, marginBottom: 8 }} />
          {err && <div style={{ fontSize: 11, color: C.red, marginBottom: 8 }}>{err}</div>}
          <button type="submit" disabled={busy}
            style={{ width: "100%", padding: "11px", borderRadius: 6, border: "none", background: C.purple, color: "#0a0510", fontWeight: 800, fontSize: 13, opacity: busy ? 0.6 : 1 }}>
            {busy ? "…" : "确认提款"}
          </button>
          <div style={{ fontSize: 10, color: C.textDim, marginTop: 8, lineHeight: 1.6 }}>
            这里只是记账，不会帮你从 BK8 等平台真的把钱转出来——那一步要你自己在
            对应平台操作，这里登记一笔是为了让追踪的资金曲线跟真实情况保持一致。
          </div>
        </form>
      )}

      {withdrawals.length > 0 && (
        <div style={{ marginTop: 12, paddingTop: 12, borderTop: `1px solid ${C.border}` }}>
          <div style={{ fontSize: 10, color: C.textDim, marginBottom: 6, fontWeight: 700 }}>提款记录</div>
          {withdrawals.map(w => (
            <div key={w.id} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: 11, padding: "6px 0", borderBottom: `1px solid ${C.border}` }}>
              <span style={{ color: C.textDim }}>
                {fdatetime(w.withdrawn_at)}{w.note ? ` · ${w.note}` : ""}
              </span>
              <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <strong>-{w.amount.toLocaleString()} {w.currency}</strong>
                <button onClick={() => undo(w.id)} disabled={cancellingId === w.id}
                  style={{ background: "none", border: `1px solid ${C.border}`, color: C.textDim, borderRadius: 5, padding: "3px 7px", fontSize: 10 }}>
                  {cancellingId === w.id ? "…" : "撤销"}
                </button>
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Chart Tab ────────────────────────────────────────────────
// 逐日盈亏展开后的明细：按「下注记账的那一天」（虚拟盘/串关用 created_at，
// 实盘单场用 placed_at）分组已结算的注单，算出当天虚拟盘/实盘各自的
// 输赢场次、盈亏、净盈亏，并逐注列出是哪一场比赛。故意用跟后端
// /api/bankroll-summary 构造资金曲线事件完全相同的日期口径（那边虚拟盘/
// 串关也是用 created_at，实盘单场用 placed_at，不是 settled_at）——这样
// 展开明细里逐注的净盈亏加起来，才能跟外面折叠行里资金曲线算出来的净变化
// 对上，不会出现「明细加起来跟外面显示的不是同一个数」这种自相矛盾。
//
// items 里存的是原始字段（队名不翻译、赛事只存 id），翻译和赛事名查表
// 都放到渲染时做——这个函数没有 React context，拿不到 zhTeam 和赛事表。
function buildDailyDetail(bets, realBets, parlays) {
  const map = new Map();
  function bucket(date, kind) {
    if (!map.has(date)) map.set(date, {
      virtual: { win: 0, loss: 0, gross: 0, net: 0, items: [] },
      real: { win: 0, loss: 0, gross: 0, net: 0, items: [] },
    });
    return map.get(date)[kind];
  }
  function add(bk, item) {
    bk[item.result === "win" ? "win" : "loss"]++;
    bk.net += item.net;
    bk.gross += item.gross;
    bk.items.push(item);
  }
  for (const b of bets || []) {
    if (b.result === "pending" || !b.created_at) continue;
    add(bucket(b.created_at.slice(0, 10), "virtual"), {
      type: "single", result: b.result, outcome: b.outcome,
      team1: b.team1, team2: b.team2, matchDate: b.date,
      competition_id: b.competition_id, odds: b.odds_used, stake: b.stake || 0,
      net: b.pnl || 0, gross: grossPnl(b.pnl || 0, b.stake || 0),
    });
  }
  for (const b of realBets || []) {
    if (b.result === "pending" || !b.placed_at) continue;
    add(bucket(b.placed_at.slice(0, 10), "real"), {
      type: "single", result: b.result, outcome: b.outcome,
      team1: b.team1, team2: b.team2, matchDate: b.date,
      competition_id: b.competition_id, odds: b.odds_used, stake: b.stake_real || 0,
      net: b.pnl_real || 0, gross: grossPnl(b.pnl_real || 0, b.stake_real || 0),
    });
  }
  for (const p of parlays || []) {
    if (p.result === "pending" || !p.created_at) continue;
    add(bucket(p.created_at.slice(0, 10), p.kind === "real" ? "real" : "virtual"), {
      type: "parlay", result: p.result, legs: p.legs || [],
      odds: p.odds_used, stake: p.stake || 0,
      net: p.pnl || 0, gross: grossPnl(p.pnl || 0, p.stake || 0),
    });
  }
  return map;
}

function ChartTab({ bankroll, settings, bets = [], realBets = [], parlayBets = [], competitions = [] }) {
  const zhTeam = useZhTeam();
  // 赛事 id → 中文名。注单本身只存 competition_id，展开明细里要显示
  // 「比赛名字」（联赛名）就得自己查一次表。
  const compName = useMemo(() => {
    const m = new Map();
    for (const c of competitions) m.set(c.id, c.name_zh || c.name);
    return id => m.get(id) || null;
  }, [competitions]);
  // 后端现在返回单一合并数据集（每个点同时带 virtual 和 real 两个值）。
  // 之前是两个独立数组分别喂给两条 Line，配 category 类型的 X 轴时
  // recharts 会因两边日期集合不同而画歪——这是曲线之前有问题的原因之一。
  const series = bankroll.series || [];
  const last = series[series.length - 1];
  const vRows = dailyPnl(series, "virtual");
  const rRows = dailyPnl(series, "real");
  // 两条线的按天记录合并成一份按日期排序的列表，一行里同时看到虚拟盘/
  // 实盘那天各自的变化，而不是两张互相对不上日期的表。
  const byDate = new Map();
  for (const r of vRows) byDate.set(r.date, { date: r.date, v: r.delta, r: 0 });
  for (const r of rRows) {
    const row = byDate.get(r.date) || { date: r.date, v: 0, r: 0 };
    row.r = r.delta;
    byDate.set(r.date, row);
  }
  const dailyRows = [...byDate.values()].sort((a, b) => (a.date < b.date ? 1 : -1));
  const dailyDetail = useMemo(() => buildDailyDetail(bets, realBets, parlayBets), [bets, realBets, parlayBets]);
  const [openDays, setOpenDays] = useState(() => new Set());
  function toggleDay(date) {
    setOpenDays(s => {
      const next = new Set(s);
      next.has(date) ? next.delete(date) : next.add(date);
      return next;
    });
  }

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
          <>
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
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginTop: 14 }}>
              <div style={{ background: C.surface, borderRadius: 8, padding: "10px 12px" }}>
                <div style={{ fontSize: 9, color: C.textDim, textTransform: "uppercase", letterSpacing: "0.4px" }}>虚拟盘当前</div>
                <div style={{ fontSize: 20, fontWeight: 900, color: C.accent, marginTop: 2, fontVariantNumeric: "tabular-nums" }}>{Math.round(last.virtual).toLocaleString()}</div>
              </div>
              <div style={{ background: C.surface, borderRadius: 8, padding: "10px 12px" }}>
                <div style={{ fontSize: 9, color: C.textDim, textTransform: "uppercase", letterSpacing: "0.4px" }}>实盘当前</div>
                <div style={{ fontSize: 20, fontWeight: 900, color: C.purple, marginTop: 2, fontVariantNumeric: "tabular-nums" }}>{Math.round(last.real).toLocaleString()}</div>
              </div>
            </div>
          </>
        )}
      </div>

      {/* 哪天赚了/亏了多少——同一天可能有好几笔结算，这里已经按天合并成
          一个净值。日期之间的变化是"上一个有数据的日子"到"这一天"，
          不是严格意义上的自然日相邻，中间空档的日子（没有任何注单结算）
          不会显示成 0，本来就没有发生过的事没必要占一行。 */}
      {dailyRows.length > 0 && (
        <div>
          <SL>逐日盈亏 · 点一行展开当日明细</SL>
          <div style={{ background: C.surface, borderRadius: 10, overflow: "hidden", border: `1px solid ${C.border}` }}>
            <div style={{ display: "grid", gridTemplateColumns: "16px 1fr 90px 90px", padding: "6px 12px", fontSize: 9, color: C.textDim, textTransform: "uppercase", borderBottom: `1px solid ${C.border}` }}>
              <span></span><span></span><span style={{ textAlign: "right" }}>虚拟盘</span><span style={{ textAlign: "right" }}>实盘</span>
            </div>
            {dailyRows.map(row => {
              const open = openDays.has(row.date);
              const detail = dailyDetail.get(row.date);
              return (
                <div key={row.date} style={{ borderBottom: `1px solid ${C.border}` }}>
                  <button onClick={() => toggleDay(row.date)} style={{
                    width: "100%", display: "grid", gridTemplateColumns: "16px 1fr 90px 90px",
                    padding: "8px 12px", alignItems: "center", fontSize: 12,
                    background: "none", border: "none", color: C.text, textAlign: "left", cursor: "pointer",
                  }}>
                    <span style={{ color: C.textDim, fontSize: 10 }}>{open ? "▾" : "▸"}</span>
                    <span style={{ color: C.textDim }}>{fdt(row.date)}</span>
                    <span style={{ textAlign: "right", fontWeight: 700, color: row.v > 0 ? C.accent : row.v < 0 ? C.red : C.muted, fontVariantNumeric: "tabular-nums" }}>
                      {row.v !== 0 ? fnum(row.v) : "—"}
                    </span>
                    <span style={{ textAlign: "right", fontWeight: 700, color: row.r > 0 ? C.purple : row.r < 0 ? C.red : C.muted, fontVariantNumeric: "tabular-nums" }}>
                      {row.r !== 0 ? fnum(row.r) : "—"}
                    </span>
                  </button>
                  {/* 展开明细：先是当天的汇总（几胜几负、盈亏、净盈亏），
                      再逐注列出到底是哪一场——球队、赛事、比赛日期、押的
                      是哪一边、赔率、这一注各自的盈亏和净盈亏。口径跟外面
                      折叠行完全一致（created_at/placed_at 那天），所以逐注
                      的净盈亏加起来正好等于折叠行显示的那个数。
                      注意这里的日期有两个：折叠行上的是「记账日」，每条
                      明细里的是那场比赛自己的日期，两者可以不同（比如今天
                      下注、后天开球），所以明细里必须把比赛日期写出来。 */}
                  {open && (
                    <div style={{ padding: "2px 12px 12px 30px", display: "grid", gap: 12 }}>
                      {[["virtual", "虚拟盘", C.accent], ["real", "实盘", C.purple]].map(([kind, label, color]) => {
                        const d = detail?.[kind];
                        if (!d || !d.items.length) {
                          return <div key={kind} style={{ fontSize: 11, color: C.textDim }}>{label}：当天无结算</div>;
                        }
                        return (
                          <div key={kind} style={{ fontSize: 11 }}>
                            <div style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "baseline", marginBottom: 5 }}>
                              <span style={{ color, fontWeight: 700 }}>{label}</span>
                              <span style={{ color: C.textDim }}>{d.win}胜{d.loss}负</span>
                              <span style={{ color: C.textDim }}>盈亏 {fgross(d.gross)}</span>
                              <span style={{ color: d.net >= 0 ? C.accent : C.red, fontWeight: 700 }}>净盈亏 {fnum(d.net)}</span>
                            </div>
                            {d.items.map((it, i) => {
                              const won = it.result === "win";
                              const cn = it.type === "single" ? compName(it.competition_id) : null;
                              return (
                                <div key={i} style={{
                                  display: "flex", flexWrap: "wrap", gap: 6, alignItems: "baseline",
                                  padding: "4px 0", borderTop: i ? `1px solid ${C.border}` : "none", lineHeight: 1.6,
                                }}>
                                  <span style={{
                                    background: won ? C.accentDim : C.red + "22", color: won ? C.accent : C.red,
                                    borderRadius: 4, padding: "0 5px", fontSize: 10, fontWeight: 700, flexShrink: 0,
                                  }}>{won ? "赢" : "输"}</span>
                                  {it.type === "parlay" ? (
                                    <span style={{ color: C.text, fontWeight: 600 }}>{it.legs.length} 串 1</span>
                                  ) : (
                                    <span style={{ color: C.text, fontWeight: 600 }}>
                                      {zhTeam(it.team1)} vs {zhTeam(it.team2)}
                                    </span>
                                  )}
                                  {it.type === "single" && (
                                    <span style={{ color: C.textDim }}>
                                      {cn ? `${cn} · ` : ""}{it.matchDate ? fdt(it.matchDate) : ""}
                                      {" · 押 "}
                                      {it.outcome === "home" ? zhTeam(it.team1) : it.outcome === "away" ? zhTeam(it.team2) : "平局"}
                                      {it.odds ? ` @${fod(it.odds)}` : ""}
                                    </span>
                                  )}
                                  {it.type === "parlay" && (
                                    <span style={{ color: C.textDim }}>总赔率 {fod(it.odds)}</span>
                                  )}
                                  <span style={{ marginLeft: "auto", display: "flex", gap: 8, flexShrink: 0 }}>
                                    <span style={{ color: C.textDim }}>本金 {fgross(it.stake)}</span>
                                    <span style={{ color: C.textDim }}>盈亏 {fgross(it.gross)}</span>
                                    <span style={{ color: it.net >= 0 ? C.accent : C.red, fontWeight: 700 }}>净盈亏 {fnum(it.net)}</span>
                                  </span>
                                  {/* 串关一注跨好几场，每条腿单独列一行，
                                      同样带球队、赛事、比赛日期 */}
                                  {it.type === "parlay" && (
                                    <div style={{ width: "100%", paddingLeft: 10 }}>
                                      {it.legs.map((l, j) => {
                                        const lcn = compName(l.competition_id);
                                        return (
                                          <div key={j} style={{ color: C.textDim, fontSize: 10.5, lineHeight: 1.7 }}>
                                            · {zhTeam(l.team1)} vs {zhTeam(l.team2)}
                                            {lcn ? ` · ${lcn}` : ""}{l.date ? ` · ${fdt(l.date)}` : ""}
                                            {" · 押 "}
                                            {l.outcome === "home" ? zhTeam(l.team1) : l.outcome === "away" ? zhTeam(l.team2) : "平局"}
                                            {l.odds ? ` @${fod(l.odds)}` : ""}
                                            {l.score ? ` (${l.score})` : ""}
                                          </div>
                                        );
                                      })}
                                    </div>
                                  )}
                                </div>
                              );
                            })}
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Parlay Suggest Tab ──────────────────────────────────────
// 单场暴露（集中度）。串关最容易被低估的风险不是"某一注会不会中"，
// 而是同一条腿被反复用在好几张票里：一场比赛走反，所有含这条腿的票
// **整张**作废——连那些其他腿全中的票也一起没了。
//
// 为什么按 (match_id, outcome) 聚合而不是只按 match_id：同一场押主队和
// 押客队是互斥的两笔，不该加在一起当成同一份风险。押罗奇代尔的票和押
// 布拉德福德的票不会同时死。
//
// 单场实盘注单也计进去：它输了同样是真金白银没了。串关是"整张作废"、
// 单场是"输自己那一注"，机制不同，但对"这场走反我会亏掉多少待结算的钱"
// 这个问题来说两者都算。
//
// 只算实盘（kind === "real" 的串关 + RealBet）。虚拟盘是测试模型用的假钱，
// 集中不集中都不产生财务风险，混进来只会稀释这个数字的含义。
function buildExposure(realBets, parlays) {
  const byLeg = new Map();   // "matchId:outcome" -> { stake, tickets, ... }
  let total = 0;
  function add(matchId, outcome, stake, meta) {
    const key = `${matchId}:${outcome}`;
    if (!byLeg.has(key)) byLeg.set(key, { key, matchId, outcome, stake: 0, tickets: 0, ...meta });
    const e = byLeg.get(key);
    e.stake += stake;
    e.tickets += 1;
  }
  for (const b of realBets || []) {
    if (b.result !== "pending") continue;
    const s = b.stake_real || 0;
    total += s;
    add(b.match_id, b.outcome, s, { team1: b.team1, team2: b.team2, date: b.date, competition_id: b.competition_id });
  }
  for (const p of parlays || []) {
    if (p.result !== "pending" || p.kind !== "real") continue;
    const s = p.stake || 0;
    total += s;
    for (const l of p.legs || []) {
      add(l.match_id, l.outcome, s, { team1: l.team1, team2: l.team2, date: l.date, competition_id: l.competition_id });
    }
  }
  const rows = [...byLeg.values()].sort((a, b) => b.stake - a.stake);
  return { byLeg, rows, total };
}

// 单场暴露的提醒线。业界常见的自律区间是"任何单一比赛不超过在压总额的
// 20-25%"，这里取 25% 提醒、40% 判定为危险。刻意不做成可配置项——
// 这不是一个需要调参的策略参数，是一条自律线，做成可调的第一件事就是
// 把它调高。数字本身也不是从这个项目的数据里推出来的，只是常识性上限，
// 注释在这里写清楚，免得以后被当成"验证过的最优阈值"。
const EXPOSURE_WARN_PCT = 25;
const EXPOSURE_DANGER_PCT = 40;
const exposureColor = p => p >= EXPOSURE_DANGER_PCT ? C.red : p >= EXPOSURE_WARN_PCT ? C.gold : C.textDim;

function ParlaySuggestTab({ upcoming, settings, parlayBets, realBets = [], onRefresh }) {
  const zhTeam = useZhTeam();
  const [selected, setSelected] = useState({});   // { matchId: true }
  // 赔率从后端已保存的记录初始化——之前这里是空对象，赔率只活在 React state 里，
  // 一刷新就没了；单场那边（MatchCard）一直是从 match.latest_odds 读的，
  // 所以单场不受影响。现在两边用同一个数据来源。
  const [odds, setOdds] = useState(() => {
    const init = {};
    for (const m of upcoming) {
      // 自己填过的价优先；没填过就用市场**平均价**兜底，这样自动抓回来的
      // 比赛不用先去单场页手填一遍才能参与串关搜索。用平均价不用最优价，
      // 理由跟 MatchCard 一样：最优价你在 BK8 多半拿不到，拿它算 EV 是
      // 给自己发假信号。
      const src = m.latest_odds || (m.market_odds && {
        odds_home: m.market_odds.avg_home,
        odds_draw: m.market_odds.avg_draw,
        odds_away: m.market_odds.avg_away,
      });
      if (src) {
        init[m.id] = {
          home: src.odds_home?.toString() || "",
          draw: src.odds_draw?.toString() || "",
          away: src.odds_away?.toString() || "",
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

  // 候选池只留"已经手动输入过赔率"的比赛——串关本来就要先在单场那边
  // （MatchCard）把赔率填进去、存到后端，才谈得上"选它下注"；把还没
  // 定价的比赛也混在列表里，找起来反而费劲。平局赔率允许缺（有些用户
  // 只填主客），主客两边任一没填就当没定价，跟单场那边的校验口径一致
  // （见 MatchCard 的 compute()：!h || !a 直接不算）。
  // 加上"有市场报价"这一路：以前只认手填的价，导致自动抓回来的比赛必须
  // 先去单场页手填一遍才会出现在这里，那就等于没自动化。
  const withOdds = useMemo(
    () => upcoming.filter(m =>
      (m.latest_odds?.odds_home && m.latest_odds?.odds_away) ||
      (m.market_odds?.avg_home && m.market_odds?.avg_away)),
    [upcoming]
  );

  const matchById = useMemo(
    () => Object.fromEntries(upcoming.map(m => [m.id, m])),
    [upcoming]
  );

  // 把后端拼好的英文腿标签换成"中文队名 + 联赛 + 日期"——后端那条 label
  // 是格式化字符串（球队名 + vs），字符串层面翻译太脆，直接用 match_id
  // 查回原始比赛对象重新拼一遍更可靠。查不到（理论上不该发生）就退回
  // 后端原文，不让这条腿从界面上消失。
  function legDisplay(matchId, outcome) {
    const m = matchById[matchId];
    if (!m) return null;
    const who = outcome === "home" ? zhTeam(m.team1)
      : outcome === "away" ? zhTeam(m.team2)
      : "平局";
    const comp = m.competition_name ? `${m.competition_name} · ` : "";
    return `${who}（${zhTeam(m.team1)} vs ${zhTeam(m.team2)}）· ${comp}${fdt(m.date)}`;
  }

  // 判断"这组推荐是不是已经下过注了"——按 (match_id, outcome) 这套腿的
  // 组成来比对，不看队名文本，中英文切换、翻译表有没有覆盖到都不影响
  // 判断结果。虚拟盘、实盘只要腿的组合一样就算重复，不区分 kind——
  // 用户关心的是"这注我是不是已经下过"，不是"我是不是已经用真钱下过"。
  function legSetSignature(legs) {
    return legs.map(l => `${l.match_id}:${l.outcome}`).sort().join("|");
  }
  const betSignatures = useMemo(() => {
    const map = new Map();
    for (const p of parlayBets || []) {
      const sig = legSetSignature(p.legs);
      if (!map.has(sig)) map.set(sig, []);
      map.get(sig).push(p.kind);
    }
    return map;
  }, [parlayBets]);

  // 当前实盘待结算的单场暴露，见 buildExposure 的说明。
  // 刻意用未按联赛筛过的原始注单——集中度问题是整个账本的属性，
  // 筛到某个联赛只会让它看起来比实际轻。
  const exposure = useMemo(() => buildExposure(realBets, parlayBets), [realBets, parlayBets]);
  const [showExposure, setShowExposure] = useState(false);

  // 「这一注按 stake 下进去之后，本组合里最集中的那条腿会变成多少」。
  // 分母也要加上这一注——新钱同时抬高分子和分母，只加分子会高估。
  function projectExposure(combo, stake) {
    const s = +stake || 0;
    // 手上一注待结算的都没有时不提示。那种情况下算出来必然是 100%
    // （唯一一注当然占全部），字面上没错但没有任何信息量——"集中度"
    // 是相对于一个已经存在的账本才有意义的概念。等真的有票在压着了，
    // 再开始提醒。
    if (exposure.total <= 0) return null;
    const newTotal = exposure.total + s;
    if (newTotal <= 0) return null;
    let worst = null;
    for (const leg of combo.legs) {
      const cur = exposure.byLeg.get(`${leg.match_id}:${leg.outcome}`);
      const after = (cur?.stake || 0) + s;
      const pct = after / newTotal * 100;
      if (!worst || pct > worst.pct) {
        worst = { leg, pct, after, before: cur?.stake || 0, tickets: (cur?.tickets || 0) + 1 };
      }
    }
    return worst;
  }

  const allSelected = withOdds.length > 0 && withOdds.every(m => selected[m.id]);
  function toggleSelectAll() {
    if (allSelected) {
      setSelected(s => {
        const next = { ...s };
        for (const m of withOdds) delete next[m.id];
        return next;
      });
    } else {
      setSelected(s => ({ ...s, ...Object.fromEntries(withOdds.map(m => [m.id, true])) }));
    }
  }

  function updateMinLegs(v) {
    setMinLegs(v);
    setMaxLegs(m => Math.max(m, v));
  }
  function updateMaxLegs(v) {
    setMaxLegs(v);
    setMinLegs(m => Math.min(m, v));
  }

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

      // 把赔率存回后端，这样刷新页面后还在（之前只存在 React state 里，一刷新就丢）。
      //
      // 两处跟原来不一样，都是为了修 "Failed to fetch"：
      //
      // 1) 走 /odds/bulk，一个请求搞定。原来是**每场一个 POST**——以前候选池
      //    里只有手填过赔率的那几场，撑死十几个请求；接进市场赔率自动导入后
      //    用户能选 115 场，就变成 115 个并发请求、115 次写事务，浏览器排 19
      //    批、服务端连接池只有 10 条，超时一到连接被切断，前端报 Failed to fetch。
      //
      // 2) **只存用户真正改过的价**。市场价是每轮更新自动刷新的，把它原样
      //    存成"用户填的价"有两个坏处：一是本来就不是用户填的，二是存完之后
      //    latest_odds 会永久盖过 market_odds，界面上那个价就冻在当时那一刻，
      //    再也不跟着市场更新了。
      const edited = matches.filter(m => {
        if (!m.odds_home || !m.odds_away) return false;
        const mk = matchById[m.match_id]?.market_odds;
        if (!mk) return true;                  // 没有市场价，那必然是手填的
        const same = (a, b) => (a == null && b == null) || Math.abs((a || 0) - (b || 0)) < 1e-9;
        return !(same(m.odds_home, mk.avg_home) &&
                 same(m.odds_draw, mk.avg_draw) &&
                 same(m.odds_away, mk.avg_away));
      });
      if (edited.length) {
        await api("/odds/bulk", {
          method: "POST",
          body: JSON.stringify({ items: edited }),
        }).catch(() => null);   // 存赔率失败不该挡住搜索结果
      }

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
        只有单腿本身是正EV的选项才会进入候选池——赔率再高，如果模型概率算下来是负EV，
        不会被推荐。热门强队的赔率经常被市场压得低于其真实胜率对应的公平赔率，串起来只会让负EV被放大，
        不会凭空创造价值。
      </div>

      {/* 当前账本的单场集中度。放在搜索结果之前，因为它描述的是"你现在
          已经压了什么"，跟这次搜什么组合无关。默认收起，只把最集中的
          那一条和总额摆在标题上——平时不碍事，超过提醒线时颜色会变。 */}
      {exposure.total > 0 && exposure.rows.length > 0 && (() => {
        const top = exposure.rows[0];
        const topPct = top.stake / exposure.total * 100;
        return (
          <div style={{ background: C.card, border: `1px solid ${topPct >= EXPOSURE_WARN_PCT ? exposureColor(topPct) + "66" : C.border}`,
                        borderRadius: 9, marginBottom: 12, overflow: "hidden" }}>
            <button onClick={() => setShowExposure(v => !v)} style={{
              width: "100%", display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap",
              background: "none", border: "none", color: C.text, textAlign: "left",
              padding: "10px 13px", fontSize: 11.5, cursor: "pointer",
            }}>
              <span style={{ color: C.textDim, fontSize: 10 }}>{showExposure ? "▾" : "▸"}</span>
              <span style={{ fontWeight: 700 }}>单场集中度</span>
              <span style={{ color: C.textDim }}>实盘待结算 {fgross(exposure.total)}</span>
              <span style={{ marginLeft: "auto", color: exposureColor(topPct), fontWeight: 800 }}>
                最集中一场 {topPct.toFixed(1)}%
              </span>
            </button>
            {showExposure && (
              <div style={{ padding: "0 13px 12px" }}>
                <div style={{ fontSize: 10, color: C.textDim, lineHeight: 1.6, marginBottom: 8 }}>
                  这一场走反 → 所有含这条腿的串关<strong>整张作废</strong>（其他腿全中也一样），
                  加上押同一边的单场注单，一共会亏掉多少待结算本金。
                  百分比加起来会超过 100%——同一张票被它含的每条腿各算一次，这正是重叠的定义。
                  提醒线 {EXPOSURE_WARN_PCT}%，{EXPOSURE_DANGER_PCT}% 以上按危险标红。
                </div>
                {exposure.rows.slice(0, 10).map(r => {
                  const p = r.stake / exposure.total * 100;
                  const m = matchById[r.matchId];
                  const t1 = zhTeam(r.team1 || m?.team1), t2 = zhTeam(r.team2 || m?.team2);
                  const who = r.outcome === "home" ? t1 : r.outcome === "away" ? t2 : "平局";
                  return (
                    <div key={r.key} style={{ display: "flex", alignItems: "center", gap: 8, padding: "5px 0",
                                              borderTop: `1px solid ${C.border}`, fontSize: 11 }}>
                      <span style={{ flex: 1, minWidth: 0 }}>
                        <span style={{ color: C.text }}>{t1} vs {t2}</span>
                        <span style={{ color: C.textDim }}>（押 {who}）</span>
                        {(r.date || m?.date) && <span style={{ color: C.textDim }}> · {fdt(r.date || m.date)}</span>}
                      </span>
                      <span style={{ color: C.textDim, whiteSpace: "nowrap" }}>{r.tickets} 笔</span>
                      <span style={{ color: C.textDim, whiteSpace: "nowrap", width: 62, textAlign: "right" }}>{fgross(r.stake)}</span>
                      <span style={{ color: exposureColor(p), fontWeight: 800, whiteSpace: "nowrap", width: 48, textAlign: "right" }}>
                        {p.toFixed(1)}%
                      </span>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        );
      })()}

      <div style={{ display: "flex", gap: 12, alignItems: "center", marginBottom: 12, flexWrap: "wrap" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ fontSize: 11, color: C.textDim }}>最少腿数:</span>
          {/* 最少/最多两个下拉互相夹住对方——改最多到比当前最少还低（比如
              两边都设成2，串死2腿）时把最少也拉下来，反过来同理，不然会
              选出一个后端直接拒绝的 min>max 组合，用户不知道为什么报错。 */}
          <select value={minLegs} onChange={e => updateMinLegs(+e.target.value)}
            style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 6, padding: "5px 8px", color: C.text, fontSize: 12 }}>
            {[2, 3, 4, 5, 6, 7, 8].map(n => <option key={n} value={n}>{n}</option>)}
          </select>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ fontSize: 11, color: C.textDim }}>最多腿数:</span>
          <select value={maxLegs} onChange={e => updateMaxLegs(+e.target.value)}
            style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 6, padding: "5px 8px", color: C.text, fontSize: 12 }}>
            {[2, 3, 4, 5, 6, 7, 8].map(n => <option key={n} value={n}>{n}</option>)}
          </select>
        </div>
        <span style={{ fontSize: 11, color: C.textDim }}>已选 {selectedIds.length} / {withOdds.length} 场（已定价）</span>
        {withOdds.length > 0 && (
          <button onClick={toggleSelectAll}
            style={{ background: "none", border: `1px solid ${C.accent}`, color: C.accent,
                     borderRadius: 6, padding: "4px 10px", fontSize: 11, fontWeight: 700 }}>
            {allSelected ? "取消全选" : "全选"}
          </button>
        )}
      </div>

      {upcoming.length === 0 && <Empty text="暂无即将赛事可供选择" />}
      {upcoming.length > 0 && withOdds.length === 0 && (
        <Empty text="即将赛事都还没有输入赔率——先在「单场」里给要考虑的比赛填上赔率，才会出现在这里" />
      )}

      <DayGroups matches={withOdds} selectedIds={new Set(selectedIds)} renderMatch={m => {
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
                  <div style={{ fontWeight: 700, fontSize: 13 }}>{zhTeam(m.team1)} <span style={{ color: C.textDim, fontWeight: 400, fontSize: 12 }}>vs</span> {zhTeam(m.team2)}</div>
                  <div style={{ fontSize: 10, color: C.textDim, marginTop: 2 }}>
                    {m.competition_name && <span style={{ color: C.accent }}>{m.competition_name}</span>} · {fdt(m.date)}{m.round ? ` · ${m.round}` : ""}
                  </div>
                </div>
              </div>
              {p && <span style={{ fontSize: 10, color: C.textDim }}>主{pct(p.prob_home)} 平{pct(p.prob_draw)} 客{pct(p.prob_away)}</span>}
            </div>
            {isSel && (
              <div style={{ padding: "8px 14px 12px", borderTop: `1px solid ${C.border}`, display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(90px, 1fr))", gap: 7 }}>
                {[
                  { label: `${zhTeam(m.team1)} 赔率`, field: "home" },
                  { label: "平局 赔率", field: "draw" },
                  { label: `${zhTeam(m.team2)} 赔率`, field: "away" },
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
        {loading ? "搜索中..." : `生成推荐组合（已选${selectedIds.length}场）`}
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
          {result.combinations.map((combo, i) => {
            const dupKinds = betSignatures.get(legSetSignature(combo.legs));
            return (
            <div key={i} style={{ background: C.card, border: `1px solid ${dupKinds ? C.gold + "88" : combo.tag ? C.accent + "66" : C.border}`, borderRadius: 10, marginBottom: 10, padding: "12px 14px" }}>
              {dupKinds && (
                <div style={{ fontSize: 11, fontWeight: 800, color: C.gold, marginBottom: 8 }}>
                  ⚠️ 已下过注（{[...new Set(dupKinds)].map(k => k === "real" ? "实盘" : "虚拟盘").join("+")}）· 同样的腿，避免重复下注
                </div>
              )}
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
                相对最弱一腿（{legDisplay(combo.weakest_leg_match_id, combo.weakest_leg_outcome) || combo.weakest_leg_label} {pct(combo.weakest_leg_prob)}）命中率打了 {(combo.risk_ratio_vs_weakest_leg * 10).toFixed(1)} 折
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 4, marginBottom: 10 }}>
                {combo.legs.map((leg, j) => (
                  <div key={j} style={{ display: "flex", justifyContent: "space-between", fontSize: 11, background: C.bg, borderRadius: 6, padding: "6px 9px" }}>
                    <span>{legDisplay(leg.match_id, leg.outcome) || leg.label}</span>
                    <span style={{ color: C.textDim }}>@{leg.odds} · {pct(leg.prob)}</span>
                  </div>
                ))}
              </div>

              {/* 下这一注之后集中度会变成多少。金额优先用实盘表单里已经填的，
                  没填就按凯利建议试算——这样还没展开实盘表单时也看得到。
                  只在超过提醒线时才显示，平时不占地方也不制造噪音。 */}
              {(() => {
                const stake = +realStake[i] || combo.kelly_amount || 0;
                const w = stake > 0 ? projectExposure(combo, stake) : null;
                if (!w || w.pct < EXPOSURE_WARN_PCT) return null;
                const c = exposureColor(w.pct);
                const beforePct = exposure.total > 0 ? w.before / exposure.total * 100 : 0;
                return (
                  <div style={{ background: c + "18", border: `1px solid ${c}55`, borderRadius: 7,
                                padding: "8px 10px", marginBottom: 10, fontSize: 10.5, color: c, lineHeight: 1.6 }}>
                    <strong>{w.pct >= EXPOSURE_DANGER_PCT ? "⛔ 集中度危险" : "⚠️ 集中度偏高"}</strong>
                    ：按 {fgross(stake)} 下这一注，
                    「{legDisplay(w.leg.match_id, w.leg.outcome) || w.leg.label}」
                    会占到实盘待结算总额的 <strong>{w.pct.toFixed(1)}%</strong>
                    （现在 {beforePct.toFixed(1)}%，共 {w.tickets} 笔）。
                    这一场走反，这些票会整张作废。
                  </div>
                );
              })()}

              <div style={{ display: "flex", gap: 6, borderTop: `1px solid ${C.border}`, paddingTop: 10 }}>
                <button
                  onClick={() => recordParlay(combo, i, "virtual")}
                  disabled={recording === `virtual-${i}` || recorded === `virtual-${i}`}
                  style={{ flex: 1, padding: "9px 4px", borderRadius: 6, border: "none", background: C.blue, color: "#fff", fontWeight: 700, fontSize: 12 }}
                >
                  {recorded === `virtual-${i}` ? "✓ 已记录" : recording === `virtual-${i}` ? "..." : `记虚拟盘（${Math.round(combo.kelly_amount || 0).toLocaleString()}）`}
                </button>
                <button
                  onClick={() => setShowRealForm(showRealForm === i ? null : i)}
                  style={{ flex: 1, padding: "9px 4px", borderRadius: 6, border: `1px solid ${C.purple}`, background: showRealForm === i ? C.purple : "transparent", color: showRealForm === i ? "#0a0510" : C.purple, fontWeight: 700, fontSize: 12 }}
                >
                  记实盘
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
                      {recorded === `real-${i}` ? "✓" : recording === `real-${i}` ? "..." : "确认"}
                    </button>
                  </div>
                </div>
              )}
            </div>
            );
          })}
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
      <div style={{ color: C.text, fontWeight: 700, marginBottom: 6 }}>休赛期，暂时没有比赛</div>
      <div style={{ fontSize: 12, maxWidth: 420, margin: "0 auto" }}>
        库里已有 <strong style={{ color: C.text }}>{played.length}</strong> 场完赛数据，
        最后一场是 <strong style={{ color: C.text }}>{last.date}</strong>（{days} 天前），
        所以抓取本身是正常的。新赛季的赛程数据源还没发布，
        发布后系统会在下次更新时<strong style={{ color: C.text }}>自动接上</strong>，不需要改任何东西。
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
      <div>{text}</div>
    </div>
  );
}
