"""测试2: 只做串关，2-6腿，EV>5%，全凯利，单注上限10%。用贝叶斯版本（更强的那个）。"""
import csv, math, sys, itertools
from datetime import date, datetime
from collections import defaultdict
sys.path.insert(0,'.'); sys.path.insert(0,'training')
from train_mle import build_team_index, fit_parameters
from app.model import poisson_pmf, dc_tau, BayesianTeamState

WANT={"E0","SP1","I1","D1"}; CUT=date(2024,8,1)
rows=[]
for r in csv.DictReader(open('/tmp/m.csv')):
    if r['Division'] not in WANT: continue
    if not (r.get('OddHome') and r.get('OddDraw') and r.get('OddAway')): continue
    if not r.get('FTHome') or not r.get('FTAway'): continue
    try:
        d=datetime.strptime(r['MatchDate'],'%Y-%m-%d').date()
        if d<date(2014,8,1): continue
        rows.append({"date":d,"home":r['HomeTeam'].strip(),"away":r['AwayTeam'].strip(),
            "hg":int(float(r['FTHome'])),"ag":int(float(r['FTAway'])),
            "oh":float(r['OddHome']),"od":float(r['OddDraw']),"oa":float(r['OddAway']),"neutral":False})
    except: continue
rows.sort(key=lambda m:m["date"])
train=[m for m in rows if m["date"]<CUT]; test=[m for m in rows if m["date"]>=CUT]
team_idx,counts=build_team_index(train,min_matches=10)
attack,defense,home_adv,_=fit_parameters(train,team_idx)
states={t: BayesianTeamState(t,attack[t],defense[t],n_historical_matches=counts.get(t,100)) for t in team_idx}

def probs(h,a):
    a1,d1=states[h].current_attack(),states[h].current_defense()
    a2,d2=states[a].current_attack(),states[a].current_defense()
    lam=max(0.05,min(6.0,math.exp(a1-d2+home_adv)));mu=max(0.05,min(6.0,math.exp(a2-d1)))
    w=dr=l=0.0
    for x in range(9):
        px=poisson_pmf(x,lam)
        for y in range(9):
            p=px*poisson_pmf(y,mu)*dc_tau(x,y,lam,mu)
            if x>y:w+=p
            elif x<y:l+=p
            else:dr+=p
    t=w+dr+l; return (w/t,dr/t,l/t)

# 按日期分组，同一天的比赛才能串
byday=defaultdict(list)
for m in test: byday[m["date"]].append(m)

EV_MIN=0.05; KELLY=1.0; CAP=0.10; MINLEG,MAXLEG=2,6
BANK=10000.0; bank=BANK; nbets=0; nwin=0; staked=0.0; leg_hist=defaultdict(int)

for d in sorted(byday):
    day=byday[d]
    cands=[]
    for m in day:
        h,a=m["home"],m["away"]
        if h not in states or a not in states: continue
        ps=probs(h,a); os_=(m["oh"],m["od"],m["oa"])
        actual=0 if m["hg"]>m["ag"] else 2 if m["hg"]<m["ag"] else 1
        for i in range(3):
            ev=ps[i]*os_[i]-1
            if ev>EV_MIN:
                cands.append({"m":id(m),"p":ps[i],"o":os_[i],"ev":ev,"hit":(i==actual)})
    # 同一场只能出现一次；按EV排序取前12控制组合数
    cands.sort(key=lambda c:-c["ev"]); cands=cands[:12]
    best=None
    for k in range(MINLEG,MAXLEG+1):
        for combo in itertools.combinations(cands,k):
            if len({c["m"] for c in combo})!=k: continue
            jp=1.0; jo=1.0
            for c in combo: jp*=c["p"]; jo*=c["o"]
            ev=jp*jo-1
            if ev<=EV_MIN: continue
            if best is None or ev>best["ev"]: best={"combo":combo,"jp":jp,"jo":jo,"ev":ev}
    if not best: continue
    b=best["jo"]-1; q=1-best["jp"]
    f=max(0.0,min((best["jp"]*b-q)/b*KELLY,CAP))
    stake=bank*f
    if stake<1: continue
    staked+=stake; nbets+=1; leg_hist[len(best["combo"])]+=1
    if all(c["hit"] for c in best["combo"]):
        bank+=stake*b; nwin+=1
    else:
        bank-=stake
    # 结算完再更新后验
    for m in day:
        h,a=m["home"],m["away"]
        if h in states and a in states:
            states[h].update_after_match(m["hg"],states[a].current_defense())
            states[a].update_after_match(m["ag"],states[h].current_defense())
            states[h].update_defense_after_match(m["ag"],states[a].current_attack())
            states[a].update_defense_after_match(m["hg"],states[h].current_attack())

print("="*56)
print(f"策略: 只串关 {MINLEG}-{MAXLEG}腿, EV>{EV_MIN:.0%}, 全凯利, 上限{CAP:.0%}")
print("="*56)
print(f"起始资金  {BANK:,.0f}")
print(f"下注      {nbets} 注 (总投入 {staked:,.0f})")
print(f"命中      {nwin} 注 ({nwin/nbets:.1%})" if nbets else "无下注")
print(f"腿数分布  {dict(sorted(leg_hist.items()))}")
print(f"最终资金  {bank:,.2f}")
print(f"净盈亏    {bank-BANK:+,.0f}  ({(bank-BANK)/BANK:+.1%})")
print(f"ROI       {(bank-BANK)/staked*100:+.2f}%" if staked else "")
