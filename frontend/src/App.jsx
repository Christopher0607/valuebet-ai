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
  const [backtestByComp, setBacktestByComp] = useState([]);
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
      setBacktestByComp(bt.by_competition || []);
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
  // 顶部统计栏取第一个赛事的数字。刻意不做跨赛事求和/平均——后端已经
  // 拆开了，前端再合并回去等于把刚修的 bug 重新引入一遍。
  const backtest = backtestByComp[0] || null;

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
          <ParlaySuggestTab upcoming={upcoming} settings={settings} onRefresh={loadAll} />
        )}

        {!loading && tab === "backtest" && (
          <div>
            {backtestByComp.length === 0 && <Empty text="还没有已完赛的比赛" />}
            {backtestByComp.map(bc => {
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

        {!loading && tab === "strategy" && <StrategyTab />}
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

  const loadLog = useCallback(async () => {
    try { setLog(await api("/price-log")); } catch { /* 后端没起时静默 */ }
  }, []);
  useEffect(() => { loadLog(); }, [loadLog]);

  // 赔率一变就重算，不需要按按钮
  useEffect(() => {
    const o = parseFloat(odds);
    if (!o || o <= 1) { setRes(null); return; }
    const body = { odds: o };
    const a = parseFloat(avg), b = parseFloat(best);
    if (a > 1 && b > 1) { body.market_avg = a; body.market_best = b; }
    let cancelled = false;
    api("/strategy/evaluate", { method: "POST", body: JSON.stringify(body) })
      .then(r => { if (!cancelled) setRes(r); })
      .catch(() => { if (!cancelled) setRes(null); });
    return () => { cancelled = true; };
  }, [odds, avg, best]);

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
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 10, marginTop: 8 }}>
          <div><div style={lbl}>你的平台赔率 *</div>
            <input style={inp} value={odds} onChange={e => setOdds(e.target.value)} placeholder="1.45" inputMode="decimal" /></div>
          <div><div style={lbl}>市场平均赔率</div>
            <input style={inp} value={avg} onChange={e => setAvg(e.target.value)} placeholder="1.42" inputMode="decimal" /></div>
          <div><div style={lbl}>全市场最高赔率</div>
            <input style={inp} value={best} onChange={e => setBest(e.target.value)} placeholder="1.50" inputMode="decimal" /></div>
        </div>
        <div style={{ fontSize: 10, color: C.textDim, marginTop: 8, lineHeight: 1.6 }}>
          只填第一格也能判断方向，但<strong style={{ color: C.text }}>能不能赚钱由后两格决定</strong>
          ——同一个赔率，价格捕获率 0% 是 -1.43%，100% 是 +1.67%。
          平均价和最高价可以在任意赔率比较网站上查。
        </div>
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
                   style={{ ...inp, width: 180, fontSize: 11, fontWeight: 600 }} />
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
          <select value={margin} onChange={e => setMargin(+e.target.value)} style={{ ...inp, width: 260, fontSize: 12 }}>
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
function Empty({ text }) {
  return (
    <div style={{ textAlign: "center", padding: "36px 20px", color: C.textDim }}>
      <div style={{ fontSize: 28, opacity: 0.3, marginBottom: 10 }}>📭</div>
      <div>{text}</div>
    </div>
  );
}
