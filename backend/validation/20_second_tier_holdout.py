"""德乙/法乙并入俱乐部参数表 —— 严格时间切分的样本外检验。

为什么必须做这个：把德乙/法乙加进训练之后，**样本内** RPS 变差了
（配对差 +0.00020 ± 0.00005, t=+3.7）。那个数字有误导性——
旧参数里帕德博恩只有 34 场（全是 2019-20 德甲那一季），用 34 场去拟合
自己那 34 场，样本内当然好看。样本内变差恰恰是过拟合被削掉的表现，
不能拿来判断这次改动的好坏。

所以按项目定的规矩，做真正的样本外：
    在 CUTOFF 之前的数据上分别拟合两套参数（一套含德乙/法乙，一套不含），
    再到 CUTOFF 之后的比赛上比 RPS。两套参数都没见过评测集。

评测集只取**我们实际会去预测的联赛**（一级联赛），不含德乙/法乙本身——
否则等于让加了德乙数据的那一版在自己的主场比赛，赢了也不说明问题。

结论必须带配对标准误（CLAUDE.md 的方法要求）。
"""
import sys
import os
import csv
import math
import statistics as st
from datetime import date, datetime
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "training"))

from train_mle import build_team_index, fit_parameters          # noqa: E402
from app.model import dc_tau, poisson_pmf                        # noqa: E402

CSV = os.path.join(HERE, "..", "training", "historical_results_club.csv")
CUTOFF = date(2024, 7, 1)          # 拟合用 < CUTOFF，评测用 >= CUTOFF
NEW_LEAGUES = {"de.2", "fr.2"}
# 评测只看一级联赛：这些才是系统真正拿去下注的对象
EVAL_LEAGUES = {"en.1", "es.1", "it.1", "de.1", "fr.1"}


def load_rows():
    out = []
    with open(CSV, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["home_score"] == "NA" or r["away_score"] == "NA":
                continue
            out.append({
                "date": datetime.strptime(r["date"], "%Y-%m-%d").date(),
                "home": r["home_team"], "away": r["away_team"],
                "hs": int(r["home_score"]), "as": int(r["away_score"]),
                "league": r["tournament"], "neutral": r["neutral"] == "TRUE",
            })
    return out


def to_train_shape(rows):
    """转成 train_mle 期望的形状（进球字段叫 hg/ag，不是 home_score）。"""
    return [{"date": m["date"], "home": m["home"], "away": m["away"],
             "hg": m["hs"], "ag": m["as"],
             "neutral": m["neutral"], "tournament": m["league"]} for m in rows]


def fit(rows, label):
    ms = to_train_shape(rows)
    idx, _ = build_team_index(ms)
    attack, defense, home_adv, _ = fit_parameters(
        ms, idx, reference_date=CUTOFF, use_fallback_opponent=True)
    print(f"   {label}: {len(ms)} 场, {len(idx)} 支球队, 主场优势 {home_adv:.4f}")
    return {"attack": attack, "defense": defense, "home_advantage": home_adv}


def probs(p, h, a, neutral):
    atk, dfn = p["attack"], p["defense"]
    if h not in atk or a not in atk:
        return None
    lam = math.exp(atk[h] - dfn[a] + (0.0 if neutral else p["home_advantage"]))
    mu = math.exp(atk[a] - dfn[h])
    W = D = L = 0.0
    for x in range(9):
        px = poisson_pmf(x, lam)
        for y in range(9):
            q = px * poisson_pmf(y, mu) * dc_tau(x, y, lam, mu)
            if x > y:
                W += q
            elif x == y:
                D += q
            else:
                L += q
    t = W + D + L
    return W / t, D / t, L / t


def rps(p3, res):
    o = [1 if res == "H" else 0, 1 if res == "D" else 0, 1 if res == "A" else 0]
    c1 = p3[0] - o[0]
    c2 = p3[0] + p3[1] - o[0] - o[1]
    return (c1 * c1 + c2 * c2) / 2


def main():
    rows = load_rows()
    train = [m for m in rows if m["date"] < CUTOFF]
    evalset = [m for m in rows if m["date"] >= CUTOFF and m["league"] in EVAL_LEAGUES]
    print(f"总样本 {len(rows)} 场；切分点 {CUTOFF}")
    print(f"训练集 {len(train)} 场，评测集 {len(evalset)} 场（只含一级联赛）\n")

    print("拟合两套参数（都只用 CUTOFF 之前的数据）：")
    p_without = fit([m for m in train if m["league"] not in NEW_LEAGUES], "不含德乙/法乙")
    p_with = fit(train, "含德乙/法乙   ")

    pairs = []
    only_new = 0
    for m in evalset:
        res = "H" if m["hs"] > m["as"] else ("A" if m["as"] > m["hs"] else "D")
        a = probs(p_without, m["home"], m["away"], m["neutral"])
        b = probs(p_with, m["home"], m["away"], m["neutral"])
        if b is None:
            continue
        if a is None:
            # 只有加了德乙/法乙才有参数的比赛（正是我们要修的那类）。
            # 不能计入配对比较——配对要求两边都有数，单独计数报出来。
            only_new += 1
            continue
        pairs.append((rps(a, res), rps(b, res)))

    if not pairs:
        print("\n评测集为空，无法比较")
        return
    ro = [x for x, _ in pairs]
    rn = [y for _, y in pairs]
    d = [y - x for x, y in pairs]
    n = len(d)
    se = st.stdev(d) / math.sqrt(n)
    mean_d = st.mean(d)

    print(f"\n{'='*62}")
    print(f"样本外结果（{n} 场配对，两套参数都没见过这些比赛）")
    print(f"{'='*62}")
    print(f"  不含德乙/法乙  RPS  {st.mean(ro):.5f}")
    print(f"  含德乙/法乙    RPS  {st.mean(rn):.5f}")
    print(f"  配对差              {mean_d:+.5f} ± {se:.5f}   t = {mean_d/se:+.2f}")
    verdict = ("加了更好" if mean_d < 0 else "加了更差") if abs(mean_d / se) > 2 else "没有显著差别"
    print(f"  判定：{verdict}")
    print(f"\n  另有 {only_new} 场是「只有加了德乙/法乙才算得出来」的"
          f"（旧参数查无此队，只能退回兜底），不计入上面的配对。")


if __name__ == "__main__":
    main()
