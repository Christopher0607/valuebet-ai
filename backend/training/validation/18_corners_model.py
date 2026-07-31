"""
角球模型：冷门市场路线的可行性预研。

必须先说清楚这个脚本能证明什么、不能证明什么
--------------------------------------------
**不能**证明角球市场有优势。要证明优势必须有历史角球赔率，而经过实际排查，
免费源里不存在：football-data.co.uk（以及所有基于它的数据集，包括本项目用的
Matches.csv）有角球和牌的**比赛统计**，但赔率字段只有 1X2、大小球 2.5、
亚洲让球三种。本会话的网络策略也无法访问 the-odds-api / oddsportal 等商业源。

**能**证明的是：角球这个标的本身有多少可预测性。如果模型对角球的区分力
明显强于它对进球的区分力，那么「冷门市场值得投入」这个假设至少有原材料支撑，
值得为它去买赔率数据；如果区分力同样贫瘠，那这条路可以提前关掉，省下买数据的钱。

这是在没有赔率的情况下能问出的最有信息量的问题。

方法
----
完全复用 lib_walkforward 那套已经验证过的走查骨架，只把「进球」换成「角球」：
  lam = exp(角球攻击力[主] - 角球防守力[客] + 主场优势)
即球队「制造角球」和「被制造角球」的能力。滚动起点、每月重拟合、超参沿用。

一个已知的模型缺陷（如实记录）：总角球的方差/均值 = 1.21，存在过离散，
泊松假设偏紧，会低估极端值的概率。负二项分布更合适。这会让本脚本的区分力
估计偏保守——也就是说，真实可预测性只会比这里测出的更好，不会更差。
"""
import numpy as np
import pandas as pd
from scipy.special import gammaln
from sklearn.isotonic import IsotonicRegression
import lib_walkforward as L

df = L.load_matches()
for c in ["HomeCorners", "AwayCorners"]:
    df[c] = pd.to_numeric(df[c], errors="coerce")
cd = df.dropna(subset=["HomeCorners", "AwayCorners"]).copy()

# 把角球塞进 FTHome/FTAway，直接复用走查骨架——不复制一份代码出来改，
# 免得两份实现悄悄分叉（这个项目历史上吃过重复实现的亏）。
cd["goals_home"], cd["goals_away"] = cd["FTHome"], cd["FTAway"]
cd["FTHome"] = cd["HomeCorners"].astype(int)
cd["FTAway"] = cd["AwayCorners"].astype(int)

print(f"角球数据 {len(cd):,} 场，{cd.MatchDate.min().date()} ~ {cd.MatchDate.max().date()}")
print("跑角球走查（骨架与进球模型完全一致）...\n")
cp = L.run_walkforward(cd, L.DEV_END, L.TEST_END, verbose=False)
print(f"样本外角球预测 {len(cp):,} 场\n")

lam = cp["lam"].to_numpy()
mu = cp["mu"].to_numpy()
total = (cp["FTHome"] + cp["FTAway"]).to_numpy()
K = 30


def total_dist(lam, mu, kmax=K):
    """总角球分布。角球没有 Dixon-Coles 那种低比分相关性的理由，用独立泊松。"""
    k = np.arange(kmax + 1)
    ph = np.exp(-lam[:, None] + k * np.log(lam[:, None]) - gammaln(k + 1))
    pa = np.exp(-mu[:, None] + k * np.log(mu[:, None]) - gammaln(k + 1))
    out = np.zeros((len(lam), 2 * kmax + 1))
    for t in range(2 * kmax + 1):
        lo = max(0, t - kmax)
        hi = min(kmax, t)
        idx = np.arange(lo, hi + 1)
        out[:, t] = (ph[:, idx] * pa[:, t - idx]).sum(axis=1)
    return out


dist = total_dist(lam, mu)
cum = np.cumsum(dist, axis=1)


def corp_binary(p, o):
    """二元事件的 CORP 分解，返回 (Brier, MCB, DSC, UNC)。"""
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=1)
    ph = iso.fit_transform(p, o)
    s_raw, s_cal, s_ref = np.mean((p - o) ** 2), np.mean((ph - o) ** 2), np.mean((o.mean() - o) ** 2)
    return s_raw, s_raw - s_cal, s_ref - s_cal, s_ref


print("=" * 74)
print("角球大小盘的可预测性（区分力 DSC 越高 = 模型越有真信息）")
print("=" * 74)
print(f"{'盘口':>8s} {'过盘率':>8s} {'模型Brier':>10s} {'常数基准':>10s} "
      f"{'MCB':>8s} {'DSC':>9s} {'相对提升':>9s}")
rows = []
for line in [8.5, 9.5, 10.5, 11.5, 12.5]:
    p_over = 1 - cum[:, int(np.floor(line))]
    o = (total > line).astype(int)
    b, mcb, dsc, unc = corp_binary(p_over, o)
    rows.append({"line": line, "brier": b, "mcb": mcb, "dsc": dsc, "unc": unc})
    print(f"{line:>8.1f} {o.mean():>7.1%} {b:>10.5f} {unc:>10.5f} "
          f"{mcb:>8.5f} {dsc:>9.5f} {dsc/unc:>8.1%}")

# ── 关键对照：同一把尺子下，角球的区分力 vs 进球的区分力 ────────
print("\n" + "=" * 74)
print("关键对照：角球 vs 进球，谁更可预测？（同一走查骨架、同一 CORP 口径）")
print("=" * 74)
gp = pd.read_parquet("walkforward_test.parquet")
g_tot = (gp["FTHome"] + gp["FTAway"]).to_numpy()
g_dist = total_dist(gp["lam"].to_numpy(), gp["mu"].to_numpy(), kmax=15)
g_cum = np.cumsum(g_dist, axis=1)
_, g_mcb, g_dsc, g_unc = corp_binary(1 - g_cum[:, 2], (g_tot > 2.5).astype(int))

best = max(rows, key=lambda r: r["dsc"] / r["unc"])
print(f"进球 大小2.5   DSC {g_dsc:.5f}   UNC {g_unc:.5f}   相对提升 {g_dsc/g_unc:.1%}")
print(f"角球 大小{best['line']:.1f}  DSC {best['dsc']:.5f}   UNC {best['unc']:.5f}   "
      f"相对提升 {best['dsc']/best['unc']:.1%}")
ratio = (best["dsc"] / best["unc"]) / (g_dsc / g_unc)
print(f"\n角球的相对区分力是进球的 {ratio:.2f} 倍")
print()
if ratio > 1.3:
    print("→ 角球明显比进球可预测。冷门市场路线有原材料，值得为它去买赔率数据验证优势。")
elif ratio > 0.8:
    print("→ 角球和进球的可预测性相当。考虑到进球市场已被证明零增量，")
    print("  且角球市场抽水通常高得多（7-10% vs 1X2 最优价的 0.9%），")
    print("  同样的区分力在更贵的市场里更难翻正。这条路不乐观。")
else:
    print("→ 角球比进球更难预测。冷门市场路线可以提前关掉。")

print("\n" + "=" * 74)
print("这个脚本没有回答的问题（需要历史角球赔率才能回答）:")
print("  1. 角球市场的定价有多软？模型对角球价格有没有增量信息？")
print("  2. 抽水扣掉之后还剩多少？角球盘抽水通常远高于 1X2。")
print("  3. 限额多低？冷门市场单注上限往往只有几百块，规模上不去。")
print("免费源（football-data.co.uk 系）只有角球统计、没有角球赔率，已实际排查确认。")
print("=" * 74)
