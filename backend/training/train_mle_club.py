"""
俱乐部联赛 MLE 训练 —— 十个联赛 + 欧冠，训练成**一张**共享参数表。

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
from app.updater import (normalize_team_name, _extract_final_score,  # noqa: E402
                         parse_openfootball_txt)

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
    # 第二批：英甲/英乙/西乙。桥是**阶梯式**的，实测共享球队数：
    #   英甲 en.3 ∩ 英冠 = 24 支      英乙 en.4 ∩ 英甲 = 34 支(67%)
    #   西乙 es.2 ∩ 现有池 = 21 支(40%，全是西甲升降级队)
    # 注意英乙单独看跟现有池只共享 7 支（14%，太细），必须跟英甲**一起**
    # 加进来才成立——它是通过英甲这一级连上英冠、再连上英超的。
    # 所以这三个要么一起加，要么都不加，不能只挑英乙。
    #
    # 赛季覆盖有真实断档（逐个 HTTP 验过，不是抓取失败）：
    #   en.3/en.4 只有 2015,2018,2019,2020,2024,2025 六季
    #   es.2 有 2015-2020 + 2024,2025 八季
    # collect_domestic 对缺失赛季是静默跳过，所以断档不影响训练。
    "en.3": "英甲", "en.4": "英乙", "es.2": "西乙",
}
DOMESTIC_SEASONS = [
    "2015-16", "2016-17", "2017-18", "2018-19", "2019-20",
    "2020-21", "2021-22", "2022-23", "2023-24", "2024-25", "2025-26",
]
# 欧冠可用赛季 —— 逐个 HEAD 请求验证过，2020-21~2023-24 四季数据源确实没有
UCL_SEASONS = [
    "2014-15", "2015-16", "2016-17", "2017-18", "2018-19", "2019-20", "2024-25",
]

# 只有 .txt 源、没有 .json 镜像的联赛。football.json 里 en.5 全部赛季 404
# （逐季验过），只能直接读 openfootball/england 的 .txt。
#
# 这批文件有个坑：已完赛的旧赛季会整份省掉年份（首个日期行写 "Sat Aug 6"
# 而不是 "Sat Aug 6 2022"）。以前 parse_openfootball_txt 遇到这种会一路
# 跳过、解析出 0 场且不报错。现在传 season_start_year 兜底，实测
# 2019-20~2023-24 五季从 0 场恢复到 451/462/506/552/552 场。
TXT_LEAGUES = {
    "en.5": ("英格兰全国联赛",
             "https://raw.githubusercontent.com/openfootball/england/master/{season}/5-nationalleague.txt",
             ["2019-20", "2020-21", "2021-22", "2022-23", "2023-24", "2024-25", "2025-26"]),
    # 德乙/法乙。加它们不是为了「多几个联赛」，是为了补两个具体的洞：
    # 2026-27 德甲升班马 SV 07 Elversberg 和法甲升班马 Le Mans，在只训练
    # 一级联赛的参数表里**一场数据都没有**，预测直接退回 (0,0) 兜底，
    # 前端标 data_backing=none。实测这两支球队牵连 68 场 2026-27 赛程。
    #
    # 桥（升降级共享球队）实测数出来的，不是假设：
    #   德乙 ∩ 德甲 = 18 支（科隆/沙尔克/纽伦堡/杜塞尔多夫/比勒费尔德…）
    #   法乙 ∩ 法甲 = 24 支（欧塞尔/圣埃蒂安/亚眠/昂热/克莱蒙…）
    # 比法甲当初靠欧冠那 5 支桥结实得多，跨度也大（不是只锚顶端）。
    #
    # 赛季范围是逐个 HTTP 验过的真实覆盖，不是照着一级联赛抄的：
    #   德乙 2-bundesliga2.txt 从 2012-13 才有（2010-11/2011-12 是 404）
    #   法乙 {season}_fr2.txt 从 2014-15 才有
    # 目录结构两边不一样（德国按赛季目录，法国是国家目录+文件名前缀），
    # 见 updater.py 里 fr.1 那条注释踩过的坑。
    "de.2": ("德乙",
             "https://raw.githubusercontent.com/openfootball/deutschland/master/{season}/2-bundesliga2.txt",
             ["2012-13", "2013-14", "2014-15", "2015-16", "2016-17", "2017-18", "2018-19",
              "2019-20", "2020-21", "2021-22", "2022-23", "2023-24", "2024-25", "2025-26"]),
    "fr.2": ("法乙",
             "https://raw.githubusercontent.com/openfootball/france/master/france/{season}_fr2.txt",
             ["2014-15", "2015-16", "2016-17", "2017-18", "2018-19", "2019-20",
              "2020-21", "2021-22", "2022-23", "2023-24", "2024-25", "2025-26"]),
}

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


def collect_txt_leagues() -> list:
    """读只有 .txt 源的联赛（全国联赛/德乙/法乙）。"""
    rows = []
    for code, (label, template, seasons) in TXT_LEAGUES.items():
        got = 0
        for season in seasons:
            r = requests.get(template.replace("{season}", season), timeout=25)
            if r.status_code != 200:
                continue
            r.encoding = "utf-8"    # 同 updater：别让 requests 猜成 ISO-8859-1
            for m in parse_openfootball_txt(r.text, season_start_year=int(season[:4])):
                score = _extract_final_score(m.get("score"))
                if score is None:
                    continue
                rows.append({
                    "date": m["date"],
                    "home_team": normalize_team_name(m["team1"]),
                    "away_team": normalize_team_name(m["team2"]),
                    "home_score": score[0], "away_score": score[1],
                    "tournament": code,
                    "neutral": "FALSE",
                })
                got += 1
        print(f"   {label} ({code}): {got} 场")
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
    print("\n📥 抓取只有 .txt 源的联赛...")
    domestic += collect_txt_leagues()
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
        "note": "九个联赛（英超/英冠/英甲/英乙/西甲/西乙/意甲/德甲/法甲）+欧冠合并训练的共享参数表。跨联赛可比。桥有两种：欧冠提供跨国对局；英格兰四级和西班牙两级靠升降级共享球队逐级相连（英超↔英冠24支↔英甲24支↔英乙34支）。",
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
