"""
Dixon-Coles bivariate Poisson model + actuarial math (EV, Kelly)
+ Bayesian incremental updating + independent-match parlay probability.

演进历史（为什么长这样）：
1. 最早版本：ELO 硬编码 48 支球队，diff/400 线性映射进球期望 —— 粗糙近似。
2. MLE 版本：用 martj42/international_results 的 32,372 场真实历史比赛
   （1990年至今），通过最大似然估计拟合出每支球队独立的进攻力(attack)、
   防守力(defense)参数，加一个全局主场优势常数。这是 Dixon-Coles (1997)
   原始论文的标准做法，lam = exp(attack_home - defense_away + home_adv)，
   不再是 ELO 差值的线性近似。训练脚本见 training/train_mle.py，
   拟合参数见 training/fitted_parameters.json。
3. 本版本：在 MLE 点估计基础上，加入贝叶斯增量更新 —— 每支球队的参数
   不再是训练完就固定不变的死数字，而是带有不确定性的分布，每打完一场
   新比赛就用泊松-伽马共轭做一次解析更新，让参数持续跟着最新战绩微调，
   而不需要每次都重新跑一次全量 MLE。
"""
import math
import json
import os

DC_RHO = -0.13

# ══════════════════════════════════════════════════════════
# MLE 拟合参数加载
# ══════════════════════════════════════════════════════════
_PARAMS_PATH = os.path.join(os.path.dirname(__file__), "..", "training", "fitted_parameters.json")

def _load_fitted_params():
    with open(_PARAMS_PATH) as f:
        return json.load(f)

_FITTED = _load_fitted_params()
HOME_ADVANTAGE = _FITTED["home_advantage"]

# 场次不足以单独拟合的球队（训练时被排除），兜底用联赛平均水平
FALLBACK_ATTACK = 0.0
FALLBACK_DEFENSE = 0.0


def get_mle_params(name: str) -> tuple:
    """返回 (attack, defense) 点估计。球队不在拟合表里时返回联赛平均水平。"""
    key = (name or "").strip()
    for team_name, val in _FITTED["attack"].items():
        if team_name.lower() == key.lower():
            return val, _FITTED["defense"][team_name]
    return FALLBACK_ATTACK, FALLBACK_DEFENSE


def poisson_pmf(k: int, lam: float) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def dc_tau(a: int, b: int, lam: float, mu: float, rho: float = DC_RHO) -> float:
    if a == 0 and b == 0:
        return 1 - lam * mu * rho
    if a == 0 and b == 1:
        return 1 + lam * rho
    if a == 1 and b == 0:
        return 1 + mu * rho
    if a == 1 and b == 1:
        return 1 - rho
    return 1.0


def dixon_coles(team1: str, team2: str, attack_override: dict = None,
                 defense_override: dict = None, neutral: bool = True) -> dict:
    """
    返回主/平/客概率，以及支撑数据。

    attack_override / defense_override：可选参数，传入时优先于 MLE 拟合表使用。
    这是贝叶斯更新接入的关键接口——贝叶斯更新产生的是"当前后验均值"，
    跟训练时固定不变的 MLE 点估计是两回事，通过这两个参数，调用方可以传入
    "贝叶斯更新后的最新参数"而不用改动 dixon_coles 内部逻辑。不传时，
    行为跟纯 MLE 版本完全一致（向后兼容）。

    neutral：世界杯是中立场地赛制，默认 True（不加主场优势）。
    """
    if attack_override and team1 in attack_override:
        a1, d1 = attack_override[team1], defense_override[team1]
    else:
        a1, d1 = get_mle_params(team1)

    if attack_override and team2 in attack_override:
        a2, d2 = attack_override[team2], defense_override[team2]
    else:
        a2, d2 = get_mle_params(team2)

    home_adv = 0.0 if neutral else HOME_ADVANTAGE
    lam = max(0.05, min(6.0, math.exp(a1 - d2 + home_adv)))
    mu = max(0.05, min(6.0, math.exp(a2 - d1)))

    win_a = draw = win_b = 0.0
    for a in range(9):
        p_a = poisson_pmf(a, lam)
        for b in range(9):
            p = p_a * poisson_pmf(b, mu) * dc_tau(a, b, lam, mu)
            if a > b:
                win_a += p
            elif a < b:
                win_b += p
            else:
                draw += p

    total = win_a + draw + win_b
    predicted = (
        "win1" if win_a > win_b and win_a > draw else
        "win2" if win_b > win_a and win_b > draw else
        "draw"
    )
    return {
        "prob_home": round(win_a / total, 4),
        "prob_draw": round(draw / total, 4),
        "prob_away": round(win_b / total, 4),
        "xg_home": round(lam, 2),
        "xg_away": round(mu, 2),
        "attack_home": round(a1, 3),
        "defense_home": round(d1, 3),
        "attack_away": round(a2, 3),
        "defense_away": round(d2, 3),
        "predicted": predicted,
    }


def score_distribution(team1: str, team2: str, attack_override: dict = None,
                        defense_override: dict = None, neutral: bool = True,
                        max_goals: int = 8) -> dict:
    """
    返回完整的比分概率矩阵，以及总进球数的边际分布。
    这是串关推荐里"西班牙总进球少于X"这类总进球盘口的计算基础——
    Dixon-Coles 模型本身已经是完整的联合分布（双重循环覆盖所有比分组合），
    这里只是把它导出成可直接查询的结构，而不是重新建一个模型。
    """
    if attack_override and team1 in attack_override:
        a1, d1 = attack_override[team1], defense_override[team1]
    else:
        a1, d1 = get_mle_params(team1)
    if attack_override and team2 in attack_override:
        a2, d2 = attack_override[team2], defense_override[team2]
    else:
        a2, d2 = get_mle_params(team2)

    home_adv = 0.0 if neutral else HOME_ADVANTAGE
    lam = max(0.05, min(6.0, math.exp(a1 - d2 + home_adv)))
    mu = max(0.05, min(6.0, math.exp(a2 - d1)))

    score_probs = {}
    total_goals_probs = {}
    for a in range(max_goals + 1):
        p_a = poisson_pmf(a, lam)
        for b in range(max_goals + 1):
            p = p_a * poisson_pmf(b, mu) * dc_tau(a, b, lam, mu)
            score_probs[(a, b)] = p
            tg = a + b
            total_goals_probs[tg] = total_goals_probs.get(tg, 0.0) + p

    total = sum(score_probs.values())
    team1_under = {}
    team1_over = {}
    for threshold in [x * 0.5 for x in range(1, 11)]:  # 0.5, 1.0, 1.5 ... 5.0
        team1_under[threshold] = round(
            sum(p for (a, b), p in score_probs.items() if a < threshold) / total, 4
        )
        team1_over[threshold] = round(
            sum(p for (a, b), p in score_probs.items() if a >= threshold) / total, 4
        )

    return {
        "score_probs": {f"{a}-{b}": round(p / total, 5) for (a, b), p in score_probs.items()},
        "total_goals_probs": {str(tg): round(p / total, 5) for tg, p in total_goals_probs.items()},
        "team1_goals_under": team1_under,
        "team1_goals_over": team1_over,
    }


def calc_rps(probs: list, actual: str) -> float:
    """Ranked Probability Score. Lower is better. Random guessing ≈ 0.245."""
    order = ["win1", "draw", "win2"]
    obs = [1 if actual == o else 0 for o in order]
    rps = 0.0
    for i in range(3):
        cum_p = sum(probs[: i + 1])
        cum_o = sum(obs[: i + 1])
        rps += (cum_p - cum_o) ** 2
    return round(rps / 2, 6)


def expected_value(model_prob: float, odds: float) -> float:
    return model_prob * odds - 1


def kelly_pct(model_prob: float, odds: float, fraction: float, cap: float) -> float:
    """Fractional Kelly with a hard cap, e.g. fraction=0.5 (half-Kelly), cap=0.15 (15% max)."""
    b = odds - 1
    q = 1 - model_prob
    if b <= 0:
        return 0.0
    full = (model_prob * b - q) / b
    return max(0.0, min(full * fraction, cap))


# ══════════════════════════════════════════════════════════
# 贝叶斯增量更新（泊松-伽马共轭）
# ══════════════════════════════════════════════════════════
#
# 数学背景：如果一支球队的进球率服从伽马分布（伽马是泊松的共轭先验），
# 那么观测到一场新比赛的真实进球数之后，后验分布依然是伽马分布，
# 且有解析解——不需要 MCMC 采样或者数值优化，几行代数就能算出新参数。
#
# 具体做法：MLE 拟合出的 attack 参数是在对数空间的（lam = exp(attack - defense)），
# 但泊松-伽马共轭要求先验建在"进球率"这个正数空间上，不是对数空间。
# 所以这里做一个变换：把 exp(attack) 当作"进攻率"的伽马分布均值，
# 更新时在进球率空间做贝叶斯更新，更新完再转回对数空间存回 attack 参数。
#
# 先验的"信心程度"（对应伽马分布的 shape 参数）由该队 MLE 训练时的历史场次
# 决定——场次越多，先验越强，一场新比赛能撼动的幅度越小；场次少的球队，
# 一场新比赛的影响会相对更大。

class BayesianTeamState:
    """维护一支球队的贝叶斯后验状态。可以序列化存进数据库，
    每场新比赛结束后加载出来、更新、存回去。"""

    def __init__(self, team_name: str, mle_attack: float, mle_defense: float,
                 n_historical_matches: int, decay: float = 0.98):
        self.team_name = team_name
        # 进攻力：直接对 exp(attack) 这个"进攻率"做伽马更新，跟模型公式
        # lam = exp(attack - defense) 里 attack 的符号方向一致。
        self.attack_shape = max(1.0, n_historical_matches * 0.3)
        self.attack_rate = self.attack_shape / max(0.05, math.exp(mle_attack))

        # 防守力：模型公式里 defense 是以负号形式出现的
        # （mu = exp(attack_away - defense_home)），所以真正应该做伽马更新的量
        # 是 theta = exp(-defense)，不是 exp(defense) 本身。这是修复方向错误
        # 之后的正确参数化——见 current_defense() 和 update_defense_after_match()
        # 的详细推导注释。
        self.defense_theta_shape = max(1.0, n_historical_matches * 0.3)
        self.defense_theta_rate = self.defense_theta_shape / max(0.05, math.exp(-mle_defense))

        self.decay = decay  # 时间衰减：每次更新前，把旧证据的权重打个折扣
        self.n_updates = 0

    def current_attack(self) -> float:
        """返回当前后验均值，转换回对数空间（跟 MLE 的 attack 参数同一个尺度）。"""
        rate_mean = self.attack_shape / self.attack_rate
        return math.log(max(1e-6, rate_mean))

    def current_defense(self) -> float:
        """
        返回当前防守力后验均值，对数空间。
        内部维护的是 theta = exp(-defense) 的伽马后验（见 update_defense_after_match
        的推导说明），所以这里要把 theta 的均值转换回 defense = -log(theta_mean)，
        注意这个负号 —— 之前一版没有这个负号转换，导致方向搞反，已通过
        "零封应使防守力上升"的单元测试验证修复后方向正确。
        """
        theta_mean = self.defense_theta_shape / self.defense_theta_rate
        return -math.log(max(1e-6, theta_mean))

    def current_attack_std(self) -> float:
        """后验标准差（进球率空间），用于展示不确定性区间——
        这是贝叶斯方法相比纯 MLE 点估计的核心增益：不只给一个数字，
        还能说清楚这个数字有多大把握。"""
        variance = self.attack_shape / (self.attack_rate ** 2)
        return round(math.sqrt(variance), 4)

    def current_defense_std(self) -> float:
        """theta = exp(-defense) 空间的后验标准差。"""
        variance = self.defense_theta_shape / (self.defense_theta_rate ** 2)
        return round(math.sqrt(variance), 4)

    def update_after_match(self, goals_scored: int, opponent_defense_log: float):
        """
        观测到一场新比赛后更新进攻力的后验。
        goals_scored：这支球队在这场比赛的真实进球数。
        opponent_defense_log：对手的防守参数（对数空间）。

        泊松-伽马共轭更新公式：
          先验 attack_rate ~ Gamma(shape, rate)
          这场比赛的"暴露量"= exp(-opponent_defense_log)
          后验：shape' = shape*decay + goals_scored
                rate'  = rate*decay + 暴露量
        """
        exposure = math.exp(-opponent_defense_log)
        self.attack_shape = self.attack_shape * self.decay + goals_scored
        self.attack_rate = self.attack_rate * self.decay + exposure
        self.n_updates += 1

    def update_defense_after_match(self, goals_conceded: int, opponent_attack_log: float):
        """
        观测到一场新比赛的失球数后，更新防守力后验。

        推导（修复了此前方向搞反的问题，过程记录见下）：
        模型公式 mu = exp(attack_away - defense_home) 可以重写成
          mu = exp(attack_away) * exp(-defense_home) = exposure * theta
        其中 exposure = exp(attack_away)（对手的进攻强度，观测时已知），
        theta = exp(-defense_home)（待估计的量，theta 越小代表防守越强）。

        这跟 update_after_match 里对 attack 的更新是完全对称的标准
        伽马-泊松共轭形式：
          先验 theta ~ Gamma(shape, rate)
          观测 goals_conceded ~ Poisson(exposure * theta)
          后验：shape' = shape*decay + goals_conceded
                rate'  = rate*decay + exposure

        此前版本引入了一个"1/(1+失球数)"的人造代理量，这个量不服从
        泊松分布，破坏了共轭更新成立的数学前提，导致"零封应使防守力
        上升"这个方向性单元测试失败（实测：零封后数值反而下降）。
        本版本让 theta 直接对观测到的失球数做标准更新，不再引入任何
        代理量，方向性测试已重新验证通过。
        """
        exposure = math.exp(opponent_attack_log)
        self.defense_theta_shape = self.defense_theta_shape * self.decay + goals_conceded
        self.defense_theta_rate = self.defense_theta_rate * self.decay + exposure
        self.n_updates += 1

    def to_dict(self) -> dict:
        return {
            "team_name": self.team_name,
            "attack_shape": self.attack_shape, "attack_rate": self.attack_rate,
            "defense_theta_shape": self.defense_theta_shape, "defense_theta_rate": self.defense_theta_rate,
            "decay": self.decay, "n_updates": self.n_updates,
        }

    @classmethod
    def from_dict(cls, d: dict):
        obj = cls.__new__(cls)
        obj.team_name = d["team_name"]
        obj.attack_shape = d["attack_shape"]
        obj.attack_rate = d["attack_rate"]
        obj.defense_theta_shape = d["defense_theta_shape"]
        obj.defense_theta_rate = d["defense_theta_rate"]
        obj.decay = d["decay"]
        obj.n_updates = d["n_updates"]
        return obj


# ══════════════════════════════════════════════════════════
# 独立比赛串关（Parlay）— 联合概率与风险提示
# ══════════════════════════════════════════════════════════

def parlay_joint_probability(leg_probs: list) -> float:
    """
    独立事件的串关联合概率 = 各自概率相乘。
    前提：leg_probs 里的每个事件必须来自不同的、互相独立的比赛
    （比如西班牙vs意大利的"西班牙赢"，和法国vs德国的"法国赢"）。
    如果是同一场比赛内的两个事件（比如"西班牙赢"和"总进球>2.5"），
    这两者不独立，不能用这个函数，必须用 score_distribution 算真正的
    联合分布——这是上一轮讨论时特意划清楚的边界，这里的实现严格
    只处理跨比赛的独立事件场景。
    """
    joint = 1.0
    for p in leg_probs:
        joint *= p
    return round(joint, 6)


def parlay_ev_and_risk(legs: list, parlay_odds: float, fraction: float, cap: float) -> dict:
    """
    legs: [{"prob": 模型概率, "odds": 单腿赔率, "label": "西班牙胜"}, ...]
    parlay_odds: 串关的实际总赔率（通常约等于各腿赔率相乘，但博彩公司
                 可能有自己的串关定价，不一定严格等于乘积，所以作为
                 独立输入而不是自动算出来）

    刻意把"单腿最低胜率"和"组合胜率"都算出来并排返回，是为了让前端
    界面清楚展示"串关整体命中率被压缩了多少"，而不是只展示一个诱人的
    高赔率数字——这是上一轮讨论时明确要求的风险提示规格。
    """
    joint_prob = parlay_joint_probability([leg["prob"] for leg in legs])
    ev = expected_value(joint_prob, parlay_odds)
    kelly = kelly_pct(joint_prob, parlay_odds, fraction, cap)

    single_leg_probs = [leg["prob"] for leg in legs]
    weakest_leg = min(legs, key=lambda l: l["prob"])

    return {
        "joint_probability": joint_prob,
        "ev": round(ev, 4),
        "kelly_pct": round(kelly, 4),
        "n_legs": len(legs),
        "single_leg_probs": single_leg_probs,
        "weakest_leg_label": weakest_leg["label"],
        "weakest_leg_prob": weakest_leg["prob"],
        "risk_ratio_vs_weakest_leg": round(joint_prob / weakest_leg["prob"], 3) if weakest_leg["prob"] > 0 else 0,
    }


# ══════════════════════════════════════════════════════════
# 串关推荐引擎 — 从一批候选腿里自动搜索3-6腿的最优组合
# ══════════════════════════════════════════════════════════
#
# 设计依据（已用数值案例验证，见本次调试记录）：对独立事件而言，
# EV_combo = Π(1 + EV_i) - 1。任何一条腿的 EV_i < 0，都必然拉低整体
# 乘积——哪怕那条腿看起来是"十拿九稳的强队"、能把总赔率推高。这正是
# "串几个强队提高赔率"这个直觉的数学反例：强队的赔率往往被市场压得
# 极低（甚至低于其真实胜率对应的公平赔率，即"favorite-longshot bias"
# 热门-冷门偏差，一个博彩市场里有实证文献支持的现象），一旦某条强队
# 腿本身是负EV，把它算进候选池只会拖累组合表现。
#
# 所以这里的搜索严格只从正EV的单腿里选组合，负EV的腿在进入组合搜索
# 之前就被过滤掉，不会出现在任何推荐结果里。

import itertools

MAX_CANDIDATE_LEGS_FOR_SEARCH = 20
# 组合数随候选腿数量阶乘级增长（C(n,6)）。候选池若超过20条正EV腿，
# 只取EV最高的前20条参与搜索——高EV的腿本来就更该被优先组合，
# 这个截断在实践里几乎不会漏掉真正的最优组合，同时把最坏情况
# C(40,6)≈380万的搜索量控制在 C(20,6)≈38,760 这个几毫秒级别。


def build_candidate_legs(match_odds_list: list) -> list:
    """
    match_odds_list: [{"match_id":.., "team1":.., "team2":.., "prob_home":..,
                        "prob_draw":.., "prob_away":.., "odds_home":..,
                        "odds_draw":.., "odds_away":..}, ...]
    （prob_* 来自已经算好的 Dixon-Coles + 贝叶斯后验预测，odds_* 是用户
    输入的赔率）

    返回展开后的候选腿列表，每条腿代表"某场比赛的某一个1X2结果"，
    只保留 EV > 0 的腿——负EV腿在这一步就被排除，不会进入后续的
    组合搜索，见上方模块说明。
    """
    candidates = []
    for m in match_odds_list:
        outcomes = [
            ("home", m.get("odds_home"), m.get("prob_home"), m["team1"]),
            ("draw", m.get("odds_draw"), m.get("prob_draw"), "平局"),
            ("away", m.get("odds_away"), m.get("prob_away"), m["team2"]),
        ]
        for outcome, odds, prob, label in outcomes:
            if odds is None or prob is None or odds <= 1:
                continue
            leg_ev = expected_value(prob, odds)
            if leg_ev <= 0:
                continue  # 负EV腿直接淘汰，不进候选池
            candidates.append({
                "match_id": m["match_id"],
                "outcome": outcome,
                "odds": odds,
                "prob": prob,
                "label": f"{label}（{m['team1']} vs {m['team2']}）" if outcome != "draw" else f"平局（{m['team1']} vs {m['team2']}）",
                "leg_ev": round(leg_ev, 4),
            })
    return candidates


def suggest_parlays(match_odds_list: list, min_legs: int, max_legs: int,
                     fraction: float, cap: float, top_n: int = 5) -> dict:
    """
    核心入口：给定一批比赛的模型概率+用户输入赔率，自动搜索 min_legs 到
    max_legs 腿的正EV组合，按EV从高到低排序，返回前 top_n 个。

    同时额外挑出"命中率最高"和"联合赔率最高"两个候选（仍然要求组合本身
    EV>0，不会为了追求高赔率或高胜率而牺牲这个底线），让使用者能看到
    "高赔率、高胜率、高EV"三者之间真实的取舍关系，而不是把三者混成
    一个模糊的单一"最优"数字——这三个目标天然互相冲突（赔率越高通常
    对应概率越低），假装存在一个能同时最大化三者的组合是不诚实的。
    """
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

    # 候选池过大时，只取EV最高的前N条，控制组合数量级（见上方模块说明）
    candidates_sorted_by_ev = sorted(candidates, key=lambda c: -c["leg_ev"])
    search_pool = candidates_sorted_by_ev[:MAX_CANDIDATE_LEGS_FOR_SEARCH]
    pool_truncated = len(candidates) > MAX_CANDIDATE_LEGS_FOR_SEARCH

    all_combos = []
    for k in range(min_legs, max_legs + 1):
        for combo in itertools.combinations(search_pool, k):
            match_ids = [leg["match_id"] for leg in combo]
            if len(set(match_ids)) != len(match_ids):
                continue  # 同一场比赛不能出现两条腿

            legs = list(combo)
            joint_prob = parlay_joint_probability([leg["prob"] for leg in legs])
            combined_odds = 1.0
            for leg in legs:
                combined_odds *= leg["odds"]
            combo_ev = expected_value(joint_prob, combined_odds)

            if combo_ev <= 0:
                continue  # 组合整体EV必须为正，这是唯一的硬门槛

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
    dedupe_add(recommendations, by_ev[0], seen)  # 最高EV，主推荐
    if len(by_probability) and by_probability[0] not in recommendations:
        dedupe_add(recommendations, by_probability[0], seen)  # 最稳（命中率最高）
    if len(by_odds) and by_odds[0] not in recommendations:
        dedupe_add(recommendations, by_odds[0], seen)  # 赔率最高（在仍为正EV的前提下）

    # 补齐剩余名额，按EV继续往下填
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
