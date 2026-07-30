#!/bin/bash
# 重建 xG 数据集。原始射门数据来自 douglasbc/scraping-understat-dataset（2014-2021）
mkdir -p /tmp/xg && cd /tmp/xg
for L in epl la_liga serie_a bundesliga ligue_1; do
  for S in 14-15 15-16 16-17 17-18 18-19 19-20 20-21 21-22; do
    curl -sL "https://raw.githubusercontent.com/douglasbc/scraping-understat-dataset/main/datasets/${L}/shots_${L}_${S}.csv" -o "${L}_${S}.csv" &
  done
done
wait
echo "下载完成，共 $(ls -1 *.csv | wc -l) 个文件"
# 赔率数据（含 Elo / Form / 历史赔率）
curl -sL "https://raw.githubusercontent.com/xgabora/Club-Football-Match-Data-2000-2025/main/data/Matches.csv" -o /tmp/m.csv
echo "赔率数据 $(du -h /tmp/m.csv | cut -f1)"
