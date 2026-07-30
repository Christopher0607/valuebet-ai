"""
TEST 期主评估（2013-2025）：走查产生的严格样本外预测 vs 市场赔率。

这是替代 01_model_vs_market.py 的新尺子。区别：
  01_       ：单一时间切分，测试集 1267 场，同一批数据被反复用于选特征
  本脚本    ：滚动起点，每月重拟合，测试集十几万场，超参在 2013 年之前就锁死

产出 walkforward_test.parquet，下游的 Murphy 分解、校准、主客场变体、
下注模拟全部复用同一份预测，保证各实验比较的是同一批比赛。
"""
import time
import numpy as np
import pandas as pd
import lib_walkforward as L

t0 = time.time()
df = L.load_matches()
print(f"数据 {len(df)} 场，{df.MatchDate.min().date()} ~ {df.MatchDate.max().date()}")
print(f"锁定超参：half_life={L.DEFAULT_HALF_LIFE_DAYS:.0f}天  "
      f"ridge={L.DEFAULT_RIDGE}  rho={L.DEFAULT_RHO}")
print(f"TEST 期 {L.DEV_END.date()} ~ {L.TEST_END.date()}（超参选择完全没看过这段）\n")

pred = L.run_walkforward(df, L.DEV_END, L.TEST_END)
pred = L.attach_market(pred)
print(f"\n走查完成，用时 {time.time() - t0:.0f}s")

pred.to_parquet("walkforward_test.parquet", index=False)
print(f"预测表已存 walkforward_test.parquet（{len(pred)} 行）\n")

# ── 主结果 ────────────────────────────────────────────────
n = len(pred)
acc_model = (pred[["p_home", "p_draw", "p_away"]].to_numpy().argmax(1) == pred.outcome).mean()
acc_market = (pred[["q_home", "q_draw", "q_away"]].to_numpy().argmax(1) == pred.outcome).mean()

print("=" * 60)
print(f"样本外场次: {n:,}   （原方案 1,267 场，放大 {n / 1267:.0f} 倍）")
print(f"庄家平均抽水: {pred.vig.mean() * 100:.2f}%")
print()
print(f"{'':16s} {'准确率':>9s} {'RPS':>10s}")
print(f"{'模型':16s} {acc_model:>8.1%} {pred.rps_model.mean():>10.4f}")
print(f"{'市场（平均赔率）':16s} {acc_market:>8.1%} {pred.rps_market.mean():>10.4f}")
print(f"{'随机基准':16s} {'33.3%':>9s} {0.2450:>10.4f}")

g = L.paired_gap(pred)
print()
print(f"配对差值 (模型 - 市场): {g['gap']:+.5f}  ±{g['se']:.5f} (SE)   t = {g['t']:+.1f}")
if g["gap"] < 0:
    print("✅ 模型 RPS 低于市场")
else:
    print(f"❌ 模型 RPS 高于市场 {g['gap']:.5f}——没有优势")
print("=" * 60)

# ── 按年份看缺口是否稳定 ──────────────────────────────────
print("\n逐年缺口（看差距是在收敛还是在扩大）:")
pred["year"] = pred.MatchDate.dt.year
yr = pred.groupby("year").apply(
    lambda d: pd.Series({"n": len(d), "model": d.rps_model.mean(),
                         "market": d.rps_market.mean(),
                         "gap": (d.rps_model - d.rps_market).mean()}),
    include_groups=False)
print(yr.to_string(float_format=lambda x: f"{x:.4f}"))

# ── 按池看，找出是否有联赛模型确实占优 ────────────────────
print("\n分池缺口（gap<0 = 模型赢市场，t 值给出可信度）:")
rows = []
for pool, d in pred.groupby("pool"):
    if len(d) < 500:
        continue
    gg = L.paired_gap(d)
    rows.append({"pool": pool, "n": gg["n"], "model": d.rps_model.mean(),
                 "market": d.rps_market.mean(), "gap": gg["gap"],
                 "se": gg["se"], "t": gg["t"]})
pool_df = pd.DataFrame(rows).sort_values("gap")
print(pool_df.to_string(index=False, float_format=lambda x: f"{x:.4f}"))

print("\n注意：分池是 %d 次比较，最好的那个池的 t 值要按多重比较打折看待，"
      "\n不能直接当成「找到了一个能打的联赛」。" % len(pool_df))
