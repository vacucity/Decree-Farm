@echo off
chcp 65001 >nul
cd /d d:\AdventureX2026\src\python
echo ============================================
echo   分身农庄 · 与 Agent 对话窗口 (LLM 解析)
echo   示例: 去浇水 / 走到 52 68 / 用镐挖 30 40 / 回家 / 停下
echo   特殊: 状态 = 看主玩家   退出 = 结束
echo ============================================
C:\Users\20984\anaconda3\python.exe agent.py --llm
pause
