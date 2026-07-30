"""
下注模拟重做：全量走查预测 + 置信区间。

为什么要重做 02_simulate_betting.py
----------------------------------
原来的模拟是 659 注、ROI -13.9%，被 08_MARKET_VALIDATION.md 当作「最直观」的
证据，并据此拆出「亏损 = 抽水 4.7% + 模型挑错边 10.6%」。

但 659 注不足以支撑那个拆解。按该模拟自身的命中率 22.6% 和推算出的平均赔率
3.81，单注收益标准差约 1.59，ROI 的标准误约 6.2%——-13.9% 距离「纯抽水」
基线只有约 1.5 个标准误。也就是说「模型系统性挑错边」在统计上没有被证实，
它可能只是运气差。

本脚本用 141,287 场走查预测重做，注数是原来的两个数量级以上，
每个数字都带标准误和 95% 置信区间。EV 公式严格按 CLAUDE.md：
    EV = 独立模型概率 × 市场原始赔率 - 1
不使用任何去抽水后的市场概率。
"""
import numpy as np
import pandas as pd

pred = pd.read_parquet("walkforward_test.parquet")
P = pred[["p_home", "p_draw", "p_away"]].to_numpy()
y = pred["outcome"].to_numpy()
avg = pred[["OddHome", "OddDraw", "OddAway"]].to_numpy(float)
mx = pred[["MaxHome", "MaxDraw", "MaxAway"]].to_numpy(float)
has_max = ~np.isnan(mx).any(axis=1)


def simulate(probs, odds, mask, thresholds, label):
    """平注模拟。每场只下 EV 最高的那个选项，且要求 EV 超过门槛。"""
    ev = probs * odds - 1                       # ← CLAUDE.md 规定的 EV 公式
    best = ev.argmax(axis=1)
    best_ev = ev[np.arange(len(ev)), best]
    won = (best == y)
    payoff = np.where(won, odds[np.arange(len(odds)), best] - 1, -1.0)

    print(f"\n{label}")
    print(f"{'门槛':>6s} {'注数':>8s} {'命中率':>8s} {'ROI':>9s} {'±SE':>7s} "
          f"{'95% CI':>20s} {'vs 抽水基线':>12s}")
    out = []
    for thr in thresholds:
        sel = mask & (best_ev > thr)
        if sel.sum() < 50:
            continue
        r = payoff[sel]
        roi = r.mean()
        se = r.std(ddof=1) / np.sqrt(len(r))
        # 被选中那些注单的原始抽水（该场三个选项的隐含概率和 - 1）
        vig = (1 / odds[sel]).sum(axis=1) - 1
        base = -vig.mean() / (1 + vig.mean())   # 零信息下注的期望 ROI
        z = (roi - base) / se
        out.append({"thr": thr, "n": int(sel.sum()), "roi": roi, "se": se, "z": z})
        print(f"{thr:>6.0%} {sel.sum():>8,} {won[sel].mean():>7.1%} {roi:>+8.2%} "
              f"{se:>6.2%}  [{roi-1.96*se:>+6.2%}, {roi+1.96*se:>+6.2%}] {z:>+11.1f}σ")
    return pd.DataFrame(out)


THR = [0.0, 0.03, 0.05, 0.10, 0.20]
all_mask = np.ones(len(pred), bool)

print("=" * 84)
print(f"全量走查预测 {len(pred):,} 场，2013-2025，严格样本外")
print("最后一列 = ROI 与「零信息下注只付抽水」基线的差距，单位是标准误")
print("=" * 84)

simulate(P, avg, all_mask, THR, "【平均赔率】")
simulate(P, mx, has_max, THR, "【全市场最高赔率】")

# ── 拆解亏损来源 ──────────────────────────────────────────
print("\n" + "=" * 84)
print("亏损成分拆解（EV>3%，平均赔率）")
print("=" * 84)
ev = P * avg - 1
best = ev.argmax(axis=1)
sel = ev[np.arange(len(ev)), best] > 0.03
r = np.where(best[sel] == y[sel], avg[sel, best[sel]] - 1, -1.0)
vig = (1 / avg[sel]).sum(axis=1) - 1
base = -vig.mean() / (1 + vig.mean())
roi = r.mean()
se = r.std(ddof=1) / np.sqrt(len(r))
print(f"实际 ROI          {roi:+.2%}  ±{se:.2%}")
print(f"零信息基线(抽水)  {base:+.2%}")
print(f"模型挑边的贡献    {roi - base:+.2%}  ±{se:.2%}   "
      f"({(roi-base)/se:+.1f}σ)")
print()
if abs((roi - base) / se) < 2:
    print("→ 模型挑边的贡献在统计上与 0 无法区分：亏的就是抽水，")
    print("  模型既没帮忙也没帮倒忙。08 号文档「模型挑错边占三分之二」的拆解")
    print("  在 659 注上得不出，这里用两个数量级更多的注单否定了它。")
else:
    print("→ 模型挑边确实有显著的方向性贡献。")

# ── 检验「EV 门槛越高越差」是否真实存在 ──────────────────
print("\n" + "=" * 84)
print("「EV 门槛越高越差」是真的吗？（08 号文档称这是没有优势的指纹）")
print("=" * 84)
res = []
for lo, hi in [(0.0, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 0.50), (0.50, 9.9)]:
    e = ev[np.arange(len(ev)), best]
    s = (e > lo) & (e <= hi)
    if s.sum() < 100:
        continue
    rr = np.where(best[s] == y[s], avg[s, best[s]] - 1, -1.0)
    v = (1 / avg[s]).sum(axis=1) - 1
    b = -v.mean() / (1 + v.mean())
    res.append({"EV区间": f"{lo:.0%}-{hi:.0%}", "注数": int(s.sum()),
                "ROI": rr.mean(), "SE": rr.std(ddof=1) / np.sqrt(len(rr)),
                "抽水基线": b, "超额": rr.mean() - b})
rdf = pd.DataFrame(res)
print(rdf.to_string(index=False, float_format=lambda x: f"{x:+.2%}"))
print("\n「超额」列 = 扣掉抽水后模型选边的净贡献。若随 EV 升高而单调变负，")
print("则确实是模型在高分歧处系统性错得更狠；若在 0 附近波动，则是抽水而已。")
