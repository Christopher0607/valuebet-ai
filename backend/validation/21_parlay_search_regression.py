"""串关搜索优化 —— 逐字节回归测试 + 性能对比。

改动内容：suggest_parlays 的热循环原来对**每一个**正EV组合都当场构造完整
的嵌套 dict、算 kelly、找最弱腿、做 6 次 round，然后把十几万个 dict 全排序，
而最终只有 5 个会被返回。现在循环里只留排序需要的三个标量，重的部分推迟
到选出赢家之后。

风险在于「顺序变了 → 并列时选中的组合变了 → 推荐结果变了」。所以这个测试
不看指标、不看近似，直接把新旧两版的完整返回值做 JSON 逐字节比对。

旧版实现在下面 legacy_suggest_parlays 里原样保留（从 git 历史抄下来的，
除了改名什么都没动），这样比对是真的在跟改动前的行为比，不是跟我记忆里
的行为比。
"""
import sys
import os
import json
import time
import random
import itertools

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from app.model import (suggest_parlays, build_candidate_legs,          # noqa: E402
                       parlay_joint_probability, expected_value, kelly_pct,
                       MAX_CANDIDATE_LEGS_FOR_SEARCH)


def legacy_suggest_parlays(match_odds_list, min_legs, max_legs, fraction, cap, top_n=5):
    """改动之前的原版实现，一字未改，只改了函数名。"""
    candidates = build_candidate_legs(match_odds_list)
    if len(candidates) < min_legs:
        return {
            "status": "insufficient_candidates",
            "detail": f"当前输入的比赛中，只有 {len(candidates)} 条正EV的单腿选项，"
                      f"不足以组成 {min_legs} 腿的组合。这通常说明：要么这批比赛的"
                      f"赔率本身定价已经很有效率（市场没有明显低估任何一方），"
                      f"要么强队的赔率确实被压得过低、达不到正EV门槛——这正是"
                      f"这个工具设计要主动暴露给你看的情况，而不是硬凑一注出来。",
            "n_candidates": len(candidates),
            "candidates": candidates,
            "combinations": [],
        }

    candidates_sorted_by_ev = sorted(candidates, key=lambda c: -c["leg_ev"])
    search_pool = candidates_sorted_by_ev[:MAX_CANDIDATE_LEGS_FOR_SEARCH]
    pool_truncated = len(candidates) > MAX_CANDIDATE_LEGS_FOR_SEARCH

    all_combos = []
    for k in range(min_legs, max_legs + 1):
        for combo in itertools.combinations(search_pool, k):
            match_ids = [leg["match_id"] for leg in combo]
            if len(set(match_ids)) != len(match_ids):
                continue
            legs = list(combo)
            joint_prob = parlay_joint_probability([leg["prob"] for leg in legs])
            combined_odds = 1.0
            for leg in legs:
                combined_odds *= leg["odds"]
            combo_ev = expected_value(joint_prob, combined_odds)
            if combo_ev <= 0:
                continue
            combo_kelly = kelly_pct(joint_prob, combined_odds, fraction, cap)
            weakest = min(legs, key=lambda l: l["prob"])
            all_combos.append({
                "legs": [{"label": l["label"], "outcome": l["outcome"], "odds": l["odds"],
                          "prob": l["prob"], "match_id": l["match_id"]} for l in legs],
                "n_legs": k,
                "joint_probability": round(joint_prob, 4),
                "combined_odds": round(combined_odds, 3),
                "ev": round(combo_ev, 4),
                "kelly_pct": round(combo_kelly, 4),
                "weakest_leg_label": weakest["label"],
                "weakest_leg_match_id": weakest["match_id"],
                "weakest_leg_outcome": weakest["outcome"],
                "weakest_leg_prob": weakest["prob"],
                "risk_ratio_vs_weakest_leg": round(joint_prob / weakest["prob"], 3) if weakest["prob"] > 0 else 0,
            })

    if not all_combos:
        return {
            "status": "no_positive_ev_combination",
            "detail": f"从 {len(search_pool)} 条正EV单腿里尝试了 {min_legs}-{max_legs} 腿的"
                      f"所有组合，没有找到整体EV为正的组合。单腿正EV不代表串起来还是正EV——"
                      f"多条腿的联合概率是相乘关系，衰减速度往往比赔率相乘的增速更快。",
            "n_candidates": len(candidates),
            "candidates": candidates_sorted_by_ev,
            "combinations": [],
        }

    by_ev = sorted(all_combos, key=lambda c: -c["ev"])
    by_probability = sorted(all_combos, key=lambda c: -c["joint_probability"])
    by_odds = sorted(all_combos, key=lambda c: -c["combined_odds"])

    def dedupe_add(target_list, combo, seen_signatures):
        sig = tuple(sorted(leg["match_id"] for leg in combo["legs"]))
        if sig in seen_signatures:
            return
        seen_signatures.add(sig)
        target_list.append(combo)

    seen = set()
    recommendations = []
    dedupe_add(recommendations, by_ev[0], seen)
    if len(by_probability) and by_probability[0] not in recommendations:
        dedupe_add(recommendations, by_probability[0], seen)
    if len(by_odds) and by_odds[0] not in recommendations:
        dedupe_add(recommendations, by_odds[0], seen)
    for combo in by_ev:
        if len(recommendations) >= top_n:
            break
        dedupe_add(recommendations, combo, seen)

    for i, combo in enumerate(recommendations):
        if combo is by_ev[0]:
            combo["tag"] = "🏆 最高EV"
        elif combo is by_probability[0]:
            combo["tag"] = "🛡️ 最稳（命中率最高）"
        elif combo is by_odds[0]:
            combo["tag"] = "🎯 赔率最高"
        else:
            combo["tag"] = None

    return {
        "status": "ok",
        "n_candidates": len(candidates),
        "pool_truncated": pool_truncated,
        "n_combinations_evaluated": len(all_combos),
        "candidates": candidates_sorted_by_ev,
        "combinations": recommendations,
    }


def make_matches(n, seed, margin, identical_odds=False):
    """造一批比赛。identical_odds=True 是**故意**制造大量并列——
    并列正是「排序稳定性变了就会露馅」的地方，必须专门测。"""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        ph = rng.uniform(0.20, 0.65)
        pd_ = rng.uniform(0.18, 0.32)
        pa = max(0.05, 1 - ph - pd_)
        s = ph + pd_ + pa
        ph, pd_, pa = ph / s, pd_ / s, pa / s
        if identical_odds:
            oh, od, oa = 2.40, 3.50, 3.00
        else:
            oh, od, oa = (round(1 / ph * margin, 2), round(1 / pd_ * margin, 2),
                          round(1 / pa * margin, 2))
        out.append({"match_id": i, "team1": f"主队{i}", "team2": f"客队{i}",
                    "prob_home": ph, "prob_draw": pd_, "prob_away": pa,
                    "odds_home": oh, "odds_draw": od, "odds_away": oa})
    return out


def main():
    # margin<1.0 会让所有腿都是负EV，专门覆盖 insufficient_candidates 分支
    cases = []
    for n in (3, 5, 10, 20, 40, 60):
        for margin in (0.85, 0.95, 1.02, 1.08, 1.25):
            for seed in (1, 7, 99):
                for ident in (False, True):
                    for (lo, hi) in ((2, 3), (2, 5), (3, 8), (2, 8)):
                        cases.append((n, margin, seed, ident, lo, hi))

    print(f"共 {len(cases)} 个用例，逐个把新旧两版的完整返回做 JSON 逐字节比对")
    print("（旧版实现本身很慢，全跑一遍要二十分钟左右，所以下面每 60 个报一次进度）\n",
          flush=True)
    mismatch = []
    t_old = t_new = 0.0
    statuses = {}
    for done, (n, margin, seed, ident, lo, hi) in enumerate(cases, 1):
        if done % 60 == 0:
            print(f"   ...{done}/{len(cases)}，目前不一致 {len(mismatch)} 个", flush=True)
        ms = make_matches(n, seed, margin, ident)
        t = time.time()
        old = legacy_suggest_parlays(ms, lo, hi, 0.25, 0.05, top_n=5)
        t_old += time.time() - t
        t = time.time()
        new = suggest_parlays(ms, lo, hi, 0.25, 0.05, top_n=5)
        t_new += time.time() - t
        statuses[old["status"]] = statuses.get(old["status"], 0) + 1
        so = json.dumps(old, ensure_ascii=False, sort_keys=True)
        sn = json.dumps(new, ensure_ascii=False, sort_keys=True)
        if so != sn:
            mismatch.append((n, margin, seed, ident, lo, hi))

    print("覆盖到的返回状态：", statuses)
    print(f"\n不一致的用例: {len(mismatch)}")
    if mismatch:
        print("  前 5 个:", mismatch[:5])
        print("\n❌ 回归测试未通过——改动改变了推荐结果，不能上线")
        sys.exit(1)
    print("✅ 全部逐字节一致：推荐结果、排序、并列先后、标签都没变\n")
    print(f"性能（{len(cases)} 个用例累计）：旧 {t_old:.1f}s → 新 {t_new:.1f}s   "
          f"提速 {t_old/t_new:.1f}×")

    print("\n单个最坏情况（60 场输入、2-8 腿）：")
    ms = make_matches(60, 7, 1.08)
    t = time.time(); legacy_suggest_parlays(ms, 2, 8, 0.25, 0.05); a = time.time() - t
    t = time.time(); suggest_parlays(ms, 2, 8, 0.25, 0.05); b = time.time() - t
    print(f"  旧 {a:.2f}s → 新 {b:.2f}s   提速 {a/b:.1f}×")


if __name__ == "__main__":
    main()
