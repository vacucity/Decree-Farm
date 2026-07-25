"""分身农庄 - Agent 指令接口 (自然语言 → player_* 游戏动作)

职责：
  1. 接收自然语言指令（如"去浇水""走到 52 68""用镐挖 30 40""停下"）。
  2. 解析为具体的桥接动作（对齐纯玩家驾驶版 MOD 的 player_* 协议）。
  3. 通过 actions 目录文件桥接下发给游戏，驱动主玩家 Game1.player 亲自干活。
  4. 可读回 bridge_data.json 的 agentPlayer 字段做执行反馈。

解析双通道：
  - 规则解析（默认，离线可靠、可演示）：中文关键词 + 坐标抽取。
  - LLM 解析（可选，config.llm_api_key 存在且 use_llm=True 时启用）：Claude 输出结构化动作，
    失败自动回退规则解析。

用法：
  python agent.py                # 交互式 REPL，逐条输入指令
  python agent.py "去浇水"        # 一次性执行单条指令
  python agent.py --llm "帮我把成熟的作物收了"   # 启用 LLM 解析
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from loguru import logger
except Exception:  # pragma: no cover - loguru 缺失时的兜底
    import logging

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger = logging.getLogger("agent")

# 路径：优先读项目 config，退化到环境变量/默认安装路径
try:
    from config import config

    _ACTION_DIR = config.stardew_action_dir
    _BRIDGE_DIR = config.stardew_bridge_path
    _LLM_KEY = config.llm_api_key
    _LLM_MODEL = config.llm_model
    _LLM_BASE = config.llm_base_url
except Exception:  # pragma: no cover
    import os

    _DEF = r"C:\Program Files (x86)\Steam\steamapps\common\Stardew Valley\Mods\StardewMCPBridge"
    _BRIDGE_DIR = os.getenv("STARDEW_BRIDGE_PATH", _DEF)
    _ACTION_DIR = os.getenv("STARDEW_ACTION_DIR", str(Path(_DEF) / "actions"))
    _LLM_KEY = os.getenv("LLM_API_KEY", "")
    _LLM_MODEL = os.getenv("LLM_MODEL", "claude-sonnet-4-20250514")
    _LLM_BASE = os.getenv("LLM_BASE_URL", "https://api.anthropic.com")


# ======================================================================
# 1. 动作桥接：写 actions/*.json 下发，读 bridge_data.json 反馈
# ======================================================================
class ActionBridge:
    """文件桥接：一命令一文件，原子写入，MOD 按文件名顺序消费后删除。"""

    def __init__(self, action_dir: str = _ACTION_DIR, bridge_dir: str = _BRIDGE_DIR):
        self.action_dir = Path(action_dir)
        self.bridge_path = Path(bridge_dir) / "bridge_data.json"
        self._seq = 0

    def send(self, action: Dict[str, Any]) -> str:
        """原子写入单条动作。返回文件名。"""
        self.action_dir.mkdir(parents=True, exist_ok=True)
        self._seq += 1
        name = f"{int(time.time() * 1000)}-{self._seq:06d}.json"
        final = self.action_dir / name
        tmp = self.action_dir / (name + ".tmp")  # ".json.tmp" 不被 MOD 的 *.json 扫描命中
        tmp.write_text(json.dumps(action, ensure_ascii=False), encoding="utf-8")
        tmp.replace(final)  # 原子发布，避免 MOD 读到半截文件
        logger.info(f"→ 游戏: {json.dumps(action, ensure_ascii=False)}")
        return name

    def read_state(self) -> Dict[str, Any]:
        try:
            return json.loads(self.bridge_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def read_agent_player(self) -> Dict[str, Any]:
        return (self.read_state() or {}).get("agentPlayer") or {}


# ======================================================================
# 2. 意图解析：自然语言 → player_* 动作列表
# ======================================================================

# 工具名词 → MOD 工具枚举
_TOOL_NOUN = {
    "镐": "pickaxe", "稿": "pickaxe", "十字镐": "pickaxe", "鹤嘴锄": "pickaxe",
    "斧": "axe", "斧头": "axe",
    "锄": "hoe", "锄头": "hoe",
    "壶": "watering_can", "浇水壶": "watering_can", "喷壶": "watering_can", "水壶": "watering_can",
    "剑": "sword", "武器": "sword",
}
# 动词 → 工具（"砍(30,40)""敲(30,40)"这类无工具名词时兜底）
_VERB_TOOL = {
    "锄地": "hoe", "翻地": "hoe", "耕": "hoe",
    "砍": "axe", "伐": "axe",
    "敲": "pickaxe", "挖矿石": "pickaxe", "破": "pickaxe",
}
# 朝向：中文 → 0上 1右 2下 3左
_DIRECTION = {
    "上": 0, "北": 0, "右": 1, "东": 1, "下": 2, "南": 2, "左": 3, "西": 3,
}
# 目标地点 → (location, x, y)  安全落点
_LOCATIONS = {
    "回家": ("FarmHouse", 3, 11), "家": ("FarmHouse", 3, 11), "睡觉": ("FarmHouse", 3, 11),
    "屋里": ("FarmHouse", 3, 11), "房子": ("FarmHouse", 3, 11),
    "农场": ("Farm", 64, 15), "农田": ("Farm", 64, 15), "地里": ("Farm", 64, 15),
    "小镇": ("Town", 43, 60), "镇上": ("Town", 43, 60), "镇子": ("Town", 43, 60),
    "矿洞": ("Mine", 17, 5), "矿井": ("Mine", 17, 5), "矿": ("Mine", 17, 5),
}

_FARM_KW = ("浇水", "收割", "收成", "收获", "采收", "收了", "成熟", "种地", "务农",
            "耕作", "干活", "农活", "打理", "照料", "种田")
_STOP_KW = ("停下", "停止", "别动", "待命", "停", "站住", "歇会", "idle")
_MOVE_KW = ("移动", "走到", "走去", "过去", "去到", "走", "去")
_MINE_KW = ("采矿", "挖矿", "下矿", "挖矿石")
_FISH_KW = ("钓鱼", "捕鱼")
_ATTACK_KW = ("攻击", "打怪", "砍怪")
_FACE_KW = ("朝", "面向", "转向", "面朝")
_INTERACT_KW = ("交互", "打开", "查看", "调查", "使用箱子", "开箱")


def _extract_coords(text: str) -> Optional[List[int]]:
    """从文本抽取前两个整数作为 (x, y)。"""
    nums = re.findall(r"-?\d+", text)
    if len(nums) >= 2:
        return [int(nums[0]), int(nums[1])]
    return None


def _find_tool(text: str) -> Optional[str]:
    for noun, tool in _TOOL_NOUN.items():
        if noun in text:
            return tool
    for verb, tool in _VERB_TOOL.items():
        if verb in text:
            return tool
    return None


class IntentParser:
    """自然语言 → List[player_* 动作 dict]。规则优先，可选 LLM 兜底。"""

    def __init__(self, use_llm: bool = False):
        self.use_llm = use_llm and bool(_LLM_KEY)

    def parse(self, text: str) -> List[Dict[str, Any]]:
        text = (text or "").strip()
        if not text:
            return []
        # LLM 通道（可选）：失败静默回退规则
        if self.use_llm:
            try:
                acts = _llm_parse(text)
                if acts:
                    return acts
            except Exception as e:
                logger.warning(f"LLM 解析失败，回退规则：{e}")
        return self._rule_parse(text)

    def _rule_parse(self, text: str) -> List[Dict[str, Any]]:
        coords = _extract_coords(text)

        # 1) 停止 —— 最高优先，随时可打断
        if any(k in text for k in _STOP_KW):
            return [{"actionType": "player_stop"}]

        # 2) 显式用工具（有工具/工具动词 + 坐标）
        tool = _find_tool(text)
        if tool and coords:
            return [{"actionType": "player_use_tool", "tool": tool, "x": coords[0], "y": coords[1]}]

        # 3) 朝向
        if any(k in text for k in _FACE_KW):
            for zh, d in _DIRECTION.items():
                if zh in text:
                    return [{"actionType": "player_face", "direction": d}]

        # 4) 交互 / 开箱（需坐标）
        if any(k in text for k in _INTERACT_KW) and coords:
            return [{"actionType": "player_interact", "x": coords[0], "y": coords[1]}]

        # 5) 攻击
        if any(k in text for k in _ATTACK_KW):
            return [{"actionType": "player_attack"}]

        # 6) 农活（浇水/收割/种地都归自主农场模式，MOD 自动扫描优先级最高的近处任务）
        if any(k in text for k in _FARM_KW):
            return [{"actionType": "player_farm"}]

        # 7) 采矿 / 钓鱼 —— 自主模式暂未实现，先把化身带到位并提示
        if any(k in text for k in _MINE_KW):
            loc = _LOCATIONS["矿洞"]
            return [
                {"actionType": "player_warp", "location": loc[0], "x": loc[1], "y": loc[2]},
                {"actionType": "chat", "metadata": {"message": "已到矿洞口（自主采矿待实现，可用 player_use_tool 逐格敲矿）"}},
            ]
        if any(k in text for k in _FISH_KW):
            return [{"actionType": "chat", "metadata": {"message": "自主钓鱼暂未实现，请手动或后续扩展 player_fish"}}]

        # 8) 传送到已知地点
        for kw, (loc, x, y) in _LOCATIONS.items():
            if kw in text:
                return [{"actionType": "player_warp", "location": loc, "x": x, "y": y}]

        # 9) 移动到坐标
        if coords and any(k in text for k in _MOVE_KW):
            return [{"actionType": "player_move_to", "x": coords[0], "y": coords[1]}]
        if coords:  # 光给坐标也当移动
            return [{"actionType": "player_move_to", "x": coords[0], "y": coords[1]}]

        # 10) 无法映射为动作 → 当作聊天说出去（至少有反馈），并记录
        logger.warning(f"未识别为动作，转为聊天：{text}")
        return [{"actionType": "chat", "metadata": {"message": text}}]


# ======================================================================
# 3. 可选 LLM 解析（Claude / Anthropic messages API，stdlib urllib）
# ======================================================================
_LLM_SYSTEM = """你是《星露谷物语》主玩家控制助手。把用户的中文指令翻译成动作 JSON 数组。
只输出 JSON 数组，不要解释。可用动作（字段必须齐全）：
- {"actionType":"player_farm"}  自主务农（自动浇水/收割/清杂物）
- {"actionType":"player_move_to","x":int,"y":int}  寻路走到瓦片
- {"actionType":"player_use_tool","tool":"pickaxe|axe|hoe|watering_can|sword","x":int,"y":int}
- {"actionType":"player_stop"}  停止待命
- {"actionType":"player_warp","location":"Farm|FarmHouse|Town|Mine","x":int,"y":int}
- {"actionType":"player_face","direction":0}  0上1右2下3左
- {"actionType":"player_interact","x":int,"y":int}
- {"actionType":"player_attack"}
- {"actionType":"chat","metadata":{"message":"..."}}
无法映射时用 chat 回应。"""


def _extract_json_array(txt: str) -> Optional[List[Dict[str, Any]]]:
    m = re.search(r"\[.*\]", txt, re.S)
    if not m:
        return None
    data = json.loads(m.group(0))
    return data if isinstance(data, list) and data else None


def _llm_parse(text: str) -> Optional[List[Dict[str, Any]]]:
    import urllib.request

    body = {
        "model": _LLM_MODEL,
        "max_tokens": 512,
        "system": _LLM_SYSTEM,
        "messages": [{"role": "user", "content": text}],
    }
    req = urllib.request.Request(
        _LLM_BASE.rstrip("/") + "/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-api-key": _LLM_KEY,
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    txt = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return _extract_json_array(txt)


# ======================================================================
# 4. Agent 门面 + CLI
# ======================================================================
class StardewAgent:
    """Agent 门面：execute(自然语言) → 解析 → 下发 → 反馈。"""

    def __init__(self, bridge: Optional[ActionBridge] = None, use_llm: bool = False):
        self.bridge = bridge or ActionBridge()
        self.parser = IntentParser(use_llm=use_llm)

    def execute(self, text: str) -> Dict[str, Any]:
        actions = self.parser.parse(text)
        if not actions:
            return {"ok": False, "reason": "空指令", "actions": []}
        for a in actions:
            self.bridge.send(a)
            time.sleep(0.05)  # 保证文件名单调递增、按序消费
        return {"ok": True, "actions": actions}

    def feedback(self, wait: float = 0.9) -> Dict[str, Any]:
        """等一下让 MOD 消费命令，再读回主玩家状态。"""
        time.sleep(wait)
        ap = self.bridge.read_agent_player()
        return {
            "mode": ap.get("mode"),
            "tile": ap.get("tile"),
            "moving": ap.get("moving"),
            "stamina": ap.get("stamina"),
            "last": ap.get("lastCommandResult"),
        }


def _print_result(text: str, res: Dict[str, Any], fb: Dict[str, Any]) -> None:
    print(f"  指令: {text}")
    print(f"  动作: {json.dumps(res.get('actions', []), ensure_ascii=False)}")
    if fb.get("mode") is not None or fb.get("last") is not None:
        print(f"  反馈: mode={fb.get('mode')} tile={fb.get('tile')} "
              f"stamina={fb.get('stamina')} last={fb.get('last')}")


def main(argv: List[str]) -> None:
    use_llm = "--llm" in argv
    args = [a for a in argv if a != "--llm"]
    agent = StardewAgent(use_llm=use_llm)

    if args:  # 一次性模式
        text = " ".join(args)
        res = agent.execute(text)
        _print_result(text, res, agent.feedback())
        return

    # 交互式 REPL
    print("分身农庄 Agent 已就绪（输入指令回车执行；'状态' 看主玩家；'退出' 结束）")
    print("示例：去浇水 / 走到 52 68 / 用镐挖 30 40 / 回家 / 停下")
    if use_llm:
        print("[LLM 解析已启用]")
    while True:
        try:
            text = input("指令> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text in ("退出", "quit", "exit", "q"):
            break
        if text in ("状态", "state", "status"):
            print(f"  {json.dumps(agent.bridge.read_agent_player(), ensure_ascii=False)}")
            continue
        res = agent.execute(text)
        _print_result(text, res, agent.feedback())


if __name__ == "__main__":
    main(sys.argv[1:])
