"""
DEV 期超参搜索（2004-2013）。TEST 期（2013 之后）在这一步一次都不会被读到。

要定三个超参：
  half_life_days —— 时间衰减半衰期。现有 train_mle.py 用的是 4 年，那是给国家队
                    定的（国家队阵容变化慢）。俱乐部转会窗一年两次，4 年几乎肯定太长。
  ridge          —— attack/defense 的岭正则强度。它同时承担识别性的职责
                    （整体平移不可识别，靠正则钉住）。
  rho            —— Dixon-Coles 低比分修正。model.py 里硬编码 -0.13，从没拟合过。

判据是 DEV 期样本外 RPS。注意这里只跟自己比，不跟市场比——超参选择不应该
参考市场表现，否则等于间接把市场信息泄漏进模型。
"""
import sys, time, itertools
import numpy as np
import pandas as pd
import lib_walkforward as L

df = L.load_matches()
dev = df[(df.MatchDate >= L.DEV_START) & (df.MatchDate < L.DEV_END)]
print(f"DEV 期比赛 {len(dev)} 场（{L.DEV_START.date()} ~ {L.DEV_END.date()}）")
print("注意：走查会用到 DEV 起点之前的历史做训练，但只在 DEV 期内评分\n")

HALF_LIVES = [180.0, 365.0, 550.0, 730.0, 1460.0]
RIDGES = [0.003, 0.01, 0.03, 0.1]
RHOS = [-0.20, -0.16, -0.13, -0.10, -0.06, -0.03, 0.0]

results = []
print(f"{'半衰期(天)':>10s} {'ridge':>7s} {'最优rho':>8s} {'RPS':>9s} {'场数':>8s}")
print("-" * 48)

for hl, ridge in itertools.product(HALF_LIVES, RIDGES):
    t0 = time.time()
    pred = L.run_walkforward(df, L.DEV_START, L.DEV_END,
                             half_life_days=hl, ridge=ridge, verbose=False)
    lam = pred["lam"].to_numpy()
    mu = pred["mu"].to_numpy()
    outcome = pred["outcome"].to_numpy()
    best = None
    for rho in RHOS:
        p = L.probs_from_rates(lam, mu, rho)
        r = L.rps(p, outcome).mean()
        if best is None or r < best[1]:
            best = (rho, r)
    results.append({"half_life": hl, "ridge": ridge, "rho": best[0],
                    "rps": best[1], "n": len(pred), "secs": time.time() - t0})
    print(f"{hl:>10.0f} {ridge:>7.3f} {best[0]:>8.2f} {best[1]:>9.5f} {len(pred):>8d}")

res = pd.DataFrame(results).sort_values("rps")
print("\n" + "=" * 48)
print("DEV 期最优组合：")
top = res.iloc[0]
print(f"  half_life_days = {top.half_life:.0f}")
print(f"  ridge          = {top.ridge:.3f}")
print(f"  rho            = {top.rho:.2f}")
print(f"  DEV RPS        = {top.rps:.5f}   ({int(top.n)} 场)")

base = res[(res.half_life == 1460.0) & (res.ridge == 0.01)]
if len(base):
    b = base.iloc[0]
    print(f"\n对比现有设定（4年半衰期 / ridge 0.01）: RPS {b.rps:.5f}")
    print(f"改进 {b.rps - top.rps:+.5f}")

res.to_csv("dev_tuning_results.csv", index=False)
print("\n完整结果已存 dev_tuning_results.csv")
