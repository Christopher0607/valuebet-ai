"""
换市场：大小球 2.5（Over/Under）和亚洲让球。

为什么这是唯一还没试过、且值得试的方向
--------------------------------------
Dixon-Coles 产出的是完整的比分联合分布，1X2 只是从这个分布里取的一个边际。
项目至今所有验证都只用 1X2——而 1X2 恰恰是全世界定价最充分的足球市场：
成交量最大、参与者最多、模型最成熟。

大小球是同一个分布的另一个边际，理论上模型有没有优势要分开测。实践中
进球总数比胜负结果更依赖「攻防强度」这类可从历史比分估计的量，更少依赖
「谁红牌了、教练怎么摆」这类市场信息优势集中的东西。这是先验上最可能
出现缺口的地方。

亚洲让球同理，而且抽水通常比 1X2 低得多。

沿用同一批走查预测（lam/mu 已经存好），不需要重新拟合，成本几乎为零。
"""
import numpy as np
import pandas as pd
from scipy.special import gammaln
from sklearn.linear_model import LogisticRegression
import lib_walkforward as L

pred = pd.read_parquet("walkforward_test.parquet")
lam = pred["lam"].to_numpy()
mu = pred["mu"].to_numpy()
K = 15


def total_goals_dist(lam, mu, rho=L.DEFAULT_RHO, kmax=K):
    """从 lam/mu 算进球总数的分布（含 Dixon-Coles 低比分修正）。

    tau 修正只动 (0,0)(0,1)(1,0)(1,1) 四格，但这四格全都落在「总进球 ≤ 2」
    的区间里，正好是大小球 2.5 盘口的小球侧——所以修正对这个市场不是可忽略的
    细节，不能图省事用纯独立泊松。
    """
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
    tot = np.add.outer(np.arange(kmax + 1), np.arange(kmax + 1))
    dist = np.zeros((len(lam), 2 * kmax + 1))
    for t in range(2 * kmax + 1):
        dist[:, t] = (joint * (tot == t)).sum(axis=(1, 2))
    return dist


dist = total_goals_dist(lam, mu)
p_over = dist[:, 3:].sum(axis=1)          # 总进球 >= 3，即大于 2.5
pred["p_over25"] = p_over

ou = pred.dropna(subset=["Over25", "Under25"]).copy()
ou = ou[(ou.Over25 > 1) & (ou.Under25 > 1)]
actual_over = ((ou.FTHome + ou.FTAway) > 2.5).to_numpy().astype(int)
po = ou["p_over25"].to_numpy()

io, iu = 1 / ou["Over25"].to_numpy(), 1 / ou["Under25"].to_numpy()
s = io + iu
qo = io / s
vig_ou = s - 1

print("=" * 62)
print(f"大小球 2.5 市场    {len(ou):,} 场")
print("=" * 62)
print(f"庄家抽水: 1X2 {ou.vig.mean()*100:.2f}%   大小球 {vig_ou.mean()*100:.2f}%")
print(f"实际大球比例 {actual_over.mean():.1%}   模型均值 {po.mean():.1%}   市场均值 {qo.mean():.1%}")

# 二元事件用 Brier 分数（等价于两分类的 RPS）
brier_m = np.mean((po - actual_over) ** 2)
brier_k = np.mean((qo - actual_over) ** 2)
d = (po - actual_over) ** 2 - (qo - actual_over) ** 2
se = d.std(ddof=1) / np.sqrt(len(d))
print(f"\n{'模型 Brier':16s} {brier_m:.5f}")
print(f"{'市场 Brier':16s} {brier_k:.5f}")
print(f"配对差 {d.mean():+.5f} ±{se:.5f}   t = {d.mean()/se:+.1f}")
print("✅ 模型更准" if d.mean() < 0 else "❌ 模型不如市场")

# 增量信息检验：模型对大小球价格还有没有增量
def logit(p):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


ou["year"] = ou["MatchDate"].dt.year
Xb = np.column_stack([logit(qo), logit(po)])
Xm = logit(qo).reshape(-1, 1)
oos_b = np.full(len(ou), np.nan)
oos_m = np.full(len(ou), np.nan)
yrs = sorted(ou["year"].unique())
for yr in yrs[1:]:
    tr = (ou["year"] < yr).to_numpy()
    te = (ou["year"] == yr).to_numpy()
    if tr.sum() < 2000:
        continue
    oos_b[te] = LogisticRegression(max_iter=2000, C=1e6).fit(Xb[tr], actual_over[tr]).predict_proba(Xb[te])[:, 1]
    oos_m[te] = LogisticRegression(max_iter=2000, C=1e6).fit(Xm[tr], actual_over[tr]).predict_proba(Xm[te])[:, 1]

mm = ~np.isnan(oos_b)
db = (oos_b[mm] - actual_over[mm]) ** 2 - (oos_m[mm] - actual_over[mm]) ** 2
se2 = db.std(ddof=1) / np.sqrt(len(db))
print(f"\n增量信息检验（样本外，{mm.sum():,} 场）:")
print(f"  仅市场      Brier {np.mean((oos_m[mm]-actual_over[mm])**2):.5f}")
print(f"  市场+模型   Brier {np.mean((oos_b[mm]-actual_over[mm])**2):.5f}")
print(f"  配对差 {db.mean():+.6f} ±{se2:.6f}   t = {db.mean()/se2:+.1f}")
print("  → 模型含市场之外的信息" if db.mean() / se2 < -3 else "  → 模型对市场无可检出增量")

# 按抽水后的最高赔率做平注模拟，直接看钱
print(f"\n{'-'*62}\n平注模拟（EV>0 就下，用全市场最高赔率）")
mo = pd.to_numeric(ou["MaxOver25"], errors="coerce").fillna(ou["Over25"]).to_numpy()
mu_ = pd.to_numeric(ou["MaxUnder25"], errors="coerce").fillna(ou["Under25"]).to_numpy()
for thr in [0.0, 0.03, 0.05]:
    ev_o = po * mo - 1
    ev_u = (1 - po) * mu_ - 1
    bets, rets = [], []
    for i in range(len(ou)):
        if ev_o[i] > thr and ev_o[i] >= ev_u[i]:
            rets.append(mo[i] - 1 if actual_over[i] == 1 else -1); bets.append(1)
        elif ev_u[i] > thr:
            rets.append(mu_[i] - 1 if actual_over[i] == 0 else -1); bets.append(1)
    if not rets:
        continue
    r = np.array(rets)
    roi = r.mean()
    se_roi = r.std(ddof=1) / np.sqrt(len(r))
    print(f"  EV>{thr:.0%}: {len(r):>6,} 注   ROI {roi:+.2%} ±{se_roi:.2%}   "
          f"95%CI [{roi-1.96*se_roi:+.2%}, {roi+1.96*se_roi:+.2%}]")
print("=" * 62)
