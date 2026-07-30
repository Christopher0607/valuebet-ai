"""检验假设：用全市场最高赔率（比BK8还优惠）能否翻盘"""
import csv, math, sys
from datetime import date, datetime
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
        e={"date":d,"home":r['HomeTeam'].strip(),"away":r['AwayTeam'].strip(),
           "hg":int(float(r['FTHome'])),"ag":int(float(r['FTAway'])),
           "oh":float(r['OddHome']),"od":float(r['OddDraw']),"oa":float(r['OddAway']),"neutral":False}
        # 最高赔率，缺失就退回平均赔率
        for k,s2,fb in (("mh","MaxHome","oh"),("md","MaxDraw","od"),("ma","MaxAway","oa")):
            try: e[k]=float(r[s2]) if r.get(s2) else e[fb]
            except: e[k]=e[fb]
        rows.append(e)
    except: continue
rows.sort(key=lambda m:m["date"])
train=[m for m in rows if m["date"]<CUT]; test=[m for m in rows if m["date"]>=CUT]
ti,counts=build_team_index(train,min_matches=10)
at,df,ha,_=fit_parameters(train,ti)

# 先看两种赔率的抽水差多少
v1=v2=0.;n=0
for m in test:
    v1+=(1/m["oh"]+1/m["od"]+1/m["oa"]-1)*100
    v2+=(1/m["mh"]+1/m["md"]+1/m["ma"]-1)*100
    n+=1
print(f"平均赔率抽水: {v1/n:.2f}%")
print(f"最高赔率抽水: {v2/n:.2f}%   ← 比BK8还优惠的价格水平")
print()

def run(use_max, ev_min, kelly, cap, label):
    states={t:BayesianTeamState(t,at[t],df[t],n_historical_matches=counts.get(t,100)) for t in ti}
    bank=10000.;nb=0;nw=0;st=0.
    for m in test:
        h,a=m["home"],m["away"]
        if h not in states or a not in states: continue
        a1,d1=states[h].current_attack(),states[h].current_defense()
        a2,d2=states[a].current_attack(),states[a].current_defense()
        lam=max(0.05,min(6.,math.exp(a1-d2+ha)));mu=max(0.05,min(6.,math.exp(a2-d1)))
        w=dr=l=0.
        for x in range(9):
            px=poisson_pmf(x,lam)
            for y in range(9):
                p=px*poisson_pmf(y,mu)*dc_tau(x,y,lam,mu)
                if x>y:w+=p
                elif x<y:l+=p
                else:dr+=p
        t=w+dr+l; ps=(w/t,dr/t,l/t)
        os_=(m["mh"],m["md"],m["ma"]) if use_max else (m["oh"],m["od"],m["oa"])
        act=0 if m["hg"]>m["ag"] else 2 if m["hg"]<m["ag"] else 1
        for i in range(3):
            ev=ps[i]*os_[i]-1
            if ev<=ev_min: continue
            b=os_[i]-1;q=1-ps[i]
            f=max(0.,min((ps[i]*b-q)/b*kelly,cap))
            stake=bank*f
            if stake<1: continue
            st+=stake;nb+=1
            if i==act: bank+=stake*b;nw+=1
            else: bank-=stake
        states[h].update_after_match(m["hg"],states[a].current_defense())
        states[a].update_after_match(m["ag"],states[h].current_defense())
        states[h].update_defense_after_match(m["ag"],states[a].current_attack())
        states[a].update_defense_after_match(m["hg"],states[h].current_attack())
    roi=(bank-10000)/st*100 if st else 0
    print(f"{label:28s} 注数{nb:>4d} 命中{nw/nb if nb else 0:>6.1%} ROI{roi:>+8.2f}%  余额{bank:>9,.0f}")

run(False,0.03,0.5,0.15,"平均赔率 EV>3% 半凯利")
run(True, 0.03,0.5,0.15,"最高赔率 EV>3% 半凯利")
run(True, 0.05,0.5,0.15,"最高赔率 EV>5% 半凯利")
run(True, 0.10,0.5,0.15,"最高赔率 EV>10% 半凯利")
