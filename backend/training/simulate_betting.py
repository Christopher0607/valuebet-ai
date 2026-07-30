"""模拟：完全按当前系统的规则（EV>3%就下注，半凯利仓位）跑一遍测试集，看真实盈亏。"""
import csv, math, sys
from datetime import date, datetime
sys.path.insert(0,'.'); sys.path.insert(0,'training')
from train_mle import build_team_index, fit_parameters
from app.model import poisson_pmf, dc_tau

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

train=[m for m in rows if m["date"]<CUT]; test=[m for m in rows if m["date"]>=CUT]
team_idx,_=build_team_index(train,min_matches=10)
attack,defense,home_adv,_=fit_parameters(train,team_idx)

def probs(t1,t2):
    a1=attack.get(t1,0.0);d1=defense.get(t1,0.0);a2=attack.get(t2,0.0);d2=defense.get(t2,0.0)
    lam=max(0.05,min(6.0,math.exp(a1-d2+home_adv)));mu=max(0.05,min(6.0,math.exp(a2-d1)))
    w=dr=l=0.0
    for x in range(9):
        px=poisson_pmf(x,lam)
        for y in range(9):
            p=px*poisson_pmf(y,mu)*dc_tau(x,y,lam,mu)
            if x>y:w+=p
            elif x<y:l+=p
            else:dr+=p
    t=w+dr+l; return w/t,dr/t,l/t

BANK=10000.0; bank=BANK; nbets=0; nwin=0; staked=0.0
EV_MIN=0.03; KELLY=0.5; CAP=0.15
for m in test:
    if m["home"] not in attack or m["away"] not in attack: continue
    ps=probs(m["home"],m["away"]); os_=(m["oh"],m["od"],m["oa"])
    actual=0 if m["hg"]>m["ag"] else 2 if m["hg"]<m["ag"] else 1
    for i in range(3):
        ev=ps[i]*os_[i]-1
        if ev<=EV_MIN: continue
        b=os_[i]-1; q=1-ps[i]
        f=max(0.0,min((ps[i]*b-q)/b*KELLY,CAP))
        stake=bank*f
        if stake<1: continue
        staked+=stake; nbets+=1
        if i==actual: bank+=stake*b; nwin+=1
        else: bank-=stake
print("="*52)
print(f"起始资金: {BANK:,.0f}")
print(f"下注场次: {nbets} 注（总投入 {staked:,.0f}）")
print(f"命中: {nwin} 注 ({nwin/nbets:.1%})" if nbets else "无下注")
print(f"最终资金: {bank:,.0f}")
print(f"净盈亏: {bank-BANK:+,.0f}  ({(bank-BANK)/BANK:+.1%})")
print(f"ROI: {(bank-BANK)/staked*100:+.2f}%" if staked else "")
