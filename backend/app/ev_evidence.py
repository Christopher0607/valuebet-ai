"""
把「EV 数字实际意味着什么」的实测结果接进产品。

为什么需要这个模块
------------------
系统算出的 EV = 模型概率 × 赔率 - 1。这个公式本身没问题（也不许改，见 CLAUDE.md）。
问题在于**它的解读**：界面此前把高 EV 当成好事——绿色、⚡VALUE 徽章、建议凯利下注额。

但 141,287 场走查（handoff/09_WALKFORWARD_FINDINGS.md）测出来的是反过来的：
EV 越高，实际回报越差，且单调。原因不难理解——模型对市场价格的增量信息为零
（增量信息检验 t = -0.2），所以「模型和价格分歧大」不代表发现了价值，
只代表模型在这一场错得多。EV 筛选器筛的是模型自己的误差。

所以这个模块不改 EV 公式，只是在 EV 旁边挂上「历史上这个 EV 区间实际发生了什么」。
用户看到的不再是一个孤立的 +18%，而是「EV +18%；历史上 EV 10-20% 的注单，
按最高赔率平注实际 ROI -2.91%」。

数据来源
--------
backend/training/validation/15_betting_power.py，2013-2025 走查预测，
每档都是几万注，ROI 带标准误。复现方式见 09 号文档。
"""
from typing import Optional

# (EV 下界, EV 上界, 注数, 实际 ROI, ROI 标准误, 抽水基线, 扣掉抽水后的净贡献)
# 两套：用平均赔率下注 vs 全市场比价拿最高赔率下注。
_BANDS_AVG = [
    (0.00, 0.05, 30257, -0.0610, 0.0080, -0.0575, -0.0035),
    (0.05, 0.10, 22523, -0.0750, 0.0097, -0.0570, -0.0180),
    (0.10, 0.20, 27875, -0.0943, 0.0092, -0.0570, -0.0373),
    (0.20, 0.50, 26599, -0.1058, 0.0108, -0.0571, -0.0487),
    (0.50, 99.0, 6992, -0.1381, 0.0272, -0.0582, -0.0799),
]
_BANDS_MAX = [
    (0.00, 0.05, 22397, -0.0016, 0.0096, -0.0107, +0.0091),
    (0.05, 0.10, 26927, -0.0052, 0.0091, -0.0070, +0.0018),
    (0.10, 0.20, 37055, -0.0291, 0.0083, -0.0057, -0.0234),
    (0.20, 0.50, 39955, -0.0278, 0.0094, -0.0039, -0.0239),
    (0.50, 99.0, 13102, -0.0256, 0.0228, -0.0001, -0.0255),
]

# 整体水平，用于「不分档」的提示
OVERALL = {
    "n_matches": 141287,
    "model_rps": 0.2104,
    "market_rps": 0.2046,
    "rps_gap": 0.00585,
    "rps_gap_se": 0.00015,
    "incremental_info_t": -0.2,   # 模型对市场价格的增量信息检验
    "flat_roi_avg_odds": -0.0871,
    "flat_roi_max_odds": -0.0195,
}


def _lookup(ev: float, bands):
    for lo, hi, n, roi, se, base, net in bands:
        if lo <= ev < hi:
            # 最高一档没有真实上界（99.0 只是个哨兵值），显示成 ">50%"
            band = f"{lo:.0%}-{hi:.0%}" if hi < 1.0 else f">{lo:.0%}"
            return {"ev_band": band, "n_bets": n,
                    "historical_roi": round(roi, 4), "roi_se": round(se, 4),
                    "vig_baseline": round(base, 4), "net_of_vig": round(net, 4)}
    return None


def ev_evidence(ev: Optional[float]) -> Optional[dict]:
    """给一个 EV 值，返回历史上这个区间实际发生了什么。

    EV <= 0 的选项不返回证据——系统本来就不推荐它们，挂上历史 ROI 反而
    容易被读成「负 EV 也能下」。
    """
    if ev is None or ev <= 0:
        return None
    avg = _lookup(ev, _BANDS_AVG)
    mx = _lookup(ev, _BANDS_MAX)
    if not avg or not mx:
        return None
    return {
        "ev_band": avg["ev_band"],
        "at_average_odds": {k: v for k, v in avg.items() if k != "ev_band"},
        "at_best_odds": {k: v for k, v in mx.items() if k != "ev_band"},
        # 置信区间跨过 0 = 该档在统计上与「不赚不亏」无法区分；
        # 完全在 0 以下 = 该档已被确认为亏损。
        "best_odds_ci_includes_zero": abs(mx["historical_roi"]) < 1.96 * mx["roi_se"],
        "note": "历史实测，非预测。EV 越高实际回报越差且单调——"
                "模型对市场价格的增量信息为零，高 EV 反映的是模型误差而非价值。",
    }


def bet_advisory(ev: Optional[float]) -> dict:
    """给界面用的一句话结论。

    刻意不使用「推荐下注」这类措辞：09 号文档的结论是本模型没有可利用的优势，
    在这个前提下任何「建议下注 X 元」都是没有依据的。凯利公式的前提是
    你确实有正期望，这个前提已经被 141,287 场证伪了。
    """
    if ev is None or ev <= 0:
        return {"level": "none", "text": "负 EV，不下注"}
    e = ev_evidence(ev)
    if e is None:
        return {"level": "unknown", "text": "EV 超出实测区间，无历史参照"}
    roi = e["at_best_odds"]["historical_roi"]
    if ev >= 0.10:
        return {"level": "warn",
                "text": f"EV {ev:.0%} 落在 {e['ev_band']} 档。该档历史平注 ROI "
                        f"{roi:+.1%}（{e['at_best_odds']['n_bets']:,} 注）。"
                        f"EV 越高历史回报越差——这一档是亏得较多的一档。"}
    if e["best_odds_ci_includes_zero"]:
        return {"level": "neutral",
                "text": f"EV {ev:.0%} 落在 {e['ev_band']} 档。该档历史平注 ROI "
                        f"{roi:+.1%} ±{e['at_best_odds']['roi_se']:.1%}，"
                        f"与不赚不亏无法区分——前提是你拿到全市场最高赔率。"}
    return {"level": "caution",
            "text": f"EV {ev:.0%} 落在 {e['ev_band']} 档，历史平注 ROI {roi:+.1%}。"}
