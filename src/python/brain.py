"""分身农庄 - AI 大脑（autonomous agent · 真正的自主智能体）

真正的自主闭环（区别于「命令驱动」与「专注状态驱动」）：
    读游戏状态(bridge_data.json) → 决策 → 下发 player_* 动作 → 观察结果 → 循环。

两种决策后端：
  - LLM 大脑（--llm）：调用云端大模型（真实 API）做开放式决策，应对未预设的局面。
  - 启发式策略（默认）：基于状态/时间的规则自治，离线可跑，用于无 key 兜底与演示。

用法：
    python brain.py --check              # 只验证 LLM API 连通性（不控制游戏）
    python brain.py --once               # 启发式：跑一个「观察-决策-执行」周期
    python brain.py --once --llm         # LLM：跑一个周期（打印大模型的思考与动作）
    python brain.py                      # 启发式：持续自主运行
    python brain.py --llm                # LLM：持续自主运行
    python brain.py --llm --goal "先把成熟作物收完再去挖矿"

安全护栏：夜晚/低体力自动回家；每轮最多若干动作；轮次间隔；Ctrl-C 停止。
"""
from __future__ import annotations

import json
import random
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple


def _load_dotenv() -> None:
    """在导入 config 前把同目录 .env 注入环境变量（密钥不入库）。"""
    import os
    from pathlib import Path

    p = Path(__file__).with_name(".env")
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

try:
    from loguru import logger
except Exception:  # pragma: no cover
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logger = logging.getLogger("brain")

from agent import ActionBridge
from ws_bridge import WsStateServer, HybridBridge

try:
    from config import config

    _LLM_KEY = config.llm_api_key
    _LLM_MODEL = config.llm_model
    _LLM_BASE = config.llm_base_url
    _LLM_MAXTOK = config.llm_max_tokens
    _LLM_TEMP = config.llm_temperature
except Exception:  # pragma: no cover
    import os

    _LLM_KEY = os.getenv("LLM_API_KEY", "")
    _LLM_MODEL = os.getenv("LLM_MODEL", "claude-sonnet-4-20250514")
    _LLM_BASE = os.getenv("LLM_BASE_URL", "https://api.anthropic.com")
    _LLM_MAXTOK = 1024
    _LLM_TEMP = 0.7

import os

_LLM_PROVIDER = os.getenv("LLM_PROVIDER", "auto")  # auto | anthropic | openai

# ============ 护栏参数 ============
NIGHT_TIME = 2400          # 过午夜(00:00 后)→回家准备睡觉
LOW_STAMINA_RATIO = 0.12   # 体力低于此比例→回家休息
DEFAULT_INTERVAL = 5.0     # 每轮间隔（秒）
MAX_ACTIONS_PER_STEP = 4   # 单轮最多下发的动作数
BRAIN_DECIDE_MAXTOK = 3000 # LLM 决策预算（gpt-5 等推理模型会消耗思考 token，需留足）
SEND_GAP = 0.06            # 动作间隔，保证文件名单调
MIN_SEED_BUY_MONEY = 200   # 去超市买种子的金币下限（避免没钱空跑）

FARMHOUSE = ("FarmHouse", 3, 11)
FARM = ("Farm", 64, 15)

# 当前 MOD 已实现的动作白名单（LLM 只能用这些）
ALLOWED_ACTIONS = {
    "player_move_to", "player_farm", "player_use_tool", "player_warp",
    "player_face", "player_interact", "player_attack", "player_stop", "player_idle",
    "player_sleep", "player_plant", "player_inspect", "chat",
    "player_select_item", "player_eat", "player_enter_door", "player_use_tool_repeat",
    "player_reclaim", "player_go_outside", "player_go_to",
    "player_ship", "player_store", "player_take", "player_buy",
    "player_fish",  # 自主钓鱼（走到水边→自动抛竿收杆循环）
}

# 可售农产品/矿产等品类（用于估算背包里有多少东西值得出货；种子 -74 不算）。
# MOD 端 player_ship 才是权威判定(canBeShipped && sellToStorePrice>0)，此处只是提示。
SELLABLE_CATEGORIES = {
    -2, -4, -5, -6, -7, -12, -14, -15, -18, -26, -27, -28, -75, -79, -80, -81,
}


# ======================================================================
# 1. 状态摘要：把 bridge_data.json 压缩成决策所需的精炼上下文
# ======================================================================
def build_digest(state: Dict[str, Any]) -> Dict[str, Any]:
    """把庞大的 bridge_data.json 压缩成大模型/启发式好用的任务摘要。"""
    player = state.get("player") or {}
    ap = state.get("agentPlayer") or {}
    sur = ap.get("surroundings") or {}
    tiles = sur.get("tiles") or []

    harvest, water, clear = [], [], []
    ladders = []  # 矿洞中的梯子/洞口
    for t in tiles:
        x, y = t.get("x"), t.get("y")
        if t.get("cropReady"):
            harvest.append({"x": x, "y": y, "crop": t.get("crop")})
        elif t.get("crop") and t.get("waterState") == 0:  # 0=干,1=已浇,-1=非耕地
            water.append({"x": x, "y": y, "crop": t.get("crop")})
        if t.get("breakable"):
            clear.append({"x": x, "y": y, "obj": t.get("obj")})
        # 识别矿洞梯子/洞口（MOD SurroundingsScanner 标记 ObjectType="ladder"）
        obj_type = t.get("objectType") or ""
        obj_name = t.get("objectName") or ""
        if obj_type == "ladder" or (t.get("interactable") and ("Ladder" in obj_name or "Shaft" in obj_name)):
            ladders.append({"x": x, "y": y, "name": obj_name, "type": obj_type})

    max_st = ap.get("maxStamina") or 270
    st = player.get("stamina", ap.get("stamina", 0))
    inv = ap.get("inventory") or []
    seeds = [{"name": i.get("name"), "stack": i.get("stack")} for i in inv if i and i.get("isSeed")]
    edibles = [{"name": i.get("name"), "stack": i.get("stack")} for i in inv if i and i.get("edible")]
    has_pickaxe = any(i and ("Pickaxe" in (i.get("name") or "") or "镐" in (i.get("name") or "")
                           or (i.get("category") == -99 and "pickaxe" in (i.get("name") or "").lower()))
                      for i in inv[:12])  # hotbar only (0-11), matches MOD FindSlot scope
    has_hoe = any(i and ("Hoe" in (i.get("name") or "") or "锄" in (i.get("name") or ""))
                  for i in inv[:12])  # hotbar only, matches MOD FindSlot scope
    inventory_free = sum(1 for i in inv if i is None)
    sellable_count = sum(1 for i in inv
                         if i and not i.get("isSeed") and i.get("category") in SELLABLE_CATEGORIES)
    storable_count = sum(1 for i in inv
                         if i and not i.get("isSeed") and i.get("category") not in SELLABLE_CATEGORIES)
    nearby_chest = ap.get("nearbyChest")
    chest_has_seeds = bool(nearby_chest and any(it.get("isSeed") for it in (nearby_chest.get("items") or [])))
    return {
        "time": state.get("time"),
        "day": state.get("day"),
        "season": state.get("season"),
        "weather": state.get("weather"),
        "location": state.get("location"),
        "money": player.get("money"),
        "stamina": st,
        "maxStamina": max_st,
        "staminaPct": round(st / max_st, 2) if max_st else 0,
        "tile": ap.get("tile"),
        "mode": ap.get("mode"),
        "moving": ap.get("moving"),
        "tasks": {"harvest": harvest, "water": water, "clear": clear},
        "ladders": ladders,  # 矿洞中可见的梯子/洞口列表
        "monsters": sur.get("monsters") or [],
        "seeds": seeds,
        "edibles": edibles,
        "hasPickaxe": has_pickaxe,
        "hasHoe": has_hoe,
        "inventoryFree": inventory_free,
        "sellableCount": sellable_count,
        "storableCount": storable_count,
        "nearbyChest": nearby_chest,
        "chestHasSeeds": chest_has_seeds,
        "wateringCanWater": ap.get("wateringCanWater", -1),
        "currentTool": ap.get("currentTool"),
        "canMove": ap.get("canMove"),
        "reclaimTiles": ap.get("reclaimTiles", 0),
        "plotIdle": ap.get("plotIdle", False),
        "exiting": ap.get("exiting", False),
        "traveling": ap.get("traveling", False),
        "travelTarget": ap.get("travelTarget"),
        "lastResult": ap.get("lastCommandResult"),
    }


def _has_farm_work(d: Dict[str, Any]) -> bool:
    t = d.get("tasks") or {}
    return bool(t.get("harvest") or t.get("water") or t.get("clear"))


def _is_daytime(d: Dict[str, Any]) -> bool:
    tm = d.get("time") or 600
    return 600 <= tm < NIGHT_TIME


def _dedup_actions(actions: List[Dict[str, Any]], d: Dict[str, Any]) -> List[Dict[str, Any]]:
    """滤除会造成频闪/顿挫的冗余动作（白名单制：每个托管动作只在 mode 不匹配时下发）。
    - player_farm: mode==farm 时不重发
    - player_fish: mode==fish 时不重发
    - player_reclaim: 已有田且未闲置时不重发
    - player_go_to: 已在目标场景时不重发
    """
    out = []
    cur_loc = d.get("location")
    mode = d.get("mode")
    for a in actions:
        t = a.get("actionType")
        if t == "player_warp" and a.get("location") == cur_loc:
            continue
        if t == "player_farm" and mode == "farm":
            continue
        # 🐟 钓鱼去重：MODE has stateful ProcessFish (不可重入)，已下发过就不再发
        if t == "player_fish":
            last = d.get("lastResult") or {}
            if isinstance(last, dict) and last.get("action") == "player_fish":
                continue
        # 🚪 出门去重：pendingExitTile 已置位(exiting=true) 或 lastResult 显示正在执行中，不再重发
        if t == "player_go_outside":
            if d.get("exiting"):
                continue
            last = d.get("lastResult") or {}
            if isinstance(last, dict):
                la = last.get("action", "")
                # 刚发出过 go_outside 且成功开始行走 (walking to the door)
                if la == "player_go_outside" and last.get("success"):
                    continue
                # move 成功表示走到门边了，让 ProcessExit 接管
                if la == "move" and last.get("success") and d.get("location") == "FarmHouse":
                    continue
        # 已在目标场景就别再跨图步行（避免无意义的 travel 循环）。
        if t == "player_go_to" and a.get("target") == cur_loc:
            continue
        # 已有托管田且正在料理时，不重发 reclaim（维护唯一一块，不刷新重建）。
        if t == "player_reclaim" and d.get("reclaimTiles") and not d.get("plotIdle"):
            continue
        # 出货只在 Farm 有意义（出货箱在农场）；不在农场时丢弃，交由上层先 go_to Farm。
        if t == "player_ship" and cur_loc != "Farm":
            continue
        # 取箱只在附近有木箱时有意义（否则 MOD 也会失败，双保险丢弃）
        if t == "player_take" and not d.get("nearbyChest"):
            continue
        # 买种只在种子商店里有意义（不在店内时丢弃，交由上层先 go_to SeedShop）
        if t == "player_buy" and cur_loc != "SeedShop":
            continue
        out.append(a)
    return out


# ======================================================================
# 2. 启发式策略（无需 API key 的自治兜底）
# ======================================================================
class HeuristicPolicy:
    """基于活动类型的规则自治：专注期间 farm（种地含除杂物）或 fish（钓鱼）二选一。"""

    name = "heuristic"
    _brain_ref = None  # 由 Brain 设置，用于切换活动

    def _switch_activity(self, d: Dict[str, Any], new_activity: str):
        """切换当前活动（种地完了换钓鱼，反之亦然）"""
        if self._brain_ref:
            self._brain_ref.activity = new_activity
            if new_activity == "fish":
                self._brain_ref.activity_target = "Beach"
            else:
                self._brain_ref.activity_target = None
            logger.info(f"活动切换: → {new_activity}" +
                        (f" ({self._brain_ref.activity_target})" if self._brain_ref.activity_target else ""))

    def decide(self, d: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]]]:
        night = (d.get("time") or 0) >= NIGHT_TIME
        low = d.get("staminaPct", 1) <= LOW_STAMINA_RATIO
        loc = d.get("location")
        activity = d.get("activity", "farm")

        # ══ 通用安全优先 ══
        if night:
            if loc == "FarmHouse":
                return ("已过午夜，上床睡觉结束这一天", [{"actionType": "player_sleep"}])
            return ("已过午夜，步行回农舍睡觉", [{"actionType": "player_go_to", "target": "FarmHouse"}])
        if low:
            if d.get("edibles"):
                return ("体力见底，吃点东西回血", [{"actionType": "player_eat"}])
            if loc == "FarmHouse":
                return ("体力耗尽，上床睡觉结束这一天", [{"actionType": "player_sleep"}])
            return ("体力耗尽，步行回农舍睡觉", [{"actionType": "player_go_to", "target": "FarmHouse"}])

        # ══ 通用：正在过门/跨图步行 → 别打断 ══
        if d.get("exiting") or d.get("traveling"):
            return ("步行/过门途中，保持前进", [])

        # ══ 通用：在农舍白天 → 必须先出门（一天中第一步） ══
        if loc == "FarmHouse" and _is_daytime(d):
            return ("白天先迈出房门来到农场", [{"actionType": "player_go_outside"}])

        # ══ 通用：附近有怪 → 先打 ══
        monsters = d.get("monsters") or []
        if monsters:
            m = monsters[0]
            return (f"附近有怪物 {m.get('name')}，先清理",
                    [{"actionType": "player_move_to", "x": m.get("x"), "y": m.get("y")},
                     {"actionType": "player_attack"}])

        # ══ 通用：背包将满 → 出货（剩余≤3格时主动售卖腾空间） ══
        if d.get("inventoryFree", 99) <= 3 and d.get("sellableCount", 0) > 0:
            if loc == "Farm":
                return ("背包将满，走到出货箱把农产品全部出货",
                        [{"actionType": "player_ship"}])
            return ("背包将满，步行回农场出货",
                    [{"actionType": "player_go_to", "target": "Farm"}])

        # ══════════════════════════════════════
        # 按活动类型分支
        # ══════════════════════════════════════

        if activity == "farm":
            return self._decide_farm(d, loc)
        elif activity == "fish":
            return self._decide_fish(d, loc)
        else:
            # 未知活动，默认种地
            return self._decide_farm(d, loc)

    # ── 种地分支 ──
    def _decide_farm(self, d: Dict[str, Any], loc: str) -> Tuple[str, List[Dict[str, Any]]]:
        # 在超市：买种或离开
        if loc == "SeedShop":
            if not d.get("seeds") and d.get("money", 0) >= MIN_SEED_BUY_MONEY:
                return ("在超市里，一次性买30个当季种子",
                        [{"actionType": "player_buy", "qty": 30}])
            return ("采购完毕，步行回农场复种",
                    [{"actionType": "player_go_to", "target": "Farm"}])

        # 不在农场 → 先回农场
        if loc != "Farm":
            return ("步行回农场干活", [{"actionType": "player_go_to", "target": "Farm"}])

        # 在农场：维护托管田
        # 农活第一步：已有作物先浇水（干渴作物每天掉一天成长，MOD 托管已把 Water 排最高优先）
        if (d.get("tasks") or {}).get("water"):
            if d.get("mode") != "farm":
                return ("先给已有作物浇水", [{"actionType": "player_farm"}])
            return ("正在给已有作物浇水", [])

        if not d.get("hasHoe"):
            last = d.get("lastResult") or {}
            took_failed = (isinstance(last, dict) and last.get("action") == "player_take"
                           and last.get("success") is False)
            if d.get("nearbyChest") and not took_failed:
                return ("缺锄头，尝试从木箱取出", [{"actionType": "player_take", "item": "Hoe"}])
            if d.get("sellableCount", 0) > 0:
                return ("缺锄头，先出货", [{"actionType": "player_ship"}])
            return ("缺锄头，保持待命", [])

        if not d.get("seeds") and d.get("chestHasSeeds"):
            return ("背包没种子，先从木箱取", [{"actionType": "player_take", "seedsOnly": True}])
        if not d.get("seeds") and not d.get("chestHasSeeds") and d.get("money", 0) >= MIN_SEED_BUY_MONEY:
            return ("没种子了，去超市买", [{"actionType": "player_go_to", "target": "SeedShop"}])
        if not d.get("reclaimTiles"):
            if d.get("seeds"):
                return ("开辟一块新田：割草→锄→播种→浇水", [{"actionType": "player_reclaim"}])
            if d.get("sellableCount", 0) > 0:
                return ("无田可开，先出货", [{"actionType": "player_ship"}])
            return ("农场暂无可做，保持待命", [])
        if not d.get("plotIdle"):
            last = d.get("lastResult") or {}
            farm_stuck = (d.get("mode") == "farm"
                          and isinstance(last, dict)
                          and last.get("success") is False
                          and "no " in str(last.get("detail", "")).lower())
            if farm_stuck:
                actions = [{"actionType": "player_stop"}]
                if d.get("sellableCount", 0) > 0:
                    actions.append({"actionType": "player_ship"})
                return ("farm 卡住，先出货", actions)
            if d.get("mode") != "farm":
                return ("继续照料托管田", [{"actionType": "player_farm"}])
            return ("托管田照料中", [])
        # plotIdle：田活干完（买种→播种→浇水都已完成），托管田保持不动
        # 让作物自然生长，不要 player_stop（否则 reclaimRect 被清除导致下一轮又 reclaim 死循环）。
        # 出货和存箱优先；全部做完就原地等待——专注结束时 orchestrator 会统一发 idle 清理。
        if d.get("sellableCount", 0) > 0:
            return ("料理完，出货", [{"actionType": "player_ship"}])
        if d.get("storableCount", 0) > 0 and d.get("nearbyChest"):
            return ("料理完，存箱", [{"actionType": "player_store"}])
        # 种地全部完成：保持托管田，等待作物成熟或专注结束
        return ("种地任务全部完成，托管田等待作物生长", [])

    # ── 挖矿分支 ──
    def _decide_mine(self, d: Dict[str, Any], loc: str) -> Tuple[str, List[Dict[str, Any]]]:
        # 在农场处理：有可售物先出货，没镐试着从箱子取
        if loc == "Farm":
            if d.get("sellableCount", 0) > 0:
                return ("先把可售物出货再去矿场", [{"actionType": "player_ship"}])
            if not d.get("hasPickaxe") and d.get("nearbyChest"):
                last = d.get("lastResult") or {}
                took_failed = (isinstance(last, dict) and last.get("action") == "player_take"
                               and last.get("success") is False)
                if not took_failed:
                    return ("缺镐，尝试从木箱取出", [{"actionType": "player_take", "item": "Pickaxe"}])
            if not d.get("hasPickaxe"):
                return ("没有镐无法挖矿，保持待命", [])
            return ("带着镐步行去矿场", [{"actionType": "player_go_to", "target": "Mine"}])

        # 不在矿场且不在农场 → 先去农场整理再出发
        if loc != "Mine" and not (loc or "").startswith("UndergroundMine"):
            if not d.get("hasPickaxe"):
                return ("没有镐，步行回农场找镐", [{"actionType": "player_go_to", "target": "Farm"}])
            return ("带着镐步行去矿场", [{"actionType": "player_go_to", "target": "Mine"}])

        # 在矿场入口（Mine）→ 直接进入矿洞
        if loc == "Mine":
            return ("进入矿洞下井挖矿",
                    [{"actionType": "player_stop"},
                     {"actionType": "player_move_to", "x": 18, "y": 5},
                     {"actionType": "player_enter_door"}])

        # 在地下矿洞（UndergroundMine*）→ 挖矿
        # 优先：看到梯子/洞口 → 走过去交互进入下一层
        ladders = d.get("ladders") or []
        if ladders:
            tile = d.get("tile") or {}
            px, py = tile.get("x", 0), tile.get("y", 0)
            nearest = min(ladders, key=lambda t: (px - t["x"])**2 + (py - t["y"])**2)
            name = nearest.get("name", "梯子")
            return (f"发现{name}在({nearest['x']},{nearest['y']})，走过去进入下一层",
                    [{"actionType": "player_stop"},
                     {"actionType": "player_interact", "x": nearest["x"], "y": nearest["y"]}])

        last = d.get("lastResult") or {}
        mine_stuck = (d.get("mode") == "farm"
                      and isinstance(last, dict)
                      and (last.get("success") is False
                           or "no tasks" in str(last.get("detail", "")).lower()))
        if mine_stuck:
            # 当前层清完但没有看到梯子 → 停止farm等surroundings刷新出梯子
            return ("当前层已清完，附近暂未看到梯子，先停一下",
                    [{"actionType": "player_stop"}])
        if d.get("mode") != "farm":
            return ("在矿洞中挖矿", [{"actionType": "player_farm"}])
        return ("矿洞作业中", [])

    # ── 钓鱼分支 ──
    # MOD 的 player_fish handler 自动完成：寻水→走岸→面水→抛竿→小游戏自动收竿
    # brain 只需确保人物到达目标地图；扫水半径给大（40 格），warp 落点离水远也能自己走到水边

    def _decide_fish(self, d: Dict[str, Any], loc: str) -> Tuple[str, List[Dict[str, Any]]]:
        target = d.get("activity_target") or "Beach"

        # ── 已在钓鱼目标地图 ──
        if loc == target:
            last = d.get("lastResult") or {}
            # player_fish 已下发给 MOD 且成功接收 → 等 MOD 自己跑 20 轮抛竿收竿
            if isinstance(last, dict) and last.get("action") == "player_fish":
                if last.get("success"):
                    detail = str(last.get("detail", ""))
                    if "stopping" in detail or "fished 20" in detail:
                        return ("🎣 钓鱼已完成（20轮抛竿收竿结束），等待专注结束", [])
                    return ("🎣 正在钓鱼（MOD 自动寻水→走岸→抛竿→收竿，最多 20 轮）", [])
                # MOD 返回了失败 → 停止，不发重复指令
                return (f"钓鱼失败: {last.get('detail', '')}",
                        [{"actionType": "player_stop"}])
            # 刚到目标地图，触发 MOD 自动钓鱼（大半径扫水，自己走到最近水边）
            return (f"到达{target}，交给 MOD 全自动钓鱼（自动走到最近水边）",
                    [{"actionType": "player_fish", "radius": 40}])

        # ── 不在目标地图 ──
        if loc == "Farm" and d.get("sellableCount", 0) > 0:
            return ("先出货再去钓鱼", [{"actionType": "player_ship"}])
        return (f"步行去{target}钓鱼", [{"actionType": "player_go_to", "target": target}])

    # ── 清理采集分支 ──
    FORAGE_AREAS = ["Forest", "Mountain", "Beach", "Farm"]

    def _decide_forage(self, d: Dict[str, Any], loc: str) -> Tuple[str, List[Dict[str, Any]]]:
        """清理采集：走着转，在各区域清理杂草/石头/树枝，采集地上物品。"""
        # 背包满先出货
        if loc == "Farm" and d.get("sellableCount", 0) > 0:
            return ("先出货再继续采集", [{"actionType": "player_ship"}])

        # 当前区域有东西可清 → player_farm
        last = d.get("lastResult") or {}
        farm_done = (d.get("mode") == "farm"
                     and isinstance(last, dict)
                     and (last.get("success") is False
                          or "no tasks" in str(last.get("detail", "")).lower()))

        if farm_done or (d.get("mode") == "idle" and d.get("plotIdle")):
            # 当前区域清完了 → 走去下一个区域
            order = self.FORAGE_AREAS
            idx = order.index(loc) if loc in order else -1
            next_area = order[(idx + 1) % len(order)]
            return (f"{loc}已清完，走去{next_area}",
                    [{"actionType": "player_stop"},
                     {"actionType": "player_go_to", "target": next_area}])

        if d.get("mode") != "farm":
            return (f"在{loc}清理采集", [{"actionType": "player_farm"}])
        return (f"在{loc}采集中", [])


def _warp(loc: str, x: int, y: int) -> Dict[str, Any]:
    return {"actionType": "player_warp", "location": loc, "x": x, "y": y}


def _safety_override(d: Dict[str, Any]) -> Optional[Tuple[str, List[Dict[str, Any]]]]:
    """硬护栏：无论后端（LLM/启发式）怎么想，体力见底/过午夜时先保命。
    优先步行回农舍（player_go_to），到家才 player_sleep；player_sleep 自带的传送回家
    仅作为无路可走时的兵底。返回 (thought, actions) 表示接管本轮；None 表示交给后端。"""
    night = (d.get("time") or 0) >= NIGHT_TIME
    low = d.get("staminaPct", 1) <= LOW_STAMINA_RATIO
    if not (night or low):
        return None
    loc = d.get("location")
    # 体力低且非午夜：有吃的先吃，避免提早结束这一天
    if low and not night and d.get("edibles"):
        return ("护栏：体力见底，先吃东西回血", [{"actionType": "player_eat"}])
    reason = "已过午夜" if night else "体力耗尽"
    if loc == "FarmHouse":
        return (f"护栏：{reason}，上床睡觉结束这一天", [{"actionType": "player_sleep"}])
    # 已在移动/过门/跨图途中：别重发 go_to 反复重规划路线（会绕远甚至走偏）
    if d.get("moving") or d.get("exiting") or d.get("traveling"):
        return (f"护栏：{reason}，回农舍路上保持前进", [])
    return (f"护栏：{reason}，步行回农舍准备睡觉", [{"actionType": "player_go_to", "target": "FarmHouse"}])


# ======================================================================
# 3. LLM 客户端（OpenAI 兼容 / Anthropic 双协议，真实 API 调用）
# ======================================================================

def _detect_win_proxy() -> Optional[str]:
    """尝试从 Windows 注册表读取系统代理设置（IE/Edge 代理）。"""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
        if enabled:
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
            if server and not server.startswith("http"):
                server = "http://" + server
            return server
    except Exception:
        pass
    return None


class LLMClient:
    """极简大模型客户端：一个 chat(system,user)->text，自动适配两大主流协议。"""

    def __init__(self, key: str = _LLM_KEY, model: str = _LLM_MODEL,
                 base_url: str = _LLM_BASE, provider: str = _LLM_PROVIDER):
        self.key = key
        self.model = model
        self.base_url = (base_url or "").rstrip("/")
        self.provider = self._resolve_provider(provider)

    def _resolve_provider(self, p: str) -> str:
        if p and p != "auto":
            return p
        b = (self.base_url or "").lower()
        if "anthropic" in b or (self.model or "").lower().startswith("claude"):
            return "anthropic"
        return "openai"

    def available(self) -> bool:
        return bool(self.key)

    def chat(self, system: str, user: str, max_tokens: int = _LLM_MAXTOK) -> str:
        import urllib.request

        if self.provider == "anthropic":
            url = self.base_url + "/v1/messages"
            body = {
                "model": self.model, "max_tokens": max_tokens,
                "temperature": _LLM_TEMP, "system": system,
                "messages": [{"role": "user", "content": user}],
            }
            headers = {
                "content-type": "application/json",
                "x-api-key": self.key,
                "anthropic-version": "2023-06-01",
                "user-agent": "Mozilla/5.0",
                "accept": "*/*",
            }
        else:  # openai 兼容
            url = self.base_url + "/v1/chat/completions"
            body = {
                "model": self.model, "max_tokens": max_tokens,
                "temperature": _LLM_TEMP,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
            headers = {
                "content-type": "application/json",
                "authorization": f"Bearer {self.key}",
                "user-agent": "Mozilla/5.0",
                "accept": "*/*",
            }

        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers)

        # 代理支持：优先 LLM_PROXY 环境变量，其次 HTTPS_PROXY，最后尝试系统代理
        proxy_url = os.environ.get("LLM_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if not proxy_url:
            # 尝试读取 Windows 系统代理（IE 设置）
            proxy_url = _detect_win_proxy()

        if proxy_url:
            proxy_handler = urllib.request.ProxyHandler({"https": proxy_url, "http": proxy_url})
            opener = urllib.request.build_opener(proxy_handler)
            with opener.open(req, timeout=30) as r:
                data = json.loads(r.read())
        else:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read())

        if self.provider == "anthropic":
            return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        return (((data.get("choices") or [{}])[0].get("message") or {}).get("content")) or ""


_SYSTEM_PROMPT = """你是《星露谷物语》主玩家(Game1.player)的自主 AI 大脑。你会不断收到当前游戏状态的 JSON 摘要，
需要据此自主决定「接下来做什么」，目标是像一个勤劳的农夫一样高效经营农场。

总目标：{goal}

可用动作（只能用这些，字段必须齐全）：
- {{"actionType":"player_farm"}}  进入自主务农：MOD 会自动扫描全场并就近收割/浇水/播种(背包有种子时)/锄地/清杂物，且都是先走到旁边再动手（维护现有作物的首选）
- {{"actionType":"player_reclaim","seed":"可选种子名","width":int,"height":int,"anchorX":int,"anchorY":int}}  开辟/重建唯一一块「托管田」：默认按背包种子数定尺寸(最少3x3)并锤定最近可耕地块；也可显式指定 width/height(1..8)、anchorX/anchorY(左上角)。开辟后分阶段成片干活：割草→锄成矩形→播种→浇水，并会自动持续维护(成熟自收→补种→再浇)。注意：只维护一块田，reclaimTiles>0 时不要重发 reclaim(除非你确实要重开新田块)
- {{"actionType":"player_go_outside","target":"可选目标场景如Farm"}}  走到房门/传送点并踏上去(真走路出门，不瞬移)，在室内(如FarmHouse)白天想去户外时用它
- {{"actionType":"player_go_to","target":"场景名如Mine|Farm|FarmHouse|Town|Mountain|SeedShop","x":int,"y":int}}  跨场景步行：MOD 沿游戏自己的传送网络一站站走过去(真走路，不瞬移)；x,y 可选(抵达目标场景后再走到该瓦片)。去矿场/镇上等其他地图都用它，优先于 player_warp
- {{"actionType":"player_move_to","x":int,"y":int}}  寻路到瓦片
- {{"actionType":"player_use_tool","tool":"pickaxe|axe|hoe|watering_can|sword","x":int,"y":int}}  用工具（会先走到目标旁再挥）：挖矿石/砍树/锄地
- {{"actionType":"player_plant","x":int,"y":int,"seed":"可选种子名"}}  走到指定耕地旁播种（不指定则用背包第一个种子）
- {{"actionType":"player_inspect","x":int,"y":int}}  查看指定瓦片详情（省略则看面前一格），结果在下一轮 lastResult 里
- {{"actionType":"player_warp","location":"Farm|FarmHouse|Town|Mine","x":int,"y":int}}  传送
- {{"actionType":"player_attack"}}  挥武器攻击当前朝向
- {{"actionType":"player_interact","x":int,"y":int}}  交互/开箱/收获
- {{"actionType":"player_face","direction":0}}  0上1右2下3左
- {{"actionType":"player_eat"}}  吃掉快捷栏里第一个可食物品回体力（可带 "slot":0-11 指定）
- {{"actionType":"player_select_item","slot":0}}  切换快捷栏选中格（0-11）
- {{"actionType":"player_use_tool_repeat","count":5}}  原地连续挥当前工具 N 次（矿洞清场/整地）
- {{"actionType":"player_ship"}}  走到农场出货箱并把背包里所有可售农产品投入(当晚原生结算金币，无需菜单)；必须先在 Farm(不在先 player_go_to Farm)
- {{"actionType":"player_store","item":"可选名称过滤"}}  走到当前场景最近的木箱，把矿石/木料等非售卖物存箱腾包(附近无箱会失败，可改用 player_ship)
- {{"actionType":"player_take","seedsOnly":true,"item":"可选名称过滤"}}  走到当前场景最近的木箱，从箱里取出种子(默认 seedsOnly)或指定物资进背包(用于复种/补给；附近无箱或箱内无匹配会失败)
- {{"actionType":"player_buy","budget":int,"qty":int}}  在皮埃尔超市(SeedShop)走到柜台打开进货菜单，按预算(默认花 60% 金币)买当季最便宜的种子；budget/qty 可选。必须先在 SeedShop(不在先 player_go_to SeedShop)，营业时间 9:00-17:00
- {{"actionType":"player_enter_door"}}  走进面前的门/传送点（进出建筑、下矿层）
- {{"actionType":"player_sleep"}}  回农舍上床睡觉、结束当天并推进到第二天（会自动传送回家）
- {{"actionType":"player_stop"}}  停止待命
- {{"actionType":"chat","metadata":{{"message":"..."}}}}  在游戏里说一句话（用于说明或暂不支持的操作）

决策原则：
1. 体力(staminaPct)过低或已过午夜(time>=2400)：体力低且非夜晚且背包有可食物品(见 edibles) → player_eat；否则要回家睡觉：不在 FarmHouse 先用 player_go_to 步行回 FarmHouse，到家后 player_sleep 结束这一天。
2. 附近有怪物 → 先靠近再 player_attack。
3. 正在过门(exiting)或跨图步行(traveling) → 返回空 actions 让它走完，不要打断。
4. 白天人在室内(如 FarmHouse) → 用 player_go_outside 走出房门(不要用 player_warp 瞬移)。
5. 农场策略（只维护一块托管田）：在 Farm 且 reclaimTiles==0 且有种子 → player_reclaim 开一块田；已有田且 plotIdle==false → player_farm 持续照料(若已 mode==farm 则返回空 actions)；plotIdle==true(或无种子/无田)且背包有镐(hasPickaxe) → player_go_to Mine 去挖矿。不要在已有托管田时反复 reclaim。
6. 在矿场(Mine/UndergroundMine…)且白天体力足 → player_farm 作为轻量挖矿(清石头/杂物)；体力低/夜晚 → 按原则 1 走回家睡觉。
7. 背包快满(inventoryFree<=1)且有可售物(sellableCount>0)：在 Farm → player_ship 出货；不在 Farm → 先 player_go_to Farm 再出货。一轮农活干完(无 harvest/water/clear 任务且 plotIdle)时：有可售物先 player_ship，其次把原料(storableCount>0)用 player_store 存进就近储藏箱腾包。背包没种子(seeds 为空)但就近箱里有种子(chestHasSeeds) → player_take 取出种子再复种。洒水壶已会在浇水时自动去水源补水(wateringCanWater=剩余水量)，无需你干预。背包与就近木箱都没种子(seeds 空且 chestHasSeeds 假)且金币充足(money>=200) → player_go_to SeedShop 再 player_buy 买当季最便宜种子，买完 player_go_to Farm 复种，闭合「卖钱→买种→复种」。
8. 背包种子见 seeds，是否带镐见 hasPickaxe。跨场景一律用 player_go_to 步行，尽量不用 player_warp。购买/酿酒等 MOD 未内建的复杂操作用 chat 说明，别臆造未列出的 actionType。

严格只输出一个 JSON 对象，不要解释、不要代码块围栏：
{{"thought":"一句话说明你的判断","actions":[ 动作对象, ... ]}}
actions 建议 1~3 个，可为空数组表示继续观察。"""


class LLMBrain:
    """LLM 决策后端：状态摘要 → 大模型 → JSON 动作。失败自动回退启发式。"""

    name = "llm"

    def __init__(self, goal: str, client: Optional[LLMClient] = None):
        self.client = client or LLMClient()
        self.goal = goal
        self.fallback = HeuristicPolicy()
        self.history: List[str] = []

    def decide(self, d: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]]]:
        if not self.client.available():
            t, a = self.fallback.decide(d)
            return (f"[无API key，启发式] {t}", a)
        system = _SYSTEM_PROMPT.format(goal=self.goal)
        user = "当前状态:\n" + json.dumps(d, ensure_ascii=False)
        if self.history:
            user += "\n\n最近几步:\n" + "\n".join(self.history[-3:])
        try:
            raw = self.client.chat(system, user, max_tokens=BRAIN_DECIDE_MAXTOK)
            thought, actions = _parse_decision(raw)
            self.history.append(f"想法:{thought} 动作:{[a.get('actionType') for a in actions]}")
            return thought, actions
        except Exception as e:
            logger.warning(f"LLM 决策失败，回退启发式：{e}")
            t, a = self.fallback.decide(d)
            return (f"[LLM失败回退] {t}", a)


def _parse_decision(raw: str) -> Tuple[str, List[Dict[str, Any]]]:
    """从大模型输出里抽出 {"thought","actions"}，并过滤非法 actionType。"""
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return ("(无法解析大模型输出)", [])
    obj = json.loads(m.group(0))
    thought = str(obj.get("thought", ""))
    actions = []
    for a in (obj.get("actions") or []):
        if isinstance(a, dict) and a.get("actionType") in ALLOWED_ACTIONS:
            actions.append(a)
    return thought, actions


# ======================================================================
# 4. 自主循环
# ======================================================================
class Brain:
    def __init__(self, backend, bridge: Optional[ActionBridge] = None):
        self.backend = backend
        self.activity = "farm"  # 当前专注活动（farm/mine/fish/forage），由 orchestrator 随机设置
        self.activity_target = None  # 活动目标（如钓鱼地点），由 orchestrator 设置
        if bridge is None:
            file_bridge = ActionBridge()
            ws_server = WsStateServer(config.ws_host, config.ws_port)
            ws_server.start()  # 库缺失/端口占用时静默回退文件桥
            bridge = HybridBridge(file_bridge, ws_server)
        self.bridge = bridge
        # 让 HeuristicPolicy 能反向引用 Brain 来切换活动
        if hasattr(backend, '_brain_ref'):
            backend._brain_ref = self

    def step(self) -> bool:
        """一个「观察-决策-执行」周期。返回 False 表示读不到状态。"""
        state = self.bridge.read_state()
        if not state:
            logger.error(f"读不到 bridge_data.json（游戏是否已进世界？）: {self.bridge.bridge_path}")
            return False
        if "agentPlayer" not in state:
            logger.warning("bridge_data.json 无 agentPlayer：存档未进世界或运行了旧版 MOD")

        d = build_digest(state)
        d["activity"] = self.activity  # 注入当前随机选定的活动类型
        d["activity_target"] = self.activity_target  # 注入活动目标（如钓鱼地点）
        guard = _safety_override(d)
        if guard:
            thought, actions = guard
        else:
            thought, actions = self.backend.decide(d)
        actions = _dedup_actions(actions, d)
        logger.info(f"[{self.backend.name}|{self.activity}] think: {thought} | "
                    f"time={d.get('time')} loc={d.get('location')} "
                    f"stamina={d.get('staminaPct')} tasks="
                    f"H{len(d['tasks']['harvest'])}/W{len(d['tasks']['water'])}/C{len(d['tasks']['clear'])}")
        for a in actions[:MAX_ACTIONS_PER_STEP]:
            self.bridge.send(a)
            time.sleep(SEND_GAP)
        return True

    def run(self, interval: float = DEFAULT_INTERVAL, once: bool = False):
        logger.info(f"AI 大脑启动（后端={self.backend.name}，间隔={interval}s）。Ctrl-C 停止。")
        try:
            while True:
                ok = self.step()
                if once:
                    break
                time.sleep(interval if ok else max(interval, 3))
        except KeyboardInterrupt:
            logger.info("已停止 AI 大脑。")


# ======================================================================
# 5. CLI
# ======================================================================
def _check_api() -> int:
    client = LLMClient()
    print(f"provider={client.provider}  model={client.model}  base={client.base_url}")
    if not client.available():
        print("[FAIL] 未配置 LLM_API_KEY —— 请在 .env 填入真实 key（当前为空/占位）。")
        return 2
    try:
        reply = client.chat("你是连通性测试助手。", "只回复两个字：在线", max_tokens=16)
        print(f"[OK] LLM API 连通，返回: {reply.strip()[:80]}")
        return 0
    except Exception as e:
        print(f"[FAIL] LLM API 调用失败: {e}")
        return 1


def main(argv: List[str]) -> int:
    use_llm = "--llm" in argv
    once = "--once" in argv
    check = "--check" in argv

    goal = "高效经营农场：优先收获成熟作物、给缺水作物浇水、清理杂物；农场无活则去矿洞挖矿。"
    if "--goal" in argv:
        i = argv.index("--goal")
        if i + 1 < len(argv):
            goal = argv[i + 1]
    interval = DEFAULT_INTERVAL
    if "--interval" in argv:
        i = argv.index("--interval")
        if i + 1 < len(argv):
            try:
                interval = float(argv[i + 1])
            except ValueError:
                pass

    if check:
        return _check_api()

    backend = LLMBrain(goal=goal) if use_llm else HeuristicPolicy()
    Brain(backend).run(interval=interval, once=once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
