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
_TRAINING_DIR = os.path.join(os.path.dirname(__file__), "..", "training")

# 参数表按「作用域」区分，不是按赛事。两张表：
#   international —— 国家队（世界杯、欧洲杯等），261支国家队
#   club          —— 俱乐部（四大联赛 + 欧冠合并训练），164支俱乐部
#
# 为什么俱乐部只有一张表、而不是每个联赛一张：MLE 拟合出的 attack/defense
# 只在同一训练池内部可比——给某联赛所有球队的 attack 和 defense 同时加一个
# 常数，league 内部的预测完全不变，存在无法识别的「联赛整体偏移量」。
# 分开训练的话，曼城的 attack=1.5 和皇马的 attack=1.5 不能直接比，欧冠这种
# 跨联赛对局就没法算。四大联赛+欧冠一起训练，欧冠比赛充当连接各联赛的
# 「桥」，所有参数才落在同一把尺子上。
_PARAM_FILES = {
    "international": "fitted_parameters.json",
    "club": "fitted_parameters_club.json",
    "mls": "fitted_parameters_mls.json",
    "efl_cup": "fitted_parameters_efl_cup.json",
}

# 赛事代码 → 参数作用域。新增赛事时在这里登记，忘了登记会走 fallback 兜底，
# 产出的是所有球队都一样的空洞预测（不会报错，所以要留意）。
#
# mls / efl_cup 各自单独一张表，没有并进 club 那张联合表：club 表的桥接
# 逻辑（四大联赛+欧冠联合训练）成立的前提是训练池里有真正跨联赛的对局
# 把不同联赛的尺子锚在一起。美职联不跟这四个联赛有任何交叉赛事，联赛杯
# 虽然后期轮次有英超球队参战，但数据源（API-Football 免费档）只有
# 2022-2024，跟 club 表的训练区间未必对齐，且联赛杯参赛的英冠/英甲/英乙
# 球队本来就不在 club 表里——勉强拼进同一张表反而会产生"看似可比、实则
# 没有真实桥接对局支撑"的假象。各自独立训练更诚实。
COMPETITION_SCOPE = {
    "wc2026": "international",
    "epl": "club", "laliga": "club", "seriea": "club", "bundesliga": "club",
    "ucl": "club",
    # 法甲、英冠同样并进 club 表——各自的桥（法甲靠欧冠 5 支、英冠靠
    # 升降级 24 支共享球队）见 training/train_mle_club.py 的 DOMESTIC_LEAGUES
    "ligue1": "club", "championship": "club",
    # 英甲/英乙/西乙。桥是阶梯式的：英超↔英冠24支↔英甲24支↔英乙34支，
    # 西甲↔西乙21支。英乙单独看跟上层只共享7支，是靠英甲这一级连上去的，
    # 所以这三个必须一起在同一张表里训练，见 train_mle_club.py 的说明。
    "leagueone": "club", "leaguetwo": "club", "segunda": "club",
    # 意乙。桥：跟意甲共享 24 支升降级球队（实测），跟法乙/英甲同一量级。
    # 训练数据只能走 .txt——football.json 的 it.2 在 2021-24 三季是 404。
    "serieb": "club",
    # 德乙/法乙。桥：德乙∩德甲 18 支、法乙∩法甲 24 支（实测升降级共享球队）。
    "bundesliga2": "club", "ligue2": "club",
    # 全国联赛（英格兰第五级）。它没有 .json 镜像，训练数据走 .txt，
    # 见 train_mle_club.py 的 TXT_LEAGUES。桥：跟英乙共享升降级球队。
    "nationalleague": "club",
    "mls": "mls",
    "efl_cup": "efl_cup",
}

# 世界杯是中立场地赛制，主场优势恒为0；俱乐部联赛是真实主客场，要启用。
COMPETITION_NEUTRAL = {
    "wc2026": True,
    "epl": False, "laliga": False, "seriea": False, "bundesliga": False,
    "ligue1": False, "championship": False,      # 常规主客场联赛
    "leagueone": False, "leaguetwo": False, "segunda": False,
    "serieb": False,
    "bundesliga2": False, "ligue2": False,
    "nationalleague": False,
    "ucl": False,     # 欧冠只有决赛在中立场，训练数据里已按轮次区分，这里取多数情况
    "mls": False,
    "efl_cup": False,  # 联赛杯只有决赛中立场，跟欧冠同样处理，取多数情况
}

_PARAM_CACHE = {}


def load_params(scope: str = "international") -> dict:
    """按作用域加载参数表，带缓存（每次调用都读文件的话，跑一遍全量预测会读几百次）。"""
    if scope in _PARAM_CACHE:
        return _PARAM_CACHE[scope]
    filename = _PARAM_FILES.get(scope, _PARAM_FILES["international"])
    path = os.path.join(_TRAINING_DIR, filename)
    if not os.path.exists(path):
        # 俱乐部参数表还没训练出来时不要整个崩掉，退回国家队表并给出明确警告，
        # 否则排查起来会很困惑（预测能出数字，但全是兜底值）
        print(f"⚠️  参数表 {filename} 不存在，scope={scope} 退回 international 表。"
              f"俱乐部比赛的预测会是无意义的兜底值，请先跑 training/train_mle_club.py")
        path = os.path.join(_TRAINING_DIR, _PARAM_FILES["international"])
    # encoding 必须显式指定。不写的话 Python 用 locale.getpreferredencoding()，
    # Linux/Mac 是 UTF-8 所以看不出问题，Windows 却是系统代码页（英文区 cp1252、
    # 中文区 gbk），拿它去读 UTF-8 的参数表会炸。真实报错：
    #   'charmap' codec can't decode byte 0x81 in position 39
    # position 39 正是 fitted_parameters_club.json 里 note 字段「四大联赛」的
    # 第三个字节。这个异常发生在 update_predictions() 内部，被 run_full_update
    # 的外层 except 吞掉变成 status="error" 并回滚——比赛记录因为 upsert_matches
    # 自己 commit 过所以还在，但**全部预测被清空**，而 /api/backtest-summary
    # 是 Prediction JOIN Match，于是每个赛事都变成 total==0 被跳过，
    # 界面上看起来就是「世界杯数据全部不见了」。一个 encoding 参数的连锁反应。
    #
    # 国家队表更阴：它在 cp1252 下不报错但会乱码（Curaçao → CuraÃ§ao），
    # 队名匹配不上就静默退回 (0,0) 兜底，产出看起来正常实则无意义的预测。
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    # 队名索引统一转小写，避免每次查询都遍历整张表做大小写不敏感比较
    data["_index"] = {k.lower(): (v, data["defense"][k]) for k, v in data["attack"].items()}
    _PARAM_CACHE[scope] = data
    return data


def scope_for_competition(competition_code: str) -> str:
    return COMPETITION_SCOPE.get(competition_code, "international")


def neutral_for_competition(competition_code: str) -> bool:
    return COMPETITION_NEUTRAL.get(competition_code, True)


def home_advantage_for(scope: str = "international") -> float:
    return load_params(scope)["home_advantage"]


# 兼容旧代码：模块级 HOME_ADVANTAGE 仍指国家队表的值
HOME_ADVANTAGE = load_params("international")["home_advantage"]

# 场次不足以单独拟合的球队（训练时被排除），兜底用联赛平均水平
FALLBACK_ATTACK = 0.0
FALLBACK_DEFENSE = 0.0


def get_mle_params(name: str, scope: str = "international") -> tuple:
    """返回 (attack, defense) 点估计。球队不在拟合表里时返回联赛平均水平。"""
    key = (name or "").strip().lower()
    hit = load_params(scope)["_index"].get(key)
    return hit if hit else (FALLBACK_ATTACK, FALLBACK_DEFENSE)


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
                 defense_override: dict = None, neutral: bool = True,
                 scope: str = "international") -> dict:
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
        a1, d1 = get_mle_params(team1, scope)

    if attack_override and team2 in attack_override:
        a2, d2 = attack_override[team2], defense_override[team2]
    else:
        a2, d2 = get_mle_params(team2, scope)

    # 主场优势取对应作用域的值：国家队表 0.2471、俱乐部表 0.2100，两者不同，
    # 混用会系统性地高估或低估主队
    home_adv = 0.0 if neutral else home_advantage_for(scope)
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
                        max_goals: int = 8, scope: str = "international") -> dict:
    """
    返回完整的比分概率矩阵，以及总进球数的边际分布。
    这是串关推荐里"西班牙总进球少于X"这类总进球盘口的计算基础——
    Dixon-Coles 模型本身已经是完整的联合分布（双重循环覆盖所有比分组合），
    这里只是把它导出成可直接查询的结构，而不是重新建一个模型。
    """
    if attack_override and team1 in attack_override:
        a1, d1 = attack_override[team1], defense_override[team1]
    else:
        a1, d1 = get_mle_params(team1, scope)
    if attack_override and team2 in attack_override:
        a2, d2 = attack_override[team2], defense_override[team2]
    else:
        a2, d2 = get_mle_params(team2, scope)

    home_adv = 0.0 if neutral else home_advantage_for(scope)
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

    # ── 热循环：只算排序用得上的三个标量，别的一律推迟 ──────────────
    #
    # 20 条腿、最多 8 腿，组合数是 Σ C(20,k) = 263,929。原来对**每一个**正EV
    # 组合都当场构造完整的嵌套 dict（含 legs 列表）、算 kelly、找最弱腿、
    # 做 6 次 round，然后把十几万个 dict 全排序——而最终只有 5 个会被返回。
    # cProfile 实测（30 场输入，2.31s 总耗时）：
    #     构造 legs 列表        0.492s
    #     round() ×728,658      0.240s
    #     min() ×242,856        0.272s
    #     kelly_pct ×121,428    0.123s
    # 都是给那十几万个注定被丢掉的组合白算的。
    #
    # 现在循环里只留下排序真正需要的三个值，重的部分留到选出赢家之后再做。
    # **枚举顺序一个字没改**，所以 sorted 的稳定性、并列时谁排前面、最终
    # 推荐哪几注，跟改之前完全一致——这一点有 byte 级回归测试兜着
    # （validation/21_parlay_search_regression.py）。
    raw = []
    for k in range(min_legs, max_legs + 1):
        for combo in itertools.combinations(search_pool, k):
            match_ids = [leg["match_id"] for leg in combo]
            if len(set(match_ids)) != len(match_ids):
                continue  # 同一场比赛不能出现两条腿

            joint_prob = parlay_joint_probability([leg["prob"] for leg in combo])
            combined_odds = 1.0
            for leg in combo:
                combined_odds *= leg["odds"]
            combo_ev = expected_value(joint_prob, combined_odds)

            if combo_ev <= 0:
                continue  # 组合整体EV必须为正，这是唯一的硬门槛

            # 排序键用的是**四舍五入后**的值，跟原来 sorted 读的字段完全一样
            raw.append((round(combo_ev, 4), round(joint_prob, 4), round(combined_odds, 3),
                        k, combo, joint_prob, combined_odds, combo_ev))

    def materialize(i):
        """把一条轻量记录还原成完整的推荐 dict。只对最终入选的那几条调用。"""
        ev_r, jp_r, co_r, k, combo, joint_prob, combined_odds, combo_ev = raw[i]
        weakest = min(combo, key=lambda l: l["prob"])
        return {
            "legs": [{"label": l["label"], "outcome": l["outcome"], "odds": l["odds"],
                      "prob": l["prob"], "match_id": l["match_id"]} for l in combo],
            "n_legs": k,
            # 概率给 8 位，不是 4 位。
            #
            # 4 位的时候，返回的这三个数**自己乘不出来**：5 腿串关的联合概率
            # 是 0.0362、联合赔率 245.03，前端拿这两个数按 EV = p×赔率-1 一算
            # 得 +786.99%，而这里返回的 ev 是 +786.3%（它是用未舍入的 joint_prob
            # 算的）。p 舍到 4 位的那点误差被 245 倍的赔率放大了。
            #
            # 以前看不出来是因为前端只显示、不重算。现在推荐卡片支持就地改
            # 单腿赔率并当场重算，一开输入框数字就会自己跳一下——所以这里
            # 必须给够精度。8 位在 245 倍赔率下的 EV 误差是 1.2e-6，远小于
            # 显示用的 4 位。
            # 界面显示走 pct()，只保留一位小数，多出来的位数不会出现在屏幕上。
            "joint_probability": round(joint_prob, 8),
            "combined_odds": co_r,
            "ev": ev_r,
            "kelly_pct": round(kelly_pct(joint_prob, combined_odds, fraction, cap), 4),
            "weakest_leg_label": weakest["label"],
            "weakest_leg_match_id": weakest["match_id"],
            "weakest_leg_outcome": weakest["outcome"],
            "weakest_leg_prob": weakest["prob"],
            "risk_ratio_vs_weakest_leg": round(joint_prob / weakest["prob"], 3) if weakest["prob"] > 0 else 0,
        }

    all_combos = raw
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

    # 排的是下标不是 dict 本身。sorted 是稳定排序，键又跟原来一模一样，
    # 所以并列时的先后顺序跟改之前逐字一致。
    by_ev = sorted(range(len(raw)), key=lambda i: -raw[i][0])
    by_probability = sorted(range(len(raw)), key=lambda i: -raw[i][1])
    by_odds = sorted(range(len(raw)), key=lambda i: -raw[i][2])

    def dedupe_add(target_list, i, seen_signatures):
        sig = tuple(sorted(leg["match_id"] for leg in raw[i][4]))
        if sig in seen_signatures:
            return
        seen_signatures.add(sig)
        target_list.append(i)

    seen = set()
    picked = []
    dedupe_add(picked, by_ev[0], seen)  # 最高EV，主推荐
    if by_probability[0] not in picked:
        dedupe_add(picked, by_probability[0], seen)  # 最稳（命中率最高）
    if by_odds[0] not in picked:
        dedupe_add(picked, by_odds[0], seen)  # 赔率最高（在仍为正EV的前提下）

    # 补齐剩余名额，按EV继续往下填
    for i in by_ev:
        if len(picked) >= top_n:
            break
        dedupe_add(picked, i, seen)

    recommendations = []
    for i in picked:
        combo = materialize(i)
        if i == by_ev[0]:
            combo["tag"] = "🏆 最高EV"
        elif i == by_probability[0]:
            combo["tag"] = "🛡️ 最稳（命中率最高）"
        elif i == by_odds[0]:
            combo["tag"] = "🎯 赔率最高"
        else:
            combo["tag"] = None
        recommendations.append(combo)

    return {
        "status": "ok",
        "n_candidates": len(candidates),
        "pool_truncated": pool_truncated,
        "n_combinations_evaluated": len(all_combos),
        "candidates": candidates_sorted_by_ev,
        "combinations": recommendations,
    }
