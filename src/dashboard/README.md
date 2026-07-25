# dashboard · 专注前端 HUD（规划中 · P4）

本目录用于放置「专注前端界面小工具」（HUD），设计详见
[../DEV_ROADMAP.md](../DEV_ROADMAP.md) 第 E 节。

## 定位
轻量悬浮面板，专注时展示：现实状态 + 分身农活 + 戒指连接 + 日程进度。

## 技术选型
- 复用 `archive/garden-web` 的零构建栈（HTML + CSS + gsap，可选 Three.js 花园虚化背景）。
- 数据通道：Python 侧新增 WebSocket 推送（复用 `config.ws_host/ws_port=8765`），
  前端只读渲染，不回控（控制权仍在戒指/语音）。

## 计划文件
- `hud.html`：单文件 HUD（原生 WebSocket 客户端 + 卡片渲染）。
- Python 侧 WebSocket 推送端（每 tick 推一帧状态 JSON）。

> 当前为占位说明，尚未实现（见 DEV_ROADMAP §E.5 黑客松最小版）。
