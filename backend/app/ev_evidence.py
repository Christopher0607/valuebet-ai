"""
下注建议的实证依据层。

这个模块经历过一次彻底改写，两版的差别就是整个项目的结论
--------------------------------------------------------
**第一版**（模型 EV 路线）：把「历史上这个 EV 区间实际发生了什么」挂在模型 EV 旁边，
用来警告用户高 EV 反而更亏。它是对的，但它建立在一个已经死掉的框架上——
模型对市场价格的增量信息为零（09 号文档，t = -0.2），所以模型 EV 这个量
根本不携带信息，围绕它做任何事都是徒劳。

**这一版**（价格偏差路线）：信号完全不来自模型，来自**价格本身**。
141,287 场走查 + 独立 HOLDOUT 验证发现了一个真实且可复现的结构性偏差：
热门-冷门偏差（favourite-longshot bias）。赌客系统性高估大赔率事件，
导致冷门定价过紧、热门定价偏松。

    赔率区间        净超额（扣抽水后）  最优价      平均价
    [1.0, 2.0) 热门      +1.79% (t=6.3)   +2.84% (t=10.9)
    [2.0, 3.5)           +0.43%           +0.17%
    [3.5, 6.0) 冷门      -2.15%           -3.70%
    [6.0, ∞)   大冷门    -4.96%          -13.57%

两个价格源上都单调下降，DEV(2013-18) 与 HOLDOUT(2019-25) 独立复现，10/13 年为正。

但偏差存在 ≠ 能赚钱
-------------------
偏差约 +1.8%，而抽水会吃掉它。能不能剩下正的，完全取决于你成交在什么价：

    实际成交价 = 平均价 + f × (最优价 - 平均价)
    f = 0.0  →  ROI -3.54%     (随便找一家下注)
    f = 0.8  →  ROI -0.04%     (盈亏平衡点在这附近)
    f = 1.0  →  ROI +0.91%     (每次都拿到全市场最优价)

**所以这个模块的核心输出不是「押不押」，是「你拿到的价够不够好」。**
选对方向只是必要条件，价格执行才是决定盈亏的那一半。

数据来源：backend/training/validation/19_price_anomalies.py
"""
from typing import Optional

# 赔率区间 → (净超额@最优价, 净超额@平均价, 样本量, 标准误)
# 净超额 = 实测 ROI - 该批注单的抽水基线，即价格偏差本身的大小，
# 已经把「不同价格源抽水不同」这个污染因素除掉了。
_BIAS_BANDS = [
    (1.00, 2.00, +0.0179, +0.0284, 80830, 0.0028),
    (2.00, 3.50, +0.0043, +0.0017, 264254, 0.0026),
    (3.50, 6.00, -0.0215, -0.0370, 213531, 0.0038),
    (6.00, 1e9, -0.0496, -0.1357, 49689, 0.0125),
]

# 成交价捕获率 f → 实测 ROI（押所有赔率 < 2.0 的选项）
_CAPTURE_CURVE = [
    (0.0, -0.0354), (0.2, -0.0267), (0.4, -0.0176), (0.6, -0.0088),
    (0.7, -0.0045), (0.8, -0.0004), (0.9, +0.0047), (1.0, +0.0091),
]
BREAKEVEN_CAPTURE = 0.80   # 低于这个捕获率，方向选对了也是亏的

OVERALL = {
    "n_matches": 141287,
    "favourite_net_edge_best_price": 0.0179,
    "favourite_roi_best_price": 0.0091,
    "favourite_roi_average_price": -0.0354,
    "bets_per_year_all_leagues": 6519,
    "prob_losing_year_at_best_price": 0.18,
    # 模型侧的结论，保留以免有人又绕回去
    "model_incremental_info_t": -0.2,
}


def _band(odds: float):
    for lo, hi, net_best, net_avg, n, se in _BIAS_BANDS:
        if lo <= odds < hi:
            hi_s = "∞" if hi > 1e8 else f"{hi:.2f}"
            return {"band": f"[{lo:.2f},{hi_s})", "net_edge_best_price": net_best,
                    "net_edge_average_price": net_avg, "n_bets": n, "se": se}
    return None


def price_capture(taken_odds: float, average_odds: float, best_odds: float) -> Optional[float]:
    """你拿到的价，在「平均价→全市场最优价」这条线上处在什么位置。

    返回 f。f=0 表示你只拿到市场平均价，f=1 表示你拿到了最优价。
    最优价与平均价差距过小时（<0.5%）返回 None——此时这个比例的分母不稳定，
    报一个放大了噪声的 f 会误导人。
    """
    if best_odds <= average_odds * 1.005:
        return None
    return (taken_odds - average_odds) / (best_odds - average_odds)


def _roi_at_capture(f: float) -> float:
    """按实测曲线线性插值出该捕获率下的预期 ROI。"""
    f = max(0.0, min(1.0, f))
    for i in range(len(_CAPTURE_CURVE) - 1):
        x0, y0 = _CAPTURE_CURVE[i]
        x1, y1 = _CAPTURE_CURVE[i + 1]
        if x0 <= f <= x1:
            return y0 + (y1 - y0) * (f - x0) / (x1 - x0)
    return _CAPTURE_CURVE[-1][1]


def bet_advisory(odds: float, average_odds: Optional[float] = None,
                 best_odds: Optional[float] = None) -> dict:
    """给一个选项的赔率，返回基于实测价格偏差的建议。

    odds         你实际能下到的赔率
    average_odds 市场平均赔率（可选，用于算价格捕获率）
    best_odds    全市场最高赔率（可选，同上）

    刻意不接收模型概率作为参数：模型对价格的增量信息实测为零，
    把它放进来只会让人以为它在起作用。
    """
    b = _band(odds)
    if b is None:
        return {"level": "none", "text": "赔率超出实测区间", "band": None}

    out = {"band": b["band"], "n_bets": b["n_bets"],
           "net_edge_best_price": round(b["net_edge_best_price"], 4)}

    # 方向判断：只有热门档的净超额显著为正
    favourable = b["net_edge_best_price"] > 0 and b["net_edge_best_price"] > 2 * b["se"]

    if not favourable:
        out["level"] = "avoid"
        out["text"] = (f"赔率 {odds:.2f} 落在 {b['band']}，该档实测净超额 "
                       f"{b['net_edge_best_price']:+.2%}（{b['n_bets']:,} 注）。"
                       f"这是热门-冷门偏差里吃亏的一侧，不建议下注。")
        return out

    # 方向对了，接下来价格执行才是决定性的
    if average_odds is None or best_odds is None:
        out["level"] = "need_price_check"
        out["text"] = (f"赔率 {odds:.2f} 落在 {b['band']}，该档实测净超额 "
                       f"{b['net_edge_best_price']:+.2%}——方向是对的。但这个优势会被抽水吃掉，"
                       f"是否盈利取决于你拿到的价：只拿市场平均价是 -3.54%，"
                       f"拿到全市场最优价才是 +0.91%。请填入平均价和最高价再判断。")
        return out

    f = price_capture(odds, average_odds, best_odds)
    if f is None:
        out["level"] = "need_price_check"
        out["text"] = "各平台报价过于接近，无法判断价格捕获率，请核对赔率来源。"
        return out

    out["price_capture"] = round(f, 3)
    out["expected_roi"] = round(_roi_at_capture(f), 4)
    if f >= BREAKEVEN_CAPTURE:
        out["level"] = "ok"
        out["text"] = (f"赔率 {odds:.2f}（{b['band']}），价格捕获率 {f:.0%}，"
                       f"高于 {BREAKEVEN_CAPTURE:.0%} 的盈亏平衡线。"
                       f"该条件下实测 ROI 约 {out['expected_roi']:+.2%}——"
                       f"薄，但为正。")
    else:
        out["level"] = "bad_price"
        out["text"] = (f"赔率 {odds:.2f} 方向对，但价格捕获率只有 {f:.0%}，"
                       f"低于 {BREAKEVEN_CAPTURE:.0%} 的盈亏平衡线。"
                       f"该条件下实测 ROI 约 {out['expected_roi']:+.2%}——"
                       f"选对了方向仍然是亏的。去比价，或者不下。")
    return out


def reality_check() -> dict:
    """这条路的三个现实约束，不该被界面上的正 ROI 数字盖掉。"""
    return {
        "execution": "必须捕获最优价溢价的 80% 以上才保本。只拿平均价是 -3.54%。",
        "account_limits": "用最优价押热门是博彩公司识别专业玩家最典型的特征，"
                          "赢了会被限额或封号。这是无法用历史数据验证的天花板，"
                          "也是这条路最现实的终点。",
        "variance": "全库每年约 6,519 注、ROI +0.91% 的情况下，"
                    "单年亏损概率约 18%。这不是稳赚，是一个薄到随时会被"
                    "执行成本吃掉的统计优势。",
    }
