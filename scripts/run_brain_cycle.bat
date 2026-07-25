@echo off
chcp 65001 >nul
cd /d d:\AdventureX2026\src\python
C:\Users\20984\anaconda3\python.exe -u brain.py --llm --interval 5 --goal "去农场开垦一片新地：先清理杂草和石头(除草)，再锄地，然后播种防风草种子，最后给种下的作物浇水。目标是完整跑通 除草→锄地→播种→浇水 一轮。" > brain_cycle.log 2>&1
