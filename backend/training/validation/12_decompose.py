"""
把 RPS 缺口拆开：模型输在「概率没校准」还是「信息量不够」？

这是整件事的分水岭，08_MARKET_VALIDATION.md 里没做过这一步。它列的所有
后续方向（伤停、赛程、转会、新 xG）全是「加新信息」，但如果缺口主要来自
校准，那根本不需要新信息——一层后处理就能拿回来，成本几乎为零。

三个测量
--------
A. CORP / Murphy 分解。任何概率预测的评分都可以拆成
       Score = MCB - DSC + UNC
   MCB (miscalibration) 越大 = 概率越不诚实（说 70% 的时候实际不是 70%）
   DSC (discrimination)  越大 = 区分能力越强（真正含有信息）
   UNC (uncertainty)     基准难度，两边相同
   用等渗回归（PAV）做重校准基准，即 Dimitriadis-Gneiting-Jordan (2021) 的
   CORP 方法，避免分箱宽度带来的任意性。
   RPS 等于两个累积二元事件（主胜 / 主胜或平）Brier 分数的平均，所以逐个分解再平均。

   注意 MCB 是**在样本内**用等渗回归算出来的，它是校准最多能拿回多少的
   上界，不是实际能拿回多少。真实可得量看 B。

B. 样本外校准实验。按时间滚动：用 Y 年之前的预测拟合校准层，套到 Y 年上。
   这才是能落地的数字。

C. 增量信息检验（最关键的一个）。多项 logistic 回归：
       结果 ~ 市场对数几率 + 模型对数几率
   如果模型系数 β 显著大于 0，说明模型含有价格里没有的信息——那么优势
   在原则上存在。如果 β≈0，模型对市场毫无增量，任何下注策略都不可能盈利，
   项目应该停在这里。这是「还有没有戏」的唯一决定性检验。

   注意：C 只是测量，不改 EV 公式。CLAUDE.md 的硬约束（EV 必须用独立模型
   概率 × 市场原始赔率）在这个脚本里没有被触碰。
"""
import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
import lib_walkforward as L

pred = pd.read_parquet("walkforward_test.parquet")
print(f"TEST 期样本外预测 {len(pred):,} 场\n")

P = pred[["p_home", "p_draw", "p_away"]].to_numpy()
Q = pred[["q_home", "q_draw", "q_away"]].to_numpy()
y = pred["outcome"].to_numpy()


# ── A. CORP 分解 ──────────────────────────────────────────
def corp_rps(probs, outcome):
    """RPS 的 CORP 分解。返回 (score, MCB, DSC, UNC)。

    RPS = mean over 2 个累积事件的 Brier。逐事件分解后取平均，
    因为分解的三项都是期望的线性泛函，平均后仍然满足 Score = MCB - DSC + UNC。
    """
    cum_p = np.cumsum(probs, axis=1)[:, :2]
    obs = np.zeros_like(probs)
    obs[np.arange(len(outcome)), outcome] = 1.0
    cum_o = np.cumsum(obs, axis=1)[:, :2]

    mcb = dsc = unc = sc = 0.0
    for j in range(2):
        x, o = cum_p[:, j], cum_o[:, j]
        iso = IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=1)
        x_hat = iso.fit_transform(x, o)
        s_raw = np.mean((x - o) ** 2)
        s_cal = np.mean((x_hat - o) ** 2)
        s_ref = np.mean((o.mean() - o) ** 2)
        mcb += s_raw - s_cal
        dsc += s_ref - s_cal
        unc += s_ref
        sc += s_raw
    return sc / 2, mcb / 2, dsc / 2, unc / 2


print("=" * 66)
print("A. CORP 分解    Score = MCB - DSC + UNC")
print("=" * 66)
rows = []
for name, pr in [("模型", P), ("市场", Q)]:
    s, mcb, dsc, unc = corp_rps(pr, y)
    rows.append({"": name, "RPS": s, "MCB(失准)": mcb, "DSC(区分力)": dsc, "UNC": unc})
d = pd.DataFrame(rows)
print(d.to_string(index=False, float_format=lambda x: f"{x:.5f}"))

gap = d.loc[0, "RPS"] - d.loc[1, "RPS"]
gap_mcb = d.loc[0, "MCB(失准)"] - d.loc[1, "MCB(失准)"]
gap_dsc = d.loc[1, "DSC(区分力)"] - d.loc[0, "DSC(区分力)"]
print(f"\nRPS 缺口 {gap:+.5f} 拆成：")
print(f"  失准差异 (MCB)   {gap_mcb:+.5f}   ← 校准层理论上最多能拿回这么多")
print(f"  区分力差异 (DSC) {gap_dsc:+.5f}   ← 只能靠新信息，校准拿不回来")
print(f"  校准可解释比例   {gap_mcb / gap:.0%}")


# ── B. 样本外校准 ─────────────────────────────────────────
print("\n" + "=" * 66)
print("B. 样本外校准（按年滚动，用过去年份拟合、套到当年）")
print("=" * 66)

pred["year"] = pred["MatchDate"].dt.year
logP = np.log(np.clip(P, 1e-9, 1))
years = sorted(pred["year"].unique())

cal_probs = np.full_like(P, np.nan)
for yr in years[1:]:
    tr = (pred["year"] < yr).to_numpy()
    te = (pred["year"] == yr).to_numpy()
    if tr.sum() < 2000:
        continue
    # 多项 logistic，特征 = 三个类别的对数概率。这一族包含温度缩放
    # （所有系数相等）和向量缩放，让数据自己决定要哪一种。
    lr = LogisticRegression(max_iter=2000, C=1e6)
    lr.fit(logP[tr], y[tr])
    cal_probs[te] = lr.predict_proba(logP[te])

m = ~np.isnan(cal_probs[:, 0])
rps_raw = L.rps(P[m], y[m]).mean()
rps_cal = L.rps(cal_probs[m], y[m]).mean()
rps_mkt = L.rps(Q[m], y[m]).mean()
print(f"评分场次 {m.sum():,}（首年用作校准训练，不参与评分）")
print(f"  模型原始      RPS {rps_raw:.5f}")
print(f"  模型+校准     RPS {rps_cal:.5f}   ({rps_cal - rps_raw:+.5f})")
print(f"  市场          RPS {rps_mkt:.5f}")
print(f"  校准后仍落后市场 {rps_cal - rps_mkt:+.5f}")

d_cal = L.rps(cal_probs[m], y[m]) - L.rps(Q[m], y[m])
se = d_cal.std(ddof=1) / np.sqrt(len(d_cal))
print(f"  配对 SE {se:.5f}   t = {d_cal.mean() / se:+.1f}")


# ── C. 增量信息检验 ───────────────────────────────────────
print("\n" + "=" * 66)
print("C. 增量信息检验：模型对市场价格还有没有增量？")
print("=" * 66)

logQ = np.log(np.clip(Q, 1e-9, 1))
# 只用市场
lr_m = LogisticRegression(max_iter=3000, C=1e6).fit(logQ, y)
# 市场 + 模型
X_both = np.hstack([logQ, logP])
lr_b = LogisticRegression(max_iter=3000, C=1e6).fit(X_both, y)

ll_m = -np.mean(np.log(np.clip(lr_m.predict_proba(logQ)[np.arange(len(y)), y], 1e-12, 1)))
ll_b = -np.mean(np.log(np.clip(lr_b.predict_proba(X_both)[np.arange(len(y)), y], 1e-12, 1)))
print(f"仅市场特征      平均负对数似然 {ll_m:.5f}")
print(f"市场+模型特征   平均负对数似然 {ll_b:.5f}   ({ll_b - ll_m:+.5f})")

n = len(y)
lr_stat = 2 * n * (ll_m - ll_b)
from scipy.stats import chi2
dfree = 6  # 模型三个对数概率 × (3-1) 个 logit 方程
p_value = chi2.sf(lr_stat, dfree)
print(f"\n似然比检验 χ² = {lr_stat:.1f}  (df={dfree})   p = {p_value:.3g}")

# 样本外版本：同样按年滚动，避免上面是样本内拟合造成的乐观偏差
oos = np.full_like(P, np.nan)
oos_m = np.full_like(P, np.nan)
for yr in years[1:]:
    tr = (pred["year"] < yr).to_numpy()
    te = (pred["year"] == yr).to_numpy()
    if tr.sum() < 2000:
        continue
    oos[te] = LogisticRegression(max_iter=3000, C=1e6) \
        .fit(X_both[tr], y[tr]).predict_proba(X_both[te])
    oos_m[te] = LogisticRegression(max_iter=3000, C=1e6) \
        .fit(logQ[tr], y[tr]).predict_proba(logQ[te])

mm = ~np.isnan(oos[:, 0])
r_both = L.rps(oos[mm], y[mm]).mean()
r_mkt_only = L.rps(oos_m[mm], y[mm]).mean()
r_raw_mkt = L.rps(Q[mm], y[mm]).mean()
print(f"\n样本外（按年滚动，{mm.sum():,} 场）:")
print(f"  市场原始              RPS {r_raw_mkt:.5f}")
print(f"  市场去抽水+重校准     RPS {r_mkt_only:.5f}")
print(f"  市场+模型             RPS {r_both:.5f}   ({r_both - r_mkt_only:+.5f})")

dd = L.rps(oos[mm], y[mm]) - L.rps(oos_m[mm], y[mm])
se2 = dd.std(ddof=1) / np.sqrt(len(dd))
print(f"  配对差 {dd.mean():+.5f} ±{se2:.5f}   t = {dd.mean() / se2:+.1f}")
if dd.mean() < 0 and dd.mean() / se2 < -3:
    print("\n  → 模型确实含有市场价格之外的信息（虽然模型单独用不如市场）")
else:
    print("\n  → 模型对市场没有可检出的增量信息")
