"""赛程队名核对 —— 把 The Odds API 的队名跟参数表对一遍，产出别名清单。

用法：把 The Odds API /events 返回里的队名一行一个喂给它，指定赛事 code。

    python3 validation/24_fixture_name_check.py serieb < names.txt

它做三件事：
  1. 每个名字跑一遍 normalize_team_name，再查该赛事作用域的参数表
  2. 对得上的直接报「✅ 已就绪」，不用加别名
  3. 对不上的，从**该联赛训练数据里真实出现过的球队**里找候选，按相似度
     排序，并标出每个候选的真实场次和最后一场日期

为什么候选要限定在"该联赛的球队"而不是整张参数表：项目里踩过一次
"Las Palmas" 子串命中意甲的 "Hellas Verona"（341 场）的坑——整张表里
凑巧像的名字太多，范围不收窄就会给出看着合理其实无关的建议。

**候选只是线索，必须人工确认再写进别名表。** 这个脚本不自动改任何代码。
"""
import sys
import os
import csv
import difflib
import collections

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from app.updater import normalize_team_name                          # noqa: E402
from app.model import load_params, scope_for_competition             # noqa: E402

CSV = os.path.join(HERE, "..", "training", "historical_results_club.csv")

# 赛事 code → 训练数据里的 tournament 标记
TOURNAMENT = {
    "serieb": "it.2", "bundesliga2": "de.2", "ligue2": "fr.2",
    "leagueone": "en.3", "leaguetwo": "en.4", "segunda": "es.2",
    "epl": "en.1", "laliga": "es.1", "seriea": "it.1",
    "bundesliga": "de.1", "ligue1": "fr.1", "championship": "en.2",
    "nationalleague": "en.5",
}


def league_teams(tour):
    """该联赛出现过的球队 → [场次, 最后一场日期]。"""
    stat = collections.defaultdict(lambda: [0, ""])
    with open(CSV, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["tournament"] != tour:
                continue
            for t in (r["home_team"], r["away_team"]):
                d = stat[t]
                d[0] += 1
                d[1] = max(d[1], r["date"])
    return stat


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in TOURNAMENT:
        print(f"用法: python3 {os.path.basename(__file__)} <赛事code> < names.txt")
        print(f"可用: {', '.join(sorted(TOURNAMENT))}")
        raise SystemExit(2)
    code = sys.argv[1]
    tour = TOURNAMENT[code]

    names = [ln.strip() for ln in sys.stdin if ln.strip()]
    names = list(dict.fromkeys(names))               # 去重但保序
    if not names:
        print("没有读到任何队名（从标准输入一行一个喂进来）")
        raise SystemExit(2)

    known = set(load_params(scope_for_competition(code))["_index"].keys())
    stat = league_teams(tour)
    pool = sorted(stat)

    ok, bad = [], []
    for raw in names:
        norm = normalize_team_name(raw)
        (ok if norm.strip().lower() in known else bad).append((raw, norm))

    print(f"赛事 {code}（训练数据 {tour}，该联赛历史 {len(pool)} 支球队）")
    print(f"输入 {len(names)} 个队名：对得上 {len(ok)}，对不上 {len(bad)}\n")

    if ok:
        print("── 已就绪（不用加别名）──")
        for raw, norm in ok:
            n, last = stat.get(norm, [0, "—"])
            extra = f"   归一化→{norm}" if norm != raw else ""
            print(f"  ✅ {raw:32s} {n:4d} 场  最后 {last or '—'}{extra}")

    if bad:
        print(f"\n── 对不上，需要加别名（{len(bad)} 个）──")
        for raw, norm in bad:
            cands = difflib.get_close_matches(norm, pool, n=4, cutoff=0.45)
            print(f"  ❌ {raw!r}   归一化→{norm!r}")
            if cands:
                for c in cands:
                    n, last = stat[c]
                    print(f"        候选: {c:34s} {n:4d} 场  最后 {last}")
            else:
                print("        该联赛训练数据里找不到相近的——可能是升班马/新队，"
                      "真的没有历史数据，加别名也没用")
        print("\n把确认无误的写进 updater.py 的 _CLUB_NAME_ALIASES，格式：")
        print('    "The Odds API 的写法": "参数表里的规范名",   # N 场')
    else:
        print("\n🎉 全部对得上，不需要任何别名——这个赛事的赛程可以直接用")


if __name__ == "__main__":
    main()
