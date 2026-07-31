"""
亚洲让球盘：09 号文档里唯一 claim 过要测、但实际没测的市场。

为什么值得单独测
----------------
1. 抽水通常比 1X2 低得多（两边盘、竞争激烈），所以同样大小的模型优势，
   在让球盘上更容易翻成正收益。
2. 没有平局这个选项。Dixon-Coles 的 tau 修正就是为了补独立泊松低估平局，
   平局恰恰是模型最没把握的部分；让球盘把这块不确定性从赔付里拿掉了。
3. 亚洲盘的定价逻辑和欧洲盘不同，理论上可能存在不同的错价。

结算规则（必须写对，写错整个测试作废）
--------------------------------------
让球 h 加在主队净胜球上：adj = (主进球 - 客进球) + h
  adj > 0  → 主队盘口赢，赔付 odds - 1
  adj == 0 → 走盘退本金，收益 0
  adj < 0  → 输，收益 -1
四分之一球盘（.25 / .75）本金对半分到相邻的两条线上，分别结算再平均。

数据集把 .25 记成 .3、.75 记成 .8（一位小数四舍五入），必须还原，
否则 -0.3 会被当成一条根本不存在的整数盘去结算。

符号约定已用数据经验验证：让球在 [-3,-1) 区间时主胜率 76.8%、
在 [+0.5,+3) 区间时 19.4%，确认负值 = 主队让球 = 主队是热门。
"""
import numpy as np
import pandas as pd
from scipy.special import gammaln
from sklearn.linear_model import LogisticRegression
import lib_walkforward as L

K = 12
pred = pd.read_parquet("walkforward_test.parquet")


def restore_quarter_lines(h):
    """把数据集里四舍五入过的 .3/.8 还原成真实的 .25/.75。"""
    sign = np.sign(h)
    a = np.abs(h)
    frac = np.round(a - np.floor(a), 2)
    a = np.where(np.isclose(frac, 0.3), np.floor(a) + 0.25, a)
    a = np.where(np.isclose(frac, 0.8), np.floor(a) + 0.75, a)
    return sign * a


def margin_dist(lam, mu, rho=L.DEFAULT_RHO, kmax=K):
    """净胜球分布。返回 (n, 2*kmax+1)，索引 i 对应净胜球 i-kmax。"""
    k = np.arange(kmax + 1)
    ph = np.exp(-lam[:, None] + k * np.log(lam[:, None]) - gammaln(k + 1))
    pa = np.exp(-mu[:, None] + k * np.log(mu[:, None]) - gammaln(k + 1))
    joint = ph[:, :, None] * pa[:, None, :]
    joint[:, 0, 0] *= 1 - lam * mu * rho
    joint[:, 0, 1] *= 1 + lam * rho
    joint[:, 1, 0] *= 1 + mu * rho
    joint[:, 1, 1] *= 1 - rho
    np.clip(joint, 1e-15, None, out=joint)
    joint /= joint.sum(axis=(1, 2), keepdims=True)

    diff = np.subtract.outer(np.arange(kmax + 1), np.arange(kmax + 1))
    out = np.zeros((len(lam), 2 * kmax + 1))
    for m in range(-kmax, kmax + 1):
        out[:, m + kmax] = (joint * (diff == m)).sum(axis=(1, 2))
    return out


def settle(margin, line, odds):
    """单条线（整球或半球）的单位本金收益。"""
    adj = margin + line
    return np.where(adj > 0, odds - 1.0, np.where(adj == 0, 0.0, -1.0))


def settle_any(margin, line, odds):
    """任意盘口（含四分之一球）的收益。四分之一球对半拆到相邻两条线。"""
    frac = np.round(np.abs(line) - np.floor(np.abs(line)), 2)
    is_q = np.isclose(frac, 0.25) | np.isclose(frac, 0.75)
    r_plain = settle(margin, line, odds)
    r_split = 0.5 * settle(margin, line - 0.25, odds) + 0.5 * settle(margin, line + 0.25, odds)
    return np.where(is_q, r_split, r_plain)


def expected_return(dist, line, odds):
    """模型算出的单位本金期望收益：对净胜球分布求期望。"""
    margins = np.arange(-K, K + 1)
    ev = np.zeros(len(line))
    for i, m in enumerate(margins):
        ev += dist[:, i] * settle_any(np.full(len(line), m), line, odds)
    return ev


ah = pred.dropna(subset=["HandiSize", "HandiHome", "HandiAway"]).copy()
for c in ["HandiSize", "HandiHome", "HandiAway"]:
    ah[c] = pd.to_numeric(ah[c], errors="coerce")
ah = ah[(ah.HandiHome > 1) & (ah.HandiAway > 1)].dropna(subset=["HandiSize"])

line = restore_quarter_lines(ah["HandiSize"].to_numpy())
oh = ah["HandiHome"].to_numpy()
oa = ah["HandiAway"].to_numpy()
margin = (ah.FTHome - ah.FTAway).to_numpy()
dist = margin_dist(ah["lam"].to_numpy(), ah["mu"].to_numpy())

print("=" * 72)
print(f"亚洲让球盘    {len(ah):,} 场（2013-2025 走查样本外）")
print("=" * 72)
vig_ah = 1 / oh + 1 / oa - 1
print(f"抽水:  让球盘 {vig_ah.mean()*100:.2f}%   1X2 {ah.vig.mean()*100:.2f}%"
      f"   → 让球盘便宜 {(ah.vig.mean()-vig_ah.mean())*100:.2f} 个百分点")

real_h = settle_any(margin, line, oh)
real_a = settle_any(-margin, -line, oa)

# ── 结算正确性自检 ────────────────────────────────────────
# 同时押两边是押了 **2 个单位**本金，所以合计收益的理论值是 -2v/(1+v)，
# 不是 -v/(1+v)。走盘会退还本金，那部分不亏抽水，所以还要乘上非走盘比例。
# （第一版自检漏了这个 2 倍因子，误报成结算逻辑有 bug——留此注释以免重蹈。）
push_rate = ((real_h == 0) & (real_a == 0)).mean()
v = vig_ah.mean()
expected_both = -2 * v / (1 + v) * (1 - push_rate)
actual_both = (real_h + real_a).mean()
print(f"\n结算自检: 主队盘 {real_h.mean():+.4f}  客队盘 {real_a.mean():+.4f}")
print(f"  两边合计 {actual_both:+.4f}   理论 {expected_both:+.4f}"
      f"（走盘率 {push_rate:.1%}）")
assert abs(actual_both - expected_both) < 0.006, (
    f"结算与理论不符（{actual_both:.4f} vs {expected_both:.4f}），"
    f"盘口约定可能理解错了，拒绝相信后续结果")
print("  ✅ 自检通过——结算逻辑与盘口约定一致")

ev_h = expected_return(dist, line, oh)
ev_a = expected_return(dist, -line, oa)

print("\n" + "-" * 72)
print("下注模拟（每场只下模型 EV 更高的一边，且需超过门槛）")
print(f"{'门槛':>6s} {'注数':>8s} {'ROI':>9s} {'±SE':>7s} {'95% CI':>21s}")
for thr in [0.0, 0.02, 0.05, 0.10]:
    pick_h = (ev_h > thr) & (ev_h >= ev_a)
    pick_a = (ev_a > thr) & (ev_a > ev_h)
    r = np.concatenate([real_h[pick_h], real_a[pick_a]])
    if len(r) < 100:
        continue
    roi, se = r.mean(), r.std(ddof=1) / np.sqrt(len(r))
    print(f"{thr:>6.0%} {len(r):>8,} {roi:>+8.2%} {se:>6.2%}  "
          f"[{roi-1.96*se:>+6.2%}, {roi+1.96*se:>+6.2%}]")

# ── 增量信息检验：只用半球盘，此时结果是干净的二元事件（不可能走盘）──
print("\n" + "-" * 72)
print("增量信息检验（只用半球盘，无走盘，结果是干净二元事件）")
half = np.isclose(np.abs(line) % 1.0, 0.5)
hh = ah[half].copy()
lh, ohh, oah = line[half], oh[half], oa[half]
cover = ((margin[half] + lh) > 0).astype(int)
q_home = (1 / ohh) / (1 / ohh + 1 / oah)          # 市场去抽水后主队盘概率
p_home = np.zeros(half.sum())
margins = np.arange(-K, K + 1)
dh = dist[half]
for i, m in enumerate(margins):
    p_home += dh[:, i] * ((m + lh) > 0)


def logit(p):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


hh["year"] = hh["MatchDate"].dt.year
Xb = np.column_stack([logit(q_home), logit(p_home)])
Xm = logit(q_home).reshape(-1, 1)
ob = np.full(len(hh), np.nan)
om = np.full(len(hh), np.nan)
for yr in sorted(hh["year"].unique())[1:]:
    tr = (hh["year"] < yr).to_numpy()
    te = (hh["year"] == yr).to_numpy()
    if tr.sum() < 2000:
        continue
    ob[te] = LogisticRegression(max_iter=2000, C=1e6).fit(Xb[tr], cover[tr]).predict_proba(Xb[te])[:, 1]
    om[te] = LogisticRegression(max_iter=2000, C=1e6).fit(Xm[tr], cover[tr]).predict_proba(Xm[te])[:, 1]

mm = ~np.isnan(ob)
print(f"半球盘 {half.sum():,} 场，其中参与评分 {mm.sum():,} 场")
print(f"  模型 Brier {np.mean((p_home[mm]-cover[mm])**2):.5f}   "
      f"市场 Brier {np.mean((q_home[mm]-cover[mm])**2):.5f}")
db = (ob[mm] - cover[mm]) ** 2 - (om[mm] - cover[mm]) ** 2
se2 = db.std(ddof=1) / np.sqrt(len(db))
print(f"  仅市场    Brier {np.mean((om[mm]-cover[mm])**2):.5f}")
print(f"  市场+模型 Brier {np.mean((ob[mm]-cover[mm])**2):.5f}")
print(f"  配对差 {db.mean():+.6f} ±{se2:.6f}   t = {db.mean()/se2:+.1f}")
print("  → 模型含市场之外的信息" if db.mean() / se2 < -3 else "  → 模型对市场无可检出增量")
print("=" * 72)
