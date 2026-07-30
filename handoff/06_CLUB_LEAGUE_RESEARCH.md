# 06 · 俱乐部联赛研究成果

**这份文档的价值**：训练脚本 `train_mle_club_league.py` 和英超西甲的参数产出文件都在沙盒重置中丢失了，但支撑它们的**调研结论完整保留在这里**。照着这份文档可以重建，不需要重新做一遍真实数据核对。

---

## 一、赛季数据可用性（已逐个 HEAD 请求验证）

`openfootball/football.json`：

- 英超（`en.1`）：2010-11 到 2025-26 **每个赛季都存在**（HTTP 200）
- 西甲（`es.1`）、意甲（`it.1`）、德甲（`de.1`）：至少 2015-16 到 2025-26 这 11 个赛季全部存在

一个完整赛季 = **380 场**（20 队循环赛），可作为数据完整性快速检查。抽查 2019-20 英超验证过。

**训练窗口选 2015-16 到 2025-26 共 11 季**。理由：俱乐部联赛数据密度远高于国际赛（一季 380 场 vs 国家队一年打不了几场），11 季足够，不需要像国家队那样回溯到 1990 年。

---

## 二、队名归一化（**这部分代码就在包里，已验证，直接复用**）

`backend/app/updater.py` 的 `normalize_team_name()`，两道处理：

**第一道**：剥离末尾 `" FC"` / `" AFC"`，但保留 "AFC Bournemouth" 这种前缀式 AFC。
**这一道单独就完全覆盖英超**——验证方式：抓 11 个历史赛季 + 当季队名集合求差集，剩下 14 个逐一核实，**全部是真的降级球队**（卡迪夫城、赫尔城、沃特福德等），零命名变体。

**第二道**：20 条显式别名表（`_CLUB_NAME_ALIASES`），处理第一道覆盖不到的情况：

```python
_CLUB_NAME_ALIASES = {
    # 西甲 9 条
    "Atlético Madrid": "Club Atlético de Madrid",
    "CD Alavés": "Deportivo Alavés",
    "RC Celta": "RC Celta de Vigo",
    "Espanyol Barcelona": "RCD Espanyol de Barcelona",
    "Rayo Vallecano": "Rayo Vallecano de Madrid",
    "Real Betis": "Real Betis Balompié",
    "Real Madrid": "Real Madrid CF",
    "Real Sociedad": "Real Sociedad de Fútbol",
    "Real Valladolid": "Real Valladolid CF",
    # 意甲 6 条
    "Atalanta": "Atalanta BC",
    "Bologna": "Bologna FC 1909",
    "Inter": "FC Internazionale Milano",
    "Lazio Roma": "SS Lazio",
    "Sassuolo Calcio": "US Sassuolo Calcio",
    "UC Sampdoria": "Sampdoria",
    # 德甲 5 条
    "1899 Hoffenheim": "TSG 1899 Hoffenheim",
    "Bayer Leverkusen": "Bayer 04 Leverkusen",
    "Bayern München": "FC Bayern München",
    "Bor. Mönchengladbach": "Borussia Mönchengladbach",
    "Werder Bremen": "SV Werder Bremen",
}
```

**重建训练脚本时直接 `from app.updater import normalize_team_name`，不要重写一份。**

### 如果以后遇到新的命名不一致，照这个流程判断

1. 抓该联赛全部历史赛季的完整队名集合 `historical`
2. 抓最新一季的队名集合 `current`
3. **先对两边都套用 `normalize_team_name()`**，再求 `historical - current` 差集（漏这步会产生大量假阳性，见 `04_BUGS_AND_LESSONS.md` 第 11 条）
4. 差集里每个名字，看是否在 `current` 里有明显对应
5. 有对应的：**优先以当季实际用的名字为准**；如果该队已不在当季联赛（降级），没有当季信号，才退回"哪个名字在历史里出现场次更多"兜底
6. 找不到对应的：大概率是真降级/解散的球队，不需处理

**⚠️ 绝对不要用自动模糊匹配**（"共享大部分文字就认为是同一队"）——"Real Madrid"、"Real Betis"、"Real Sociedad"、"Real Valladolid" 是四支**完全不同**的球队，都共享 "Real"。错误合并比漏合并更糟，因为它是静默的。

---

## 三、训练脚本设计（`train_mle_club_league.py`，需重建）

**核心原则：不重新实现任何 MLE 数学逻辑**，复用 `training/train_mle.py` 里已验证的向量化似然函数、解析梯度、`load_matches` / `build_team_index` / `fit_parameters`。只新增"抓 JSON → 转 CSV"这一段。

```python
"""
LEAGUE_CODES = {
    "epl": ("en.1", "英超"),
    "laliga": ("es.1", "西甲"),
    "seriea": ("it.1", "意甲"),
    "bundesliga": ("de.1", "德甲"),
}

SEASONS_TO_FETCH = ["2015-16", ..., "2025-26"]  # 11 个

fetch_season(联赛代码, 赛季):
    GET https://raw.githubusercontent.com/openfootball/football.json/master/{赛季}/{代码}.json
    每场比赛：
        用 updater._extract_final_score() 判断+取比分（跳过未踢的）
        用 updater.normalize_team_name() 归一化主客队名
        neutral 固定 "FALSE"  ← 联赛在主队主场，不是世界杯的中立场地
    返回该季比赛列表

build_csv(联赛key, 输出路径):
    遍历 SEASONS_TO_FETCH 调 fetch_season 汇总
    写成 train_mle.py 认得的格式：
    date,home_team,away_team,home_score,away_score,tournament,neutral

train_league(联赛key):
    build_csv 生成 historical_results_{key}.csv
    load_matches(csv_path=..., start_date=date(2015,1,1))
        ← 不用国家队那个 1990 起始日，数据本身只到 2015
    build_team_index(matches, min_matches=10)
        ← 门槛比国家队的 15 低。单赛季 38 轮容易达标，
          但降级队场次会被摊薄，10 是合理折中
    fit_parameters(matches, team_idx)
        ← 内含梯度自动校验，不通过会抛异常中止
    打印进攻/防守力排名，人工核对是否符合直觉
    保存 fitted_parameters_{key}.json
        （结构同国家队那份：attack 字典、defense 字典、
          home_advantage、n_matches_used、n_teams_fitted 等元数据）
"""
```

调用方式：

```bash
cd backend/training
python3 train_mle_club_league.py epl
python3 train_mle_club_league.py laliga
python3 train_mle_club_league.py seriea
python3 train_mle_club_league.py bundesliga
```

---

## 四、英超训练的真实结果（可用来核对重跑是否正常）

- **4180 场**比赛（11 季 × 380）
- 达到门槛（≥10 场）的球队：**34 支**
- 梯度校验：**0.000000（完全无差异）**——向量化数学和解析梯度直接复用到俱乐部数据零问题
- **排名**：曼城第一，紧随利物浦、阿森纳、切尔西、曼联、热刺——正是这 11 季英超"六强"格局，很好的直觉核对基准
- **主场优势 = 0.1826**（对比国家队数据的 0.2471）
  - 这是 `home_advantage` 第一次在非中立场地数据上被真正训练出来（世界杯 `neutral` 恒为 True，这参数从未生效过）
  - **一个未经证实的猜测**：这 11 季包含 2020-21 全程空场和 2019-20 后半程空场（疫情），缺少主场声浪可能拉低了估计值。这只是假设，不是结论

---

## 五、西甲：完整历史队名列表（40 个，归一化前）

重跑训练后可以拿这份对照，确认该合并的都合并了（40 个应收敛成 **31 支**真实球队）：

```
Athletic Club, Atlético Madrid, CA Osasuna, CD Alavés, CD Leganés,
Club Atlético de Madrid, Cádiz CF, Deportivo Alavés, Deportivo La Coruña,
Elche CF, Espanyol Barcelona, FC Barcelona, Getafe CF, Girona,
Granada CF, Levante UD, Málaga CF, RC Celta, RC Celta de Vigo,
RCD Espanyol de Barcelona, RCD Mallorca, Rayo Vallecano,
Rayo Vallecano de Madrid, Real Betis, Real Betis Balompié, Real Madrid,
Real Madrid CF, Real Oviedo, Real Sociedad, Real Sociedad de Fútbol,
Real Valladolid, Real Valladolid CF, SD Eibar, SD Huesca, Sevilla,
Sporting Gijón, UD Almería, UD Las Palmas, Valencia CF, Villarreal CF
```

注意 "Girona" 和 "Sevilla"——当季确实是 "Girona FC" / "Sevilla FC"，但**已被第一道 FC 后缀规则正确处理**，不需要进别名表。这是核查时一度以为要处理、后来确认已覆盖的例子。**加别名前先确认现有规则是否已够用。**

---

## 六、意甲 / 德甲当季名单（供核对训练结果）

### 意甲 2025-26（20 队）
```
AC Milan, AC Pisa 1909, ACF Fiorentina, AS Roma, Atalanta BC,
Bologna FC 1909, Cagliari Calcio, Como 1907, FC Internazionale Milano,
Genoa CFC, Hellas Verona FC, Juventus FC, Parma Calcio 1913, SS Lazio,
SSC Napoli, Torino FC, US Cremonese, US Lecce, US Sassuolo Calcio,
Udinese Calcio
```

### 德甲 2025-26（18 队）
```
1. FC Heidenheim 1846, 1. FC Köln, 1. FC Union Berlin, 1. FSV Mainz 05,
Bayer 04 Leverkusen, Borussia Dortmund, Borussia Mönchengladbach,
Eintracht Frankfurt, FC Augsburg, FC Bayern München, FC St. Pauli 1910,
Hamburger SV, RB Leipzig, SC Freiburg, SV Werder Bremen,
TSG 1899 Hoffenheim, VfB Stuttgart, VfL Wolfsburg
```

这两个联赛的队名核对已经做完（结论体现在上面的别名表里），但**训练脚本从未真正跑过这两个联赛**。
