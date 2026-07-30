"""集成实验：MLE(进球) + MLE(xG) + Elo + 近期状态 → 逻辑回归融合
纪律：训练集内部再切一刀做融合权重的拟合，保留集只在最后验一次。"""
import csv, math, json, sys
import numpy as np
from datetime import date, datetime
from scipy.optimize import minimize
from scipy.special import gammaln
sys.path.insert(0,'.')
from app.model import poisson_pmf, dc_tau, calc_rps

CUT=date(2021,7,1); mapping=json.load(open('/tmp/name_map.json'))
xg={}
for m in csv.DictReader(open('/tmp/xg_matches.csv')):
    xg[(m['date'],m['home'],int(m['hg']),int(m['ag']))]=m

rows=[]
for r in csv.DictReader(open('/tmp/m.csv')):
    if r['Division'] not in {"E0","SP1","I1","D1","F1"}: continue
    if not (r.get('OddHome') and r.get('OddDraw') and r.get('OddAway') and r.get('FTHome')): continue
    h=mapping.get(r['HomeTeam'])
    if not h: continue
    try:
        hg,ag=int(float(r['FTHome'])),int(float(r['FTAway']))
        k=(r['MatchDate'],h,hg,ag)
        if k not in xg: continue
        m=xg[k]
        e={"date":datetime.strptime(r['MatchDate'],'%Y-%m-%d').date(),
           "home":h,"away":m['away'],"hg":hg,"ag":ag,
           "hxg":float(m['hxg']),"axg":float(m['axg']),
           "oh":float(r['OddHome']),"od":float(r['OddDraw']),"oa":float(r['OddAway'])}
        e["eloh"]=float(r['HomeElo']) if r.get('HomeElo') else 1500.
        e["eloa"]=float(r['AwayElo']) if r.get('AwayElo') else 1500.
        e["f5h"]=float(r['Form5Home']) if r.get('Form5Home') else 7.5
        e["f5a"]=float(r['Form5Away']) if r.get('Form5Away') else 7.5
        rows.append(e)
    except: continue
rows.sort(key=lambda m:m["date"])
train=[m for m in rows if m["date"]<CUT]; hold=[m for m in rows if m["date"]>=CUT]

# 训练集内部再切：前80%拟合基模型，后20%学融合权重（避免用保留集调权重）
split=int(len(train)*0.8)
base_tr, blend_tr = train[:split], train[split:]
print(f"基模型训练 {len(base_tr)} | 融合权重训练 {len(blend_tr)} | 保留集 {len(hold)}\n")

teams=sorted({m["home"] for m in base_tr}|{m["away"] for m in base_tr})
tidx={t:i for i,t in enumerate(teams)}; n=len(teams)

def fit(th,ta,ms,hl=540):
    hi=np.array([tidx[m["home"]] for m in ms]); ai=np.array([tidx[m["away"]] for m in ms])
    yh=np.array([th(m) for m in ms],float); ya=np.array([ta(m) for m in ms],float)
    ref=max(m["date"] for m in ms)
    w=np.array([0.5**(((ref-m["date"]).days)/hl) for m in ms])
    def nll(p):
        at,df,ha=p[:n],p[n:2*n],p[2*n]
        lam=np.exp(at[hi]-df[ai]+ha); mu=np.exp(at[ai]-df[hi])
        ll=yh*np.log(lam)-lam-gammaln(yh+1)+ya*np.log(mu)-mu-gammaln(ya+1)
        return -np.sum(w*ll)+0.01*np.sum(p[:2*n]**2)
    def gr(p):
        at,df,ha=p[:n],p[n:2*n],p[2*n]
        lam=np.exp(at[hi]-df[ai]+ha); mu=np.exp(at[ai]-df[hi])
        rh=w*(yh-lam); ra=w*(ya-mu)
        ga=np.zeros(n); gd=np.zeros(n)
        np.add.at(ga,hi,-rh); np.add.at(ga,ai,-ra)
        np.add.at(gd,ai,rh);  np.add.at(gd,hi,ra)
        g=np.concatenate([ga,gd,[-np.sum(rh)]]); g[:2*n]+=0.02*p[:2*n]; return g
    x0=np.concatenate([np.zeros(n),np.zeros(n),[0.25]])
    r=minimize(nll,x0,jac=gr,method="L-BFGS-B",options={"maxiter":2000})
    return r.x[:n],r.x[n:2*n],r.x[2*n]

atG,dfG,haG=fit(lambda m:m["hg"],lambda m:m["ag"],base_tr)
atX,dfX,haX=fit(lambda m:m["hxg"],lambda m:m["axg"],base_tr)

def dcprobs(at,df,ha,h,a):
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
    t=w+dr+l; return np.array([w/t,dr/t,l/t])

def feats(m):
    pg=dcprobs(atG,dfG,haG,m["home"],m["away"]); px=dcprobs(atX,dfX,haX,m["home"],m["away"])
    if pg is None or px is None: return None
    ed=(m["eloh"]-m["eloa"])/400.
    fd=(m["f5h"]-m["f5a"])/15.
    return np.concatenate([np.log(pg+1e-9), np.log(px+1e-9), [ed, fd, 1.0]])

def softmax(z):
    z=z-z.max(); e=np.exp(z); return e/e.sum()

X=[];Y=[]
for m in blend_tr:
    f=feats(m)
    if f is None: continue
    X.append(f); Y.append(0 if m["hg"]>m["ag"] else 1 if m["hg"]==m["ag"] else 2)
X=np.array(X); Y=np.array(Y); D=X.shape[1]
print(f"融合训练样本 {len(X)}，特征维度 {D}")

def mnll(w):
    W=w.reshape(3,D); ll=0.
    for i in range(len(X)):
        p=softmax(W@X[i]); ll+=np.log(p[Y[i]]+1e-12)
    return -ll/len(X)+0.02*np.sum(w**2)
r=minimize(mnll, np.zeros(3*D), method="L-BFGS-B", options={"maxiter":400})
W=r.x.reshape(3,D)
print("融合权重训练完成\n")

res={k:[0.,0] for k in ["进球MLE","xG MLE","集成(含Elo+状态)","市场"]}
ne=0
for m in hold:
    f=feats(m)
    if f is None: continue
    pg=dcprobs(atG,dfG,haG,m["home"],m["away"]); px=dcprobs(atX,dfX,haX,m["home"],m["away"])
    pe=softmax(W@f)
    ih,idr,ia=1/m["oh"],1/m["od"],1/m["oa"]; s=ih+idr+ia; pk=np.array([ih/s,idr/s,ia/s])
    act="win1" if m["hg"]>m["ag"] else "win2" if m["hg"]<m["ag"] else "draw"
    for nm,p in (("进球MLE",pg),("xG MLE",px),("集成(含Elo+状态)",pe),("市场",pk)):
        res[nm][0]+=calc_rps(list(p),act)
        pr="win1" if p[0]>p[2] and p[0]>p[1] else "win2" if p[2]>p[0] and p[2]>p[1] else "draw"
        if pr==act: res[nm][1]+=1
    ne+=1

print("="*58)
print(f"保留集验证 ({ne} 场)")
print("="*58)
mk=res["市场"][0]/ne
print(f"{'':22s}{'准确率':>9s}{'RPS':>10s}")
for nm in ["进球MLE","xG MLE","集成(含Elo+状态)","市场"]:
    r_,h_=res[nm]
    mark="" if nm=="市场" else ("  ✅赢市场" if r_/ne<mk else "  ❌")
    print(f"{nm:22s}{h_/ne:>8.1%}{r_/ne:>10.4f}{mark}")
