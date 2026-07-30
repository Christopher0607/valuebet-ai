"""
唯一一条数学上确定为正的路子有多大？——跨平台套利的历史规模测量。

背景
----
09 号文档确认建模路线关闭之后，剩下的方向里只有跨平台套利（sure bet）
在数学上是确定为正的：如果 1/最高主胜赔率 + 1/最高平局赔率 + 1/最高客胜赔率 < 1，
那么按比例分配资金在三个平台各下一注，无论结果如何都盈利，不需要任何预测模型。

但「数学上为正」和「值得做」是两回事。这个脚本用 Matches.csv 里的
MaxHome/MaxDraw/MaxAway（全市场最高赔率）测量它的真实规模：
出现频率多高、利润率多厚、够不够覆盖成本。

必须说清楚的三个高估来源
------------------------
1. MaxHome/MaxDraw/MaxAway 是**各自独立取的历史最高值**，不保证同一时刻
   在三家平台同时挂着。真实可执行的套利机会一定少于这里数出来的。
2. 数据集的赔率通常是赛前某个时点的快照，真实套利窗口往往只有几分钟。
3. 没有计入任何成本：账户资金占用、转账费、汇率、以及最关键的——
   赢了会被限额封号。

所以本脚本给出的是**机会规模的上界**，不是可实现收益。上界很小的话，
就不用再往下讨论了。
"""
import numpy as np
import pandas as pd
import lib_walkforward as L

df = L.load_matches()
m = df.dropna(subset=["MaxHome", "MaxDraw", "MaxAway"]).copy()
for c in ["MaxHome", "MaxDraw", "MaxAway", "OddHome", "OddDraw", "OddAway"]:
    m[c] = pd.to_numeric(m[c], errors="coerce")
m = m[(m[["MaxHome", "MaxDraw", "MaxAway"]] > 1).all(axis=1)]

# 套利指标：三个最高赔率的隐含概率之和。< 1 即存在无风险利润
book_max = 1 / m["MaxHome"] + 1 / m["MaxDraw"] + 1 / m["MaxAway"]
book_avg = 1 / m["OddHome"] + 1 / m["OddDraw"] + 1 / m["OddAway"]
m["book_max"] = book_max
m["profit"] = 1 / book_max - 1        # 按比例分配后的保证收益率

print("=" * 68)
print(f"跨平台套利扫描    {len(m):,} 场（2000-2025，全市场最高赔率）")
print("=" * 68)
print(f"平均抽水  平均赔率 {(book_avg.mean()-1)*100:>6.2f}%   "
      f"最高赔率 {(book_max.mean()-1)*100:>6.2f}%")

arb = m[m.book_max < 1]
print(f"\n存在套利的场次: {len(arb):,} / {len(m):,} = {len(arb)/len(m):.2%}")

if len(arb):
    print(f"利润率  中位数 {arb.profit.median():.2%}   "
          f"均值 {arb.profit.mean():.2%}   最大 {arb.profit.max():.2%}")
    print("\n利润率分布:")
    for lo, hi in [(0, 0.005), (0.005, 0.01), (0.01, 0.02), (0.02, 0.05), (0.05, 1)]:
        s = (arb.profit >= lo) & (arb.profit < hi)
        if s.sum():
            print(f"  {lo:>5.1%} - {hi:>5.1%}: {s.sum():>6,} 场 "
                  f"({s.sum()/len(m):.3%} of all)")

    print("\n逐年（看机会是在增加还是被市场效率吃掉）:")
    arb_y = arb.groupby(arb.MatchDate.dt.year).size()
    all_y = m.groupby(m.MatchDate.dt.year).size()
    rate = (arb_y / all_y).dropna()
    for yr, r in rate.items():
        if yr >= 2013:
            print(f"  {int(yr)}: {r:>6.2%}  ({int(arb_y.get(yr,0)):>4,}/{int(all_y[yr]):>5,} 场)"
                  f"   中位利润 {arb[arb.MatchDate.dt.year==yr].profit.median():.2%}")

    # 折算成年收益：假设每次投入固定本金
    recent = arb[arb.MatchDate.dt.year >= 2020]
    recent_all = m[m.MatchDate.dt.year >= 2020]
    n_years = recent_all.MatchDate.dt.year.nunique()
    print("\n" + "-" * 68)
    print(f"近年规模（2020 起，{n_years} 年）:")
    print(f"  每年平均 {len(recent)/n_years:,.0f} 次机会，中位利润率 {recent.profit.median():.2%}")
    print(f"  若每次三边合计投入 1,000 元：年毛利约 "
          f"{len(recent)/n_years * recent.profit.median() * 1000:,.0f} 元")
    print(f"  但需要同时在多个平台各备足资金，且这是**上界**——见脚本顶部三条高估来源")

print("\n" + "=" * 68)
print("对照：两边市场（大小球 2.5）的套利，抽水更低所以机会通常更多")
ou = df.dropna(subset=["MaxOver25", "MaxUnder25"]).copy()
for c in ["MaxOver25", "MaxUnder25"]:
    ou[c] = pd.to_numeric(ou[c], errors="coerce")
ou = ou[(ou[["MaxOver25", "MaxUnder25"]] > 1).all(axis=1)]
b2 = 1 / ou["MaxOver25"] + 1 / ou["MaxUnder25"]
arb2 = b2[b2 < 1]
print(f"{len(ou):,} 场，存在套利 {len(arb2):,} = {len(arb2)/len(ou):.2%}"
      f"，中位利润 {(1/arb2 - 1).median():.2%}" if len(arb2) else "无")
print("=" * 68)
