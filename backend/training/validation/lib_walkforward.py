"""
走查（walk-forward）评估的公共库。

为什么要有这个文件
------------------
`01_model_vs_market.py` 用的是「一刀切」保留集：2014-2024 训练、2024-08 之后测试，
最终只有 1267 场。08_MARKET_VALIDATION.md 里所有结论（xG、Elo、集成、联赛分层、
赔率区间）都在这同一批 651-1267 场上裁决过——这批数据已经被反复使用，
再往上加特征就是在挑选噪声。

同时它还浪费了数据：Matches.csv 里有 227,525 场带赔率和比分的比赛，
只用了其中 0.5% 来做判断。RPS 差距的标准误大约按 1/sqrt(n) 缩小，
1267 场上 0.0043 的差距只有约 2.7 个标准误，几万场上则是几十个。

这个库把评估改成「滚动起点」：每个月重新用该时点之前的全部历史拟合一次参数，
预测接下来一个月的比赛，滚过 25 年。产出的每一条预测都是严格样本外的，
累计几万条，构成一把真正能用的尺子。

关键设计
--------
1. 严格时间隔离。拟合第 T 个月的参数时，只用 date < T 月 1 号的比赛。
   不存在任何未来信息泄漏，包括时间衰减权重的参考日也设成 T 月 1 号。
2. 按国家分池。attack/defense 只在同一训练池内部可比（给池内所有球队的
   attack 和 defense 同时加常数，池内预测不变——存在不可识别的整体偏移）。
   同国家的多个级别放同一池，靠升降级球队自身的历史充当跨级别的桥。
   主场优势也按池单独拟合——南美和北欧的主场优势差异很大，全局一个常数是错的。
3. 热启动。上个月的解作为这个月的初值，L-BFGS-B 通常几十次迭代就收敛，
   否则 300 次滚动 × 每次上千维优化跑不完。
4. 超参在 DEV 期定、在 TEST 期评。半衰期、正则强度、rho 都只用 2013 年之前的
   数据挑选，之后锁死不动。否则又会滑回「在评估集上调参」的老问题。
"""
import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.special import gammaln

# ── 时间切分 ────────────────────────────────────────────────
# DEV 期用来挑超参（半衰期、正则、rho），TEST 期一次都不许用来挑任何东西。
DEV_START = pd.Timestamp("2004-01-01")   # 前几年留给模型积累历史
DEV_END = pd.Timestamp("2013-01-01")     # DEV/TEST 分界
TEST_END = pd.Timestamp("2025-07-01")

# ── 默认超参（DEV 期选出后写回这里）────────────────────────
# 以下三个值由 10_tune_dev.py 在 DEV 期（2004-2013）选出，之后锁死不动。
# 对比现有 model.py/train_mle.py 的设定（4 年半衰期、rho=-0.13）：DEV RPS 改善 0.0013。
DEFAULT_HALF_LIFE_DAYS = 365.0   # 原为 1460（4年）——那是给国家队定的，俱乐部转会窗一年两次
DEFAULT_RIDGE = 0.1              # DEV 上几乎不敏感（组间差异 5e-5），取网格内最优
DEFAULT_RHO = -0.07              # 原为硬编码 -0.13，从未拟合过
MIN_TRAIN_MATCHES = 12       # 球队在训练窗内少于这么多场就不预测它的比赛
MAX_GOALS = 10               # 比分矩阵截断

# ── 分池：同国家的各级联赛共用一个参数池 ──────────────────
POOLS = {
    "ENG": ["E0", "E1", "E2", "E3", "EC"],
    "SCO": ["SC0", "SC1", "SC2", "SC3"],
    "ESP": ["SP1", "SP2"],
    "ITA": ["I1", "I2"],
    "GER": ["D1", "D2"],
    "FRA": ["F1", "F2"],
    "NED": ["N1"],
    "BEL": ["B1"],
    "POR": ["P1"],
    "TUR": ["T1"],
    "GRE": ["G1"],
    "ARG": ["ARG"], "USA": ["USA"], "BRA": ["BRA"], "JAP": ["JAP"],
    "MEX": ["MEX"], "ROM": ["ROM"], "POL": ["POL"], "SWE": ["SWE"],
    "NOR": ["NOR"], "RUS": ["RUS"], "DEN": ["DEN"], "CHN": ["CHN"],
    "IRL": ["IRL"], "FIN": ["FIN"], "AUT": ["AUT"], "SUI": ["SUI"],
}
DIV_TO_POOL = {d: p for p, ds in POOLS.items() for d in ds}


def load_matches(path="/tmp/m.csv"):
    """读 Matches.csv，只保留有比分和 1X2 赔率的比赛，附上分池标签。"""
    df = pd.read_csv(path, low_memory=False)
    df["MatchDate"] = pd.to_datetime(df["MatchDate"], errors="coerce")
    need = ["MatchDate", "HomeTeam", "AwayTeam", "FTHome", "FTAway",
            "OddHome", "OddDraw", "OddAway"]
    df = df.dropna(subset=need)
    # 十进制赔率必须 > 1（1.0 表示押注不赚不赔，更低无意义）。数据集里有 7 行
    # OddHome=0、1 行 MaxAway=0 的脏记录，不过滤会在取倒数时产生 inf 概率。
    for c in ["OddHome", "OddDraw", "OddAway"]:
        df = df[pd.to_numeric(df[c], errors="coerce") > 1.0]
    for c in ["MaxHome", "MaxDraw", "MaxAway"]:
        bad = pd.to_numeric(df[c], errors="coerce") <= 1.0
        df.loc[bad, c] = np.nan
    df = df[df["Division"].isin(DIV_TO_POOL)]
    df["pool"] = df["Division"].map(DIV_TO_POOL)
    df["FTHome"] = df["FTHome"].astype(int)
    df["FTAway"] = df["FTAway"].astype(int)
    # 球队名带上池前缀：不同国家可能有同名球队，混在一起会把两支队当成一支
    df["home"] = df["pool"] + "|" + df["HomeTeam"].str.strip()
    df["away"] = df["pool"] + "|" + df["AwayTeam"].str.strip()
    # 实际结果：0=主胜 1=平 2=客胜
    res = np.where(df["FTHome"] > df["FTAway"], 0,
                   np.where(df["FTHome"] == df["FTAway"], 1, 2))
    df["outcome"] = res
    return df.sort_values("MatchDate").reset_index(drop=True)


# ── Dixon-Coles 泊松似然（向量化 + 解析梯度）──────────────
def _nll_and_grad(params, hi, ai, hg, ag, w, n_teams, ridge):
    """负对数似然与解析梯度。

    梯度推导跟 train_mle.py 里那份一致（残差 = 真实进球 - 模型预期进球），
    但这里把函数值和梯度合并返回，省掉一半的 exp 计算——走查要跑几百次
    优化，每次上百轮迭代，这个常数因子是实打实的。
    """
    attack = params[:n_teams]
    defense = params[n_teams:2 * n_teams]
    home_adv = params[2 * n_teams]

    lam = np.exp(attack[hi] - defense[ai] + home_adv)
    mu = np.exp(attack[ai] - defense[hi])

    ll = w * (hg * np.log(lam) - lam - gammaln(hg + 1)
              + ag * np.log(mu) - mu - gammaln(ag + 1))
    nll = -ll.sum() + ridge * np.sum(params[:2 * n_teams] ** 2)

    rh = w * (hg - lam)
    ra = w * (ag - mu)
    g_att = np.zeros(n_teams)
    g_def = np.zeros(n_teams)
    np.add.at(g_att, hi, -rh)
    np.add.at(g_att, ai, -ra)
    np.add.at(g_def, ai, rh)
    np.add.at(g_def, hi, ra)
    grad = np.concatenate([g_att, g_def, [-rh.sum()]])
    grad[:2 * n_teams] += 2 * ridge * params[:2 * n_teams]
    return nll, grad


def fit_pool(hi, ai, hg, ag, w, n_teams, ridge=DEFAULT_RIDGE, x0=None):
    """拟合一个池的 attack/defense/home_adv。x0 用于热启动。"""
    if x0 is None:
        x0 = np.concatenate([np.zeros(2 * n_teams), [0.25]])
    res = minimize(_nll_and_grad, x0, jac=True,
                   args=(hi, ai, hg, ag, w, n_teams, ridge),
                   method="L-BFGS-B", options={"maxiter": 500})
    return res.x


# ── 从 lambda/mu 到 1X2 概率 ───────────────────────────────
def _poisson_vec(lam, kmax):
    """返回 shape (n, kmax+1) 的泊松 pmf 矩阵。"""
    k = np.arange(kmax + 1)
    return np.exp(-lam[:, None] + k * np.log(lam[:, None]) - gammaln(k + 1))


def probs_from_rates(lam, mu, rho=DEFAULT_RHO, kmax=MAX_GOALS):
    """向量化算主/平/客概率，含 Dixon-Coles 低比分修正 tau。

    tau 只改动 (0,0)(0,1)(1,0)(1,1) 四格——它存在的理由是独立泊松低估平局，
    尤其是 0-0 和 1-1。修正后重新归一化。
    """
    ph = _poisson_vec(lam, kmax)          # (n, K+1)
    pa = _poisson_vec(mu, kmax)
    joint = ph[:, :, None] * pa[:, None, :]   # (n, K+1, K+1)

    joint[:, 0, 0] *= 1 - lam * mu * rho
    joint[:, 0, 1] *= 1 + lam * rho
    joint[:, 1, 0] *= 1 + mu * rho
    joint[:, 1, 1] *= 1 - rho
    np.clip(joint, 1e-15, None, out=joint)

    idx = np.arange(kmax + 1)
    home_win = idx[:, None] > idx[None, :]
    draw = idx[:, None] == idx[None, :]
    away_win = idx[:, None] < idx[None, :]

    p_h = (joint * home_win).sum(axis=(1, 2))
    p_d = (joint * draw).sum(axis=(1, 2))
    p_a = (joint * away_win).sum(axis=(1, 2))
    tot = p_h + p_d + p_a
    return np.stack([p_h / tot, p_d / tot, p_a / tot], axis=1)


def market_probs(oh, od, oa):
    """赔率取倒数再归一化（比例法去抽水）。返回概率和抽水率。

    注意：这只用于「市场作为对照基准准不准」的度量。按 CLAUDE.md 的硬约束，
    这个概率绝不能进 EV 公式——EV 用的是模型独立概率 × 市场原始赔率。
    """
    ih, idr, ia = 1 / oh, 1 / od, 1 / oa
    s = ih + idr + ia
    return np.stack([ih / s, idr / s, ia / s], axis=1), (s - 1)


def rps(probs, outcome):
    """Ranked Probability Score，逐场返回。越低越好，随机猜约 0.245。"""
    cum_p = np.cumsum(probs, axis=1)[:, :2]
    obs = np.zeros_like(probs)
    obs[np.arange(len(outcome)), outcome] = 1.0
    cum_o = np.cumsum(obs, axis=1)[:, :2]
    return ((cum_p - cum_o) ** 2).sum(axis=1) / 2


# ── 主循环 ────────────────────────────────────────────────
def run_walkforward(df, start, end, half_life_days=DEFAULT_HALF_LIFE_DAYS,
                    ridge=DEFAULT_RIDGE, rho=DEFAULT_RHO,
                    min_train=MIN_TRAIN_MATCHES, freq="MS", verbose=True,
                    fit_fn=None, predict_fn=None):
    """滚动起点评估。

    每个周期（默认自然月）用该周期开始之前的全部历史重新拟合，预测周期内的比赛。
    fit_fn / predict_fn 留作扩展点（主客场分离参数等变体沿用同一套走查骨架，
    保证不同变体是在完全相同的比赛集合上被比较的）。

    返回逐场预测的 DataFrame。
    """
    fit_fn = fit_fn or _default_fit
    predict_fn = predict_fn or _default_predict
    periods = pd.date_range(start, end, freq=freq)
    out = []

    for pool, pdf in df.groupby("pool", sort=True):
        teams = pd.Index(sorted(set(pdf["home"]) | set(pdf["away"])))
        t2i = {t: i for i, t in enumerate(teams)}
        n_teams = len(teams)
        hi_all = pdf["home"].map(t2i).to_numpy()
        ai_all = pdf["away"].map(t2i).to_numpy()
        hg_all = pdf["FTHome"].to_numpy(float)
        ag_all = pdf["FTAway"].to_numpy(float)
        dates = pdf["MatchDate"].to_numpy()
        state = None
        n_pred = 0

        for i in range(len(periods) - 1):
            p0, p1 = periods[i], periods[i + 1]
            train_m = dates < np.datetime64(p0)
            test_m = (dates >= np.datetime64(p0)) & (dates < np.datetime64(p1))
            if test_m.sum() == 0 or train_m.sum() < 200:
                continue

            age_days = (np.datetime64(p0) - dates[train_m]) / np.timedelta64(1, "D")
            w = 0.5 ** (age_days / half_life_days)

            state = fit_fn(hi_all[train_m], ai_all[train_m], hg_all[train_m],
                           ag_all[train_m], w, n_teams, ridge, state)

            # 训练窗内出场数不足的球队不预测——参数会过拟合到几场偶然结果
            cnt = np.bincount(np.concatenate([hi_all[train_m], ai_all[train_m]]),
                              minlength=n_teams)
            ok = test_m & (cnt[hi_all] >= min_train) & (cnt[ai_all] >= min_train)
            if ok.sum() == 0:
                continue

            lam, mu = predict_fn(state, hi_all[ok], ai_all[ok], n_teams)
            sub = pdf.loc[ok].copy()
            # 存 lambda/mu 而不是只存概率：rho 只在从进球率转 1X2 概率这一步起作用，
            # 存下费率之后，调 rho 就不需要重跑几百次优化了。
            sub["lam"] = lam
            sub["mu"] = mu
            sub[["p_home", "p_draw", "p_away"]] = probs_from_rates(lam, mu, rho)
            sub["period"] = p0
            out.append(sub)
            n_pred += ok.sum()

        if verbose:
            print(f"  {pool:4s} {n_teams:5d} 队  样本外预测 {n_pred:6d} 场")

    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def _default_fit(hi, ai, hg, ag, w, n_teams, ridge, state):
    return fit_pool(hi, ai, hg, ag, w, n_teams, ridge, x0=state)


def _default_predict(state, hi, ai, n_teams):
    """返回预期进球率 (lam, mu)，不直接返回概率——见 run_walkforward 里的说明。"""
    attack = state[:n_teams]
    defense = state[n_teams:2 * n_teams]
    home_adv = state[2 * n_teams]
    lam = np.clip(np.exp(attack[hi] - defense[ai] + home_adv), 0.05, 8.0)
    mu = np.clip(np.exp(attack[ai] - defense[hi]), 0.05, 8.0)
    return lam, mu


def attach_market(pred):
    """给预测表加上市场概率（平均赔率和全市场最高赔率两套）和 RPS。"""
    mp, vig = market_probs(pred["OddHome"].to_numpy(float),
                           pred["OddDraw"].to_numpy(float),
                           pred["OddAway"].to_numpy(float))
    pred[["q_home", "q_draw", "q_away"]] = mp
    pred["vig"] = vig
    pred["rps_model"] = rps(pred[["p_home", "p_draw", "p_away"]].to_numpy(), pred["outcome"].to_numpy())
    pred["rps_market"] = rps(mp, pred["outcome"].to_numpy())
    has_max = pred[["MaxHome", "MaxDraw", "MaxAway"]].notna().all(axis=1)
    pred["rps_market_max"] = np.nan
    if has_max.any():
        sub = pred.loc[has_max]
        mmp, mvig = market_probs(sub["MaxHome"].to_numpy(float),
                                 sub["MaxDraw"].to_numpy(float),
                                 sub["MaxAway"].to_numpy(float))
        pred.loc[has_max, "rps_market_max"] = rps(mmp, sub["outcome"].to_numpy())
        pred.loc[has_max, "vig_max"] = mvig
    return pred


def paired_gap(pred, a="rps_model", b="rps_market"):
    """模型与市场的 RPS 配对差值：均值、标准误、t 值。

    必须用配对而不是各算各的均值——同一场比赛两个预测的 RPS 高度相关
    （难预测的比赛两边都高），配对后差值的标准差比单边小一个数量级，
    这正是 RPS 对照比 ROI 模拟灵敏得多的原因。
    """
    d = (pred[a] - pred[b]).dropna().to_numpy()
    n = len(d)
    mean = d.mean()
    se = d.std(ddof=1) / np.sqrt(n)
    return {"n": n, "gap": mean, "se": se, "t": mean / se if se > 0 else np.nan}
