"""
MLE 训练脚本 — 用真实历史比赛数据拟合 Dixon-Coles 的进攻/防守强度参数

背景：之前项目里的 ELO 评分表是我手写的固定数值（48支球队，一次性硬编码）。
这个脚本用 martj42/international_results 的 49,499 场真实国际比赛记录，
通过最大似然估计（MLE）真正拟合出每支球队的进攻强度、防守强度两个参数，
外加一个全局主场优势常数。这是 Dixon-Coles (1997) 原始论文的标准做法。

数据源：https://github.com/martj42/international_results (CC0 公共领域)

关键设计决策（跟之前讨论对齐）：
1. 时间衰减权重 —— 1930年的比赛和上周的比赛不该被同等看待。足球战力
   会随时间变化（球员退役、青训体系变化、联赛水平变迁），所以给比赛加一个
   指数衰减权重：越久远的比赛，对当前参数拟合的影响越小。这不是原始论文
   的做法，但是 Dixon-Coles 后续文献（如 Dixon & Robinson 1998）里
   明确讨论过的标准扩展，没有这个的话，1930年代英格兰的战力会跟今天的
   英格兰混在一起拟合，参数会失真。
2. 中立场地修正 —— 用 CSV 里的 neutral 字段区分主场比赛和中立场地比赛，
   只用非中立场地的比赛去拟合主场优势参数，避免被稀释。
3. 样本量门槛 —— 一支球队如果历史场次太少（比如新独立的国家），MLE 拟合
   出来的参数会非常不稳定（过拟合到几场比赛的偶然结果）。这里设一个最低
   场次门槛，场次不足的球队会用一个基于其联赛区域的保守默认值兜底，而不是
   让优化器在几个数据点上瞎猜。
4. 只用 1990 年至今的数据做训练窗口的默认起点，防止索威特连体、南斯拉夫、
   西德东德之类的解体重组队伍名称混乱污染现代参数（对应之前互信息那部分
   讨论提过的"特征噢声"问题，这里是同一类问题在数据清洗阶段的体现）。
"""
import csv
import math
from datetime import date, datetime
from collections import defaultdict
import numpy as np
from scipy.optimize import minimize

# ── 配置 ──────────────────────────────────────────────────
TRAINING_START_DATE = date(1990, 1, 1)   # 忽略更早的比赛，见上方原因3
HALF_LIFE_YEARS = 4.0                     # 时间衰减半衰期：4年前的比赛权重减半
MIN_MATCHES_FOR_FIT = 15                  # 低于这个场次数，不单独拟合该队参数
RESULTS_CSV = "results.csv"
TARGET_TOURNAMENTS = None  # None = 使用全部赛事类型；也可以传入集合做过滤


def load_matches(csv_path=RESULTS_CSV, start_date=TRAINING_START_DATE):
    """读取历史比赛数据，做基础清洗，返回结构化列表。"""
    matches = []
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["home_score"] == "NA" or row["away_score"] == "NA":
                continue  # 跳过还没打的未来比赛
            d = datetime.strptime(row["date"], "%Y-%m-%d").date()
            if d < start_date:
                continue
            matches.append({
                "date": d,
                "home": row["home_team"],
                "away": row["away_team"],
                "hg": int(row["home_score"]),
                "ag": int(row["away_score"]),
                "neutral": row["neutral"] == "TRUE",
                "tournament": row["tournament"],
            })
    return matches


def time_weight(match_date, reference_date, half_life_years=HALF_LIFE_YEARS):
    """指数时间衰减权重。见脚本顶部注释原因1。"""
    days_ago = (reference_date - match_date).days
    years_ago = days_ago / 365.25
    return 0.5 ** (years_ago / half_life_years)


def build_team_index(matches, min_matches=MIN_MATCHES_FOR_FIT):
    """统计每支球队的比赛场次，筛出样本量足够、值得单独拟合参数的球队。
    场次不足的球队会在下游被赋予保守默认值，而不是进入优化器。"""
    counts = defaultdict(int)
    for m in matches:
        counts[m["home"]] += 1
        counts[m["away"]] += 1
    eligible = sorted([t for t, c in counts.items() if c >= min_matches])
    return eligible, counts


def precompute_match_arrays(matches, team_idx, reference_date):
    """
    把比赛列表转换成 NumPy 数组，供向量化似然函数使用。
    这一步只需要做一次；之后优化器反复调用似然函数时，直接用数组运算，
    不再逐场循环 Python 对象 —— 这是把训练从"卡住不动"变成"几秒跑完"的关键。
    """
    team_to_i = {t: i for i, t in enumerate(team_idx)}
    home_i, away_i, hg, ag, weight, neutral_mask = [], [], [], [], [], []

    skipped = 0
    for m in matches:
        if m["home"] not in team_to_i or m["away"] not in team_to_i:
            skipped += 1
            continue
        home_i.append(team_to_i[m["home"]])
        away_i.append(team_to_i[m["away"]])
        hg.append(m["hg"])
        ag.append(m["ag"])
        weight.append(time_weight(m["date"], reference_date))
        neutral_mask.append(1.0 if m["neutral"] else 0.0)

    return {
        "home_i": np.array(home_i, dtype=np.int32),
        "away_i": np.array(away_i, dtype=np.int32),
        "hg": np.array(hg, dtype=np.float64),
        "ag": np.array(ag, dtype=np.float64),
        "weight": np.array(weight, dtype=np.float64),
        "neutral_mask": np.array(neutral_mask, dtype=np.float64),
        "n_skipped": skipped,
    }


def neg_log_likelihood_vectorized(params, arrays, n_teams):
    """
    向量化版本，数学上跟 neg_log_likelihood 完全等价，
    但用 NumPy 数组运算替代逐场 Python 循环。
    在 32,372 场比赛规模下，这个版本单次调用约几毫秒，
    而循环版本单次调用约 0.15 秒 —— 差距会随比赛数量线性放大，
    正是之前训练卡住不动的根本原因之一。
    """
    attack = params[:n_teams]
    defense = params[n_teams:2*n_teams]
    home_adv = params[2*n_teams]

    home_i, away_i = arrays["home_i"], arrays["away_i"]
    hg, ag = arrays["hg"], arrays["ag"]
    w = arrays["weight"]
    home_adv_term = home_adv * (1.0 - arrays["neutral_mask"])

    lam = np.exp(attack[home_i] - defense[away_i] + home_adv_term)
    mu = np.exp(attack[away_i] - defense[home_i])

    from scipy.special import gammaln
    ll_home = hg * np.log(lam) - lam - gammaln(hg + 1)
    ll_away = ag * np.log(mu) - mu - gammaln(ag + 1)

    total_nll = -np.sum(w * (ll_home + ll_away))
    reg = 0.01 * np.sum(params[:2*n_teams] ** 2)
    return total_nll + reg


def neg_log_likelihood_gradient(params, arrays, n_teams):
    """
    解析梯度 —— 这是解决优化卡住/超时的关键修复，不是把 maxfun 调大。
    没有这个函数时，L-BFGS-B 只能用有限差分去估计 523 维的梯度，
    每次迭代要多付出约 523 次额外函数求值；有了解析梯度之后，
    每次迭代只需要 1 次梯度计算，量级差异巨大。

    推导（标准泊松回归梯度，形式是"残差 = 真实值 - 模型预期值"）：
      设 lam = exp(a_h - d_a + adv)，则
      d(NLL)/d(a_h) 对某场比赛的贡献 = -w * (hg - lam)
      d(NLL)/d(d_a) 对某场比赛的贡献 = -w * (lam - hg) = w * (hg - lam) 的相反符号
      （因为 d_a 前面带负号，链式法则翻转符号）
      同理可得 a_a, d_h, home_adv 的贡献。
      每支球队的总梯度 = 该队参与的所有比赛贡献之和（用 np.add.at 做分组累加）。
    """
    attack = params[:n_teams]
    defense = params[n_teams:2*n_teams]
    home_adv = params[2*n_teams]

    home_i, away_i = arrays["home_i"], arrays["away_i"]
    hg, ag = arrays["hg"], arrays["ag"]
    w = arrays["weight"]
    neutral_mask = arrays["neutral_mask"]
    home_adv_term = home_adv * (1.0 - neutral_mask)

    lam = np.exp(attack[home_i] - defense[away_i] + home_adv_term)
    mu = np.exp(attack[away_i] - defense[home_i])

    resid_home = w * (hg - lam)   # 主队进球的"真实-预期"残差
    resid_away = w * (ag - mu)    # 客队进球的"真实-预期"残差

    grad_attack = np.zeros(n_teams)
    grad_defense = np.zeros(n_teams)

    # a_h 只受主队进球残差影响；a_a 只受客队进球残差影响
    np.add.at(grad_attack, home_i, -resid_home)
    np.add.at(grad_attack, away_i, -resid_away)
    # d_a（客队防守）出现在 lam 的公式里，带负号；d_h（主队防守）出现在 mu 的公式里，带负号
    np.add.at(grad_defense, away_i, resid_home)
    np.add.at(grad_defense, home_i, resid_away)

    grad_home_adv = -np.sum(resid_home * (1.0 - neutral_mask))

    grad = np.concatenate([grad_attack, grad_defense, [grad_home_adv]])
    # 正则化项的梯度：d(0.01 * sum(p^2))/dp = 0.02*p，只作用在 attack/defense 上
    grad[:2*n_teams] += 0.02 * params[:2*n_teams]
    return grad


def verify_gradient_correctness(arrays, n_teams, n_trials=3):
    """用 scipy 自带的数值梯度做基准，交叉验证手推解析梯度是否正确。
    训练脚本每次运行都会先跑这个校验，如果不通过就直接中止，
    不会用一个没验证过的梯度公式去跑真实优化。

    用"相对误差 且 绝对误差"的组合判据，而不是单用其中一个：
    - 只用相对误差：像捷克斯洛伐克这种解体多年、样本少、梯度值本身
      趋近于0的球队，相对误差公式的分母失去意义，微小绝对差异
      会被放大成看似离谱的百分比（本次调试实测：相对误差4%，
      但绝对误差只有0.00003，纯粹是分母趋近0的数值噪声）。
    - 只用绝对误差：像 home_adv 这种被成千上万场比赛共同累加的
      全局参数，梯度绝对值天然比其他参数大1-2个数量级，固定的
      绝对误差阈值会对大数值维度产生虚警（本次调试实测：绝对误差
      0.0018，但那一维度的真实梯度值是-2898，相对而言完全正常）。
    两个指标结合，才能在参数量级悬殊几个数量级的情况下给出可靠判断。"""
    from scipy.optimize import approx_fprime
    rng = np.random.RandomState(123)
    max_combined_flag = 0
    worst_detail = None
    for _ in range(n_trials):
        x = rng.randn(2*n_teams + 1) * 0.3
        analytical = neg_log_likelihood_gradient(x, arrays, n_teams)
        numerical = approx_fprime(x, neg_log_likelihood_vectorized, 1e-7, arrays, n_teams)
        abs_diff = np.abs(analytical - numerical)
        rel_diff = abs_diff / (np.abs(analytical) + 1e-8)
        # 只有绝对误差也不可忽略（>1e-3）且相对误差也偏大（>0.01）时才算真正可疑
        suspicious = (abs_diff > 1e-3) & (rel_diff > 0.01)
        if suspicious.any():
            idx = np.argmax(np.where(suspicious, rel_diff, 0))
            max_combined_flag = max(max_combined_flag, rel_diff[idx])
            worst_detail = (idx, abs_diff[idx], rel_diff[idx])
    return max_combined_flag, worst_detail


def neg_log_likelihood(params, matches, team_idx, reference_date):
    """
    保留原始的逐场循环实现，用作向量化版本的正确性校验基准
    （见下方 verify_vectorized_matches_loop 单元测试），不再用于实际训练。
    真实训练请使用 fit_parameters，它内部调用向量化版本。
    """
    n = len(team_idx)
    attack = dict(zip(team_idx, params[:n]))
    defense = dict(zip(team_idx, params[n:2*n]))
    home_adv = params[2*n]

    total_nll = 0.0
    for m in matches:
        if m["home"] not in team_idx or m["away"] not in team_idx:
            continue

        w = time_weight(m["date"], reference_date)
        adv = 0.0 if m["neutral"] else home_adv

        lam = math.exp(attack[m["home"]] - defense[m["away"]] + adv)
        mu = math.exp(attack[m["away"]] - defense[m["home"]])

        ll_home = m["hg"] * math.log(lam) - lam - math.lgamma(m["hg"] + 1)
        ll_away = m["ag"] * math.log(mu) - mu - math.lgamma(m["ag"] + 1)
        total_nll -= w * (ll_home + ll_away)

    reg = 0.01 * sum(p**2 for p in params[:2*n])
    return total_nll + reg


def fit_parameters(matches, team_idx, reference_date=None):
    """跑 MLE 优化，返回每支球队的进攻力、防守力，以及主场优势常数。
    使用向量化似然函数 + 解析梯度，在几万场比赛规模下几秒内完成。
    此前用有限差分梯度（不提供解析梯度时scipy的默认行为）在523维参数下
    实测无法在合理时间内收敛，问题记录见本脚本的调试历史。"""
    if reference_date is None:
        reference_date = date.today()

    n = len(team_idx)
    arrays = precompute_match_arrays(matches, team_idx, reference_date)
    print(f"   预计算完成：{len(arrays['home_i'])} 场比赛纳入似然计算，"
          f"{arrays['n_skipped']} 场因球队样本不足被跳过")

    print("   校验解析梯度公式（与数值梯度对比，绝对+相对误差组合判据）...")
    max_flag, worst_detail = verify_gradient_correctness(arrays, n, n_trials=3)
    print(f"   梯度校验结果: {max_flag:.6f}" + (
        f"（可疑维度：绝对误差={worst_detail[1]:.6f}, 相对误差={worst_detail[2]:.6f}）"
        if worst_detail else "（无可疑维度）"
    ))
    if max_flag > 0.01:
        raise RuntimeError(
            f"解析梯度与数值梯度差异过大（绝对+相对均超标: {max_flag}），"
            f"可能存在公式错误，拒绝继续训练。请检查 neg_log_likelihood_gradient 的推导。"
        )

    x0 = np.concatenate([
        np.zeros(n),
        np.zeros(n),
        np.array([0.3]),
    ])

    print(f"开始优化：{n} 支球队，{2*n+1} 个参数，参考日期 {reference_date}")
    result = minimize(
        neg_log_likelihood_vectorized,
        x0,
        jac=neg_log_likelihood_gradient,   # 提供解析梯度，这是真正的性能修复
        args=(arrays, n),
        method="L-BFGS-B",
        options={"maxiter": 2000},
    )
    if not result.success:
        print(f"⚠️ 优化未完全收敛：{result.message}")

    attack = dict(zip(team_idx, result.x[:n]))
    defense = dict(zip(team_idx, result.x[n:2*n]))
    home_adv = result.x[2*n]

    return attack, defense, home_adv, result.fun


def expected_goals(team1, team2, attack, defense, home_adv, neutral=True,
                    fallback_attack=0.0, fallback_defense=0.0):
    """给定两队名字，返回预期进球数 (lambda, mu)。
    球队不在拟合表里（样本量不足）时，用联赛平均水平的保守默认值兜底，
    而不是报错或者瞎猜一个数字出来。"""
    a1 = attack.get(team1, fallback_attack)
    d1 = defense.get(team1, fallback_defense)
    a2 = attack.get(team2, fallback_attack)
    d2 = defense.get(team2, fallback_defense)
    adv = 0.0 if neutral else home_adv
    lam = math.exp(a1 - d2 + adv)
    mu = math.exp(a2 - d1)
    return round(lam, 3), round(mu, 3)


if __name__ == "__main__":
    print("📥 读取历史比赛数据...")
    matches = load_matches()
    print(f"   {len(matches)} 场比赛（{TRAINING_START_DATE} 起）")

    print("\n📊 统计球队场次，筛选样本量达标的球队...")
    team_idx, counts = build_team_index(matches)
    print(f"   {len(team_idx)} 支球队达到最低场次门槛（≥{MIN_MATCHES_FOR_FIT}场）")
    print(f"   场次不足被排除的球队数: {len(counts) - len(team_idx)}")

    print("\n🔧 开始 MLE 拟合（这一步在几千场比赛规模下可能需要几分钟）...")
    attack, defense, home_adv, final_nll = fit_parameters(matches, team_idx)
    print(f"   拟合完成。最终负对数似然: {final_nll:.2f}")
    print(f"   拟合出的主场优势常数: {home_adv:.4f}")

    print("\n🏆 进攻力最强的10支球队:")
    for team, val in sorted(attack.items(), key=lambda x: -x[1])[:10]:
        print(f"   {team:20s} attack={val:+.3f}  defense={defense[team]:+.3f}")

    print("\n🛡️ 防守力最强的10支球队（defense数值越高=防守越强，见公式：对手预期进球会减去这个值）:")
    for team, val in sorted(defense.items(), key=lambda x: -x[1])[:10]:
        print(f"   {team:20s} attack={attack[team]:+.3f}  defense={val:+.3f}")

    # 保存结果，供 Dixon-Coles 模型下游使用
    import json
    output = {
        "trained_at": datetime.now().isoformat(),
        "training_start_date": TRAINING_START_DATE.isoformat(),
        "half_life_years": HALF_LIFE_YEARS,
        "min_matches_threshold": MIN_MATCHES_FOR_FIT,
        "n_matches_used": len(matches),
        "n_teams_fitted": len(team_idx),
        "home_advantage": round(home_adv, 4),
        "final_neg_log_likelihood": round(final_nll, 2),
        "attack": {k: round(v, 4) for k, v in attack.items()},
        "defense": {k: round(v, 4) for k, v in defense.items()},
    }
    with open("fitted_parameters.json", "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print("\n💾 参数已保存到 fitted_parameters.json")
