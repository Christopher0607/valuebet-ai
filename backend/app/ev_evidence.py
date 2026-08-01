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

# ── 抽水口径（推荐用这个）──────────────────────────────────
# 上面那条捕获率曲线有个实用性问题：要算 f 你得知道「市场平均价」和
# 「全市场最高价」，而多数人手上只有自己那家平台的报价。
#
# 但同一批实测数据可以换个坐标表达：**平台自己的抽水**。
# 抽水从三个赔率直接算得出来，不需要任何外部行情：
#     抽水 = 1/主胜赔率 + 1/平局赔率 + 1/客胜赔率 - 1
# 抽水才是真正吃掉优势的那个量，捕获率只是它的一个代理。
#
# 同一次走查、同一批注单，把横轴从 f 换成抽水：
#     抽水 6.82% → ROI -3.54%     抽水 3.10% → ROI -0.88%
#     抽水 5.52% → ROI -2.67%     抽水 1.97% → ROI -0.04%
#     抽水 4.28% → ROI -1.76%     抽水 0.88% → ROI +0.91%
# 盈亏平衡点在**抽水约 1.95%**。这是个很低的门槛：多数软盘 1X2 抽水
# 5-7%，只有锐盘和交易所能到 2% 附近。
#
# 注意这是押「赔率 < 2.0 的热门」这一档的曲线。冷门档无论抽水多低都是负的，
# 因为那一侧的净超额本身就是负的（见 _BIAS_BANDS）。
_VIG_CURVE = [
    (0.0088, +0.0091), (0.0142, +0.0047), (0.0197, -0.0004), (0.0253, -0.0045),
    (0.0310, -0.0088), (0.0428, -0.0176), (0.0552, -0.0267), (0.0682, -0.0354),
]
BREAKEVEN_VIG = 0.0195     # 抽水高于这个数，押热门也是亏的


def vig_from_odds(odds_home: float, odds_draw: float, odds_away: float) -> Optional[float]:
    """从一场比赛的三个赔率算出该平台的抽水。

    这是本模块推荐的输入方式：只需要你自己平台的报价，不需要市场行情。
    """
    for o in (odds_home, odds_draw, odds_away):
        if not o or o <= 1:
            return None
    return 1 / odds_home + 1 / odds_draw + 1 / odds_away - 1


def _roi_at_vig(vig: float) -> float:
    """按实测曲线插值出该抽水水平下押热门的预期 ROI。"""
    lo_v, lo_r = _VIG_CURVE[0]
    hi_v, hi_r = _VIG_CURVE[-1]
    if vig <= lo_v:
        return lo_r
    if vig >= hi_v:
        # 曲线外推：抽水每多 1 个百分点，ROI 大约少 1 个百分点
        return hi_r - (vig - hi_v)
    for i in range(len(_VIG_CURVE) - 1):
        x0, y0 = _VIG_CURVE[i]
        x1, y1 = _VIG_CURVE[i + 1]
        if x0 <= vig <= x1:
            return y0 + (y1 - y0) * (vig - x0) / (x1 - x0)
    return hi_r

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


# ── 串关 ──────────────────────────────────────────────────
# 各腿优势按乘法复合。这是真的，实测（强队、最优价、赔率<1.5 池，同日串关，
# 串关赔率 = 各腿赔率乘积，按日期聚类算标准误）：
#   1腿 +1.74%   2腿 +3.66%±1.39%   3腿 +6.25%±2.19%   4腿 +8.47%±3.16%   6腿 +15.96%±6.01%
# 全部置信区间排除 0。
#
# 两个容易搞错的点：
#
# 1. **同日的腿是正相关的，而这对串关有利。** 同一天的热门胜负正相关，
#    联合中奖概率高于各腿概率的乘积，所以同日串关的期望**高于**跨日随机串关
#    （6 腿：+15.96% vs +10.69%）。直觉上「同日相关=危险」在这里是反的——
#    相关性抬高的是联合中奖率，不是方差风险的唯一来源。
#
# 2. **t 值不随腿数上升**（一直是 2.6-2.9）。串关没有让优势更可靠，
#    只是把同一个优势加了杠杆。别把 +15.96% 读成「比 +1.74% 更确定」。
#
# 什么时候该串：**下注额度受限的时候。** 平台单注上限 S 时，
# 单场每个下注位赚 S×1.74%，6 腿串关赚 S×15.96%——同一个位子塞进 9 倍的优势。
# 这是串关唯一站得住的理由，不是「以小博大」。
#
# 平台差异：有的平台串关定价低于赔率乘积（每腿再抽 1-3%），那样 1.74% 的
# 单腿优势撑不住，2%/腿时 3 腿变成 -0.68%。**下单前务必实测自己平台的串关抽水**：
# 拿几场比赛，比较平台给的串关总赔率和各腿赔率的乘积。
TYPICAL_PARLAY_MARGIN_PER_LEG = 0.0   # 纯乘积定价的平台（如用户的 BK8）填 0


def parlay_advisory(leg_edges: list, margin_per_leg: float = TYPICAL_PARLAY_MARGIN_PER_LEG) -> dict:
    """串关是否值得，取决于「单腿优势」和「串关每腿抽水」谁大。

    leg_edges       各腿的单注预期 ROI（小数，例如 0.0174）
    margin_per_leg  串关定价相对赔率乘积的折损，每腿。BK8 这类平台需要
                    自己实测：拿几场比赛，比较平台给的串关总赔率和各腿赔率的乘积。

    数学：各腿独立时，串关期望 = Π(1+e_i) × (1-m)^n - 1。
    优势按乘法复合是真的（08 号文档的 -89.5% 是因为腿本身是负 EV，
    负的同样按乘法复合），但抽水也按腿累加，两者赛跑。

    已知的小偏差：这个闭式解假设各腿优势彼此独立。在真实注单池上做抽样模拟，
    3 腿零抽水得到 +4.74%，本式给 +5.31%，差 0.57 个百分点——来源是池内
    「赔率高低」和「命中率」之间的相关性，独立性假设没抓到。方向和量级是对的，
    但别把这个数字当成精确到小数点后两位的预测。
    """
    n = len(leg_edges)
    if n == 0:
        return {"level": "none", "text": "没有腿"}
    gross = 1.0
    for e in leg_edges:
        gross *= (1 + e)
    net = gross * (1 - margin_per_leg) ** n - 1
    gross_edge = gross - 1

    # 单腿优势要大于串关每腿抽水，串关才有意义
    worst_leg = min(leg_edges)
    out = {"n_legs": n, "gross_edge": round(gross_edge, 4),
           "net_edge": round(net, 4), "margin_per_leg": margin_per_leg,
           "breakeven_leg_edge": round(margin_per_leg / (1 - margin_per_leg), 4)}

    if worst_leg <= 0:
        # 注意：有非正的腿，不等于合计就是负的——正腿够强时合计仍可能为正。
        # 第一版这里硬写了「只会亏得更快」，但同时显示出 +0.43% 的合计，自相矛盾。
        # 正确的建议跟合计正负无关：非正的腿在拖后腿，去掉它一定更好。
        keep = [e for e in leg_edges if e > 0]
        gross_keep = 1.0
        for e in keep:
            gross_keep *= (1 + e)
        net_keep = gross_keep * (1 - margin_per_leg) ** len(keep) - 1 if keep else None
        out["level"] = "avoid"
        out["net_edge_without_bad_legs"] = round(net_keep, 4) if net_keep is not None else None
        bad_n = n - len(keep)
        if not keep:
            out["text"] = (f"{bad_n} 条腿全都是非正的（最差 {worst_leg:+.2%}）。"
                           f"负优势串起来按乘法放大，{n} 腿合计 {net:+.2%}。"
                           f"串关不会把亏钱的注变成赚钱的注。")
        else:
            out["text"] = (f"有 {bad_n} 条腿的单注预期非正（最差 {worst_leg:+.2%}），在拖后腿。"
                           f"当前 {n} 腿合计 {net:+.2%}；去掉那 {bad_n} 条之后，"
                           f"剩下 {len(keep)} 腿是 {net_keep:+.2%}。"
                           f"把负腿删掉一定更好——串关是乘法，一条负腿会按比例削掉整串的期望。")
        return out

    if net <= 0:
        out["level"] = "avoid"
        out["text"] = (f"各腿单独都是正的（合计毛优势 {gross_edge:+.2%}），但串关每腿抽水 "
                       f"{margin_per_leg:.1%}，{n} 腿之后净 {net:+.2%}。"
                       f"单腿优势要超过 {out['breakeven_leg_edge']:.2%} 串关才划算——"
                       f"实测热门档只有 1.74%。分开下单场更好。")
        return out

    out["level"] = "ok"
    tail = (f"{n} 腿，各腿优势复合后 {net:+.2%}"
            + (f"（已扣每腿 {margin_per_leg:.1%} 串关抽水）" if margin_per_leg > 0 else "（该平台串关不额外抽水）"))
    out["text"] = (tail + f"。这是把同一个优势加杠杆，不是让它更可靠——"
                   f"实测 t 值不随腿数上升。只在下注额度受限时才划算："
                   f"额度上限固定时，串关能在同一个下注位里塞进更多优势。"
                   f"中奖率随腿数急剧下降，要能扛住长连败。")
    return out


# 价格捕获率 f × 腿数 → 实测 ROI（强队、赔率<1.5 档、同日串关、纯乘积定价）
# 串关列的聚类标准误很大（6 腿 ±6%），所以只有三条读法是稳健的：
#   f ≤ 0.2 全负；f ≥ 0.6 全正且随腿数放大；中间那一带分不出来。
# f=0.4 行出现过「1腿 -0.44% 但 6腿 +1.55%」这种不该有的非单调，就是噪声的证据。
CAPTURE_BY_LEGS = {
    0.0: {1: -0.0143, 2: -0.0307, 3: -0.0426, 4: -0.0402, 6: -0.0509},
    0.2: {1: -0.0125, 2: -0.0184, 3: -0.0214, 4: -0.0191, 6: -0.0229},
    0.4: {1: -0.0044, 2: -0.0040, 3: +0.0018, 4: -0.0029, 6: +0.0155},
    0.6: {1: +0.0040, 2: +0.0083, 3: +0.0125, 4: +0.0221, 6: +0.0468},
    0.8: {1: +0.0111, 2: +0.0204, 3: +0.0353, 4: +0.0471, 6: +0.0863},
    1.0: {1: +0.0167, 2: +0.0330, 3: +0.0524, 4: +0.0705, 6: +0.1458},
}


def advisory_from_three_odds(odds_home: float, odds_draw: float, odds_away: float,
                             pick: Optional[str] = None) -> dict:
    """**推荐入口**：只用你自己平台的三个赔率就能判断。

    odds_home/draw/away  你的平台对这场比赛开的三个价
    pick                 你想押哪个（'home'/'draw'/'away'）；不传就自动选热门那侧

    相比 bet_advisory()，这个函数不需要「市场平均价」和「全市场最高价」——
    实测把价格质量重新表达成了平台自己的抽水，而抽水从三个赔率直接算得出来。
    """
    vig = vig_from_odds(odds_home, odds_draw, odds_away)
    if vig is None:
        return {"level": "none", "text": "三个赔率都要填，且都必须大于 1。"}

    picks = {"home": odds_home, "draw": odds_draw, "away": odds_away}
    if pick is None:
        # 不指定就选赔率最低的那个——热门-冷门偏差里占便宜的一侧
        pick = min(picks, key=picks.get)
    odds = picks.get(pick)
    if not odds or odds <= 1:
        return {"level": "none", "text": f"选项 {pick} 的赔率无效。"}

    b = _band(odds)
    zh = {"home": "主胜", "draw": "平局", "away": "客胜"}.get(pick, pick)
    out = {"pick": pick, "odds": odds, "vig": round(vig, 4),
           "band": b["band"] if b else None,
           "breakeven_vig": BREAKEVEN_VIG,
           "net_edge": round(b["net_edge_best_price"], 4) if b else None}

    if b is None:
        out["level"] = "none"
        out["text"] = "赔率超出实测区间。"
        return out

    # 冷门侧：净超额本身就是负的，抽水再低也补不回来
    if b["net_edge_best_price"] <= 0:
        out["level"] = "avoid"
        out["expected_roi"] = round(b["net_edge_best_price"] - vig / (1 + vig), 4)
        out["text"] = (f"押{zh} @{odds:.2f} 落在 {b['band']} 档，该档实测净超额 "
                       f"{b['net_edge_best_price']:+.2%}（{b['n_bets']:,} 注）。"
                       f"这是热门-冷门偏差里吃亏的一侧——抽水再低也补不回来，不建议下。")
        return out

    roi = _roi_at_vig(vig)
    out["expected_roi"] = round(roi, 4)
    ok = vig <= BREAKEVEN_VIG
    out["level"] = "ok" if ok else "bad_price"
    if ok:
        out["text"] = (f"押{zh} @{odds:.2f}（{b['band']} 档）。这家的抽水 {vig:.2%}，"
                       f"低于 {BREAKEVEN_VIG:.2%} 的盈亏平衡线。该条件下实测 ROI 约 "
                       f"{roi:+.2%}——薄，但为正。")
    else:
        out["text"] = (f"押{zh} @{odds:.2f} 方向对（{b['band']} 档净超额 "
                       f"{b['net_edge_best_price']:+.2%}），但这家的抽水 {vig:.2%}，"
                       f"高于 {BREAKEVEN_VIG:.2%} 的平衡线。该条件下实测 ROI 约 {roi:+.2%}"
                       f"——选对了方向仍然是亏的。这不是选球的问题，是这家平台太贵。")
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
