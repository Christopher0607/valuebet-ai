"""决定性测试：我们的模型 vs 市场赔率，同一批比赛比 RPS。
数据集自带比分和赔率，用它自己的命名体系训练，绕开队名对齐问题。"""
import csv, math, sys
from datetime import date, datetime
sys.path.insert(0,'.')
sys.path.insert(0,'training')
from train_mle import build_team_index, fit_parameters
from app.model import poisson_pmf, dc_tau, calc_rps

WANT={"E0","SP1","I1","D1"}
CUT=date(2024,8,1)

rows=[]
for r in csv.DictReader(open('/tmp/m.csv')):
    if r['Division'] not in WANT: continue
    if not (r.get('OddHome') and r.get('OddDraw') and r.get('OddAway')): continue
    if not r.get('FTHome') or not r.get('FTAway'): continue
    try:
        d=datetime.strptime(r['MatchDate'],'%Y-%m-%d').date()
        if d < date(2014,8,1): continue
        rows.append({
            "date":d, "home":r['HomeTeam'].strip(), "away":r['AwayTeam'].strip(),
            "hg":int(float(r['FTHome'])), "ag":int(float(r['FTAway'])),
            "oh":float(r['OddHome']), "od":float(r['OddDraw']), "oa":float(r['OddAway']),
            "neutral":False,
        })
    except (ValueError, TypeError): continue

train=[m for m in rows if m["date"]<CUT]
test =[m for m in rows if m["date"]>=CUT]
print(f"训练集 {len(train)} 场（2014-08 ~ 2024-08）")
print(f"测试集 {len(test)} 场（2024-08之后，模型没见过）\n")

team_idx,_=build_team_index(train, min_matches=10)
attack,defense,home_adv,_=fit_parameters(train, team_idx)
print(f"训练完成: {len(team_idx)}队, 主场优势{home_adv:.4f}\n")

def model_probs(t1,t2):
    a1=attack.get(t1,0.0); d1=defense.get(t1,0.0)
    a2=attack.get(t2,0.0); d2=defense.get(t2,0.0)
    lam=max(0.05,min(6.0,math.exp(a1-d2+home_adv)))
    mu =max(0.05,min(6.0,math.exp(a2-d1)))
    w=dr=l=0.0
    for x in range(9):
        px=poisson_pmf(x,lam)
        for y in range(9):
            p=px*poisson_pmf(y,mu)*dc_tau(x,y,lam,mu)
            if x>y: w+=p
            elif x<y: l+=p
            else: dr+=p
    t=w+dr+l
    return w/t,dr/t,l/t

def market_probs(oh,od,oa):
    ih,idr,ia=1/oh,1/od,1/oa
    s=ih+idr+ia          # 抽水后总和>1
    return ih/s, idr/s, ia/s, (s-1)*100

m_rps=k_rps=0.0; m_hit=k_hit=0; n=0; vig_sum=0.0
for m in test:
    if m["home"] not in attack or m["away"] not in attack: continue
    ph,pd,pa=model_probs(m["home"],m["away"])
    qh,qd,qa,vig=market_probs(m["oh"],m["od"],m["oa"])
    actual="win1" if m["hg"]>m["ag"] else "win2" if m["hg"]<m["ag"] else "draw"
    m_rps+=calc_rps([ph,pd,pa],actual)
    k_rps+=calc_rps([qh,qd,qa],actual)
    mp="win1" if ph>pa and ph>pd else "win2" if pa>ph and pa>pd else "draw"
    kp="win1" if qh>qa and qh>qd else "win2" if qa>qh and qa>qd else "draw"
    if mp==actual: m_hit+=1
    if kp==actual: k_hit+=1
    vig_sum+=vig; n+=1

print("="*52)
print(f"测试样本: {n} 场")
print(f"庄家平均抽水: {vig_sum/n:.2f}%")
print()
print(f"{'':12s} {'准确率':>10s} {'RPS':>10s}")
print(f"{'我们的模型':12s} {m_hit/n:>9.1%} {m_rps/n:>10.4f}")
print(f"{'市场赔率':12s} {k_hit/n:>9.1%} {k_rps/n:>10.4f}")
print(f"{'随机基准':12s} {'33.3%':>10s} {'0.2450':>10s}")
print()
diff=(m_rps/n)-(k_rps/n)
if diff<0:
    print(f"✅ 模型 RPS 比市场低 {abs(diff):.4f} —— 有优势")
else:
    print(f"❌ 模型 RPS 比市场高 {diff:.4f} —— 没有优势，模型不如市场准")
