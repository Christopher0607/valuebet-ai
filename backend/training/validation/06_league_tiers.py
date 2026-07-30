"""测试：不同级别联赛，模型 vs 市场；同时看用最高赔率能提升多少"""
import csv, math, sys
from datetime import date, datetime
sys.path.insert(0,'.'); sys.path.insert(0,'training')
from train_mle import build_team_index, fit_parameters
from app.model import poisson_pmf, dc_tau, calc_rps

TIERS={
 "英超":["E0"], "英冠":["E1"], "英甲":["E2"], "英乙":["E3"],
 "西甲":["SP1"], "西乙":["SP2"], "意甲":["I1"], "意乙":["I2"],
 "德甲":["D1"], "德乙":["D2"], "法甲":["F1"], "法乙":["F2"],
}
CUT=date(2024,8,1)
raw=list(csv.DictReader(open('/tmp/m.csv')))

def load(divs):
    out=[]
    for r in raw:
        if r['Division'] not in divs: continue
        if not (r.get('OddHome') and r.get('OddDraw') and r.get('OddAway')): continue
        if not r.get('FTHome') or not r.get('FTAway'): continue
        try:
            d=datetime.strptime(r['MatchDate'],'%Y-%m-%d').date()
            if d<date(2014,8,1): continue
            e={"date":d,"home":r['HomeTeam'].strip(),"away":r['AwayTeam'].strip(),
               "hg":int(float(r['FTHome'])),"ag":int(float(r['FTAway'])),
               "oh":float(r['OddHome']),"od":float(r['OddDraw']),"oa":float(r['OddAway']),
               "neutral":False}
            for k,src in (("mh","MaxHome"),("md","MaxDraw"),("ma","MaxAway")):
                try: e[k]=float(r[src]) if r.get(src) else e[{"mh":"oh","md":"od","ma":"oa"}[k]]
                except: e[k]=e[{"mh":"oh","md":"od","ma":"oa"}[k]]
            out.append(e)
        except: continue
    return out

print(f"{'联赛':6s}{'测试场次':>8s}{'模型RPS':>10s}{'市场RPS':>10s}{'差距':>9s}{'结论':>8s}")
print("="*54)
results={}
for name,divs in TIERS.items():
    rows=load(divs); rows.sort(key=lambda m:m["date"])
    tr=[m for m in rows if m["date"]<CUT]; te=[m for m in rows if m["date"]>=CUT]
    if len(tr)<2000 or len(te)<200: 
        print(f"{name:6s}  数据不足，跳过")
        continue
    ti,_=build_team_index(tr,min_matches=10)
    at,df,ha,_=fit_parameters(tr,ti)
    def pr(h,a):
        a1=at.get(h,0.);d1=df.get(h,0.);a2=at.get(a,0.);d2=df.get(a,0.)
        lam=max(0.05,min(6.,math.exp(a1-d2+ha)));mu=max(0.05,min(6.,math.exp(a2-d1)))
        w=dr=l=0.
        for x in range(9):
            px=poisson_pmf(x,lam)
            for y in range(9):
                p=px*poisson_pmf(y,mu)*dc_tau(x,y,lam,mu)
                if x>y:w+=p
                elif x<y:l+=p
                else:dr+=p
        t=w+dr+l;return w/t,dr/t,l/t
    mr=kr=0.;n=0
    for m in te:
        if m["home"] not in at or m["away"] not in at: continue
        ph,pd,pa=pr(m["home"],m["away"])
        ih,idr,ia=1/m["oh"],1/m["od"],1/m["oa"];s=ih+idr+ia
        act="win1" if m["hg"]>m["ag"] else "win2" if m["hg"]<m["ag"] else "draw"
        mr+=calc_rps([ph,pd,pa],act);kr+=calc_rps([ih/s,idr/s,ia/s],act);n+=1
    if n<150: continue
    diff=mr/n-kr/n
    results[name]=(at,df,ha,te,diff)
    flag="✅赢市场" if diff<0 else "❌"
    print(f"{name:6s}{n:>8d}{mr/n:>10.4f}{kr/n:>10.4f}{diff:>+9.4f}{flag:>10s}")
