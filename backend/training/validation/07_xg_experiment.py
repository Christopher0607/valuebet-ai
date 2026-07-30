"""xG 实验：用 xG 拟合球队强度 vs 用进球拟合，在保留集上跟市场对照。
纪律：训练 2014-08 ~ 2021-06；保留集 2021-07 之后，只验一次。"""
import csv, math, json, sys
import numpy as np
from datetime import date, datetime
from scipy.optimize import minimize
from scipy.special import gammaln
sys.path.insert(0,'.')
from app.model import poisson_pmf, dc_tau, calc_rps

CUT = date(2021,7,1)
mapping = json.load(open('/tmp/name_map.json'))

# 合并 xG + 赔率
xg = {}
for m in csv.DictReader(open('/tmp/xg_matches.csv')):
    xg[(m['date'], m['home'], int(m['hg']), int(m['ag']))] = m

rows=[]
for r in csv.DictReader(open('/tmp/m.csv')):
    if r['Division'] not in {"E0","SP1","I1","D1","F1"}: continue
    if not (r.get('OddHome') and r.get('OddDraw') and r.get('OddAway') and r.get('FTHome')): continue
    h = mapping.get(r['HomeTeam'])
    if not h: continue
    try:
        hg,ag = int(float(r['FTHome'])), int(float(r['FTAway']))
        k = (r['MatchDate'], h, hg, ag)
        if k not in xg: continue
        m = xg[k]
        e = {"date": datetime.strptime(r['MatchDate'],'%Y-%m-%d').date(),
             "home": h, "away": m['away'], "hg":hg, "ag":ag,
             "hxg": float(m['hxg']), "axg": float(m['axg']),
             "oh":float(r['OddHome']), "od":float(r['OddDraw']), "oa":float(r['OddAway'])}
        for k2,s2 in (("mh","MaxHome"),("md","MaxDraw"),("ma","MaxAway")):
            try: e[k2]=float(r[s2]) if r.get(s2) else e[{"mh":"oh","md":"od","ma":"oa"}[k2]]
            except: e[k2]=e[{"mh":"oh","md":"od","ma":"oa"}[k2]]
        rows.append(e)
    except: continue

rows.sort(key=lambda m:m["date"])
train=[m for m in rows if m["date"]<CUT]
hold =[m for m in rows if m["date"]>=CUT]
print(f"合并后 {len(rows)} 场（xG+赔率都有）")
print(f"  训练集 {len(train)} 场")
print(f"  保留集 {len(hold)} 场  ← 只验一次\n")

teams = sorted({m["home"] for m in train} | {m["away"] for m in train})
tidx = {t:i for i,t in enumerate(teams)}
n = len(teams)

def fit(target_home, target_away, matches, half_life_days=540):
    hi = np.array([tidx[m["home"]] for m in matches])
    ai = np.array([tidx[m["away"]] for m in matches])
    yh = np.array([target_home(m) for m in matches], float)
    ya = np.array([target_away(m) for m in matches], float)
    ref = max(m["date"] for m in matches)
    w = np.array([0.5**(((ref-m["date"]).days)/half_life_days) for m in matches])

    def nll(p):
        at,df,ha = p[:n], p[n:2*n], p[2*n]
        lam = np.exp(at[hi]-df[ai]+ha); mu = np.exp(at[ai]-df[hi])
        ll = yh*np.log(lam)-lam-gammaln(yh+1) + ya*np.log(mu)-mu-gammaln(ya+1)
        return -np.sum(w*ll) + 0.01*np.sum(p[:2*n]**2)

    def grad(p):
        at,df,ha = p[:n], p[n:2*n], p[2*n]
        lam = np.exp(at[hi]-df[ai]+ha); mu = np.exp(at[ai]-df[hi])
        rh = w*(yh-lam); ra = w*(ya-mu)
        ga = np.zeros(n); gd = np.zeros(n)
        np.add.at(ga, hi, -rh); np.add.at(ga, ai, -ra)
        np.add.at(gd, ai,  rh); np.add.at(gd, hi,  ra)
        g = np.concatenate([ga, gd, [-np.sum(rh)]])
        g[:2*n] += 0.02*p[:2*n]
        return g

    x0 = np.concatenate([np.zeros(n), np.zeros(n), [0.25]])
    res = minimize(nll, x0, jac=grad, method="L-BFGS-B", options={"maxiter":2000})
    return res.x[:n], res.x[n:2*n], res.x[2*n]

print("拟合 A: 用【实际进球】（现有做法）...")
atG,dfG,haG = fit(lambda m:m["hg"], lambda m:m["ag"], train)
print("拟合 B: 用【xG】（新做法）...")
atX,dfX,haX = fit(lambda m:m["hxg"], lambda m:m["axg"], train)
print(f"  主场优势: 进球版 {haG:.4f} / xG版 {haX:.4f}\n")

def probs(at,df,ha,h,a):
    if h not in tidx or a not in tidx: return None
    lam=max(0.05,min(6.,math.exp(at[tidx[h]]-df[tidx[a]]+ha)))
    mu =max(0.05,min(6.,math.exp(at[tidx[a]]-df[tidx[h]])))
    w=dr=l=0.
    for x in range(9):
        px=poisson_pmf(x,lam)
        for y in range(9):
            p=px*poisson_pmf(y,mu)*dc_tau(x,y,lam,mu)
            if x>y:w+=p
            elif x<y:l+=p
            else:dr+=p
    t=w+dr+l; return (w/t,dr/t,l/t)

res={"进球模型":[0,0],"xG模型":[0,0],"混合(50/50)":[0,0],"市场":[0,0]}
n_eval=0
for m in hold:
    pg=probs(atG,dfG,haG,m["home"],m["away"])
    px=probs(atX,dfX,haX,m["home"],m["away"])
    if pg is None or px is None: continue
    pm=tuple((a+b)/2 for a,b in zip(pg,px))
    ih,idr,ia=1/m["oh"],1/m["od"],1/m["oa"]; s=ih+idr+ia
    pk=(ih/s,idr/s,ia/s)
    act="win1" if m["hg"]>m["ag"] else "win2" if m["hg"]<m["ag"] else "draw"
    for name,p in (("进球模型",pg),("xG模型",px),("混合(50/50)",pm),("市场",pk)):
        res[name][0]+=calc_rps(list(p),act)
        pr="win1" if p[0]>p[2] and p[0]>p[1] else "win2" if p[2]>p[0] and p[2]>p[1] else "draw"
        if pr==act: res[name][1]+=1
    n_eval+=1

print("="*56)
print(f"保留集验证 ({n_eval} 场，模型从未见过)")
print("="*56)
print(f"{'':16s}{'准确率':>10s}{'RPS':>10s}")
mk=res["市场"][0]/n_eval
for name in ["进球模型","xG模型","混合(50/50)","市场"]:
    r,h=res[name]
    mark=""
    if name!="市场":
        mark = "  ✅赢市场" if r/n_eval < mk else "  ❌"
    print(f"{name:16s}{h/n_eval:>9.1%}{r/n_eval:>10.4f}{mark}")
json.dump({"atX":atX.tolist(),"dfX":dfX.tolist(),"haX":haX,
           "atG":atG.tolist(),"dfG":dfG.tolist(),"haG":haG,
           "teams":teams}, open('/tmp/xg_params.json','w'))
