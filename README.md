# Avatar Farm (分身农庄)

Ring-driven focus management system that maps real-world productivity states to Stardew Valley gameplay via an AI agent.

## Directory Structure

```
.
├── src/                    Source code
│   ├── python/             Python Agent system (brain, orchestrator, state engine)
│   └── dashboard/          Frontend HUD (single-file web app)
│
├── vendor/                 Third-party forks (MIT licensed)
│   ├── mcp-server/         Node.js MCP tool service (25 tools)
│   └── smapi-mod/          C# SMAPI MOD (shadow farmer + game bridge)
│
├── docs/                   Active project documentation
│   ├── prd.md              Product requirements
│   ├── dev_roadmap.md      Development roadmap
│   ├── project_overview.md Project overview
│   ├── implementation_plan.md Implementation plan
│   └── tool_inventory.md   MCP tool inventory & test report
│
├── scripts/                Build and run scripts
│   ├── build_mod.ps1       Compile SMAPI MOD
│   ├── run_brain.bat       Start autonomous Brain (LLM mode)
│   ├── run_brain_cycle.bat Brain cycle mode with goal
│   ├── run_chat.bat        Interactive Agent chat
│   ├── clear_actions.bat   Clear action queue
│   ├── kill_brain.ps1      Terminate Brain process
│   └── send.ps1            Manual action dispatch
│
└── archive/                Historical (not part of runtime)
    ├── garden_web/         Legacy mirror-garden frontend
    ├── docs_legacy/        Old PRD iterations
    ├── ecc_frontend_skills/ Frontend design system library
    └── misc/               Other archived files
```

## Tech Stack

| Layer | Language | Component |
|-------|----------|-----------|
| Game MOD | C# / .NET 6 | SMAPI shadow farmer + bridge |
| MCP Server | Node.js | 25 game manipulation tools |
| Agent System | Python 3.11 | Ring bridge, state engine, orchestrator, feedback |
| Frontend | HTML/JS | WebSocket HUD (zero-build) |

## Quick Start

### Prerequisites

- Windows 10/11
- Python 3.11+ with `bleak`, `websockets`, `pyttsx3`
- Node.js 18+
- .NET 6 SDK
- Stardew Valley 1.6 + SMAPI 4.5+

### Run

```bash
# Machine A (game + agent)
cd src/python
python main.py

# Machine B (ring + frontend)
python ring_remote.py --server <MachineA_IP>:8766
# Open src/dashboard/hud.html#<MachineA_IP>:8766 in browser
```

## Architecture

```
Ring (BLE) --> [ring_bridge] --> [state_engine] --> [orchestrator]
                                                         |
                                                    Cloud LLM
                                                         |
                                                   [MCP Server]
                                                         |
                                                   [SMAPI MOD]
                                                         |
                                                  Stardew Valley
                                                         |
                                                   [feedback] --> TTS / HUD
```

## LAN Collaboration (Dual-Machine)

- Machine A (ws://0.0.0.0:8766): Game + Python Agent
- Machine B: Ring detection + Frontend HUD
- Protocol: JSON over WebSocket (`ring_event`, `user_command`, `state_push`)
