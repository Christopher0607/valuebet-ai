"""
俱乐部联赛 MLE 训练 —— 六个联赛 + 欧冠，训练成**一张**共享参数表。

为什么不是每个联赛一张表（这是原计划，跑通数据后发现是错的）：
    MLE 拟合的 attack/defense 只在同一个训练池内部可比。数学上，给某联赛
    所有球队的 attack 和 defense 同时加一个常数 c，league 内部任何一场比赛的
    预测都不变——存在一个无法识别的"联赛整体偏移量"。
    后果：曼城在英超表里 attack=1.5、皇马在西甲表里 attack=1.5，这两个数字
    不能直接比较。欧冠是曼城打皇马，分开训练根本算不了。

    把四大联赛 + 欧冠一起训练就解决了：欧冠比赛是连接各联赛的"桥"，只有
    这些跨联赛对局能把不同联赛的强弱锚定到同一把尺子上。已验证欧冠队名
    去掉国家后缀后能跟联赛队名对上（Arsenal / Liverpool / Real Madrid CF /
    FC Barcelona 等都匹配），所以桥是真的连得上，不是理论上的。

数据源与覆盖（都逐个 HTTP 验证过，不是假设）：
    英超/西甲/意甲/德甲: 2015-16 ~ 2025-26，11 个赛季，每季 380/306 场
    欧冠: 2014-15 ~ 2019-20 + 2024-25，共 7 个赛季 933 场
          （2020-21 ~ 2023-24 这四季数据源没有，是真实断档，不是抓取失败）

复用已验证的组件，不重写数学：
    train_mle.py 的向量化似然 + 解析梯度（有数值梯度交叉校验）
    updater.py 的 normalize_team_name（含 20 条跨赛季队名别名表）
"""
import sys
import os
import csv
import re
import json
import requests
from datetime import date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app.updater import normalize_team_name, _extract_final_score  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from train_mle import load_matches, build_team_index, fit_parameters  # noqa: E402


BASE = "https://raw.githubusercontent.com/openfootball/football.json/master"

DOMESTIC_LEAGUES = {
    "en.1": "英超", "es.1": "西甲", "it.1": "意甲", "de.1": "德甲",
    # 后加的两个。并进同一张表的前提跟上面四个一样：必须有真实的跨联赛
    # 对局把尺子锚住。两者的桥不同，都是实测数出来的，不是假设：
    #
    #   法甲 fr.1 —— 靠欧冠。11 季 3,856 场 33 支球队，其中 5 支
    #     （巴黎/摩纳哥/里昂/里尔/布雷斯特）出现在欧冠球队池里。桥偏细：
    #     能进欧冠的都是法甲最强那几支，法甲整体水位是「先把这 5 支锚到
    #     欧冠尺子上、其余再通过国内对局相对定位」两段传导来的。
    #
    #   英冠 en.2 —— 靠升降级共享球队，不需要欧冠。11 季 4,993 场 47 支
    #     球队，其中 **24 支**同时出现在英超数据里（莱斯特/利兹/诺维奇/
    #     南安普顿…）。同一支球队在两个级别都有比赛、共用同一组
    #     attack/defense，这是最直接的桥，而且强弱跨度大，不是只锚顶端。
    "fr.1": "法甲", "en.2": "英冠",
}
DOMESTIC_SEASONS = [
    "2015-16", "2016-17", "2017-18", "2018-19", "2019-20",
    "2020-21", "2021-22", "2022-23", "2023-24", "2024-25", "2025-26",
]
# 欧冠可用赛季 —— 逐个 HEAD 请求验证过，2020-21~2023-24 四季数据源确实没有
UCL_SEASONS = [
    "2014-15", "2015-16", "2016-17", "2017-18", "2018-19", "2019-20", "2024-25",
]

OUT_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "historical_results_club.csv")
OUT_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fitted_parameters_club.json")


def strip_country_suffix(name: str) -> str:
    """
    欧冠数据的队名带国家后缀：'Aston Villa FC (ENG)'、'Juventus FC (ITA)'。
    不去掉的话，同一支球队在欧冠和联赛里会被当成两支不同的队，跨联赛的桥
    就断了——这正是这套合并训练要解决的问题，所以这一步不能省。
    """
    return re.sub(r"\s*\([A-Z]{3}\)\s*$", "", name).strip()


def fetch(url: str):
    r = requests.get(url, timeout=20)
    if r.status_code != 200:
        return None
    return r.json()


def collect_domestic() -> list:
    rows = []
    for code, name_zh in DOMESTIC_LEAGUES.items():
        got = 0
        for season in DOMESTIC_SEASONS:
            data = fetch(f"{BASE}/{season}/{code}.json")
            if not data:
                continue
            for m in data.get("matches", []):
                score = _extract_final_score(m.get("score"))
                if score is None:
                    continue
                rows.append({
                    "date": m["date"],
                    "home_team": normalize_team_name(m["team1"]),
                    "away_team": normalize_team_name(m["team2"]),
                    "home_score": score[0], "away_score": score[1],
                    "tournament": code,
                    "neutral": "FALSE",     # 联赛都在主队主场
                })
                got += 1
        print(f"   {name_zh:4s} ({code}): {got} 场")
    return rows


def collect_ucl() -> list:
    rows = []
    total = 0
    for season in UCL_SEASONS:
        data = fetch(f"{BASE}/{season}/uefa.cl.json")
        if not data:
            print(f"   欧冠 {season}: 抓取失败，跳过")
            continue
        n = 0
        for m in data.get("matches", []):
            score = _extract_final_score(m.get("score"))
            if score is None:
                continue
            rnd = m.get("round", "")
            # 决赛在中立球场，其余（小组赛/联赛阶段/淘汰赛主客场两回合）都有主场优势
            is_final = "Final" in rnd and "Semifinal" not in rnd and "Quarterfinal" not in rnd
            rows.append({
                "date": m["date"],
                "home_team": normalize_team_name(strip_country_suffix(m["team1"])),
                "away_team": normalize_team_name(strip_country_suffix(m["team2"])),
                "home_score": score[0], "away_score": score[1],
                "tournament": "uefa.cl",
                "neutral": "TRUE" if is_final else "FALSE",
            })
            n += 1
        total += n
        print(f"   欧冠 {season}: {n} 场")
    print(f"   欧冠合计: {total} 场（这些是连接各联赛的跨联赛对局）")
    return rows


def build_csv():
    print("📥 抓取各国联赛...")
    domestic = collect_domestic()
    print("\n📥 抓取欧冠（跨联赛校准用）...")
    ucl = collect_ucl()

    all_rows = domestic + ucl
    all_rows.sort(key=lambda r: r["date"])

    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "home_team", "away_team",
                                          "home_score", "away_score", "tournament", "neutral"])
        w.writeheader()
        w.writerows(all_rows)

    print(f"\n💾 合并 {len(all_rows)} 场比赛 → {OUT_CSV}")
    return len(all_rows)


def train():
    n_rows = build_csv()
    if n_rows < 500:
        raise RuntimeError(f"只抓到 {n_rows} 场，数据量不足以训练，中止")

    print("\n📊 拟合俱乐部参数（四大联赛 + 欧冠，共享一把尺子）...")
    matches = load_matches(csv_path=OUT_CSV, start_date=date(2014, 1, 1))
    print(f"   {len(matches)} 场纳入训练")

    team_idx, counts = build_team_index(matches, min_matches=10)
    print(f"   {len(team_idx)} 支球队达标（≥10场），{len(counts) - len(team_idx)} 支因场次太少被排除")

    attack, defense, home_adv, nll = fit_parameters(matches, team_idx)
    print(f"\n   拟合完成，主场优势 = {home_adv:.4f}")

    print("\n🏆 进攻力前12（跨联赛可比，这正是合并训练的意义）:")
    for t, v in sorted(attack.items(), key=lambda x: -x[1])[:12]:
        print(f"   {t:32s} attack={v:+.3f}  defense={defense[t]:+.3f}")

    print("\n🛡️ 防守力前12（数值越高防守越强）:")
    for t, v in sorted(defense.items(), key=lambda x: -x[1])[:12]:
        print(f"   {t:32s} attack={attack[t]:+.3f}  defense={v:+.3f}")

    out = {
        "scope": "club",
        "note": "六个联赛（英超/西甲/意甲/德甲/法甲/英冠）+欧冠合并训练的共享参数表。跨联赛可比。桥有两种：欧冠是真正的跨国对局；英冠靠升降级共享球队跟英超相连。",
        "trained_at": datetime.now().isoformat(),
        "domestic_leagues": list(DOMESTIC_LEAGUES.keys()),
        "domestic_seasons": DOMESTIC_SEASONS,
        "ucl_seasons": UCL_SEASONS,
        "n_matches_used": len(matches),
        "n_teams_fitted": len(team_idx),
        "home_advantage": round(home_adv, 4),
        "final_neg_log_likelihood": round(nll, 2),
        "attack": {k: round(v, 4) for k, v in attack.items()},
        "defense": {k: round(v, 4) for k, v in defense.items()},
    }
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\n💾 参数已保存 → {OUT_JSON}")
    return out


if __name__ == "__main__":
    train()
