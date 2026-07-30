"""扫描赔率区间：有没有哪个区间模型是赚钱的"""
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
        for k,s2,fb in (("mh","MaxHome","oh"),("md","MaxDraw","od"),("ma","MaxAway","oa")):
            try: e[k]=float(r[s2]) if r.get(s2) else e[fb]
            except: e[k]=e[fb]
        rows.append(e)
    except: continue
rows.sort(key=lambda m:m["date"])
train=[m for m in rows if m["date"]<CUT]; test=[m for m in rows if m["date"]>=CUT]
ti,counts=build_team_index(train,min_matches=10)
at,df,ha,_=fit_parameters(train,ti)

def run(lo, hi, label):
    states={t:BayesianTeamState(t,at[t],df[t],n_historical_matches=counts.get(t,100)) for t in ti}
    bank=10000.;nb=0;nw=0;st=0.;flat_pnl=0.
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
        t=w+dr+l; ps=(w/t,dr/t,l/t); os_=(m["mh"],m["md"],m["ma"])
        act=0 if m["hg"]>m["ag"] else 2 if m["hg"]<m["ag"] else 1
        for i in range(3):
            if not (lo <= os_[i] < hi): continue      # 赔率区间过滤
            ev=ps[i]*os_[i]-1
            if ev<=0.03: continue
            b=os_[i]-1;q=1-ps[i]
            f=max(0.,min((ps[i]*b-q)/b*0.5,0.15))
            stake=bank*f
            if stake<1: continue
            st+=stake;nb+=1
            # 同时记录平注100的盈亏，排除凯利复利的干扰
            if i==act: bank+=stake*b;nw+=1;flat_pnl+=100*b
            else: bank-=stake;flat_pnl-=100
        states[h].update_after_match(m["hg"],states[a].current_defense())
        states[a].update_after_match(m["ag"],states[h].current_defense())
        states[h].update_defense_after_match(m["ag"],states[a].current_attack())
        states[a].update_defense_after_match(m["hg"],states[h].current_attack())
    if nb==0:
        print(f"{label:20s} 无符合条件的注")
        return
    roi=(bank-10000)/st*100
    flat_roi=flat_pnl/(nb*100)*100
    mark="✅赚" if flat_roi>0 else "❌"
    print(f"{label:20s} 注数{nb:>4d} 命中{nw/nb:>6.1%} 凯利ROI{roi:>+8.2f}% 平注ROI{flat_roi:>+8.2f}% {mark}")

print("赔率区间扫描（全用最高赔率 + 贝叶斯 + EV>3%）")
print("="*76)
run(1.0, 99, "全部（基准）")
run(1.0, 2.0, "赔率 1.0-2.0（热门）")
run(2.0, 99, "赔率 >2.0  ← 你问的")
run(2.0, 3.0, "赔率 2.0-3.0")
run(3.0, 5.0, "赔率 3.0-5.0")
run(5.0, 99, "赔率 >5.0（大冷）")
