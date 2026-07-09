import { useState, useEffect, useCallback } from "react";
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine } from "recharts";

const API = "http://127.0.0.1:8000/api";

async function api(path, opts) {
  const res = await fetch(API + path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
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
  const [backtest, setBacktest] = useState(null);
  const [bets, setBets]     = useState([]);
  const [realBets, setRealBets] = useState([]);
  const [bankroll, setBankroll] = useState(null);
  const [settings, setSettings] = useState(null);
  const [showSett, setShowSett] = useState(false);
  const [loading, setLoading]   = useState(true);
  const [apiError, setApiError] = useState(null);
  const [updating, setUpdating] = useState(false);

  const loadAll = useCallback(async () => {
    setLoading(true);
    setApiError(null);
    try {
      const [st, all, bt, vb, rb, br, se] = await Promise.all([
        api("/status"),
        api("/matches"),
        api("/backtest-summary"),
        api("/bets"),
        api("/real-bets"),
        api("/bankroll-summary"),
        api("/settings"),
      ]);
      setStatus(st);
      setMatches(all);
      setBacktest(bt);
      setBets(vb);
      setRealBets(rb);
      setBankroll(br);
      setSettings(se);
    } catch (e) {
      setApiError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadAll(); }, [loadAll]);

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

  const upcoming = matches.filter(m => m.status === "upcoming");
  const played   = matches.filter(m => m.status === "played");

  // ── Backend not running: clear, actionable error state ──
  if (apiError && !loading) {
    return (
      <div style={{ minHeight: "100vh", background: C.bg, color: C.text, display: "flex", alignItems: "center", justifyContent: "center", fontFamily: "'Inter',system-ui,sans-serif", padding: 24 }}>
        <div style={{ maxWidth: 480, background: C.card, border: `1px solid ${C.red}44`, borderRadius: 12, padding: 28 }}>
          <div style={{ fontSize: 32, marginBottom: 12 }}>⚠️</div>
          <div style={{ fontSize: 16, fontWeight: 800, marginBottom: 8 }}>无法连接本地后端</div>
          <div style={{ fontSize: 13, color: C.textDim, lineHeight: 1.7, marginBottom: 16 }}>
            前端正常运行，但无法访问 <code style={{ background: C.bg, padding: "2px 5px", borderRadius: 4 }}>http://127.0.0.1:8000</code>。
            请确认后端已启动：在 <code style={{ background: C.bg, padding: "2px 5px", borderRadius: 4 }}>backend/</code> 目录运行
            <code style={{ display: "block", background: C.bg, padding: "8px 10px", borderRadius: 6, marginTop: 8 }}>uvicorn app.main:app --reload --port 8000</code>
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
        @keyframes spin { to { transform: rotate(360deg); } }
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
              <div style={{ fontSize: 9, color: C.textDim, textTransform: "uppercase", letterSpacing: "0.7px" }}>本地版 · FastAPI + SQLite</div>
            </div>
          </div>
          <div style={{ display: "flex", gap: 6 }}>
            <button
              onClick={() => setShowSett(s => !s)}
              style={{ padding: "6px 12px", borderRadius: 7, border: `1px solid ${showSett ? C.purple : C.border}`, background: showSett ? C.purpleDim : "transparent", color: showSett ? C.purple : C.textDim, fontSize: 11, fontWeight: 700 }}
            >
              ⚙ 设置
            </button>
          </div>
        </div>

        <div style={{ display: "flex", gap: 4, marginTop: 10, flexWrap: "wrap" }}>
          {[["upcoming", "⚡ 预测"], ["parlay", "🎯 串关推荐"], ["backtest", "📊 回测"], ["bets", "🎲 虚拟盘"], ["realbets", "💵 实盘"], ["chart", "📈 走势"]].map(([k, l]) => (
            <button
              key={k}
              onClick={() => setTab(k)}
              style={{ padding: "5px 11px", borderRadius: 7, border: `1px solid ${tab === k ? C.accent : C.border}`, background: tab === k ? C.accentDim : "transparent", color: tab === k ? C.accent : C.textDim, fontSize: 11, fontWeight: 700 }}
            >
              {l}
            </button>
          ))}
        </div>
      </div>

      {showSett && settings && (
        <SettingsPanel settings={settings} onSave={saveSettings} onClose={() => setShowSett(false)} />
      )}

      {/* Stats bar */}
      {backtest && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(6,1fr)", background: C.border, gap: 1 }}>
          {[
            { v: `${backtest.correct}/${backtest.total}`, l: "预测正确", c: C.blue },
            { v: pct(backtest.accuracy), l: "准确率", c: backtest.accuracy > 0.6 ? C.accent : C.gold },
            { v: backtest.avg_rps?.toFixed(3), l: "平均RPS", c: C.accent },
            { v: bets.length, l: "虚拟下注", c: C.text },
            { v: realBets.length, l: "实盘下注", c: C.purple },
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
            {upcoming.length === 0 && <Empty text="暂无即将赛事，或数据还未抓取——点顶部「立即更新」试试" />}
            {upcoming.map(m => (
              <MatchCard key={m.id} match={m} settings={settings} onRefresh={loadAll} />
            ))}
          </div>
        )}

        {!loading && tab === "parlay" && settings && (
          <ParlaySuggestTab upcoming={upcoming} settings={settings} />
        )}

        {!loading && tab === "backtest" && backtest && (
          <div>
            <SL>回测结果 · {played.length} 场已完赛 · 纯模型预测（赛前不知结果）</SL>
            <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 10, padding: 14, marginBottom: 14, display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 12 }}>
              <Stat label="总场次" val={backtest.total} color={C.blue} />
              <Stat label="模型正确" val={backtest.correct} color={C.accent} />
              <Stat label="模型错误" val={backtest.total - backtest.correct} color={C.red} />
              <Stat label="准确率" val={pct(backtest.accuracy)} color={backtest.accuracy > 0.6 ? C.accent : C.gold} />
              <Stat label="平均RPS" val={backtest.avg_rps?.toFixed(3)} color={C.blue} sub="↓越低越准" />
              <Stat label="随机基准" val={backtest.random_baseline_rps} color={C.textDim} />
              <Stat label="相对改善" val={backtest.avg_rps ? (((backtest.random_baseline_rps - backtest.avg_rps) / backtest.random_baseline_rps) * 100).toFixed(1) + "%" : "—"} color={C.accent} />
              <Stat label="评级" val={backtest.avg_rps < 0.18 ? "优秀" : backtest.avg_rps < 0.21 ? "良好" : "待提升"} color={backtest.avg_rps < 0.18 ? C.accent : backtest.avg_rps < 0.21 ? C.gold : C.red} />
            </div>
            <div style={{ background: C.surface, borderRadius: 10, overflow: "hidden", border: `1px solid ${C.border}` }}>
              <div style={{ display: "grid", gridTemplateColumns: "68px 1fr 52px 52px 52px 52px 52px 44px", padding: "7px 12px", background: C.muted, fontSize: 9, color: C.textDim, fontWeight: 700, textTransform: "uppercase", gap: 3 }}>
                {["日期", "赛事", "主胜%", "平%", "客胜%", "比分", "RPS", "结果"].map(h => <span key={h}>{h}</span>)}
              </div>
              {played.map(m => {
                const p = m.prediction;
                if (!p) return null;
                return (
                  <div key={m.id} style={{ display: "grid", gridTemplateColumns: "68px 1fr 52px 52px 52px 52px 52px 44px", padding: "7px 12px", borderBottom: `1px solid ${C.border}`, background: p.is_correct ? "transparent" : C.redDim, gap: 3, alignItems: "center", fontSize: 11 }}>
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
        )}

        {!loading && tab === "bets" && settings && (
          <div>
            <SL>虚拟下注 · 起始 {(+settings.bankroll_total).toLocaleString()} 单位</SL>
            <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 10, padding: 14, marginBottom: 14, display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 12 }}>
              <Stat label="总注数" val={bets.length} color={C.blue} />
              <Stat label="赢注" val={bets.filter(b => b.result === "win").length} color={C.accent} />
              <Stat label="待结算" val={bets.filter(b => b.result === "pending").length} color={C.gold} />
              <Stat label="总盈亏" val={fnum(bankroll?.virtual?.total_pnl)} color={(bankroll?.virtual?.total_pnl || 0) >= 0 ? C.accent : C.red} />
              <Stat label="ROI" val={bankroll?.virtual ? bankroll.virtual.roi_pct.toFixed(1) + "%" : "—"} color={(bankroll?.virtual?.roi_pct || 0) >= 0 ? C.accent : C.red} />
              <Stat label="胜率" val={bets.length ? pct(bets.filter(b => b.result === "win").length / bets.length) : "—"} color={C.blue} />
              <Stat label="" val="" color={C.textDim} />
              <Stat label="" val="" color={C.textDim} />
            </div>
            {bets.length === 0 && <Empty text="还没有虚拟下注。去「预测」页输入赔率，点「🎲 虚拟」。" />}
            {bets.length > 0 && (
              <div style={{ background: C.surface, borderRadius: 10, overflow: "hidden", border: `1px solid ${C.border}` }}>
                <div style={{ display: "grid", gridTemplateColumns: "64px 1fr 60px 52px 52px 52px 60px 40px", padding: "7px 12px", background: C.muted, fontSize: 9, color: C.textDim, fontWeight: 700, textTransform: "uppercase", gap: 3 }}>
                  {["日期", "赛事", "方向", "赔率", "本金", "EV", "盈亏", "结果"].map(h => <span key={h}>{h}</span>)}
                </div>
                {bets.map(b => (
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
          <RealBetsTab realBets={realBets} bankroll={bankroll} settings={settings} />
        )}

        {!loading && tab === "chart" && bankroll && (
          <ChartTab bankroll={bankroll} settings={settings} />
        )}
      </div>
    </div>
  );
}

// ── Status Banner — honest about the 12h mechanism ─────────
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
          <>🖥️ 本地运行中 · 首次抓取数据中，几秒后自动刷新...</>
        ) : (
          <>
            🖥️ 本地运行中 · 上次更新 {fdatetime(status.last_update)}
            {status.last_status === "error" && <span style={{ color: C.red }}> · 上次更新失败: {status.last_detail}</span>}
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
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 10 }}>
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
function MatchCard({ match, settings, onRefresh }) {
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
          <div style={{ fontSize: 10, color: C.textDim, marginTop: 2 }}>{fdt(match.date)} · {match.round} · {match.ground}</div>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <span style={{ fontSize: 10, color: C.blue }}>ELO {mdl.elo_home}/{mdl.elo_away}</span>
          <span style={{ color: C.textDim }}>{open ? "▲" : "▼"}</span>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 1, background: C.border }}>
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
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr auto", gap: 7, alignItems: "flex-end", marginBottom: 10 }}>
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
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 7, marginBottom: 10 }}>
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
                        style={{ flex: 1, padding: "5px", borderRadius: 6, border: "none", background: item.evVal > 0 ? C.blue : C.muted, color: item.evVal > 0 ? "#fff" : C.textDim, fontWeight: 700, fontSize: 10 }}>
                        {saved === "v" + item.key ? "✅" : saving === "v" + item.key ? "..." : "🎲 虚拟"}
                      </button>
                      <button onClick={() => setShowRF(showRF === item.key ? null : item.key)}
                        style={{ flex: 1, padding: "5px", borderRadius: 6, border: `1px solid ${C.purple}`, background: showRF === item.key ? C.purple : "transparent", color: showRF === item.key ? "#0a0510" : C.purple, fontWeight: 700, fontSize: 10 }}>
                        💵 实盘
                      </button>
                    </div>
                    {showRF === item.key && (
                      <div style={{ marginTop: 7, paddingTop: 7, borderTop: `1px solid ${C.border}` }}>
                        <div style={{ fontSize: 9, color: C.textDim, marginBottom: 3 }}>真实下注金额（HKD）：</div>
                        <div style={{ display: "flex", gap: 4 }}>
                          <input type="number" placeholder={`建议 ${Math.round(item.kAmt || 0)}`} value={rStake[item.key] || ""}
                            onChange={e => setRStake(r => ({ ...r, [item.key]: e.target.value }))}
                            style={{ flex: 1, background: C.card, border: `1px solid ${C.purple}66`, borderRadius: 5, padding: "5px 7px", color: C.text, fontSize: 11 }} />
                          <button onClick={() => doRBet(item.key)} disabled={saving === "r" + item.key || saved === "r" + item.key}
                            style={{ padding: "5px 9px", borderRadius: 5, border: "none", background: C.purple, color: "#0a0510", fontWeight: 800, fontSize: 10 }}>
                            {saved === "r" + item.key ? "✅" : saving === "r" + item.key ? "..." : "确认"}
                          </button>
                        </div>
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
function RealBetsTab({ realBets, bankroll, settings }) {
  const pending = realBets.filter(b => b.result === "pending");
  const settled = realBets.filter(b => b.result !== "pending");

  return (
    <div>
      <SL>实盘记录 · 真实金钱（HKD）· 起始 {(+settings.bankroll_total).toLocaleString()}</SL>
      <div style={{ background: C.purpleDim, border: `1px solid ${C.purple}44`, borderRadius: 8, padding: "9px 13px", fontSize: 11, color: C.purple, marginBottom: 12 }}>
        💡 在「预测」页点「💵 实盘」按钮记录你真实下的注。比赛结束后系统每12小时自动结算盈亏。
      </div>
      <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 10, padding: 14, marginBottom: 14, display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 12 }}>
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
  const vSeries = bankroll.virtual?.series || [];
  const rSeries = bankroll.real?.series || [];

  return (
    <div>
      <SL>资金走势 · 虚拟盘 vs 实盘</SL>
      <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 10, padding: "16px 18px", marginBottom: 16 }}>
        <div style={{ fontSize: 11, color: C.textDim, marginBottom: 8, display: "flex", gap: 16 }}>
          <span><span style={{ display: "inline-block", width: 10, height: 10, background: C.accent, borderRadius: 2, marginRight: 5 }} />虚拟盘</span>
          <span><span style={{ display: "inline-block", width: 10, height: 10, background: C.purple, borderRadius: 2, marginRight: 5 }} />实盘</span>
        </div>
        {vSeries.length <= 1 && rSeries.length <= 1 ? (
          <Empty text="还没有已结算注单，下注后这里显示走势" />
        ) : (
          <div style={{ height: 280 }}>
            <ResponsiveContainer width="100%" height="100%">
              <LineChart>
                <CartesianGrid strokeDasharray="3 3" stroke={C.border} />
                <XAxis dataKey="date" type="category" allowDuplicatedCategory={false} tick={{ fontSize: 10, fill: C.textDim }} tickFormatter={d => fdt(d)} />
                <YAxis tick={{ fontSize: 10, fill: C.textDim }} domain={["auto", "auto"]} />
                <Tooltip contentStyle={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 8, fontSize: 12 }} labelFormatter={fdt} formatter={v => [`${Math.round(v).toLocaleString()}`, "资金"]} />
                <ReferenceLine y={+settings.bankroll_total} stroke={C.gold} strokeDasharray="4 4" label={{ value: "起始", fill: C.gold, fontSize: 10 }} />
                <Line data={vSeries} type="monotone" dataKey="balance" name="虚拟盘" stroke={C.accent} strokeWidth={2} dot={{ fill: C.accent, r: 3 }} />
                <Line data={rSeries} type="monotone" dataKey="balance" name="实盘" stroke={C.purple} strokeWidth={2} dot={{ fill: C.purple, r: 3 }} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Parlay Suggest Tab ──────────────────────────────────────
function ParlaySuggestTab({ upcoming, settings }) {
  const [selected, setSelected] = useState({});   // { matchId: true }
  const [odds, setOdds] = useState({});            // { matchId: { home, draw, away } }
  const [minLegs, setMinLegs] = useState(3);
  const [maxLegs, setMaxLegs] = useState(6);
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const selectedIds = Object.keys(selected).filter(id => selected[id]).map(Number);

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

      {upcoming.map(m => {
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
      })}

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
                <MiniStat label="半凯利建议" val={combo.kelly_amount ? combo.kelly_amount.toLocaleString() : "0"} color={C.purple} sub={pct(combo.kelly_pct)} />
              </div>
              <div style={{ fontSize: 10, color: C.textDim, marginBottom: 8 }}>
                相对最弱一腿（{combo.weakest_leg_label} {pct(combo.weakest_leg_prob)}）命中率打了 {(combo.risk_ratio_vs_weakest_leg * 10).toFixed(1)} 折
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                {combo.legs.map((leg, j) => (
                  <div key={j} style={{ display: "flex", justifyContent: "space-between", fontSize: 11, background: C.bg, borderRadius: 6, padding: "6px 9px" }}>
                    <span>{leg.label}</span>
                    <span style={{ color: C.textDim }}>@{leg.odds} · {pct(leg.prob)}</span>
                  </div>
                ))}
              </div>
            </div>
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
function Empty({ text }) {
  return (
    <div style={{ textAlign: "center", padding: "36px 20px", color: C.textDim }}>
      <div style={{ fontSize: 28, opacity: 0.3, marginBottom: 10 }}>📭</div>
      <div>{text}</div>
    </div>
  );
}
