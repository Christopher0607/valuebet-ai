"""
美职联、英格兰联赛杯 —— 各自独立训练一张参数表，不并入 club 那张联合表。

为什么不并表：club 表的"四大联赛+欧冠联合训练"能把不同联赛的攻防参数
锚定到同一把尺子上，前提是训练池里有真正跨联赛的对局做桥。美职联跟
这四个联赛没有任何交叉赛事；联赛杯虽然后期轮次有英超球队参战，但数据源
（API-Football 免费档）只覆盖 2022-2024，跟 club 表的训练窗口、数据来源
都不一致，参赛的英冠/英甲/英乙球队也根本不在 club 表里——勉强合并只会
产生"看着可比、实际没有真实桥接对局支撑"的假象。分开训练更诚实，
见 model.py 里 COMPETITION_SCOPE 上方的注释。

数据源：API-Football（api_football.py），免费档只放行 2022-2024 三个
赛季——用这三季完整赛果拟合参数，当前/未来赛程另外走 The Odds API
（odds_api.py），两者衔接见 updater.py 的 run_full_update。
"""
import sys
import os
import csv
import json
from datetime import date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app.updater import normalize_team_name  # noqa: E402
from app.api_football import fetch_training_matches, FREE_TIER_SEASONS  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from train_mle import load_matches, build_team_index, fit_parameters  # noqa: E402


_OUT_DIR = os.path.dirname(os.path.abspath(__file__))

# 决赛是否中立场地，两个赛事结论不一样，别照搬——
#   联赛杯决赛在温布利，真中立场，训练数据要标 neutral=TRUE。
#   美职联季后赛（含决赛）2019 年改制后全部在种子更高的队伍主场打，
#   实测样本验证过：2024 年 MLS Cup 决赛主队 Los Angeles Galaxy 的比赛地
#   就是 "Dignity Health Sports Park"——正是这支队自己的主场，不是中立地。
#   所以美职联从小组赛到决赛全部按真实主客场处理，不设 neutral。
_NEUTRAL_FINAL_RULE = {
    "mls": lambda rnd: False,
    "efl_cup": lambda rnd: "Final" in rnd and "Semi" not in rnd and "Quarter" not in rnd,
}

_SCOPE_LABEL = {"mls": "美职联", "efl_cup": "英格兰联赛杯"}

# club 表（四大联赛+欧冠联合训练）用默认正则化系数 0.01，跑出来的球队之间
# attack 标准差是 0.34，量级合理。这个系数是在场均每队约100场比赛的密度下
# 调出来的——用同一个系数去拟合场均每队只有个位数场次的联赛杯，正则化
# 相对数据量太弱，压不住偶然结果，实测 attack 标准差被推到 0.89（比国家队
# 表——真正跨越百年、包含关岛圣马力诺这种长期弱旅的表——还要离散），
# 极端值出现 -4.6 这种"这支球队几乎不可能进球"的荒谬数字，起因是
# Bristol City 这类球队"够格拟合"的比赛数（≥6场）里有一半以上其实是
# 对手场次不够被整场跳过、真正进入似然计算的只有两三场，几场比赛的
# 偶然结果被当成了稳定信号。
#
# 修法：正则化强度按数据密度反比缩放，让"每场比赛能拉动参数多少"这个
# 比例在各赛事间大致一致，而不是所有赛事共用同一个为大样本调的固定值。
# 用 club 表的密度做基准（约100场/队），密度越低、正则化越强；下限锁在
# 0.01，不会比大样本赛事更松。
_REFERENCE_MATCHES_PER_TEAM = 100.0


def collect(league_code: str) -> list:
    rows = []
    matches = fetch_training_matches(league_code)
    neutral_rule = _NEUTRAL_FINAL_RULE[league_code]
    for m in matches:
        h, a = m["score"]["ft"]
        rows.append({
            "date": m["date"],
            "home_team": normalize_team_name(m["team1"]),
            "away_team": normalize_team_name(m["team2"]),
            "home_score": h, "away_score": a,
            "tournament": league_code,
            "neutral": "TRUE" if neutral_rule(m.get("round", "")) else "FALSE",
        })
    return rows


def train_one(league_code: str):
    label = _SCOPE_LABEL[league_code]
    print(f"\n📥 抓取{label}历史赛果（{FREE_TIER_SEASONS}）...")
    rows = collect(league_code)
    rows.sort(key=lambda r: r["date"])
    print(f"   共 {len(rows)} 场")

    if len(rows) < 200:
        raise RuntimeError(f"{label}只抓到 {len(rows)} 场，数据量不足以训练，中止")

    out_csv = os.path.join(_OUT_DIR, f"historical_results_{league_code}.csv")
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "home_team", "away_team",
                                          "home_score", "away_score", "tournament", "neutral"])
        w.writeheader()
        w.writerows(rows)
    print(f"💾 → {out_csv}")

    matches = load_matches(csv_path=out_csv, start_date=date(FREE_TIER_SEASONS[0], 1, 1))
    team_idx, counts = build_team_index(matches, min_matches=6)
    print(f"   {len(team_idx)} 支球队达标（≥6场，样本量比四大联赛小很多，门槛相应降低），"
          f"{len(counts) - len(team_idx)} 支因场次太少被排除")

    # 数据密度越低，正则化越强，见上方 _REFERENCE_MATCHES_PER_TEAM 的推导。
    # 下限锁在 0.01（club 表的系数），永远不会比大样本赛事更松。
    avg_matches_per_team = sum(counts[t] for t in team_idx) / len(team_idx)
    reg_strength = max(0.01, 0.01 * _REFERENCE_MATCHES_PER_TEAM / avg_matches_per_team)
    print(f"   场均每队 {avg_matches_per_team:.1f} 场比赛，正则化系数 reg_strength={reg_strength:.4f}"
          f"（club 表基准 0.01，按密度反比放大）")

    # 场次不够独立拟合的球队（比如 Bristol City 遇到的 Coventry、
    # Oxford United）不再让它们的对手也白白丢掉那场比赛——用「替补」节点
    # （attack/defense 钉在0）部分利用，见 train_mle.py 里
    # precompute_match_arrays 的推导。club/international 没传这个参数，
    # 默认 False，行为不受影响。
    attack, defense, home_adv, nll = fit_parameters(
        matches, team_idx, reg_strength=reg_strength, use_fallback_opponent=True)
    print(f"   拟合完成，主场优势 = {home_adv:.4f}")

    out = {
        "scope": league_code,
        "note": f"{label}独立训练的参数表，跟 club/international 表互不可比。",
        "trained_at": datetime.now().isoformat(),
        "seasons": FREE_TIER_SEASONS,
        "n_matches_used": len(matches),
        "n_teams_fitted": len(team_idx),
        "reg_strength": round(reg_strength, 4),
        "home_advantage": round(home_adv, 4),
        "final_neg_log_likelihood": round(nll, 2),
        "attack": {k: round(v, 4) for k, v in attack.items()},
        "defense": {k: round(v, 4) for k, v in defense.items()},
    }
    out_json = os.path.join(_OUT_DIR, f"fitted_parameters_{league_code}.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"💾 参数已保存 → {out_json}")
    return out


if __name__ == "__main__":
    for code in ("mls", "efl_cup"):
        train_one(code)
