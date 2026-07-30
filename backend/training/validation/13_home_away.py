"""
变体实验：主客场分离的攻防参数。

08_MARKET_VALIDATION.md 把这个列为「优先试」的第一位，理由是现在只有一个
全局主场优势常数，但各队主客场表现差异不同。

现有结构：  lam = exp(att[主] - def[客] + home_adv)      2n+1 个参数
本变体：    lam = exp(att_h[主] - def_a[客])              4n 个参数
            mu  = exp(att_a[客] - def_h[主])
home_adv 被吸收进 att_h/def_h，不再单列。

预期（写在跑之前，避免事后合理化）：收益很可能是负的。参数量翻倍，而每队
每赛季只有约 19 个主场，每个参数的有效样本减半，估计噪声的增加大概率吃掉
结构上的改进。这个实验的价值在于把它测掉，不是指望它赢。

用完全相同的走查骨架和完全相同的比赛集合，所以差异只来自参数化本身。
"""
import time
import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.special import gammaln
import lib_walkforward as L


def _nll_grad_ha(params, hi, ai, hg, ag, w, n, ridge):
    """主客场分离版的负对数似然与解析梯度。

    参数排布: [att_h(n), def_h(n), att_a(n), def_a(n)]
      lam = exp(att_h[hi] - def_a[ai])     主队进球率
      mu  = exp(att_a[ai] - def_h[hi])     客队进球率
    梯度仍是「残差 = 真实 - 预期」的标准泊松形式，只是每个球队的主场角色和
    客场角色现在落在不同的参数上，累加时不再共用同一个下标。
    """
    ath, dfh, ata, dfa = params[:n], params[n:2*n], params[2*n:3*n], params[3*n:]
    lam = np.exp(ath[hi] - dfa[ai])
    mu = np.exp(ata[ai] - dfh[hi])

    ll = w * (hg * np.log(lam) - lam - gammaln(hg + 1)
              + ag * np.log(mu) - mu - gammaln(ag + 1))
    nll = -ll.sum() + ridge * np.sum(params ** 2)

    rh = w * (hg - lam)
    ra = w * (ag - mu)
    g = np.zeros(4 * n)
    np.add.at(g[:n], hi, -rh)          # att_h
    np.add.at(g[n:2*n], hi, ra)        # def_h（出现在 mu 里，带负号）
    np.add.at(g[2*n:3*n], ai, -ra)     # att_a
    np.add.at(g[3*n:], ai, rh)         # def_a（出现在 lam 里，带负号）
    g += 2 * ridge * params
    return nll, g


def _verify_gradient(n_teams=6, seed=0):
    """按 CLAUDE.md 的要求，先验证解析梯度再拿去训练。

    这个项目历史上抓到的真 bug（贝叶斯更新方向反了）正是方向性错误，
    手推梯度的符号错最容易以「能跑、结果略差」的形式蒙混过去。
    """
    from scipy.optimize import approx_fprime
    rng = np.random.RandomState(seed)
    m = 200
    hi = rng.randint(0, n_teams, m)
    ai = (hi + 1 + rng.randint(0, n_teams - 1, m)) % n_teams
    hg = rng.poisson(1.4, m).astype(float)
    ag = rng.poisson(1.1, m).astype(float)
    w = rng.uniform(0.5, 1.0, m)
    x = rng.randn(4 * n_teams) * 0.3
    _, ana = _nll_grad_ha(x, hi, ai, hg, ag, w, n_teams, 0.1)
    num = approx_fprime(x, lambda p: _nll_grad_ha(p, hi, ai, hg, ag, w, n_teams, 0.1)[0], 1e-6)
    err = np.abs(ana - num) / (np.abs(ana) + 1e-6)
    return err.max()


def fit_ha(hi, ai, hg, ag, w, n_teams, ridge, state):
    x0 = state if state is not None else np.zeros(4 * n_teams)
    if state is None:
        x0[:n_teams] = 0.25          # att_h 起点带上主场优势的量级
    res = minimize(_nll_grad_ha, x0, jac=True,
                   args=(hi, ai, hg, ag, w, n_teams, ridge),
                   method="L-BFGS-B", options={"maxiter": 500})
    return res.x


def predict_ha(state, hi, ai, n_teams):
    n = n_teams
    ath, dfh, ata, dfa = state[:n], state[n:2*n], state[2*n:3*n], state[3*n:]
    lam = np.clip(np.exp(ath[hi] - dfa[ai]), 0.05, 8.0)
    mu = np.clip(np.exp(ata[ai] - dfh[hi]), 0.05, 8.0)
    return lam, mu


if __name__ == "__main__":
    err = _verify_gradient()
    print(f"解析梯度校验：最大相对误差 {err:.2e}")
    assert err < 1e-4, f"梯度公式有误（{err:.2e}），拒绝继续"
    print("梯度校验通过\n")

    df = L.load_matches()
    t0 = time.time()
    print("跑主客场分离变体（走查骨架、超参、比赛集合全部与基线一致）...")
    ha = L.run_walkforward(df, L.DEV_END, L.TEST_END,
                           fit_fn=fit_ha, predict_fn=predict_ha, verbose=False)
    ha = L.attach_market(ha)
    print(f"完成，用时 {time.time() - t0:.0f}s，{len(ha):,} 场\n")

    base = pd.read_parquet("walkforward_test.parquet")

    # 对齐到两个变体都预测了的比赛，否则比的是不同的样本
    key = ["MatchDate", "home", "away"]
    b = base.set_index(key)
    h = ha.set_index(key)
    common = b.index.intersection(h.index)
    b, h = b.loc[common], h.loc[common]
    print(f"共同比赛 {len(common):,} 场\n")

    print("=" * 58)
    print(f"{'':22s} {'准确率':>9s} {'RPS':>10s}")
    for name, d in [("基线（全局主场优势）", b), ("主客场分离参数", h)]:
        acc = (d[["p_home", "p_draw", "p_away"]].to_numpy().argmax(1) == d.outcome).mean()
        print(f"{name:22s} {acc:>8.1%} {d.rps_model.mean():>10.5f}")
    print(f"{'市场':22s} {'':>9s} {b.rps_market.mean():>10.5f}")

    d = (h.rps_model.to_numpy() - b.rps_model.to_numpy())
    se = d.std(ddof=1) / np.sqrt(len(d))
    print(f"\n变体 - 基线: {d.mean():+.5f} ±{se:.5f}   t = {d.mean()/se:+.1f}")
    print("（负数 = 主客场分离更好）")

    gh = L.paired_gap(h)
    print(f"\n主客场分离 vs 市场缺口: {gh['gap']:+.5f} ±{gh['se']:.5f}  t={gh['t']:+.1f}")
    print("=" * 58)

    ha.to_parquet("walkforward_homeaway.parquet", index=False)
