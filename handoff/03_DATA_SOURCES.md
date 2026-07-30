# 03 · 数据源清单

包括**失败的尝试**——知道"这个源不能用、为什么"和知道"这个能用"同样重要。

---

## ✅ 系统当前在用

### 1. `openfootball/worldcup.json` — 世界杯（生产数据源）

```
https://raw.githubusercontent.com/openfootball/worldcup.json/master/2026/worldcup.json
```

- 每 12 小时自动抓取，写入 `matches` 表
- 开源、无需 API Key
- **已知坑**：`score` 字段有两种格式——`{"ft": [h,a], "ht": [h,a]}` 或者裸数组 `[h,a]`。裸数组表示的是**真实的 0-0 平局**，不是"未开赛"。曾经因为只认第一种格式，导致 26 场真实的 0-0 被误判为未开赛而漏掉。现在由 `_extract_final_score()` 统一处理两种格式

### 2. `martj42/international_results` — 国家队历史数据（MLE 训练用）

```
https://raw.githubusercontent.com/martj42/international_results/master/results.csv
```

- 49,499 场国际比赛（1872-2026），训练时用了 1990 年后的 32,372 场
- CC0 公共领域授权
- **已知坑**：混有非正式赛事（如 "Viva World Cup" 这种非 FIFA 承认的比赛），要用精确匹配 `tournament == 'FIFA World Cup'` 而不是模糊的 `'World Cup' in tournament` 来筛选

### 3. `openfootball/football.json` — 俱乐部联赛（英超/西甲/意甲/德甲）

```
https://raw.githubusercontent.com/openfootball/football.json/master/{season}/{联赛代码}.json
```

联赛代码：英超 `en.1`、西甲 `es.1`、意甲 `it.1`、德甲 `de.1`
赛季格式：`"2025-26"`（欧洲赛季跨年，8 月开始次年 5 月结束）

- **已逐个 HEAD 请求验证**：2010-11 到 2025-26 每个赛季文件都真实存在（HTTP 200）
- 一个完整赛季 380 场（20 队循环赛），可用作数据完整性检查基准
- 通过 GitHub Actions 持续更新，不是死数据
- **已知坑**：队名跨赛季不一致，必须经 `normalize_team_name()` 处理

---

## ⚠️ 提到过但从未验证

### `openfootball/world`（TXT 格式）— MLS
只确认了这个仓库存在、声称覆盖 MLS，**没有实际抓取过任何数据**。格式是 TXT 不是 JSON，跟现有解析逻辑完全不兼容。不要假设"应该也能用"。

---

## ❌ 试过、确认不可用

### `football-data.co.uk`
被网站自己的 `robots.txt` 明确禁止自动化访问。**不要尝试绕过**——这涉及是否尊重网站方明确表达的意愿，不是技术问题。

### `footballcsv/cache.footballdata`（GitHub 镜像）
**已停止维护**，最后提交 2024 年 6 月 11 日。一开始被它 README 里"每周更新两次"的描述误导，查了实际提交记录才发现停更超过一年。

**教训**：判断数据源是否活跃，要查真实的最后提交时间，不能只看文档描述。

### The Odds API（用户提供的 key：`1f888a5c0d62746498c3f7eccf7e4d1b`）
技术上可用，但**当前本地版没有接入**。项目最早期的 artifact 版本用过（浏览器端直接调用）。现在的赔率获取方式是**用户手动输入**。

如果以后要做"赔率也自动更新"，路径是在 `updater.py` 加一个 `fetch_odds()` 调用这个 API，但这部分零代码。注意免费额度有限（每月约 500 次请求）。

---

## 选型经验

1. **优先 `raw.githubusercontent.com` 上的开源数据集**——没有 robots.txt 限制、有明确授权（CC0 最理想）、能在开发环境直接验证
2. **判断活跃度看真实提交时间，不看 README 描述**
3. **同一组织不同仓库格式可能完全不同**（openfootball 名下 `worldcup.json`、`football.json`、`world` 三个仓库结构各异）
4. **任何字段格式都要拿真实数据验证**——"比分有两种格式"、"队名跨赛季不一致"这两个坑，都是把数据真的下载下来逐条检查才发现的，看文档永远发现不了
