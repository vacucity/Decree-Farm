# 🌾 Decree Farm 戒律农场

> **软硬结合的行为激励系统** — 一枚智能戒指读懂你的专注，AI 化身在真实的《星露谷物语》里替你种田。你自律，它丰收；你分心，它枯萎。

AdventureX 2026 参赛项目。

---

## 它是什么

Decree Farm 由**双机协同**构成完整闭环：

- **Machine B（感知端）**：智能蓝牙戒指 + HMM专注分类器 + 桥接服务，把"专注"量化为实时概率信号；
- **Machine A（执行端）**：真实运行的《星露谷物语 1.6》+ SMAPI MOD + Python AI 智能体，把专注翻译成游戏化身的自主劳作（浇水/收割/钓鱼/出货），把分心翻译成真实的游戏内经济惩罚（扣金币、作物枯萎）。

```
┌────────── Machine B（感知端）──────────┐
│ 智能戒指（IMU 六轴 / 按键 / 手势）        │
│   ↓ BLE (Nordic UART)                  │
│ ring/ 桥接服务（aiohttp :8520）          │
│   ├─ 随机森林分类器（3s 窗口推理）        │
│   └─ ws_client 上行推送                 │
└─────────────┬──────────────────────────┘
              │ LAN WebSocket :8766
┌─────────────┴──────────────────────────┐
│          Machine A（执行端）             │
│ src/python/main.py                      │
│   ├─ LanRelay（:8766，接收戒指事件）      │
│   ├─ 状态机 focus/rest/distracted/sleep │
│   ├─ AI 大脑（观察→决策→执行循环）        │
│   └─ WS 服务端（:8765 ↔ SMAPI MOD）      │
│ vendor/smapi-mod（C#，驾驶游戏主玩家）    │
│ 星露谷物语 1.6（真实游戏、真实经济）       │
└────────────────────────────────────────┘
```

**核心原则**：AI 化身严格遵守游戏原生规则——寻路步行、禁止瞬移、工具消耗真实体力。化身的一天与人类玩家亲手玩完全等价。

## 仓库结构

| 目录 | 运行位置 | 内容 |
|------|---------|------|
| `src/python/` | Machine A | Python 智能体：状态机、编排器、AI 大脑、TTS 反馈、双 WebSocket 桥 |
| `src/dashboard/` | Machine A | HUD 仪表盘网页（LAN 中继自带托管） |
| `vendor/smapi-mod/` | Machine A | SMAPI MOD C# 源码（PlayerPilot 驾驶、农活扫描、A* 寻路） |
| `ring/` | Machine B | 戒指桥接服务、专注分类器 + 训练好的模型、BLE SDK、协议文档 |
| `scripts/` | Machine A | 构建 / 运行辅助脚本 |

单机也能跑：Machine A 上用 `ring_remote.py --demo`（或 `DEMO_MODE=1`）模拟戒指事件，无需硬件。

---

# 部署教程

## 前置条件

| 项 | Machine A（游戏机） | Machine B（戒指机） |
|----|--------------------|--------------------|
| 系统 | Windows 10/11 | Windows 10/11（需蓝牙） |
| Python | 3.10+（建议 Anaconda） | 3.10+ |
| 游戏 | 星露谷物语 1.6（Steam）+ [SMAPI 4.x](https://smapi.io/) | 不需要 |
| .NET SDK | 6.0+（编译 MOD 用） | 不需要 |
| 硬件 | — | 智能戒指（BLE，可选，无戒指可用 demo 模式） |
| 网络 | 两台机器同一局域网，Machine A 放行 8766 端口 | |

## 第一步：部署 Machine A（游戏机）

### 1.1 克隆仓库并安装 Python 依赖

```powershell
git clone https://github.com/vacucity/Decree-Farm.git
cd Decree-Farm
pip install -r src\python\requirements.txt
```

### 1.2 配置环境变量

```powershell
copy src\python\.env.template src\python\.env
```

编辑 `src\python\.env`，重点确认：

```ini
# 星露谷 MOD 路径（按你的实际安装位置改）
STARDEW_BRIDGE_PATH=C:\Program Files (x86)\Steam\steamapps\common\Stardew Valley\Mods\StardewMCPBridge
STARDEW_ACTION_DIR=C:\Program Files (x86)\Steam\steamapps\common\Stardew Valley\Mods\StardewMCPBridge\actions

# LLM 可留空 —— 内置规则引擎（HeuristicPolicy）无需 API Key 即可完整运行
LLM_API_KEY=
```

其他开关（环境变量，均有默认值）：

| 变量 | 默认 | 说明 |
|------|------|------|
| `LAN_ONLY` | `1` | 不连本机戒指，只接收 Machine B 的 LAN 推送（双机模式保持默认） |
| `DEMO_MODE` | `0` | `1` = 无戒指模拟事件（单机演示用） |
| `RING_MAC` | — | 仅 `LAN_ONLY=0` 本机直连戒指时需要 |

### 1.3 编译并部署 SMAPI MOD

先装好 SMAPI（官网一路下一步），然后：

```powershell
# GAME_PATH 必须指向含 StardewValley.exe 的目录
$env:GAME_PATH = 'C:\Program Files (x86)\Steam\steamapps\common\Stardew Valley'
cd vendor\smapi-mod
dotnet build -c Release
```

构建成功会自动把 MOD 部署到游戏 `Mods\StardewMCPBridge\`（SMAPI 官方 ModBuildConfig 行为）。
> ⚠️ 编译部署时游戏必须处于关闭状态，否则 DLL 被占用会部署失败。

### 1.4 启动顺序

1. **启动游戏**：通过 SMAPI 启动星露谷，进入你的存档（SMAPI 控制台应显示 `StardewMCPBridge` 加载成功）；
2. **启动智能体**：

```powershell
python src\python\main.py
```

看到以下日志即为就绪：

```
WS 服务端已监听 ws://localhost:8765，等待 MOD 连入
MOD 已连入 WebSocket — 低延迟通道在线
LAN 中继已监听 ws://0.0.0.0:8766，等待 Machine B 连入
```

3. **（可选）打开 HUD**：浏览器访问 `http://<MachineA_IP>:8766/`，实时查看金币 / 体力 / 模式。

## 第二步：部署 Machine B（戒指机）

### 2.1 安装依赖

```powershell
git clone https://github.com/vacucity/Decree-Farm.git
cd Decree-Farm
pip install bleak aiohttp websockets numpy scikit-learn joblib
```

### 2.2 方式一：完整桥接服务（分类器 + Web 界面）

```powershell
cd ring
.\start_v2_bridge.ps1 -Mac <你的戒指MAC>
```

- Web 管理界面：`http://127.0.0.1:8520`（连接戒指、IMU 曲线、标注采集）
- 桥接会加载 `ring/models/focus_classifier.joblib` 做实时专注分类，并把 `ring_state` / 按键事件经 WebSocket 上行推送到 Machine A 的 `ws://<MachineA_IP>:8766`

### 2.3 方式二：轻量客户端（仅转发按键/录音，够演示用）

```powershell
python src\python\ring_remote.py --server <MachineA_IP>:8766 --ring <你的戒指MAC>
```

没有戒指硬件时：

```powershell
python src\python\ring_remote.py --server <MachineA_IP>:8766 --demo
```

demo 模式会按键盘指令模拟双击（开始/结束专注），全流程可跑通。

## 第三步：验证闭环

1. **双击戒指**（或 demo 模拟）→ Machine A 日志出现 `专注开始！化身选择了：farm`，TTS 播报，游戏里化身出门干活（80% 种田 / 20% 钓鱼）；
2. **保持专注** → 化身持续浇水 / 收割 / 钓鱼，真实赚取金币；
3. **分心**（分类器判定或 5 分钟无活动）→ 扣 50g、作物枯萎、化身停手；
4. **再次双击结束** → 结算奖励（100g 基础 + 10g/分钟，上限 300g）；专注不足 10 秒判"提前退出"，扣 50g + 2 株作物枯萎；
5. **现实 22:00–07:00** → 化身自动步行回农舍睡觉（用户主动专注时豁免）。

## 常见问题排查

| 现象 | 原因与处理 |
|------|-----------|
| MOD 未连入 :8765 | 先启动游戏进存档，再启动 main.py；确认 SMAPI 控制台无 MOD 报错 |
| Machine B 连不上 :8766 | 确认同一局域网、Machine A 防火墙放行 8766；用 `ws://IP:8766` 而非 localhost |
| dotnet build 报错找不到游戏 | `GAME_PATH` 必须是含 `StardewValley.exe` 的目录 |
| MOD 部署失败（DLL 被占用） | 关闭游戏后重新 `dotnet build` |
| 戒指连不上 | 确认戒指未被手机 App 占用；IMU 需切到手势模式（长按至红灯）；新固件电量读取自动走 GATT 备用通道 |
| 双击没反应 | 桥接侧有 10 秒防误触冷却；Machine A 侧双击后 60 秒内忽略自动分类（手动优先） |
| 无 LLM Key 能跑吗 | 能。默认 HeuristicPolicy 规则引擎完整驱动种田/钓鱼/睡觉全流程 |

## 奖惩规则速查

| 现实行为 | 游戏后果 |
|----------|----------|
| 开始专注 | 化身出门开工（80% 种田 / 20% 钓鱼） |
| 正常完成 | +100g 基础 +10g/分钟（上限 300g）+ 农场劳动收益 |
| 提前退出（<10s） | −50g + 2 株作物枯萎 |
| 分心 | 每次 −50g（累计上限 300g）+ 作物枯萎（上限 5 株） |
| 连续专注 >3 小时 | 化身力竭晕倒（防过劳） |
| 现实 22 点后未专注 | 化身步行回家睡觉 |

## 技术栈

- **Machine A**：Python 3.11 + asyncio + websockets + loguru；C# .NET 6 + SMAPI 4.x；Windows SAPI TTS
- **Machine B**：Python + aiohttp + bleak + scikit-learn（HMM，63 维 IMU 特征，3s 窗口，准确率 0.711 / AUC 0.784）
- **通信**：双 WebSocket（:8765 MOD 桥 / :8766 LAN 中继），文件桥透明兜底

## License

Hackathon project — MIT.
