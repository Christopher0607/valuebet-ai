# 05 · 未完成的工作与下一步

按优先级排序。每项写清楚：做什么、为什么这么排、怎么做。

---

## P0 · 恢复丢失的工作（逻辑已验证过，只是代码需要重敲）

### P0.1 修复 backtest-summary 的赛事隔离

**为什么排第一**：这是加任何新联赛之前必须先修的地基。不修的话，第二个赛事的数据一进来，回测统计立刻变得毫无意义。

**改 `backend/app/main.py` 的 `backtest_summary`**：

```python
@app.get("/api/backtest-summary")
def backtest_summary(competition_id: Optional[int] = None, db: Session = Depends(get_db)):
    if competition_id is not None:
        # 返回该赛事的扁平统计（向后兼容旧格式）
        preds = db.query(Prediction).join(Match).filter(
            Match.status == "played", Match.competition_id == competition_id
        ).all()
        # ... 算 total / correct / accuracy / avg_rps
        return {"competition_id": ..., "total": ..., ...}

    # 不传 competition_id：返回按赛事拆分的数组，绝不做跨赛事聚合
    by_competition = []
    for comp in db.query(Competition).all():
        preds = db.query(Prediction).join(Match).filter(
            Match.status == "played", Match.competition_id == comp.id
        ).all()
        if not preds:
            continue  # 跳过还没有比赛的赛事
        by_competition.append({
            "competition_id": comp.id, "competition_code": comp.code,
            "competition_name": comp.name_zh or comp.name,
            "total": ..., "correct": ..., "accuracy": ..., "avg_rps": ...,
        })
    return {"by_competition": by_competition, "random_baseline_rps": 0.245}
```

**前端 `frontend/src/App.jsx` 同步改**：
- `loadAll()` 里 `setBacktest(bt)` 改成 `setBacktestByComp(bt.by_competition || [])`
- 顶部统计栏取 `backtestByComp[0]`（目前只有世界杯，视觉不变）
- 回测 Tab 改成 `backtestByComp.map(bc => ...)` 循环渲染，每个赛事一块统计 + 一张比赛明细表，明细表用 `played.filter(m => m.competition_id === bc.competition_id)` 筛选

### P0.2 重建俱乐部联赛训练脚本 + 训练英超

见 `06_CLUB_LEAGUE_RESEARCH.md`，那里有完整的脚本设计和已验证的参数。

**训练完必须验证**：梯度校验是否通过、排名是否符合直觉（英超应该看到曼城/利物浦/阿森纳在进攻力前列）、主场优势数值（上次是 0.1826，重跑应该很接近）。

### P0.3 重新训练西甲并验证皇马不再重复

训练完**专门检查**：`fitted_parameters_laliga.json` 的 `attack` 字典 key 里，"Real Madrid" 只出现一次（不能同时有 "Real Madrid" 和 "Real Madrid CF"），"Atlético Madrid" 只以 "Club Atlético de Madrid" 出现。

建议写个检查脚本：拿 `_CLUB_NAME_ALIASES` 的所有 key（那些"应该被合并掉的旧名字"）去查训练结果，确认它们都不在最终参数表里。

### P0.4 训练意甲、德甲

同一套脚本换 `league_key` 参数。验证排名直觉：意甲应该尤文/国米/AC米兰/那不勒斯靠前，德甲应该拜仁/多特/勒沃库森靠前。

---

## P1 · 让俱乐部联赛真正跑起来（全新工作，零代码）

### P1.1 让 model.py 支持多套参数表 ← **最核心的架构缺口**

**现状**：`get_mle_params(name)` 只读一个全局的 `_FITTED`（国家队参数表），不知道"这场比赛属于哪个赛事"。

**设计建议**（这是方向，不是验证过的代码，需要自己设计并测试）：

```python
# 建议：加一个带缓存的参数表加载器
_PARAM_CACHE = {}

def _load_params_for(competition_code: str) -> dict:
    """competition_code -> 对应的 fitted_parameters 文件"""
    if competition_code in _PARAM_CACHE:
        return _PARAM_CACHE[competition_code]
    filename = {
        "wc2026": "fitted_parameters.json",      # 国家队
        "epl": "fitted_parameters_epl.json",
        "laliga": "fitted_parameters_laliga.json",
        # ...
    }.get(competition_code, "fitted_parameters.json")
    # 加载、缓存、返回
```

然后 `get_mle_params()`、`dixon_coles()`、`score_distribution()` 都要能接受 `competition_code`（或者更干净的做法：由调用方查好 attack/defense 字典直接传进来，让 model.py 保持纯函数、不碰文件 IO）。

`updater.py` 的 `update_predictions()` 需要根据 `Match.competition_id` 查出 `Competition.code`，再决定传哪套参数。

**建议先写单元测试再动手**：比如"同一对球队名，用英超参数和用国家队参数算出的概率应该不同"，避免像之前 MLE 整合时那样反复出现字段不对齐的问题。

### P1.2 贝叶斯种子初始化跟着适配

`updater.py` 里球队首次出现时，调用 `get_mle_params()` 拿 MLE 点估计当贝叶斯先验种子。P1.1 改完后这里也要传对 `competition_code`，否则英超球队会拿到国家队参数（或兜底的 0.0）当种子。

### P1.3 competitions 表加入四个联赛

参照 `main.py` 里 `_seed_default_competition()` 的写法插入：

```python
{"code": "epl", "name": "English Premier League", "name_zh": "英超",
 "data_source": "https://raw.githubusercontent.com/openfootball/football.json/master/{season}/en.1.json",
 "is_active": True},
# laliga -> es.1, seriea -> it.1, bundesliga -> de.1
```

`{season}` 占位符会被 `_resolve_data_source()` 自动处理（已验证可用）。

**⚠️ 重要**：在 P1.1 完成之前**不要激活**这些赛事。否则比赛会入库，但预测用错误的（国家队）参数计算，产生一堆无意义数据混进数据库。

### P1.4 抽水率（vig%）展示

纯展示功能，**不进 EV 公式**（原因见 `02_ARCHITECTURE.md` 第 1 条）。

```python
overround = 1/odds_home + 1/odds_draw + 1/odds_away
vig_pct = (overround - 1) * 100
```

在 `/api/odds` 的返回值里加 `vig_pct` 字段，前端在 EV 旁边展示。

### P1.5 端到端验证

P1.1-P1.4 都做完后，先只激活英超，真实跑一次 `update-now`，确认：
- 英超比赛正确入库（`matches` 表有对应 `competition_id` 的记录）
- **预测真的用了英超参数**——挑一场强弱悬殊的比赛（如曼城 vs 保级队），看预测概率是否合理偏向强队。如果接近 50-50，说明没取到参数表，还在用兜底值
- `/api/backtest-summary` 不传参数时，`by_competition` 数组里能看到世界杯和英超两个独立统计块
- 前端正常展示不报错

---

## P2 · 更远期

### P2.1 主场优势真正启用
联赛训练出的 `home_advantage`（英超 0.1826）至今从未生效——世界杯是中立场地，`neutral` 恒为 `True`。P1.1 改完后要确保联赛比赛调 `dixon_coles()` 时传 `neutral=False`。

### P2.2 欧冠
**用户明确表示接下来想做**，理由是欧冠强弱悬殊对局多，更容易出现模型和市场判断分歧大的机会——这个逻辑跟串关推荐的正 EV 筛选设计是自洽的。

但**零调研**：不知道数据源在哪、赛制（小组赛+淘汰赛混合）需要什么数据结构调整。需要像英超那样从数据源调研开始做一遍。

### P2.3 MLS
数据源格式（TXT）完全没验证过。不建议在验证清楚可靠性之前投入开发。

### P2.4 赔率自动抓取
现在全靠手动输入。The Odds API 的 key 在 `03_DATA_SOURCES.md` 里，但注意免费额度每月约 500 次请求。

### P2.5 数据库效率
`update_predictions()` 目前每场比赛查两次数据库（主客队各一次贝叶斯状态）。世界杯规模（~100 场）没问题，四个联赛加起来上千场可能需要改成批量预加载（参考 `update_bayesian_states_for_newly_played_matches()` 的做法）。

---

## 统一提醒

以上每一项，**不要假设"设计已经想清楚了，照着实现就行"**——尤其 P1.1，那是从未写过一行代码的全新架构工作，上面写的只是方向建议。

整个项目最有价值的经验：几乎每一个看起来"显然对"的设计，真正跑起来测试后都发现过至少一个意料之外的问题。请务必用真实运行验证，不要只做语法检查。
