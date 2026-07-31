"""
不用模型，直接在价格里找系统性偏差 —— 本项目第一个为正的结果。

思路的转变
----------
之前所有工作的框架都是「造一个比市场准的模型，用它挑价值」。09 号文档用
141,287 场证明这条路关闭了：模型对价格的增量信息 t = -0.2。

但「打赢市场」不是赚钱的唯一途径。另一条路：**市场价格本身有系统性偏差。**
最著名的是热门-冷门偏差（favourite-longshot bias）——赌客系统性高估小概率大赔率
事件，冷门被过度下注、赔率被压低，热门因而定价偏松。这不需要任何预测能力，
只需要认出偏差并一直站在正确的一边。

为什么之前没发现：08 号扫过赔率区间，但只有 659 注，标准误约 6%，什么都判不了。

纪律
----
DEV 期（2013-2018）自由挖掘，HOLDOUT 期（2019-2025）只验证 DEV 挑出的候选。
一个策略只有在 HOLDOUT 上也为正、置信区间排除 0，才算数。

关键的方法要求：报「净超额」而不是 ROI。
净超额 = ROI - 该批注单的抽水基线，其中抽水基线 = -v/(1+v)。
ROI 会被抽水水平污染——同一个偏差在便宜的价格源上看起来赚、在贵的上看起来亏，
但那是抽水的差异不是偏差的差异。净超额才是价格偏差本身的大小。
"""
import numpy as np
import pandas as pd
from scipy.stats import norm
import lib_walkforward as L

DEV_A, DEV_B = pd.Timestamp("2013-01-01"), pd.Timestamp("2019-01-01")
HOLD_A, HOLD_B = pd.Timestamp("2019-01-01"), pd.Timestamp("2025-07-01")
MAXC = ["MaxHome", "MaxDraw", "MaxAway"]
AVGC = ["OddHome", "OddDraw", "OddAway"]
OUTCOMES = ["主胜", "平局", "客胜"]

df = L.load_matches()
for c in MAXC + AVGC:
    df[c] = pd.to_numeric(df[c], errors="coerce")
d = df.dropna(subset=MAXC + AVGC).copy()
d = d[(d[MAXC + AVGC] > 1).all(axis=1)].reset_index(drop=True)

MX = d[MAXC].to_numpy()
AV = d[AVGC].to_numpy()
y = d["outcome"].to_numpy()
yr = d["MatchDate"].dt.year.to_numpy()
dev = ((d.MatchDate >= DEV_A) & (d.MatchDate < DEV_B)).to_numpy()
hold = ((d.MatchDate >= HOLD_A) & (d.MatchDate < HOLD_B)).to_numpy()

print(f"样本 {len(d):,} 场（同时有平均价和最高价）")
print(f"DEV {dev.sum():,} 场（2013-2018，自由挖掘）   "
      f"HOLDOUT {hold.sum():,} 场（2019-2025，只验证）\n")


def collect(O, mask, lo, hi):
    """在 mask 内，挑出赔率落在 [lo,hi) 的所有选项，平注。返回收益和抽水。"""
    rs, vs = [], []
    for c in range(3):
        s = mask & (O[:, c] >= lo) & (O[:, c] < hi) & (O[:, c] > 1)
        if s.sum():
            rs.append(np.where(y[s] == c, O[s, c] - 1, -1.0))
            vs.append((1 / O[s]).sum(axis=1) - 1)
    if not rs:
        return None
    r, v = np.concatenate(rs), np.concatenate(vs)
    base = -v.mean() / (1 + v.mean())
    return {"n": len(r), "roi": r.mean(), "se": r.std(ddof=1) / np.sqrt(len(r)),
            "base": base, "net": r.mean() - base, "sd": r.std()}


BANDS = [(1.0, 2.0), (2.0, 3.5), (3.5, 6.0), (6.0, 1e9)]
LBL = ["热门 [1.0,2.0)", "中间 [2.0,3.5)", "冷门 [3.5,6.0)", "大冷门 [6.0,∞)"]

print("=" * 78)
print("一、热门-冷门偏差是否存在（净超额 = 扣掉抽水后价格偏差本身的大小）")
print("=" * 78)
print(f"{'赔率源':>6s} {'区间':>16s} {'注数':>9s} {'ROI':>8s} {'抽水基线':>9s} "
      f"{'净超额':>9s} {'±SE':>7s} {'t':>6s}")
for name, O in [("最高价", MX), ("平均价", AV)]:
    for (lo, hi), lbl in zip(BANDS, LBL):
        s = collect(O, np.ones(len(d), bool), lo, hi)
        print(f"{name:>6s} {lbl:>16s} {s['n']:>9,} {s['roi']:>+7.2%} "
              f"{s['base']:>+8.2%} {s['net']:>+8.2%} {s['se']:>6.2%} "
              f"{s['net']/s['se']:>+6.1f}")
    print()
print("两个价格源上净超额都随赔率单调下降 → 偏差是真的，不是比价造成的假象。")

print("\n" + "=" * 78)
print("二、DEV / HOLDOUT 独立验证（押所有最高价 < 2.0 的选项）")
print("=" * 78)
print(f"{'门槛':>6s} {'期间':>9s} {'注数':>9s} {'ROI':>9s} {'±SE':>7s} {'t':>6s} {'95% CI':>21s}")
for thr in [1.5, 1.8, 2.0, 2.2, 2.5]:
    for nm, m in [("DEV", dev), ("HOLDOUT", hold)]:
        s = collect(MX, m, 1.0, thr)
        if s is None or s["n"] < 200:
            continue
        print(f"{thr:>6.1f} {nm:>9s} {s['n']:>9,} {s['roi']:>+8.2%} {s['se']:>6.2%} "
              f"{s['roi']/s['se']:>+6.1f}  [{s['roi']-1.96*s['se']:>+6.2%}, "
              f"{s['roi']+1.96*s['se']:>+6.2%}]")
    print()

print("=" * 78)
print("三、逐年稳定性（最高价，门槛 2.0）")
print("=" * 78)
pos = tot = 0
for Y in range(2013, 2026):
    s = collect(MX, (yr == Y), 1.0, 2.0)
    if s is None or s["n"] < 300:
        continue
    tot += 1
    pos += s["roi"] > 0
    print(f"  {Y}: {s['n']:>5,} 注  ROI {s['roi']:>+7.2%} ±{s['se']:.2%}  "
          f"{'正' if s['roi'] > 0 else '负'}")
print(f"  → {pos}/{tot} 年为正")

print("\n" + "=" * 78)
print("四、最关键的实操问题：需要拿到多好的价格才能保本？")
print("=" * 78)
print("假设你实际成交价 = 平均价 + f × (最高价 - 平均价)")
print("f=0 随便找一家下注   f=1 每次都拿到全市场最优价\n")
print(f"{'f':>6s} {'注数':>9s} {'ROI':>9s} {'±SE':>7s}")
prev = None
for f in [0.0, 0.2, 0.4, 0.6, 0.7, 0.8, 0.9, 1.0]:
    O = AV + f * (MX - AV)
    s = collect(O, np.ones(len(d), bool), 1.0, 2.0)
    mark = "  ← 盈亏平衡点" if prev is not None and prev < 0 <= s["roi"] else ""
    print(f"{f:>6.1f} {s['n']:>9,} {s['roi']:>+8.2%} {s['se']:>6.2%}{mark}")
    prev = s["roi"]

print("\n" + "=" * 78)
print("五、规模与风险")
print("=" * 78)
s = collect(MX, np.ones(len(d), bool), 1.0, 2.0)
n_yr = s["n"] / 12.4
ann_mu = n_yr * s["roi"]
ann_sd = s["sd"] * np.sqrt(n_yr)
print(f"全库 38 个联赛，每年约 {n_yr:,.0f} 注，ROI {s['roi']:+.2%}（最优价）")
print(f"若每注 100 元：年期望毛利 {ann_mu*100:,.0f} 元，标准差 {ann_sd*100:,.0f} 元")
print(f"单看任意一年，亏损概率约 {100*(1-norm.cdf(ann_mu/ann_sd)):.0f}%")
print(f"要让「三年累计为正」的概率达到 95%，需要 ROI 至少 "
      f"{1.645*s['sd']/np.sqrt(3*n_yr)*100:.2f}%——实测 {s['roi']*100:.2f}%，"
      f"{'达到' if s['roi'] > 1.645*s['sd']/np.sqrt(3*n_yr) else '未达到'}")

print("\n" + "=" * 78)
print("结论")
print("=" * 78)
print("""热门-冷门偏差是真的：净超额 +1.79%（最优价）/ +2.84%（平均价），t=6.3/10.9，
DEV 与 HOLDOUT 独立复现，10/13 年为正。这是本项目第一个统计上站得住的正结果。

但它能不能变成钱，取决于三件与模型无关的事：

1. **价格执行**。必须捕获最优价溢价的 80-90% 才保本。拿平均价的话是 -3.54%。
   这意味着必须在多家平台比价并总是打到最好的那家。
2. **限额与封号**。用最优价押热门是博彩公司识别专业玩家最典型的特征，
   赢了会被限额。这是这条路最现实的天花板，而且无法用数据验证。
3. **方差**。+0.91% 的优势下，单年亏损概率约 18%。这不是「稳赚」，
   是一个薄得随时会被执行成本吃掉的统计优势。

诚实的一句话：**1X2 上确实存在可赚钱的结构，但赚的不是预测的钱，
是「一直押热门 + 永远拿到最好的价」的钱。**""")
