using System;
using System.Collections.Generic;
using System.Linq;
using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Locations;
using StardewValley.Buildings;
using StardewValley.Objects;
using StardewValley.TerrainFeatures;
using StardewValley.Tools;
using StardewValley.Menus;

namespace StardewMCPBridge
{
    /// <summary>
    /// Pilots the real main player (Game1.player) — human-style.
    ///
    /// Technical route (migrated from direct API calls to INPUT SIMULATION, inspired by
    /// Hunter-Thompson/stardew-mcp): every action flows through the game's native input
    /// pipeline, exactly as if a human were at the keyboard/mouse:
    ///   - Movement: own A* (Pathfinder) + one simulated direction-key press per tick.
    ///     Real walking: native speed, collision, animation. No teleports, no controller hacks.
    ///   - Tool swings (hoe/watering can/pickaxe/axe/scythe/sword): select the tool in the
    ///     hotbar, face the tile, aim the cursor, press the use-tool button. The game applies
    ///     its own rules (reach, stamina, swing timing, watering-can water level...).
    ///   - Harvest / interact: face the tile, press the action button → vanilla
    ///     GameLocation.checkAction → HoeDirt.performUseAction etc.
    ///   - Planting: select the seed stack, aim, press the use-tool button; a delayed verify
    ///     falls back to the vanilla placement path (Object.placementAction) if needed.
    ///   - Sleep: the vanilla bed dialog answer (answerDialogueAction "Sleep_Yes").
    /// </summary>
    public class PlayerPilot
    {
        public enum PilotMode { Idle, Manual, Farm, Travel }

        private const int MaxRecalcAttempts = 5;   // path recalculations before giving up
        private const int StuckLimitTicks = 120;   // ~2s without tile progress = stuck
        private const int BlockedLimitTicks = 600; // ~10s unable to move (menu/fade) = fail
        private const int MaxTaskAttempts = 5;     // per-tile attempts before skipping it
        private const int ToolRepeatCooldownTicks = 20;  // ~0.33s between chained swings
        private const int FarmQueueMaxAgeTicks = 1800;   // rescan farm tasks every ~30s

        private readonly IMonitor monitor;
        private readonly IInputHelper input;
        private readonly Pathfinder pathfinder = new Pathfinder();

        public PilotMode Mode { get; private set; } = PilotMode.Idle;

        // ---- movement state ----
        private List<Vector2> path;
        private int pathIndex;
        private Vector2? finalTarget;
        private int stuckTicks;
        private Vector2 lastTile;
        private int recalcAttempts;
        private int blockedTicks;

        // ---- task state ----
        private bool executeOnArrive;   // run currentTask when the path completes
        private FarmTask currentTask;   // task to execute on arrival (type drives behavior)
        private int actionCooldown;
        private readonly Dictionary<Vector2, int> taskAttempts = new Dictionary<Vector2, int>();
        private string attemptsLocation;

        // ---- deferred work ----
        private bool pendingSleep;          // warped home; sleep once the fade settles
        private Vector2? plantVerifyTile;   // verify an input-simulated planting
        private int plantVerifyTicks;
        private string plantSeedName;

        // ---- chained tool use (player_use_tool_repeat) ----
        private int toolRepeatRemaining;
        private int toolRepeatCooldown;

        // ---- farm task queue (nearest-neighbor chaining, P3) ----
        private List<FarmTask> farmQueue;
        private int farmQueueAge;

        // ---- rectangle reclaim / managed plot (human-like: cut grass -> hoe -> plant -> water; persists) ----
        private List<Vector2> reclaimRect;   // tiles in serpentine order (the persistent managed plot)
        private string reclaimSeed;          // seed name to plant (null = first seed)
        private string reclaimLocation;      // location the managed plot lives in (Farm)
        private bool plotIdle;               // managed plot is fully tended right now (nothing to do)

        // ---- walk out the door (human-like exit, no teleport) ----
        private Vector2? pendingExitTile;    // warp tile to step onto after arriving next to it
        private string pendingExitFrom;      // location name we are exiting (detect transition)
        private int pendingExitTicks;        // safety timeout while stepping onto the warp

        // ---- cross-location travel (walk map-to-map via the warp graph, no teleport) ----
        private string travelTarget;         // final destination location name
        private Vector2? travelFinalTile;    // optional tile to walk to inside the target
        private int travelHopFails;          // safety cap on failed hops before aborting

        // ---- deferred deposit (walk to a shipping bin / chest, then hand off on arrival) ----
        private enum DepositKind { None, Ship, Store, Take }
        private DepositKind pendingDeposit = DepositKind.None;
        private Vector2? depositTile;        // the bin/chest tile (for context only)
        private string depositFilter;        // optional item-name filter for Store/Take
        private bool takeSeedsOnly;          // for Take: restrict withdrawal to seeds

        // ---- shop purchase (player_buy): walk to the counter, open the real ShopMenu, buy ----
        private bool buyOnArrive;            // on arrival at the counter, open the shop + buy
        private int buyBudget;               // gold cap for this purchase (0 = default 60% of money)
        private int buyQtyCap;               // max seeds to buy (0 = default 30)
        private enum BuyPhase { None, Browsing, Done }
        private BuyPhase buyPhase = BuyPhase.None;
        private int buyTicks;                // countdown so the menu stays visible before/after buying

        // ---- autonomous fishing (player_fish): walk→face→cast→auto-catch ----
        private enum FishPhase { None, Walking, Waiting, Fighting, Done }
        private FishPhase fishPhase = FishPhase.None;
        private Vector2? fishTarget;          // water tile to cast into
        private Vector2? fishStandTile;       // tile adjacent to water to stand on
        private int fishTicks;                // countdown timer for fishing phases
        private int fishCastCount;            // consecutive casts without catch (abort after N)
        private bool fishHadMinigame;         // true once BobberBar opened for the current cast

        private object lastResult;

        public PlayerPilot(IMonitor monitor, IInputHelper input)
        {
            this.monitor = monitor;
            this.input = input;
        }

        // ======================
        // TICK (every frame)
        // ======================

        public void Tick()
        {
            if (!Context.IsWorldReady || Game1.player == null) return;

            // Deferred sleep: after warping home the world fades; wait until the player is
            // free to move (fade done, no menu) inside the farmhouse, then end the day.
            if (this.pendingSleep)
            {
                if (Game1.currentLocation is FarmHouse && Context.CanPlayerMove)
                    this.TrySleep();
                return;
            }

            // Shop purchase in progress: the ShopMenu is open (CanPlayerMove is false, but
            // UpdateTicked still fires) — run the timed browse -> buy -> close state machine.
            if (this.buyPhase != BuyPhase.None)
            {
                this.ProcessBuy();
                return;
            }

            // Autonomous fishing in progress: walk→face→cast→auto-catch state machine.
            // During Walking phase we MUST let normal movement (ProcessMovement below)
            // handle the walk to shore; once arrived, ArriveAtTarget transitions to Waiting.
            // Only intercept Tick for non-walking fish phases (Waiting / Fighting).
            if (this.fishPhase != FishPhase.None && this.fishPhase != FishPhase.Walking)
            {
                this.ProcessFish();
                return;
            }

            // Chained tool swings (player_use_tool_repeat): stand and swing until done.
            if (this.toolRepeatRemaining > 0)
            {
                this.ProcessToolRepeat();
                return;
            }

            // Deferred plant verification (input-sim first, vanilla fallback).
            if (this.plantVerifyTile.HasValue)
                this.VerifyPlant();

            // Movement in progress → walk one step (simulated key press) per tick.
            if (this.path != null)
            {
                this.ProcessMovement();
                return;
            }

            // Walking out a door: arrived next to the warp tile; now STEP onto it so the
            // vanilla warp auto-transitions to the target location (human-style, no teleport).
            if (this.pendingExitTile.HasValue)
            {
                this.ProcessExit();
                return;
            }

            if (this.actionCooldown > 0) { this.actionCooldown--; return; }

            if (this.Mode == PilotMode.Travel)
                this.DoTravel();
            else if (this.Mode == PilotMode.Farm)
                this.DoFarm();
        }

        // ======================
        // MOVEMENT (A* + simulated direction keys)
        // ======================

        /// <summary>Compute an A* path and start walking. Returns false if unreachable.</summary>
        private bool StartPath(int tx, int ty)
        {
            var loc = Game1.player.currentLocation;
            var goal = new Vector2(tx, ty);
            var found = this.pathfinder.FindPath(loc, Game1.player.Tile, goal);
            if (found == null)
                return false;

            this.ClearMovement();
            if (found.Count == 0)
            {
                // Already standing on the goal tile.
                this.ArriveAtTarget();
                return true;
            }

            this.path = found;
            this.pathIndex = 0;
            this.finalTarget = goal;
            this.lastTile = Game1.player.Tile;
            return true;
        }

        private void ProcessMovement()
        {
            var player = Game1.player;

            // Menus / fade transitions / tool animations: wait without failing.
            if (!Context.CanPlayerMove || player.UsingTool)
            {
                this.blockedTicks++;
                if (this.blockedTicks > BlockedLimitTicks)
                    this.FailMovement("blocked too long (menu/fade)");
                return;
            }
            this.blockedTicks = 0;

            var currentTile = player.Tile;
            var waypoint = this.path[this.pathIndex];

            if (currentTile == waypoint)
            {
                this.pathIndex++;
                this.stuckTicks = 0;
                if (this.pathIndex >= this.path.Count)
                {
                    this.ArriveAtTarget();
                    return;
                }
                waypoint = this.path[this.pathIndex];
            }

            // Stuck detection at tile granularity.
            if (currentTile == this.lastTile) this.stuckTicks++;
            else { this.stuckTicks = 0; this.lastTile = currentTile; }

            if (this.stuckTicks > StuckLimitTicks)
            {
                this.recalcAttempts++;

                // Diagnostic: why can't we advance? If the waypoint reads walkable yet the
                // player never crosses onto it, it's a walkability-vs-physics mismatch.
                var loc = player.currentLocation;
                int cx = (int)currentTile.X, cy = (int)currentTile.Y;
                int wx = (int)waypoint.X, wy = (int)waypoint.Y;
                this.monitor.Log(
                    $"Stuck detail: tile=({cx},{cy}) px=({(int)player.Position.X},{(int)player.Position.Y}) canMove={Context.CanPlayerMove} usingTool={player.UsingTool} " +
                    $"waypoint=({wx},{wy}) walkable={Pathfinder.IsWalkable(loc, wx, wy)} | " +
                    $"N={Pathfinder.IsWalkable(loc, cx, cy - 1)} S={Pathfinder.IsWalkable(loc, cx, cy + 1)} " +
                    $"E={Pathfinder.IsWalkable(loc, cx + 1, cy)} W={Pathfinder.IsWalkable(loc, cx - 1, cy)}",
                    LogLevel.Debug);

                if (this.recalcAttempts >= MaxRecalcAttempts)
                {
                    this.FailMovement($"stuck at ({cx},{cy}) after {MaxRecalcAttempts} path recalculations");
                    return;
                }
                this.monitor.Log($"Movement stuck; recalculating path ({this.recalcAttempts}/{MaxRecalcAttempts})", LogLevel.Debug);
                if (!this.RecalculatePath())
                    this.FailMovement("path blocked - cannot reach destination");
                return;
            }

            // Press the direction key toward the next waypoint (axis-aligned path).
            int dx = (int)waypoint.X - (int)currentTile.X;
            int dy = (int)waypoint.Y - (int)currentTile.Y;
            if (dx != 0 || dy != 0)
                this.input.Press(this.GetMoveButton(dx, dy));
        }

        private bool RecalculatePath()
        {
            if (!this.finalTarget.HasValue) return false;
            var found = this.pathfinder.FindPath(Game1.player.currentLocation, Game1.player.Tile, this.finalTarget.Value);
            if (found == null) return false;
            this.path = found;
            this.pathIndex = 0;
            this.stuckTicks = 0;
            this.lastTile = Game1.player.Tile;
            return true;
        }

        private void FailMovement(string reason)
        {
            this.monitor.Log($"Movement failed: {reason}", LogLevel.Warn);

            // If we were pathing to a farm task, mark THAT tile as exhausted so DoFarm /
            // DoReclaim discard it next tick and pick another reachable task, instead of
            // re-selecting the same blocked tile forever (the crowded-farm stuck root cause).
            if (this.currentTask != null)
                this.taskAttempts[this.currentTask.Tile] = MaxTaskAttempts;

            this.ClearMovement();
            this.executeOnArrive = false;
            this.currentTask = null;
            this.pendingDeposit = DepositKind.None;
            this.depositFilter = null;
            this.takeSeedsOnly = false;
            this.buyOnArrive = false;
            this.buyPhase = BuyPhase.None;
            Game1.player.Halt();

            // ── Fish walking failed: abort the fishing session ──
            if (this.fishPhase == FishPhase.Walking)
            {
                this.ClearFishing();
                this.lastResult = new { action = "player_fish", success = false, detail = reason };
                this.actionCooldown = 20;
                return;
            }

            // Cross-location travel: a failed hop shouldn't abort the whole trip. Count it,
            // and either bail after too many or let DoTravel recompute the next hop.
            if (this.Mode == PilotMode.Travel)
            {
                if (++this.travelHopFails >= 6) { this.EndTravel(false, $"travel blocked: {reason}"); return; }
                this.lastResult = new { action = "player_go_to", success = false, detail = $"hop blocked, retrying: {reason}" };
                this.actionCooldown = 15;
                return;
            }

            this.lastResult = new { action = "move", success = false, detail = reason };
            this.actionCooldown = 15;
        }

        private void ClearMovement()
        {
            this.path = null;
            this.pathIndex = 0;
            this.finalTarget = null;
            this.stuckTicks = 0;
            this.recalcAttempts = 0;
            this.blockedTicks = 0;
        }

        private void ArriveAtTarget()
        {
            Game1.player.Halt();

            // ── Fish Walking → Waiting transition: we just walked to the shore tile. ──
            // Face the water and arm the Waiting phase so ProcessFish takes over next tick.
            if (this.fishPhase == FishPhase.Walking)
            {
                this.ClearMovement();
                this.executeOnArrive = false;
                this.currentTask = null;
                if (this.fishTarget.HasValue && this.fishStandTile.HasValue)
                {
                    Vector2 dir = this.fishTarget.Value - Game1.player.Tile;
                    int facing = Math.Abs(dir.X) > Math.Abs(dir.Y)
                        ? (dir.X > 0 ? 1 : 3)   // right : left
                        : (dir.Y > 0 ? 2 : 0);  // down  : up
                    Game1.player.faceDirection(facing);
                }
                this.fishPhase = FishPhase.Waiting;
                this.fishTicks = 15;  // brief pause then cast
                return;
            }

            // Deferred purchase: we walked up to the shop counter — open the real (visible)
            // ShopMenu now; ProcessBuy runs the timed browse -> buy -> close.
            if (this.buyOnArrive)
            {
                this.buyOnArrive = false;
                this.ClearMovement();
                this.executeOnArrive = false;
                this.currentTask = null;
                this.Mode = PilotMode.Idle;

                var pierre = Game1.currentLocation?.characters?.FirstOrDefault(c => c.Name == "Pierre");
                if (pierre != null)
                    this.FaceTile(pierre.Tile);
                Utility.TryOpenShopMenu("SeedShop", "Pierre");
                this.buyPhase = BuyPhase.Browsing;
                this.buyTicks = 30;
                return;
            }

            // Deferred deposit: we walked next to a shipping bin / chest — hand off now.
            if (this.pendingDeposit != DepositKind.None)
            {
                var kind = this.pendingDeposit;
                this.pendingDeposit = DepositKind.None;
                this.depositTile = null;
                string filter = this.depositFilter;
                this.depositFilter = null;
                bool seedsOnly = this.takeSeedsOnly;
                this.takeSeedsOnly = false;
                this.ClearMovement();
                this.executeOnArrive = false;
                this.currentTask = null;
                this.Mode = PilotMode.Idle;

                if (kind == DepositKind.Ship)
                {
                    CompanionActions.ShipSellables(Game1.player, out int sc, out int sv);
                    this.lastResult = new { action = "player_ship", success = sc > 0, count = sc, value = sv, detail = sc > 0 ? $"shipped {sc} items (~{sv}g)" : "nothing sellable to ship" };
                }
                else if (kind == DepositKind.Store)
                {
                    var chest = this.FindNearestChest(Game1.player.currentLocation, Game1.player.Tile, 2);
                    int cc = 0;
                    if (chest != null) CompanionActions.StoreToChest(chest, Game1.player, filter, out cc);
                    this.lastResult = new { action = "player_store", success = cc > 0, count = cc, detail = cc > 0 ? $"stored {cc} items" : "nothing stored (chest full or no matching items)" };
                }
                else // Take
                {
                    var chest = this.FindNearestChest(Game1.player.currentLocation, Game1.player.Tile, 2);
                    int tc = 0;
                    if (chest != null) CompanionActions.WithdrawFromChest(chest, Game1.player, filter, seedsOnly, out tc);
                    this.lastResult = new { action = "player_take", success = tc > 0, count = tc, detail = tc > 0 ? $"took {tc} items from chest" : "nothing taken (chest empty / no match / inventory full)" };
                }
                this.actionCooldown = 20;
                return;
            }

            var task = this.currentTask;
            bool doTask = this.executeOnArrive;
            this.ClearMovement();
            this.executeOnArrive = false;
            this.currentTask = null;

            if (doTask && task != null)
            {
                this.ExecuteFarmAction(Game1.player.currentLocation, task);
                return; // ExecuteFarmAction sets its own cooldown
            }

            this.lastResult = new { action = "move", success = true, detail = $"arrived at ({(int)Game1.player.Tile.X},{(int)Game1.player.Tile.Y})" };
        }

        /// <summary>Human-style door exit: after pathing next to a warp tile, press the
        /// direction key toward that tile so the player STEPS ONTO the warp. Vanilla then
        /// auto-transitions to the target location. No warpFarmer teleport. Times out safely.</summary>
        private void ProcessExit()
        {
            // Location changed → we walked through the door successfully.
            if (!string.Equals(Game1.currentLocation?.Name, this.pendingExitFrom, StringComparison.OrdinalIgnoreCase))
            {
                string to = Game1.currentLocation?.Name;
                this.ClearExit();
                Game1.player.Halt();
                // Cross-location travel: keep going toward the final destination.
                if (this.Mode == PilotMode.Travel)
                {
                    this.travelHopFails = 0;
                    this.actionCooldown = 10; // let the fade settle, then DoTravel picks the next hop
                    return;
                }
                this.lastResult = new { action = "player_go_outside", success = true, detail = $"stepped outside to {to}" };
                this.actionCooldown = 20;
                return;
            }

            // Safety timeout (~4s) so a mis-located warp can't hang the pilot.
            if (++this.pendingExitTicks > 240)
            {
                this.ClearExit();
                Game1.player.Halt();
                if (this.Mode == PilotMode.Travel)
                {
                    if (++this.travelHopFails >= 6) { this.EndTravel(false, "timed out stepping onto a warp"); return; }
                    this.actionCooldown = 15; // retry: DoTravel recomputes the hop
                    return;
                }
                this.lastResult = new { action = "player_go_outside", success = false, detail = "timed out stepping onto the door" };
                this.actionCooldown = 20;
                return;
            }

            if (!Context.CanPlayerMove) return; // fade / transition already running

            var cur = Game1.player.Tile;
            var warp = this.pendingExitTile.Value;
            int dx = (int)warp.X - (int)cur.X;
            int dy = (int)warp.Y - (int)cur.Y;
            // Standing exactly on the warp tile without a transition (edge case): nudge down.
            if (dx == 0 && dy == 0)
            {
                this.input.Press(this.GetMoveButton(0, 1));
                return;
            }
            this.input.Press(this.GetMoveButton(dx, dy));
        }

        private void ClearExit()
        {
            this.pendingExitTile = null;
            this.pendingExitFrom = null;
            this.pendingExitTicks = 0;
        }

        /// <summary>Reset the autonomous fishing state machine (stop / pause / abort).
        /// Also force-cancels a cast line so the player regains control immediately —
        /// otherwise UsingTool stays true and movement commands are silently swallowed.</summary>
        private void ClearFishing()
        {
            if (this.fishPhase != FishPhase.None && Game1.player.CurrentTool is FishingRod rod
                && (rod.isFishing || rod.isCasting || rod.isTimingCast || rod.castedButBobberStillInAir
                    || rod.isNibbling || rod.isReeling || rod.pullingOutOfWater || rod.fishCaught))
            {
                try
                {
                    rod.doneFishing(Game1.player, false);
                    Game1.player.completelyStopAnimatingOrDoingAction();
                }
                catch { }
            }
            this.fishPhase = FishPhase.None;
            this.fishTarget = null;
            this.fishStandTile = null;
            this.fishTicks = 0;
            this.fishCastCount = 0;
            this.fishHadMinigame = false;
        }

        // ======================
        // CROSS-LOCATION TRAVEL (walk map-to-map via the warp graph, no teleport)
        // ======================

        /// <summary>Advance one hop toward <see cref="travelTarget"/>. If already there, walk
        /// the optional final tile and finish. Otherwise ask <see cref="LocationRouter"/> for
        /// the next hop, path next to that warp tile and arm the step-onto-warp primitive;
        /// the transition then re-enters DoTravel for the following hop (self-correcting).</summary>
        private void DoTravel()
        {
            var loc = Game1.player.currentLocation;
            if (loc == null || string.IsNullOrEmpty(this.travelTarget)) { this.EndTravel(false, "no travel target"); return; }

            // Arrived at the destination location.
            if (string.Equals(loc.Name, this.travelTarget, StringComparison.OrdinalIgnoreCase))
            {
                if (this.travelFinalTile.HasValue
                    && Game1.player.Tile != this.travelFinalTile.Value
                    && this.pathfinder.IsTileWalkable(loc, (int)this.travelFinalTile.Value.X, (int)this.travelFinalTile.Value.Y))
                {
                    var ft = this.travelFinalTile.Value;
                    this.travelFinalTile = null; // walk to it once
                    if (this.StartPath((int)ft.X, (int)ft.Y)) return;
                }
                this.EndTravel(true, $"arrived at {loc.Name}");
                return;
            }

            // Next hop toward the target via the warp graph.
            var hop = LocationRouter.GetNextHop(loc.Name, this.travelTarget);
            if (hop == null) { this.EndTravel(false, $"no route from {loc.Name} to {this.travelTarget}"); return; }

            // Try ALL warp tiles toward the next location, sorted by proximity to the avatar.
            // This handles maps like Backwoods where dozens of edge warps exist but only some
            // are reachable through the forest terrain.
            var allWarps = LocationRouter.GetAllWarpsTo(loc.Name, hop.Value.NextLocation, Game1.player.Tile);
            Vector2? successWarp = null;
            foreach (var warpTile in allWarps)
            {
                var approach = this.FindApproachTile(loc, warpTile);
                if (approach == null) continue;
                if (!this.StartPath((int)approach.Value.X, (int)approach.Value.Y)) continue;
                successWarp = warpTile;
                break;
            }

            if (successWarp == null)
            {
                if (++this.travelHopFails >= 6) { this.EndTravel(false, $"cannot reach the warp toward {hop.Value.NextLocation}"); return; }
                this.actionCooldown = 15;
                return;
            }
            // On arrival, step onto the warp tile (vanilla auto-transition). ProcessExit
            // detects the location change and re-enters DoTravel for the next hop.
            this.pendingExitTile = successWarp.Value;
            this.pendingExitFrom = loc.Name;
            this.pendingExitTicks = 0;
        }

        private void EndTravel(bool success, string detail)
        {
            this.Mode = PilotMode.Idle;
            this.travelTarget = null;
            this.travelFinalTile = null;
            this.travelHopFails = 0;
            this.ClearExit();
            this.ClearMovement();
            Game1.player.Halt();
            this.lastResult = new { action = "player_go_to", success, detail };
            this.actionCooldown = success ? 20 : 60;
        }

        // ======================
        // SHOP PURCHASE (menu engine): open ShopMenu -> keep visible -> buy via API -> close
        // ======================
        private void ProcessBuy()
        {
            var menu = Game1.activeClickableMenu as ShopMenu;

            if (this.buyPhase == BuyPhase.Browsing)
            {
                if (menu == null)
                {
                    // Shop refused to open (closed / Pierre away / festival).
                    this.buyPhase = BuyPhase.None;
                    this.lastResult = new { action = "player_buy", success = false, detail = "shop did not open (closed or Pierre away)" };
                    this.actionCooldown = 30;
                    return;
                }
                if (this.buyTicks-- > 0) return; // let the menu render for a moment (visible)

                bool ok = CompanionActions.BuySeeds(menu, Game1.player, this.buyQtyCap, this.buyBudget,
                    out int bought, out int spent, out string what);
                this.lastResult = new
                {
                    action = "player_buy",
                    success = ok,
                    count = bought,
                    spent,
                    item = what,
                    detail = ok ? $"bought {bought}x {what} (~{spent}g)" : "bought nothing (can't afford / no seeds in stock)"
                };
                this.buyTicks = 30; // hold the menu open a beat so the purchase is visible
                this.buyPhase = BuyPhase.Done;
                return;
            }

            if (this.buyPhase == BuyPhase.Done)
            {
                if (this.buyTicks-- > 0) return;
                Game1.activeClickableMenu?.exitThisMenu(); // human-style close (ESC)
                this.buyPhase = BuyPhase.None;
                this.actionCooldown = 20;
            }
        }

        // ======================
        // AUTONOMOUS FISHING (player_fish)
        // ======================

        private void ProcessFish()
        {
            // ── Auto-catch: if fishing minigame (BobberBar) is active, cheat the bobber ──
            if (Game1.activeClickableMenu is BobberBar bar)
            {
                this.fishHadMinigame = true;  // mark that a real minigame opened for this cast
                try
                {
                    var fiBobber = typeof(BobberBar).GetField("bobberPosition",
                        System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance);
                    var fiFish = typeof(BobberBar).GetField("positionOfFish",
                        System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance);
                    if (fiBobber != null && fiFish != null)
                    {
                        float bobber = (float)fiBobber.GetValue(bar);
                        float fish = (float)fiFish.GetValue(bar);
                        // Move bobber toward fish at max speed (~2.5 px/frame)
                        float speed = 2.5f;
                        if (bobber < fish) bobber = Math.Min(bobber + speed, fish);
                        else if (bobber > fish) bobber = Math.Max(bobber - speed, fish);
                        fiBobber.SetValue(bar, bobber);
                    }
                    // Skip treasure chests: the post-catch ItemGrabMenu would block the
                    // automation loop, so disable treasure before the catch completes.
                    var fiTreasure = typeof(BobberBar).GetField("treasure",
                        System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance);
                    if (fiTreasure != null)
                        fiTreasure.SetValue(bar, false);
                    // Also fast-forward catch progress so fish are reeled in quickly
                    var fiCatch = typeof(BobberBar).GetField("distanceFromCatching",
                        System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance);
                    if (fiCatch != null)
                    {
                        float dist = (float)fiCatch.GetValue(bar);
                        if (dist > 0.01f)
                            fiCatch.SetValue(bar, Math.Max(dist - 0.03f, 0f));
                    }
                }
                catch { }
                return;  // let the minigame run while we auto-catch
            }

            var fishRod = Game1.player.CurrentTool as FishingRod;

            // ── "Caught fish" show-off pose blocks until a button press → dismiss it ──
            if (fishRod != null && fishRod.fishCaught)
            {
                this.PressUseTool();
                this.actionCooldown = 10;
                return;
            }

            // ── Minigame just ended (it really opened for this cast) → reset for next cast ──
            // (we don't track individual catch results; success/failure is visible in inventory)
            if (this.fishPhase == FishPhase.Fighting && this.fishHadMinigame)
            {
                this.fishHadMinigame = false;
                this.fishPhase = FishPhase.Waiting;
                this.fishTicks = 40;  // brief cooldown then recast
                this.fishCastCount++;
                if (this.fishCastCount > 20)  // safety cap: stop after 20 casts
                {
                    this.fishPhase = FishPhase.None;
                    this.lastResult = new { action = "player_fish", success = true, detail = "fished 20 times, stopping" };
                }
                return;
            }

            // ── Fighting: rod is cast. Runs BEFORE the CanPlayerMove gate because the
            // player counts as "using tool" the whole time the line is in the water. ──
            if (this.fishPhase == FishPhase.Fighting)
            {
                if (this.actionCooldown > 0) { this.actionCooldown--; return; }
                if (fishRod == null)
                {
                    this.fishPhase = FishPhase.None;
                    this.lastResult = new { action = "player_fish", success = false, detail = "fishing rod lost mid-session" };
                    return;
                }

                // Fish is nibbling ("!") → press use-tool to hook it (FishingTweaks-style
                // auto-hook). Real fish open the BobberBar; trash is pulled straight out.
                if (fishRod.isNibbling && !fishRod.hit && !fishRod.isReeling && !fishRod.pullingOutOfWater)
                {
                    this.PressUseTool();
                    this.actionCooldown = 20;  // let the hook animation play
                    return;
                }

                // Rod finished with no minigame (cast hit land / trash / missed the bite).
                if (!Game1.player.UsingTool)
                {
                    this.fishPhase = FishPhase.Waiting;
                    this.fishTicks = 15;
                    this.fishCastCount++;
                    if (this.fishCastCount > 20)
                    {
                        this.fishPhase = FishPhase.None;
                        this.lastResult = new { action = "player_fish", success = false, detail = "no fish after 20 casts, aborting" };
                    }
                    return;
                }

                // Still waiting for a bite — on timeout reel the empty line back in
                // to regain control; the !UsingTool branch above recasts next loop.
                if (this.fishTicks-- <= 0)
                {
                    if (fishRod.isFishing && !fishRod.isReeling && !fishRod.pullingOutOfWater)
                        this.PressUseTool();
                    this.fishTicks = 180;  // allow the reel-in animation to finish
                }
                return;
            }

            if (!Context.CanPlayerMove) return;
            if (this.path != null) return;  // still walking to shore
            if (this.actionCooldown > 0) { this.actionCooldown--; return; }

            switch (this.fishPhase)
            {
                case FishPhase.None:
                default:
                    break;

                case FishPhase.Walking:
                {
                    // Path completed — Waiting will face the water right before casting
                    if (!this.fishTarget.HasValue || !this.fishStandTile.HasValue)
                    {
                        this.fishPhase = FishPhase.None;
                        break;
                    }
                    this.FaceFishTarget();
                    this.fishPhase = FishPhase.Waiting;
                    this.fishTicks = 15;
                    break;
                }

                case FishPhase.Waiting:
                {
                    // Brief pause then cast
                    if (this.fishTicks-- > 0) return;
                    int rodSlot = this.FindSlot(i => i is FishingRod);
                    if (rodSlot < 0)
                    {
                        this.fishPhase = FishPhase.None;
                        this.lastResult = new { action = "player_fish", success = false, detail = "no fishing rod in hotbar" };
                        break;
                    }
                    Game1.player.CurrentToolIndex = rodSlot;

                    // ── Auto-bait (inspired by FishingTweaks): attach bait from inventory ──
                    if (Game1.player.CurrentTool is FishingRod rod && rod.attachments?.Count > 0 && rod.attachments[0] == null)
                    {
                        int baitSlot = this.FindSlot(i => i.Category == -21);  // -21 = bait
                        if (baitSlot >= 0)
                        {
                            // attach()/canThisBeAttached() take StardewValley.Object, not Item
                            var bait = Game1.player.Items[baitSlot] as StardewValley.Object;
                            if (bait != null && rod.canThisBeAttached(bait))
                                Game1.player.Items[baitSlot] = rod.attach(bait);
                        }
                    }

                    // Re-face the water EVERY cast: the show-off pose / reel-in animation
                    // can leave the player facing land, which would cast onto the beach.
                    this.FaceFishTarget();

                    this.PressUseTool();  // cast the line into water
                    this.fishPhase = FishPhase.Fighting;
                    this.fishHadMinigame = false;  // fresh cast: no minigame seen yet
                    this.fishTicks = 600;  // up to 10s for a bite; Fighting block (above) handles hook/timeout
                    this.actionCooldown = 10;
                    break;
                }

                // NOTE: FishPhase.Fighting is handled BEFORE the CanPlayerMove gate
                // (the player counts as "using tool" while the line is in the water).
            }
        }

        /// <summary>Turn the player toward the target water tile (used before every cast).</summary>
        private void FaceFishTarget()
        {
            if (!this.fishTarget.HasValue) return;
            Vector2 dir = this.fishTarget.Value - Game1.player.Tile;
            int facing = Math.Abs(dir.X) > Math.Abs(dir.Y)
                ? (dir.X > 0 ? 1 : 3)   // right : left
                : (dir.Y > 0 ? 2 : 0);  // down  : up
            Game1.player.faceDirection(facing);
        }

        private SButton GetMoveButton(int dx, int dy)
        {
            var o = Game1.options;
            if (dx > 0) return o.moveRightButton.Length > 0 ? o.moveRightButton[0].ToSButton() : SButton.D;
            if (dx < 0) return o.moveLeftButton.Length > 0 ? o.moveLeftButton[0].ToSButton() : SButton.A;
            if (dy > 0) return o.moveDownButton.Length > 0 ? o.moveDownButton[0].ToSButton() : SButton.S;
            return o.moveUpButton.Length > 0 ? o.moveUpButton[0].ToSButton() : SButton.W;
        }

        // ======================
        // INPUT SIMULATION primitives
        // ======================

        /// <summary>Face a (usually adjacent) tile, like a human turning toward it.</summary>
        private void FaceTile(Vector2 tile)
        {
            var p = Game1.player.Tile;
            float dx = tile.X - p.X, dy = tile.Y - p.Y;
            Game1.player.faceDirection(Math.Abs(dx) > Math.Abs(dy)
                ? (dx > 0 ? 1 : 3)
                : (dy > 0 ? 2 : 0));
        }

        /// <summary>Aim the game cursor at the faced tile (keyboard-style targeting).</summary>
        private void SetCursorToFacingTile()
        {
            var player = Game1.player;
            int tileX = (int)player.Tile.X;
            int tileY = (int)player.Tile.Y;
            switch (player.FacingDirection) // 0 up, 1 right, 2 down, 3 left
            {
                case 0: tileY--; break;
                case 1: tileX++; break;
                case 2: tileY++; break;
                case 3: tileX--; break;
            }

            Game1.currentCursorTile = new Vector2(tileX, tileY);
            Game1.lastCursorMotionWasMouse = false; // game uses facing/cursor tile, not the OS mouse

            int screenX = (tileX * 64 + 32) - Game1.viewport.X;
            int screenY = (tileY * 64 + 32) - Game1.viewport.Y;
            Game1.setMousePosition(screenX, screenY); // visual feedback only
        }

        /// <summary>Press the use-tool button (left click equivalent) aimed at the faced tile.</summary>
        private void PressUseTool()
        {
            this.SetCursorToFacingTile();
            var b = Game1.options.useToolButton.Length > 0
                ? Game1.options.useToolButton[0].ToSButton()
                : SButton.MouseLeft;
            this.input.Press(b);
        }

        /// <summary>Press the action/check button (right click equivalent) aimed at the faced tile.</summary>
        private void PressActionButton()
        {
            this.SetCursorToFacingTile();
            var b = Game1.options.actionButton.Length > 0
                ? Game1.options.actionButton[0].ToSButton()
                : SButton.MouseRight;
            this.input.Press(b);
        }

        /// <summary>Find a hotbar slot (first 12 items) matching a predicate, or -1.</summary>
        private int FindSlot(Func<Item, bool> predicate)
        {
            var items = Game1.player.Items;
            int max = Math.Min(12, items.Count); // CurrentToolIndex only addresses the hotbar
            for (int i = 0; i < max; i++)
            {
                if (items[i] != null && predicate(items[i]))
                    return i;
            }
            return -1;
        }

        private bool SelectSlot(Func<Item, bool> predicate)
        {
            int idx = this.FindSlot(predicate);
            if (idx < 0) return false;
            Game1.player.CurrentToolIndex = idx;
            return true;
        }

        // ======================
        // AUTONOMOUS FARM
        // ======================

        private void DoFarm()
        {
            var loc = Game1.player.currentLocation;
            if (loc == null) return;

            // Attempt budget + task queue reset on location change.
            if (this.attemptsLocation != loc.Name)
            {
                this.taskAttempts.Clear();
                this.attemptsLocation = loc.Name;
                this.farmQueue = null;
                // NOTE: the managed plot (reclaimRect) is PERSISTENT across locations/days;
                // it is only tended while we are standing in its own location (below).
            }

            // Managed plot takes precedence over the radius scan while we are in ITS location:
            // develop/maintain one neat block phase-by-phase (harvest → cut → hoe → plant →
            // water) like a human. Elsewhere (e.g. the Mine) fall through to the normal scan.
            if (this.reclaimRect != null
                && string.Equals(loc.Name, this.reclaimLocation, StringComparison.OrdinalIgnoreCase))
            {
                this.DoReclaim();
                return;
            }

            // (Re)scan when there is no queue or it went stale; between rescans we
            // chain task-to-task so the player sweeps the area like a human would,
            // instead of re-sorting the whole map every action (which caused jitter).
            if (this.farmQueue == null)
            {
                this.farmQueue = CompanionActions.ScanForTasks(loc, this.monitor, Game1.player.Tile);
                this.farmQueueAge = 0;
            }
            else if (++this.farmQueueAge > FarmQueueMaxAgeTicks)
            {
                this.farmQueue = null; // force a fresh scan on the next call
                return;
            }

            // Drop tiles that burned their attempt budget.
            this.farmQueue.RemoveAll(t => this.taskAttempts.TryGetValue(t.Tile, out var n) && n >= MaxTaskAttempts);

            if (this.farmQueue.Count == 0)
            {
                this.farmQueue = null;
                this.lastResult = new { mode = "farm", detail = "no tasks in this location" };
                this.actionCooldown = 60;
                return;
            }

            // Nearest-neighbor chaining inside the highest priority group: finish the
            // closest task, then the next-closest of the same priority, and so on.
            var myTile = Game1.player.Tile;
            int topPri = int.MinValue;
            foreach (var t in this.farmQueue)
                if (t.Priority > topPri) topPri = t.Priority;

            FarmTask next = null;
            float bestD = float.MaxValue;
            foreach (var t in this.farmQueue)
            {
                if (t.Priority != topPri) continue;
                float d = Vector2.Distance(myTile, t.Tile);
                if (d < bestD) { bestD = d; next = t; }
            }
            this.farmQueue.Remove(next);

            // Walk to a tile ADJACENT to the task and act from there, exactly like a
            // human - never act on a far tile. Unreachable: burn an attempt, move on.
            var approach = this.FindApproachTile(loc, next.Tile);
            if (approach == null || !this.StartPath((int)approach.Value.X, (int)approach.Value.Y))
            {
                this.taskAttempts[next.Tile] = (this.taskAttempts.TryGetValue(next.Tile, out var m) ? m : 0) + 1;
                this.actionCooldown = 5; // try the next queued task almost immediately
                return;
            }

            this.currentTask = next;
            this.executeOnArrive = true;
        }

        // ======================
        // RECTANGLE RECLAIM (human-like: cut grass -> hoe rectangle -> plant -> water)
        // ======================

        /// <summary>Drive the active rectangle one PHASE at a time. The phase is the lowest
        /// action still needed by ANY rect tile (CutGrass -> Hoe -> Plant -> Water); within a
        /// phase we act on the FIRST tile in serpentine order. This yields visible, orderly
        /// sweeps (all grass cut, then all tilled, then planted, then watered).</summary>
        private void DoReclaim()
        {
            var loc = Game1.player.currentLocation;
            if (loc == null || this.reclaimRect == null || this.reclaimRect.Count == 0)
            {
                this.reclaimRect = null;
                return;
            }

            bool seedsRemain = this.FindSlot(i => i.Category == -74
                && (this.reclaimSeed == null || i.Name.IndexOf(this.reclaimSeed, StringComparison.OrdinalIgnoreCase) >= 0)) >= 0;

            // Pick the current phase = first action type still needed by any tile.
            // Water comes FIRST: existing crops must be watered before anything else
            // (a dry crop loses a growth day), then harvest, then develop the plot.
            FarmTaskType? phase = null;
            foreach (var candidate in new[] { FarmTaskType.Water, FarmTaskType.Harvest, FarmTaskType.CutGrass, FarmTaskType.Hoe, FarmTaskType.Plant })
            {
                if (candidate == FarmTaskType.Plant && !seedsRemain) continue;
                foreach (var t in this.reclaimRect)
                {
                    if (this.RectTileNeeds(loc, t, candidate, seedsRemain)) { phase = candidate; break; }
                }
                if (phase != null) break;
            }

            if (phase == null)
            {
                // Managed plot fully tended: KEEP the rectangle (it persists across days).
                // Idle here until crops ripen / a new day resets the tiles, so the brain can
                // leave for the Mine. Rebuilt only when the AI re-issues player_reclaim.
                this.plotIdle = true;
                this.lastResult = new { action = "player_reclaim", success = true, detail = "plot fully tended" };
                this.actionCooldown = 300; // ~5s; re-check periodically for ripe/dry crops
                return;
            }
            this.plotIdle = false;

            // First tile (serpentine order) that needs this phase action.
            Vector2 target = default;
            bool found = false;
            foreach (var t in this.reclaimRect)
            {
                if (this.RectTileNeeds(loc, t, phase.Value, seedsRemain)) { target = t; found = true; break; }
            }
            if (!found) { this.actionCooldown = 5; return; }

            var approach = this.FindApproachTile(loc, target);
            if (approach == null || !this.StartPath((int)approach.Value.X, (int)approach.Value.Y))
            {
                // Unreachable: burn attempts, and drop the tile once it exhausts its budget.
                int n = (this.taskAttempts.TryGetValue(target, out var m) ? m : 0) + 1;
                this.taskAttempts[target] = n;
                if (n >= MaxTaskAttempts) this.reclaimRect.Remove(target);
                this.actionCooldown = 5;
                return;
            }

            this.currentTask = new FarmTask { Type = phase.Value, Tile = target, SeedName = this.reclaimSeed };
            this.executeOnArrive = true;
        }

        /// <summary>Does this rect tile still need the given phase action, given its current
        /// terrain state? Mirrors the vanilla tile-state rules used elsewhere.</summary>
        private bool RectTileNeeds(GameLocation loc, Vector2 t, FarmTaskType phase, bool seedsRemain)
        {
            loc.terrainFeatures.TryGetValue(t, out var feature);
            switch (phase)
            {
                case FarmTaskType.Harvest:
                    return feature is HoeDirt hdHarvest && hdHarvest.crop != null && hdHarvest.readyForHarvest();
                case FarmTaskType.CutGrass:
                    return feature is Grass;
                case FarmTaskType.Hoe:
                    return feature == null
                        && !loc.objects.ContainsKey(t)
                        && loc.doesTileHaveProperty((int)t.X, (int)t.Y, "Diggable", "Back") != null;
                case FarmTaskType.Plant:
                    return seedsRemain && feature is HoeDirt hd && hd.crop == null;
                case FarmTaskType.Water:
                    return feature is HoeDirt hd2 && hd2.crop != null && hd2.state.Value != 1 && !Game1.isRaining;
                default:
                    return false;
            }
        }

        /// <summary>Build the managed-plot rectangle in serpentine tile order. Size defaults
        /// to the seed count (min 3x3, cap 6x6) but honours explicit width/height (1..8). The
        /// anchor is an explicit top-left when given (and reclaimable), otherwise the nearest
        /// fully reclaimable block to the player. Returns false if no suitable block is found.</summary>
        private bool BuildReclaimRect(string seed, int? width, int? height, int? anchorX, int? anchorY)
        {
            var loc = Game1.player.currentLocation;
            if (loc == null) return false;

            int w, h;
            if (width.HasValue || height.HasValue)
            {
                w = Math.Clamp(width ?? height.Value, 1, 8);
                h = Math.Clamp(height ?? width.Value, 1, 8);
            }
            else
            {
                int seedCount = 0;
                foreach (var it in Game1.player.Items)
                {
                    if (it != null && it.Category == -74
                        && (seed == null || it.Name.IndexOf(seed, StringComparison.OrdinalIgnoreCase) >= 0))
                        seedCount += it.Stack;
                }
                int tilesWanted = Math.Clamp(seedCount, 9, 36);   // min 3x3, cap 6x6
                w = (int)Math.Ceiling(Math.Sqrt(tilesWanted));
                h = (int)Math.Ceiling((double)tilesWanted / w);
            }

            int originX, originY;
            if (anchorX.HasValue && anchorY.HasValue)
            {
                // Explicit anchor: use it only if the whole block is reclaimable.
                if (!this.BlockIsReclaimable(loc, anchorX.Value, anchorY.Value, w, h)) return false;
                originX = anchorX.Value;
                originY = anchorY.Value;
            }
            else
            {
                // Expanding search for the top-left origin whose whole w x h block is
                // reclaimable; choose the block whose center is closest to the player.
                const int SearchRadius = 25;
                var myTile = Game1.player.Tile;
                Vector2? bestOrigin = null;
                float bestDist = float.MaxValue;
                for (int oy = (int)myTile.Y - SearchRadius; oy <= (int)myTile.Y + SearchRadius; oy++)
                {
                    for (int ox = (int)myTile.X - SearchRadius; ox <= (int)myTile.X + SearchRadius; ox++)
                    {
                        if (!this.BlockIsReclaimable(loc, ox, oy, w, h)) continue;
                        float cx = ox + (w - 1) / 2f;
                        float cy = oy + (h - 1) / 2f;
                        float d = Vector2.Distance(myTile, new Vector2(cx, cy));
                        if (d < bestDist) { bestDist = d; bestOrigin = new Vector2(ox, oy); }
                    }
                }
                if (bestOrigin == null) return false;
                originX = (int)bestOrigin.Value.X;
                originY = (int)bestOrigin.Value.Y;
            }

            var rect = new List<Vector2>();
            for (int row = 0; row < h; row++)
            {
                int ty = originY + row;
                if (row % 2 == 0)
                    for (int col = 0; col < w; col++) rect.Add(new Vector2(originX + col, ty));
                else
                    for (int col = w - 1; col >= 0; col--) rect.Add(new Vector2(originX + col, ty));
            }
            this.reclaimRect = rect;
            this.reclaimSeed = seed;
            this.reclaimLocation = loc.Name;
            this.plotIdle = false;
            return true;
        }

        /// <summary>Is every tile of the w x h block anchored at (ox,oy) reclaimable?
        /// Requires diggable ground and nothing blocking except grass (cuttable) or existing
        /// HoeDirt (already tilled). Rejects objects, trees/bushes, clumps, water, buildings.</summary>
        private bool BlockIsReclaimable(GameLocation loc, int ox, int oy, int w, int h)
        {
            for (int y = oy; y < oy + h; y++)
            {
                for (int x = ox; x < ox + w; x++)
                {
                    var t = new Vector2(x, y);
                    if (loc.doesTileHaveProperty(x, y, "Diggable", "Back") == null) return false;
                    if (loc.objects.ContainsKey(t)) return false;
                    if (loc.terrainFeatures.TryGetValue(t, out var feature)
                        && !(feature is Grass) && !(feature is HoeDirt))
                        return false;
                    if (loc.isWaterTile(x, y)) return false;
                    foreach (var clump in loc.resourceClumps)
                        if (clump.occupiesTile(x, y)) return false;
                    if (loc is Farm farm)
                        foreach (var b in farm.buildings)
                            if (b.occupiesTile(t)) return false;
                }
            }
            return true;
        }

        /// <summary>Find a walkable tile to act on the task tile from: prefer an
        /// orthogonally-adjacent tile (stand next to it and face it, like a real player);
        /// for non-occupied tiles (e.g. crop dirt) standing on the tile itself is an
        /// allowed fallback. Returns null if none walkable.
        /// When the strict ring-1 check fails (common for edge warp tiles), expands to
        /// ring 2-4 using relaxed walkability so the avatar can still approach map-edge warps.
        /// </summary>
        private Vector2? FindApproachTile(GameLocation loc, Vector2 taskTile)
        {
            var myTile = Game1.player.Tile;

            // --- Ring 1: strict IsTileWalkable (fast path for normal tiles) ---
            Vector2? best = null;
            float bestD = float.MaxValue;
            for (int dx = -1; dx <= 1; dx++)
            {
                for (int dy = -1; dy <= 1; dy++)
                {
                    if (dx == 0 && dy == 0) continue;
                    if (Math.Abs(dx) + Math.Abs(dy) > 1) continue; // cardinal only
                    var t = new Vector2(taskTile.X + dx, taskTile.Y + dy);
                    if (!this.pathfinder.IsTileWalkable(loc, (int)t.X, (int)t.Y)) continue;
                    float d = Vector2.Distance(myTile, t);
                    if (d < bestD) { bestD = d; best = t; }
                }
            }
            if (best != null) return best;

            // Fallback: the task tile itself (only if not occupied by an object).
            if (!loc.objects.ContainsKey(taskTile)
                && this.pathfinder.IsTileWalkable(loc, (int)taskTile.X, (int)taskTile.Y))
                return taskTile;

            // --- Ring 2-4: relaxed walkability (handles map-edge warp tiles where
            //     neighbors are out-of-bounds or blocked by building footprints) ---
            bestD = float.MaxValue;
            best = null;
            for (int radius = 2; radius <= 4; radius++)
            {
                for (int dx = -radius; dx <= radius; dx++)
                {
                    for (int dy = -radius; dy <= radius; dy++)
                    {
                        if (Math.Abs(dx) + Math.Abs(dy) > radius) continue; // Manhattan ring
                        if (dx == 0 && dy == 0) continue;
                        int tx = (int)taskTile.X + dx;
                        int ty = (int)taskTile.Y + dy;
                        if (!Pathfinder.IsRelaxedWalkable(loc, tx, ty)) continue;
                        var t = new Vector2(tx, ty);
                        float d = Vector2.Distance(myTile, t);
                        if (d < bestD) { bestD = d; best = t; }
                    }
                }
                if (best != null) return best; // return closest within this ring
            }

            return null;
        }

        /// <summary>Nearest placed Chest object within <paramref name="radius"/> tiles of a
        /// point (used to resolve which chest a Store deposit lands in on arrival).</summary>
        private Chest FindNearestChest(GameLocation loc, Vector2 from, int radius)
        {
            if (loc == null) return null;
            Chest best = null;
            float bestD = float.MaxValue;
            foreach (var pair in loc.objects.Pairs)
            {
                if (!(pair.Value is Chest chest)) continue;
                float d = Vector2.Distance(from, pair.Key);
                if (d <= radius && d < bestD) { bestD = d; best = chest; }
            }
            return best;
        }

        // ======================
        // TASK EXECUTION (input-simulated)
        // ======================

        private void ExecuteFarmAction(GameLocation loc, FarmTask task)
        {
            Vector2 tile = task.Tile;
            float dist = Vector2.Distance(Game1.player.Tile, tile);

            // Respect the game's reach: only act when actually next to (or on) the tile.
            if (dist > 1.6f)
            {
                this.lastResult = new { action = "farm.skip", success = false, detail = "not adjacent, skipped" };
                this.actionCooldown = 10;
                return;
            }

            // Count attempts per tile so DoFarm eventually skips a tile that won't complete.
            this.taskAttempts[tile] = (this.taskAttempts.TryGetValue(tile, out var n) ? n : 0) + 1;
            string label = task.Type.ToString().ToLower();

            // Standing ON the tile: a button press would hit the FACED tile instead, so use
            // the direct native-logic path for this rare case (same functions vanilla calls).
            if (dist < 0.1f)
            {
                bool okDirect = task.Type switch
                {
                    FarmTaskType.Harvest => CompanionActions.HarvestTile(loc, tile, this.monitor),
                    FarmTaskType.Water => CompanionActions.WaterTile(loc, tile, this.monitor),
                    FarmTaskType.Plant => CompanionActions.PlantTile(loc, tile, this.monitor, task.SeedName),
                    FarmTaskType.CutGrass => CompanionActions.CutGrassTile(loc, tile, this.monitor),
                    _ => false
                };
                this.lastResult = new { action = $"farm.{label}", success = okDirect, detail = okDirect ? "done (on-tile)" : "failed (on-tile)" };
                this.actionCooldown = 30;
                return;
            }

            this.FaceTile(tile);

            switch (task.Type)
            {
                case FarmTaskType.Harvest:
                    // Right-click the crop: vanilla checkAction → HoeDirt.performUseAction.
                    this.PressActionButton();
                    this.lastResult = new { action = "farm.harvest", success = true, detail = $"harvest click at ({(int)tile.X},{(int)tile.Y})" };
                    this.actionCooldown = 25;
                    break;

                case FarmTaskType.Water:
                {
                    if (!this.SelectSlot(i => i is WateringCan))
                    {
                        this.lastResult = new { action = "farm.water", success = false, detail = "no watering can in hotbar" };
                        this.actionCooldown = 10;
                        return;
                    }
                    // Empty can: walk to the nearest water source and refill first. The crop
                    // stays flagged as needing water and is watered on a later pass.
                    if (Game1.player.CurrentTool is WateringCan wc && wc.WaterLeft <= 0)
                    {
                        var water = CompanionActions.FindNearestWaterTile(loc, Game1.player.Tile, 30);
                        var wapproach = water.HasValue ? this.FindApproachTile(loc, water.Value) : null;
                        if (water.HasValue && wapproach != null
                            && this.StartPath((int)wapproach.Value.X, (int)wapproach.Value.Y))
                        {
                            this.currentTask = new FarmTask { Type = FarmTaskType.RefillCan, Tile = water.Value };
                            this.executeOnArrive = true;
                            this.lastResult = new { action = "farm.refill", success = true, detail = "watering can empty; walking to water" };
                            return;
                        }
                        this.lastResult = new { action = "farm.water", success = false, detail = "watering can empty; no reachable water" };
                        this.actionCooldown = 30;
                        return;
                    }
                    this.PressUseTool();
                    this.lastResult = new { action = "farm.water", success = true, detail = $"watering swing at ({(int)tile.X},{(int)tile.Y})" };
                    this.actionCooldown = 50;
                    break;
                }

                case FarmTaskType.RefillCan:
                    if (!this.SelectSlot(i => i is WateringCan))
                    {
                        this.lastResult = new { action = "farm.refill", success = false, detail = "no watering can in hotbar" };
                        this.actionCooldown = 10;
                        return;
                    }
                    // Swing at the faced water tile: vanilla fills the can at a water surface.
                    this.PressUseTool();
                    this.lastResult = new { action = "farm.refill", success = true, detail = $"refilling at water ({(int)tile.X},{(int)tile.Y})" };
                    this.actionCooldown = 50;
                    break;

                case FarmTaskType.ClearDebris:
                {
                    string name = loc.objects.TryGetValue(tile, out var o) ? o.Name ?? "" : "";
                    // 1.6: scythes are MeleeWeapon with isScythe(); weeds cost no energy with a scythe.
                    bool selected = name.Contains("Stone")
                        ? this.SelectSlot(i => i is Pickaxe)
                        : (this.SelectSlot(i => i is MeleeWeapon mw && mw.isScythe()) || this.SelectSlot(i => i is Axe));
                    if (!selected)
                    {
                        this.lastResult = new { action = "farm.clear", success = false, detail = "no suitable tool in hotbar" };
                        this.actionCooldown = 10;
                        return;
                    }
                    this.PressUseTool();
                    this.lastResult = new { action = "farm.clear", success = true, detail = $"tool swing at ({(int)tile.X},{(int)tile.Y}) {name}" };
                    this.actionCooldown = 50;
                    break;
                }

                case FarmTaskType.Hoe:
                    if (!this.SelectSlot(i => i is Hoe))
                    {
                        this.lastResult = new { action = "farm.hoe", success = false, detail = "no hoe in hotbar" };
                        this.actionCooldown = 10;
                        return;
                    }
                    this.PressUseTool();
                    this.lastResult = new { action = "farm.hoe", success = true, detail = $"hoe swing at ({(int)tile.X},{(int)tile.Y})" };
                    this.actionCooldown = 50;
                    break;

                case FarmTaskType.Plant:
                {
                    string seedName = task.SeedName;
                    int slot = this.FindSlot(i => i.Category == -74
                        && (seedName == null || i.Name.IndexOf(seedName, StringComparison.OrdinalIgnoreCase) >= 0));
                    if (slot < 0)
                    {
                        this.lastResult = new { action = "farm.plant", success = false, detail = "no matching seeds in hotbar" };
                        this.actionCooldown = 10;
                        return;
                    }
                    Game1.player.CurrentToolIndex = slot;
                    this.PressUseTool();
                    // Verify shortly; if the click didn't take, fall back to placementAction.
                    this.plantVerifyTile = tile;
                    this.plantVerifyTicks = 50;
                    this.plantSeedName = seedName;
                    this.lastResult = new { action = "farm.plant", success = true, detail = $"planting click at ({(int)tile.X},{(int)tile.Y})" };
                    this.actionCooldown = 55;
                    break;
                }

                case FarmTaskType.CutGrass:
                    // 1.6: scythe is a MeleeWeapon with isScythe(); the swing arc cuts grass
                    // on the faced tile (same weapon-swing path ClearDebris uses for weeds).
                    if (!(this.SelectSlot(i => i is MeleeWeapon mw && mw.isScythe()) || this.SelectSlot(i => i is MeleeWeapon)))
                    {
                        this.lastResult = new { action = "farm.cutgrass", success = false, detail = "no scythe in hotbar" };
                        this.actionCooldown = 10;
                        return;
                    }
                    this.PressUseTool();
                    this.lastResult = new { action = "farm.cutgrass", success = true, detail = $"scythe swing at ({(int)tile.X},{(int)tile.Y})" };
                    this.actionCooldown = 50;
                    break;

                case FarmTaskType.ToolUse:
                {
                    bool selected = string.Equals(task.ToolTypeName, "scythe", StringComparison.OrdinalIgnoreCase)
                        ? this.SelectSlot(i => i is MeleeWeapon mw && mw.isScythe())
                        : (ToolType(task.ToolTypeName) is Type tt && this.SelectSlot(i => tt.IsInstanceOfType(i)));
                    if (!selected)
                    {
                        this.lastResult = new { action = "farm.tool", success = false, detail = $"tool not found: {task.ToolTypeName}" };
                        this.actionCooldown = 10;
                        return;
                    }
                    this.PressUseTool();
                    this.lastResult = new { action = "farm.tool", success = true, detail = $"{task.ToolTypeName} swing at ({(int)tile.X},{(int)tile.Y})" };
                    this.actionCooldown = 50;
                    break;
                }

                case FarmTaskType.Interact:
                    this.PressActionButton();
                    this.lastResult = new { action = "farm.interact", success = true, detail = $"interact click at ({(int)tile.X},{(int)tile.Y})" };
                    this.actionCooldown = 25;
                    break;
            }
        }

        /// <summary>Stand in place and swing the equipped tool repeatedly, waiting for
        /// each swing animation to finish before the next - like a human holding the button.
        /// Ported from stardew-mcp's ProcessToolUse.</summary>
        private void ProcessToolRepeat()
        {
            if (this.toolRepeatCooldown > 0)
            {
                this.toolRepeatCooldown--;
                Game1.lastCursorMotionWasMouse = false;
                return;
            }

            var player = Game1.player;
            if (player.UsingTool || !Context.CanPlayerMove)
            {
                Game1.lastCursorMotionWasMouse = false;
                return; // wait out the current swing animation / transition
            }

            this.PressUseTool();
            this.toolRepeatRemaining--;
            this.toolRepeatCooldown = ToolRepeatCooldownTicks;

            if (this.toolRepeatRemaining <= 0)
                this.lastResult = new { action = "player_use_tool_repeat", success = true, detail = "done" };
        }

        /// <summary>Delayed check for input-simulated planting: if the click planted the
        /// seed, done; otherwise use the vanilla placement path (Object.placementAction),
        /// which validates season/location and consumes the seed only on success.</summary>
        private void VerifyPlant()
        {
            if (--this.plantVerifyTicks > 0) return;

            var tile = this.plantVerifyTile.Value;
            this.plantVerifyTile = null;
            string seedName = this.plantSeedName;
            this.plantSeedName = null;

            var loc = Game1.player?.currentLocation;
            if (loc == null) return;

            bool planted = loc.terrainFeatures.TryGetValue(tile, out var f)
                && f is HoeDirt dirt && dirt.crop != null;
            if (planted)
            {
                this.lastResult = new { action = "farm.plant", success = true, tile = new { x = (int)tile.X, y = (int)tile.Y } };
                return;
            }

            bool ok = CompanionActions.PlantTile(loc, tile, this.monitor, seedName);
            this.lastResult = new { action = "farm.plant", success = ok, detail = ok ? "planted (placement fallback)" : "plant failed" };
        }

        // Trigger the real vanilla end-of-day. Must be called while inside the FarmHouse
        // and free to move. Mirrors the bed interaction path so the day advances and the
        // save/shipping flow runs exactly as if the human had slept.
        private void TrySleep()
        {
            this.pendingSleep = false;
            this.Mode = PilotMode.Idle;
            this.ClearMovement();
            this.executeOnArrive = false;
            this.currentTask = null;
            Game1.player.Halt();
            Game1.player.isInBed.Value = true;
            Game1.currentLocation.answerDialogueAction("Sleep_Yes", null);
            this.lastResult = new { action = "player_sleep", success = true, detail = "Sleeping - ending the day" };
            this.monitor.Log("Player: player_sleep - ending the day via Sleep_Yes", LogLevel.Info);
        }

        private static Type ToolType(string name)
        {
            return (name ?? "").ToLower() switch
            {
                "pickaxe" => typeof(Pickaxe),
                "axe" => typeof(Axe),
                "hoe" => typeof(Hoe),
                "wateringcan" or "watering_can" => typeof(WateringCan),
                "scythe" => typeof(MeleeWeapon), // 1.6: scythe is a MeleeWeapon (isScythe)
                "sword" or "weapon" => typeof(MeleeWeapon),
                "fishingrod" or "fishing_rod" => typeof(FishingRod),
                _ => null
            };
        }

        // ======================
        // COMMAND HANDLING
        // ======================

        public void HandleCommand(string action, System.Text.Json.JsonElement root)
        {
            if (!Context.IsWorldReady || Game1.player == null)
            {
                this.lastResult = new { action, success = false, detail = "world not ready" };
                return;
            }

            try
            {
                switch (action)
                {
                    case "player_move_to":
                    {
                        int x = root.GetProperty("x").GetInt32();
                        int y = root.GetProperty("y").GetInt32();
                        this.ClearFishing();  // movement overrides fishing (e.g. stamina guard sending us home)
                        this.Mode = PilotMode.Manual;
                        this.executeOnArrive = false;
                        this.currentTask = null;
                        if (this.StartPath(x, y))
                            this.lastResult = new { action, success = true, detail = $"Walking to ({x},{y})" };
                        else
                            this.lastResult = new { action, success = false, detail = $"No path to ({x},{y})" };
                        break;
                    }

                    case "player_farm":
                        // Idempotent: re-issuing this must NOT cancel an in-progress path.
                        if (this.Mode != PilotMode.Farm)
                        {
                            this.Mode = PilotMode.Farm;
                            this.ClearMovement();
                            this.executeOnArrive = false;
                            this.currentTask = null;
                            this.actionCooldown = 0;
                            this.lastResult = new { action, success = true, detail = "Autonomous farming started" };
                        }
                        else
                        {
                            this.lastResult = new { action, success = true, detail = "Already farming" };
                        }
                        break;

                    case "player_stop":
                        // Temporary pause (distracted / short break): clear movement but
                        // preserve reclaimRect so re-focus continues working the same field.
                        this.Mode = PilotMode.Idle;
                        this.ClearMovement();
                        this.executeOnArrive = false;
                        this.currentTask = null;
                        this.travelTarget = null;
                        this.travelFinalTile = null;
                        this.pendingDeposit = DepositKind.None;
                        this.depositFilter = null;
                        this.buyOnArrive = false;
                        this.buyPhase = BuyPhase.None;
                        this.ClearFishing();
                        this.ClearExit();
                        Game1.player.Halt();
                        this.lastResult = new { action, success = true, detail = "Idle (farm preserved)" };
                        break;

                    case "player_idle":
                        // Full stop (focus end / rest): clear everything including reclaimRect.
                        this.Mode = PilotMode.Idle;
                        this.ClearMovement();
                        this.executeOnArrive = false;
                        this.currentTask = null;
                        this.reclaimRect = null;
                        this.travelTarget = null;
                        this.travelFinalTile = null;
                        this.pendingDeposit = DepositKind.None;
                        this.depositFilter = null;
                        this.buyOnArrive = false;
                        this.buyPhase = BuyPhase.None;
                        this.ClearFishing();
                        this.ClearExit();
                        Game1.player.Halt();
                        this.lastResult = new { action, success = true, detail = "Idle" };
                        break;

                    case "player_use_tool":
                    {
                        string toolName = root.GetProperty("tool").GetString();
                        int x = root.GetProperty("x").GetInt32();
                        int y = root.GetProperty("y").GetInt32();
                        Type tt = ToolType(toolName);
                        if (tt == null)
                        {
                            this.lastResult = new { action, success = false, detail = $"Unknown tool: {toolName}" };
                            break;
                        }
                        var tile = new Vector2(x, y);
                        this.Mode = PilotMode.Manual;
                        if (Vector2.Distance(Game1.player.Tile, tile) <= 1.6f)
                        {
                            this.ClearMovement();
                            this.ExecuteFarmAction(Game1.player.currentLocation,
                                new FarmTask { Type = FarmTaskType.ToolUse, Tile = tile, ToolTypeName = toolName });
                        }
                        else
                        {
                            var approach = this.FindApproachTile(Game1.player.currentLocation, tile);
                            if (approach == null || !this.StartPath((int)approach.Value.X, (int)approach.Value.Y))
                            {
                                this.lastResult = new { action, success = false, detail = "no reachable tile near target" };
                                break;
                            }
                            this.currentTask = new FarmTask { Type = FarmTaskType.ToolUse, Tile = tile, ToolTypeName = toolName };
                            this.executeOnArrive = true;
                            this.lastResult = new { action, success = true, detail = $"Walking to use {toolName} at ({x},{y})" };
                        }
                        break;
                    }

                    case "player_plant":
                    {
                        int x = root.GetProperty("x").GetInt32();
                        int y = root.GetProperty("y").GetInt32();
                        string seedName = root.TryGetProperty("seed", out var seedProp) ? seedProp.GetString() : null;
                        var tile = new Vector2(x, y);
                        this.Mode = PilotMode.Manual;
                        if (Vector2.Distance(Game1.player.Tile, tile) <= 1.6f)
                        {
                            this.ClearMovement();
                            this.ExecuteFarmAction(Game1.player.currentLocation,
                                new FarmTask { Type = FarmTaskType.Plant, Tile = tile, SeedName = seedName });
                        }
                        else
                        {
                            var approach = this.FindApproachTile(Game1.player.currentLocation, tile);
                            if (approach == null || !this.StartPath((int)approach.Value.X, (int)approach.Value.Y))
                            {
                                this.lastResult = new { action, success = false, detail = "no reachable tile near target" };
                                break;
                            }
                            this.currentTask = new FarmTask { Type = FarmTaskType.Plant, Tile = tile, SeedName = seedName };
                            this.executeOnArrive = true;
                            this.lastResult = new { action, success = true, detail = $"Walking to plant at ({x},{y})" };
                        }
                        break;
                    }

                    case "player_inspect":
                    {
                        // Look at a tile (default: the one the player faces). The agent reads
                        // the answer from agentPlayer.lastCommandResult in the next state sync.
                        int x, y;
                        if (root.TryGetProperty("x", out var ix) && root.TryGetProperty("y", out var iy))
                        {
                            x = ix.GetInt32(); y = iy.GetInt32();
                        }
                        else
                        {
                            var dirV = Game1.player.FacingDirection switch
                            {
                                0 => new Vector2(0, -1),
                                1 => new Vector2(1, 0),
                                2 => new Vector2(0, 1),
                                _ => new Vector2(-1, 0),
                            };
                            var ft = Game1.player.Tile + dirV;
                            x = (int)ft.X; y = (int)ft.Y;
                        }
                        var loc = Game1.player.currentLocation;
                        var tile = new Vector2(x, y);
                        object info;
                        if (loc.terrainFeatures.TryGetValue(tile, out var feat) && feat is HoeDirt dirt)
                        {
                            info = new
                            {
                                kind = "HoeDirt",
                                watered = dirt.state.Value == 1,
                                hasCrop = dirt.crop != null,
                                readyForHarvest = dirt.crop != null && dirt.readyForHarvest(),
                                cropPhase = dirt.crop != null ? (int)dirt.crop.currentPhase.Value : -1,
                                fullyGrown = dirt.crop != null && dirt.crop.fullyGrown.Value,
                            };
                        }
                        else if (loc.objects.TryGetValue(tile, out var obj))
                        {
                            info = new { kind = "Object", name = obj.Name, displayName = obj.DisplayName };
                        }
                        else
                        {
                            info = new
                            {
                                kind = "Ground",
                                walkable = this.pathfinder.IsTileWalkable(loc, x, y),
                                diggable = loc.doesTileHaveProperty(x, y, "Diggable", "Back") != null,
                            };
                        }
                        this.lastResult = new { action, success = true, tile = new { x, y }, info };
                        break;
                    }

                    case "player_warp":
                    {
                        string loc = root.GetProperty("location").GetString();
                        int x = root.TryGetProperty("x", out var xProp) ? xProp.GetInt32() : -1;
                        int y = root.TryGetProperty("y", out var yProp) ? yProp.GetInt32() : -1;

                        // Re-warping to the location you are ALREADY in still triggers a
                        // fade-to-black transition; skip it to stop repeated screen flashing.
                        if (string.Equals(Game1.currentLocation?.Name, loc, StringComparison.OrdinalIgnoreCase))
                        {
                            this.lastResult = new { action, success = true, detail = $"Already in {loc}; warp skipped" };
                            break;
                        }

                        // Validate target location exists.
                        GameLocation targetLoc = null;
                        foreach (var gl in Game1.locations)
                        {
                            if (string.Equals(gl?.Name, loc, StringComparison.OrdinalIgnoreCase))
                            { targetLoc = gl; break; }
                        }
                        if (targetLoc == null)
                        {
                            this.lastResult = new { action, success = false, detail = $"Unknown location '{loc}'" };
                            break;
                        }

                        // Default to a safe spawn point if coords not provided or out of range.
                        var map = targetLoc.Map;
                        int mapW = map?.Layers[0]?.LayerWidth ?? 999;
                        int mapH = map?.Layers[0]?.LayerHeight ?? 999;
                        if (x < 0 || y < 0 || x >= mapW || y >= mapH)
                        {
                            // Use the first incoming warp point as safe landing.
                            bool found = false;
                            foreach (var w in targetLoc.warps)
                            {
                                if (w.X >= 0 && w.X < mapW && w.Y >= 0 && w.Y < mapH)
                                { x = w.X; y = w.Y; found = true; break; }
                            }
                            if (!found) { x = mapW / 2; y = mapH / 2; }
                        }

                        this.Mode = PilotMode.Idle;
                        this.ClearMovement();
                        this.executeOnArrive = false;
                        this.currentTask = null;
                        this.reclaimRect = null;
                        this.travelTarget = null;
                        this.travelFinalTile = null;
                        this.ClearExit();
                        Game1.warpFarmer(loc, x, y, false);
                        this.lastResult = new { action, success = true, detail = $"Warped to {loc} ({x},{y})" };
                        break;
                    }

                    case "player_face":
                    {
                        int dir = root.GetProperty("direction").GetInt32();
                        if (dir >= 0 && dir <= 3)
                        {
                            Game1.player.faceDirection(dir);
                            this.lastResult = new { action, success = true, detail = $"Facing {dir}" };
                        }
                        else
                        {
                            this.lastResult = new { action, success = false, detail = "direction must be 0-3" };
                        }
                        break;
                    }

                    case "player_interact":
                    {
                        int x = root.GetProperty("x").GetInt32();
                        int y = root.GetProperty("y").GetInt32();
                        var tile = new Vector2(x, y);
                        this.Mode = PilotMode.Manual;
                        if (Vector2.Distance(Game1.player.Tile, tile) <= 1.6f)
                        {
                            this.ClearMovement();
                            this.ExecuteFarmAction(Game1.player.currentLocation,
                                new FarmTask { Type = FarmTaskType.Interact, Tile = tile });
                        }
                        else
                        {
                            var approach = this.FindApproachTile(Game1.player.currentLocation, tile);
                            if (approach == null || !this.StartPath((int)approach.Value.X, (int)approach.Value.Y))
                            {
                                this.lastResult = new { action, success = false, detail = "no reachable tile near target" };
                                break;
                            }
                            this.currentTask = new FarmTask { Type = FarmTaskType.Interact, Tile = tile };
                            this.executeOnArrive = true;
                            this.lastResult = new { action, success = true, detail = $"Walking to interact at ({x},{y})" };
                        }
                        break;
                    }

                    case "player_attack":
                    {
                        this.Mode = PilotMode.Manual;
                        if (!this.SelectSlot(i => i is MeleeWeapon))
                        {
                            this.lastResult = new { action, success = false, detail = "no weapon in hotbar" };
                            break;
                        }
                        this.PressUseTool();
                        this.lastResult = new { action, success = true, detail = "weapon swing" };
                        break;
                    }

                    case "player_sleep":
                        // End the day for real. Vanilla bed interaction runs
                        // answerDialogueAction("Sleep_Yes") -> startSleep() -> Game1.NewDay().
                        // If we're not home, warp to the farmhouse and finish sleeping on a
                        // later tick once the warp fade has settled (see pendingSleep in Tick).
                        this.ClearFishing();  // reel in first, or the warp/sleep stalls behind UsingTool
                        if (Game1.currentLocation is FarmHouse)
                        {
                            this.TrySleep();
                        }
                        else if (!this.pendingSleep)
                        {
                            this.Mode = PilotMode.Idle;
                            this.ClearMovement();
                            this.executeOnArrive = false;
                            this.currentTask = null;
                            Game1.warpFarmer("FarmHouse", 3, 11, false);
                            this.pendingSleep = true;
                            this.lastResult = new { action, success = true, detail = "Warping home to sleep" };
                        }
                        else
                        {
                            this.lastResult = new { action, success = true, detail = "Already heading to bed" };
                        }
                        break;

                    case "player_select_item":
                    {
                        // Select a hotbar slot (0-11), like a human pressing a number key.
                        int slot = root.GetProperty("slot").GetInt32();
                        if (slot < 0 || slot > 11)
                        {
                            this.lastResult = new { action, success = false, detail = "slot must be 0-11 (hotbar)" };
                            break;
                        }
                        if (slot >= Game1.player.Items.Count || Game1.player.Items[slot] == null)
                        {
                            this.lastResult = new { action, success = false, detail = $"no item in slot {slot}" };
                            break;
                        }
                        Game1.player.CurrentToolIndex = slot;
                        this.lastResult = new { action, success = true, detail = $"selected {Game1.player.Items[slot].DisplayName} (slot {slot})" };
                        break;
                    }

                    case "player_eat":
                    {
                        // Eat to restore stamina/health via the native right-click path
                        // (holding food + action button = eat). Ported from stardew-mcp.
                        int slot = root.TryGetProperty("slot", out var sp)
                            ? sp.GetInt32()
                            : this.FindSlot(i => i is StardewValley.Object so && so.Edibility > -300);

                        if (slot < 0 || slot > 11 || slot >= Game1.player.Items.Count || Game1.player.Items[slot] == null)
                        {
                            this.lastResult = new { action, success = false, detail = "no edible item in hotbar" };
                            break;
                        }
                        if (Game1.player.Items[slot] is not StardewValley.Object food || food.Edibility <= -300)
                        {
                            this.lastResult = new { action, success = false, detail = $"{Game1.player.Items[slot].Name} is not edible" };
                            break;
                        }
                        Game1.player.CurrentToolIndex = slot;
                        this.PressActionButton();
                        this.lastResult = new { action, success = true, detail = $"eating {food.DisplayName}" };
                        break;
                    }

                    case "player_enter_door":
                    {
                        // Walk through the door/warp the player is facing (native right-click).
                        var dirV = Game1.player.FacingDirection switch
                        {
                            0 => new Vector2(0, -1),
                            1 => new Vector2(1, 0),
                            2 => new Vector2(0, 1),
                            _ => new Vector2(-1, 0),
                        };
                        var ft = Game1.player.Tile + dirV;
                        var curLoc = Game1.currentLocation;
                        string targetName = null;
                        foreach (var w in curLoc.warps)
                        {
                            if (w.X == (int)ft.X && w.Y == (int)ft.Y) { targetName = w.TargetName; break; }
                        }
                        if (targetName == null && curLoc.doors.ContainsKey(new Point((int)ft.X, (int)ft.Y)))
                            targetName = "interior";
                        this.PressActionButton();
                        this.lastResult = new { action, success = true, detail = targetName != null ? $"entering door to {targetName}" : "no door/warp on the faced tile; action pressed anyway" };
                        break;
                    }

                    case "player_use_tool_repeat":
                    {
                        // Swing the currently-equipped tool N times in place (mines/field
                        // clearing). Ported from stardew-mcp's use_tool_repeat.
                        int count = root.TryGetProperty("count", out var cp) ? cp.GetInt32() : 1;
                        count = Math.Clamp(count, 1, 100);
                        if (Game1.player.CurrentTool == null)
                        {
                            this.lastResult = new { action, success = false, detail = "no tool equipped; use player_select_item first" };
                            break;
                        }
                        this.Mode = PilotMode.Manual;
                        this.ClearMovement();
                        this.toolRepeatRemaining = count;
                        this.toolRepeatCooldown = 0; // first swing fires immediately
                        this.lastResult = new { action, success = true, detail = $"swinging {Game1.player.CurrentTool.Name} x{count}" };
                        break;
                    }

                    case "player_reclaim":
                    {
                        // Develop/replace the managed plot: cut grass -> hoe -> plant -> water,
                        // then maintained forever (auto harvest/replant/rewater). Size defaults
                        // to seed count; optional width/height (1..8), anchorX/anchorY, seed.
                        string seed = root.TryGetProperty("seed", out var rsp) ? rsp.GetString() : null;
                        int? rw = root.TryGetProperty("width", out var rwp) ? rwp.GetInt32() : (int?)null;
                        int? rh = root.TryGetProperty("height", out var rhp) ? rhp.GetInt32() : (int?)null;
                        int? rax = root.TryGetProperty("anchorX", out var raxp) ? raxp.GetInt32() : (int?)null;
                        int? ray = root.TryGetProperty("anchorY", out var rayp) ? rayp.GetInt32() : (int?)null;
                        if (!this.BuildReclaimRect(seed, rw, rh, rax, ray))
                        {
                            this.lastResult = new { action, success = false, detail = "no diggable rectangle area nearby" };
                            break;
                        }
                        this.Mode = PilotMode.Farm;
                        this.ClearMovement();
                        this.ClearExit();
                        this.executeOnArrive = false;
                        this.currentTask = null;
                        this.travelTarget = null;
                        this.travelFinalTile = null;
                        this.actionCooldown = 0;
                        this.taskAttempts.Clear();
                        this.attemptsLocation = Game1.player.currentLocation?.Name;
                        this.lastResult = new { action, success = true, detail = $"reclaiming a {this.reclaimRect.Count}-tile rectangle" };
                        break;
                    }

                    case "player_go_outside":
                    {
                        // Walk to a warp and STEP through it (no teleport). Default: whatever
                        // warp leads outdoors; optionally target a named destination.
                        string target = root.TryGetProperty("target", out var gtp) ? gtp.GetString() : null;
                        var curLoc = Game1.currentLocation;
                        Warp chosen = null;
                        if (target != null)
                        {
                            foreach (var w in curLoc.warps)
                                if (string.Equals(w.TargetName, target, StringComparison.OrdinalIgnoreCase)) { chosen = w; break; }
                        }
                        else if (curLoc.warps.Count > 0)
                        {
                            chosen = curLoc.warps[0];
                        }
                        if (chosen == null)
                        {
                            this.lastResult = new { action, success = false, detail = target != null ? $"no warp to {target}" : "no warps here" };
                            break;
                        }
                        var warpTile = new Vector2(chosen.X, chosen.Y);
                        var approach = this.FindApproachTile(curLoc, warpTile);
                        if (approach == null || !this.StartPath((int)approach.Value.X, (int)approach.Value.Y))
                        {
                            this.lastResult = new { action, success = false, detail = "no path to the door" };
                            break;
                        }
                        this.Mode = PilotMode.Manual;
                        this.executeOnArrive = false;
                        this.currentTask = null;
                        this.pendingExitTile = warpTile;
                        this.pendingExitFrom = curLoc.Name;
                        this.pendingExitTicks = 0;
                        this.lastResult = new { action, success = true, detail = $"walking to the door → {chosen.TargetName}" };
                        break;
                    }

                    case "player_go_to":
                    {
                        // Walk map-to-map to a target location via the warp graph (no teleport).
                        // Optional x,y = a tile to walk to once inside the destination.
                        this.ClearFishing();  // movement overrides fishing (e.g. stamina guard sending us home)
                        string target = root.TryGetProperty("target", out var gtt) ? gtt.GetString() : null;
                        if (string.IsNullOrEmpty(target))
                        {
                            this.lastResult = new { action, success = false, detail = "missing target location" };
                            break;
                        }
                        Vector2? finalTile = null;
                        if (root.TryGetProperty("x", out var gtx) && root.TryGetProperty("y", out var gty))
                            finalTile = new Vector2(gtx.GetInt32(), gty.GetInt32());

                        this.ClearMovement();
                        this.ClearExit();
                        this.executeOnArrive = false;
                        this.currentTask = null;
                        this.travelHopFails = 0;

                        // Already at the target: optional final move, else done immediately.
                        if (string.Equals(Game1.currentLocation?.Name, target, StringComparison.OrdinalIgnoreCase))
                        {
                            if (finalTile.HasValue && this.StartPath((int)finalTile.Value.X, (int)finalTile.Value.Y))
                            {
                                this.Mode = PilotMode.Manual;
                                this.lastResult = new { action, success = true, detail = $"already in {target}; walking to tile" };
                            }
                            else
                            {
                                this.Mode = PilotMode.Idle;
                                this.lastResult = new { action, success = true, detail = $"already in {target}" };
                            }
                            break;
                        }
                        this.Mode = PilotMode.Travel;
                        this.travelTarget = target;
                        this.travelFinalTile = finalTile;
                        this.actionCooldown = 0;
                        this.lastResult = new { action, success = true, detail = $"walking to {target}" };
                        break;
                    }

                    case "player_ship":
                    {
                        // Walk to the farm shipping bin, then dump all sellable products in
                        // (vanilla settles the gold at end of day — no menu needed).
                        if (!(Game1.player.currentLocation is Farm farm))
                        {
                            this.lastResult = new { action, success = false, detail = "not on the Farm; player_go_to Farm first" };
                            break;
                        }
                        Building bin = null;
                        foreach (var b in farm.buildings)
                            if (b is ShippingBin) { bin = b; break; }
                        Vector2? approach = null;
                        if (bin != null)
                        {
                            int bx = bin.tileX.Value, by = bin.tileY.Value;
                            for (int yy = by - 1; yy <= by + 2 && approach == null; yy++)
                                for (int xx = bx - 1; xx <= bx + 2 && approach == null; xx++)
                                    if (bin.occupiesTile(new Vector2(xx, yy)))
                                        approach = this.FindApproachTile(farm, new Vector2(xx, yy));
                        }
                        if (approach == null)
                        {
                            this.lastResult = new { action, success = false, detail = "no reachable shipping bin on the farm" };
                            break;
                        }
                        this.Mode = PilotMode.Manual;
                        this.ClearMovement();
                        this.executeOnArrive = false;
                        this.currentTask = null;
                        this.pendingDeposit = DepositKind.Ship;
                        this.depositTile = approach;
                        if (!this.StartPath((int)approach.Value.X, (int)approach.Value.Y))
                        {
                            this.pendingDeposit = DepositKind.None;
                            this.lastResult = new { action, success = false, detail = "no path to the shipping bin" };
                            break;
                        }
                        this.lastResult = new { action, success = true, detail = "walking to the shipping bin" };
                        break;
                    }

                    case "player_store":
                    {
                        // Walk to the nearest chest in the current location and store non-seed
                        // items (optional name filter) to free up inventory space.
                        string filter = root.TryGetProperty("item", out var sip) ? sip.GetString() : null;
                        var loc = Game1.player.currentLocation;
                        Vector2 chestTile = default;
                        bool haveChest = false;
                        float bd = float.MaxValue;
                        foreach (var pair in loc.objects.Pairs)
                        {
                            if (!(pair.Value is Chest)) continue;
                            float d = Vector2.Distance(Game1.player.Tile, pair.Key);
                            if (d < bd) { bd = d; chestTile = pair.Key; haveChest = true; }
                        }
                        if (!haveChest)
                        {
                            this.lastResult = new { action, success = false, detail = "no chest nearby; try player_ship instead" };
                            break;
                        }
                        var approach = this.FindApproachTile(loc, chestTile);
                        if (approach == null)
                        {
                            this.lastResult = new { action, success = false, detail = "no reachable tile next to the chest" };
                            break;
                        }
                        this.Mode = PilotMode.Manual;
                        this.ClearMovement();
                        this.executeOnArrive = false;
                        this.currentTask = null;
                        this.pendingDeposit = DepositKind.Store;
                        this.depositFilter = filter;
                        this.depositTile = approach;
                        if (!this.StartPath((int)approach.Value.X, (int)approach.Value.Y))
                        {
                            this.pendingDeposit = DepositKind.None;
                            this.depositFilter = null;
                            this.lastResult = new { action, success = false, detail = "no path to the chest" };
                            break;
                        }
                        this.lastResult = new { action, success = true, detail = "walking to the chest" };
                        break;
                    }

                    case "player_take":
                    {
                        // Walk to the nearest chest and withdraw items into the inventory (seeds
                        // by default, or an optional name filter) so the AI can replant. Mirror
                        // of player_store; deferred to arrival like the other deposits.
                        string filter = root.TryGetProperty("item", out var tip) ? tip.GetString() : null;
                        bool seedsOnly = root.TryGetProperty("seedsOnly", out var tso) ? tso.GetBoolean() : string.IsNullOrEmpty(filter);
                        var loc = Game1.player.currentLocation;
                        Vector2 chestTile = default;
                        bool haveChest = false;
                        float bd = float.MaxValue;
                        foreach (var pair in loc.objects.Pairs)
                        {
                            if (!(pair.Value is Chest)) continue;
                            float d = Vector2.Distance(Game1.player.Tile, pair.Key);
                            if (d < bd) { bd = d; chestTile = pair.Key; haveChest = true; }
                        }
                        if (!haveChest)
                        {
                            this.lastResult = new { action, success = false, detail = "no chest nearby to take from" };
                            break;
                        }
                        var approach = this.FindApproachTile(loc, chestTile);
                        if (approach == null)
                        {
                            this.lastResult = new { action, success = false, detail = "no reachable tile next to the chest" };
                            break;
                        }
                        this.Mode = PilotMode.Manual;
                        this.ClearMovement();
                        this.executeOnArrive = false;
                        this.currentTask = null;
                        this.pendingDeposit = DepositKind.Take;
                        this.depositFilter = filter;
                        this.takeSeedsOnly = seedsOnly;
                        this.depositTile = approach;
                        if (!this.StartPath((int)approach.Value.X, (int)approach.Value.Y))
                        {
                            this.pendingDeposit = DepositKind.None;
                            this.depositFilter = null;
                            this.takeSeedsOnly = false;
                            this.lastResult = new { action, success = false, detail = "no path to the chest" };
                            break;
                        }
                        this.lastResult = new { action, success = true, detail = "walking to the chest to take items" };
                        break;
                    }

                    case "player_buy":
                    {
                        // Walk up to Pierre's counter in the SeedShop, open the real shop menu,
                        // and buy the cheapest in-season seed within a budget. Deferred to arrival.
                        if (!string.Equals(Game1.currentLocation?.Name, "SeedShop", StringComparison.OrdinalIgnoreCase))
                        {
                            this.lastResult = new { action, success = false, detail = "not in the seed shop; player_go_to SeedShop first" };
                            break;
                        }
                        int tod = Game1.timeOfDay;
                        if (tod < 900 || tod >= 1700)
                        {
                            this.lastResult = new { action, success = false, detail = "shop closed (opens 9:00-17:00)" };
                            break;
                        }
                        this.buyBudget = root.TryGetProperty("budget", out var bbud) ? bbud.GetInt32() : 0;
                        this.buyQtyCap = root.TryGetProperty("qty", out var bqty) ? bqty.GetInt32() : 0;

                        var shop = Game1.currentLocation;
                        var pierre = shop.characters?.FirstOrDefault(c => c.Name == "Pierre");
                        Vector2? approach = pierre != null ? this.FindApproachTile(shop, pierre.Tile) : null;

                        this.Mode = PilotMode.Manual;
                        this.ClearMovement();
                        this.executeOnArrive = false;
                        this.currentTask = null;
                        this.buyOnArrive = true;
                        if (approach.HasValue)
                        {
                            if (!this.StartPath((int)approach.Value.X, (int)approach.Value.Y))
                            {
                                this.buyOnArrive = false;
                                this.lastResult = new { action, success = false, detail = "no path to the shop counter" };
                                break;
                            }
                        }
                        else
                        {
                            // Pierre not found / already at the counter: open the shop from here.
                            this.ArriveAtTarget();
                            this.lastResult = new { action, success = true, detail = "opening the shop menu" };
                            break;
                        }
                        this.lastResult = new { action, success = true, detail = "walking to the shop counter" };
                        break;
                    }

                    case "player_energy":
                    {
                        // Life-sync fatigue coupling: adjust stamina (delta or absolute set).
                        // Gentle exhaustion when it bottoms out (no forced pass-out here).
                        float maxSt = Game1.player.MaxStamina;
                        float target;
                        if (root.TryGetProperty("set", out var enSet))
                            target = enSet.GetInt32();
                        else
                        {
                            int delta = root.TryGetProperty("delta", out var enDelta) ? enDelta.GetInt32() : 0;
                            target = Game1.player.Stamina + delta;
                        }
                        if (target > maxSt) target = maxSt;
                        if (target < -16f) target = -16f;
                        Game1.player.Stamina = target;
                        if (target <= 0f) Game1.player.exhausted.Value = true;
                        this.lastResult = new { action, success = true, stamina = (int)Game1.player.Stamina, exhausted = Game1.player.exhausted.Value };
                        break;
                    }

                    case "player_faint":
                    {
                        // Gentle collapse from overwork: drop to 0 stamina + exhausted + a visible
                        // sleepy emote. Does NOT end the day (harsh vanilla pass-out is out of scope).
                        Game1.player.Stamina = 0f;
                        Game1.player.exhausted.Value = true;
                        Game1.player.Halt();
                        try { Game1.player.doEmote(24); } catch { }
                        try { Game1.playSound("ow"); } catch { }
                        this.lastResult = new { action, success = true, detail = "collapsed from overwork" };
                        break;
                    }

                    case "player_reward":
                    {
                        // Reward for completing a focus session: add gold + optional happy emote.
                        int money = root.TryGetProperty("money", out var rMoney) ? rMoney.GetInt32() : 0;
                        int emote = root.TryGetProperty("emote", out var rEmote) ? rEmote.GetInt32() : 32; // 32 = happy
                        if (money > 0)
                        {
                            Game1.player.Money += money;
                        }
                        if (emote >= 0)
                        {
                            Game1.player.doEmote(emote);
                        }
                        try { Game1.playSound("questcomplete"); } catch { } // reward jingle
                        this.lastResult = new { action, success = true, detail = $"reward: +{money}g, emote={emote}" };
                        break;
                    }

                    case "player_penalty":
                    {
                        // Consequence for breaking focus: deduct gold + wither up to N live crops.
                        int money = root.TryGetProperty("money", out var pMoney) ? pMoney.GetInt32() : 0;
                        int witherCap = root.TryGetProperty("wither", out var pWither) ? pWither.GetInt32() : 0;
                        int goldLost = 0;
                        if (money > 0)
                        {
                            goldLost = Math.Min(money, Game1.player.Money);
                            Game1.player.Money = Math.Max(0, Game1.player.Money - money);
                        }
                        int withered = 0;
                        if (witherCap > 0)
                        {
                            var farm = Game1.getFarm();
                            if (farm != null)
                            {
                                foreach (var pair in farm.terrainFeatures.Pairs)
                                {
                                    if (withered >= witherCap) break;
                                    if (pair.Value is HoeDirt hd && hd.crop != null && !hd.crop.dead.Value)
                                    {
                                        try { hd.crop.Kill(); } catch { hd.crop.dead.Value = true; }
                                        withered++;
                                    }
                                }
                            }
                        }
                        this.lastResult = new { action, success = true, goldLost, withered, detail = $"penalty: -{goldLost}g, {withered} crops withered" };
                        break;
                    }

                    case "player_fish":
                    {
                        // Autonomous fishing: find water → walk to shore → face water → cast → auto-catch.
                        var loc = Game1.player.currentLocation;

                        // 1) Collect ALL water tiles in radius sorted by distance, then try
                        //    pathing to each until one is actually reachable. (The single
                        //    nearest water tile is often across a dock/cliff with no path;
                        //    with a large radius we must fall back to the next candidates.)
                        int scanR = root.TryGetProperty("radius", out var fRad) ? fRad.GetInt32() : 40;
                        int playerX = (int)Game1.player.Tile.X, playerY = (int)Game1.player.Tile.Y;
                        var candidates = new List<(int dist, Vector2 tile)>();
                        for (int dy = -scanR; dy <= scanR; dy++)
                            for (int dx = -scanR; dx <= scanR; dx++)
                            {
                                int tx = playerX + dx, ty = playerY + dy;
                                if (tx < 0 || ty < 0 || !loc.isWaterTile(tx, ty)) continue;
                                candidates.Add((Math.Abs(dx) + Math.Abs(dy), new Vector2(tx, ty)));
                            }
                        if (candidates.Count == 0)
                        {
                            this.lastResult = new { action, success = false, detail = "no water tile nearby" };
                            break;
                        }
                        candidates.Sort((a, b) => a.dist.CompareTo(b.dist));

                        // 2) Prime fishing state, then try candidates nearest-first until a path succeeds.
                        this.Mode = PilotMode.Manual;
                        this.ClearMovement();
                        this.executeOnArrive = false;
                        this.currentTask = null;
                        Vector2? waterTile = null;
                        int tried = 0;
                        foreach (var cand in candidates)
                        {
                            if (++tried > 25) break;  // cost cap: pathfinding is the expensive part
                            var ap = this.FindApproachTile(loc, cand.tile);
                            if (ap == null) continue;
                            if (!this.StartPath((int)ap.Value.X, (int)ap.Value.Y)) continue;
                            waterTile = cand.tile;
                            this.fishStandTile = ap;
                            break;
                        }
                        if (waterTile == null)
                        {
                            this.lastResult = new { action, success = false, detail = "no path to any water tile" };
                            break;
                        }
                        this.fishTarget = waterTile;
                        this.fishPhase = FishPhase.Walking;
                        this.fishTicks = 0;
                        this.fishCastCount = 0;
                        this.fishHadMinigame = false;
                        this.lastResult = new { action, success = true, detail = $"walking to water at ({waterTile.Value.X},{waterTile.Value.Y})" };
                        break;
                    }

                    default:
                        this.lastResult = new { action, success = false, detail = "unknown player command" };
                        break;
                }

                this.monitor.Log($"Player: {action} — {System.Text.Json.JsonSerializer.Serialize(this.lastResult)}", LogLevel.Info);
            }
            catch (Exception ex)
            {
                this.lastResult = new { action, success = false, detail = $"error: {ex.Message}" };
                this.monitor.Log($"Player command {action} failed: {ex.Message}", LogLevel.Error);
            }
        }

        // ======================
        // BRIDGE STATUS
        // ======================

        public object GetStatus()
        {
            if (!Context.IsWorldReady || Game1.player == null) return null;

            var loc = Game1.player.currentLocation;
            object surroundings = null;
            try
            {
                var scan = SurroundingsScanner.Scan(loc, Game1.player.Tile, 8);
                surroundings = new
                {
                    tiles = scan.Tiles.Select(t => new
                    {
                        x = t.X, y = t.Y,
                        passable = t.Passable,
                        water = t.IsWater,
                        terrain = t.Terrain,
                        crop = t.CropName,
                        cropReady = t.CropReady,
                        waterState = t.WaterState,
                        obj = t.ObjectName,
                        objType = t.ObjectType,
                        breakable = t.Breakable,
                        interactable = t.Interactable
                    }),
                    monsters = scan.Monsters.Select(m => new { name = m.Name, x = m.X, y = m.Y, health = m.Health, maxHealth = m.MaxHealth }),
                    npcs = scan.Npcs.Select(n => new { name = n.Name, x = n.X, y = n.Y })
                };
            }
            catch { }

            // Nearest chest in this location (whole-location, no radius cap) + a compact view of
            // its contents, so the brain can decide "I'm out of seeds, go take some from the box".
            object nearbyChest = null;
            try
            {
                Chest nc = null; float ncd = float.MaxValue; Vector2 ncTile = default;
                foreach (var pair in loc.objects.Pairs)
                {
                    if (!(pair.Value is Chest ch)) continue;
                    float d = Vector2.Distance(Game1.player.Tile, pair.Key);
                    if (d < ncd) { ncd = d; nc = ch; ncTile = pair.Key; }
                }
                if (nc != null)
                {
                    nearbyChest = new
                    {
                        x = (int)ncTile.X, y = (int)ncTile.Y,
                        items = nc.Items.Where(it => it != null).Take(24).Select(it => new
                        {
                            name = it.DisplayName, stack = it.Stack, isSeed = it.Category == -74
                        }).ToList()
                    };
                }
            }
            catch { }

            return new
            {
                mode = this.Mode.ToString().ToLower(),
                tile = new { x = (int)Game1.player.Tile.X, y = (int)Game1.player.Tile.Y },
                facing = Game1.player.FacingDirection,
                moving = this.path != null,
                target = this.finalTarget.HasValue ? (object)new { x = (int)this.finalTarget.Value.X, y = (int)this.finalTarget.Value.Y } : null,
                pathRemaining = this.path != null ? this.path.Count - this.pathIndex : 0,
                canMove = Context.CanPlayerMove,
                stamina = Game1.player.Stamina,
                maxStamina = Game1.player.MaxStamina,
                currentTool = Game1.player.CurrentTool?.Name,
                currentItem = Game1.player.CurrentItem?.DisplayName,
                wateringCanWater = Game1.player.Items.OfType<WateringCan>().Select(c => (int?)c.WaterLeft).FirstOrDefault() ?? -1,
                toolRepeatRemaining = this.toolRepeatRemaining,
                farmQueueSize = this.farmQueue?.Count ?? 0,
                reclaimTiles = this.reclaimRect?.Count ?? 0,
                plotIdle = this.plotIdle,
                exiting = this.pendingExitTile.HasValue,
                traveling = this.Mode == PilotMode.Travel,
                travelTarget = this.travelTarget,
                lastCommandResult = this.lastResult,
                inventory = Game1.player.Items.Select(i => i == null ? null : new
                {
                    name = i.DisplayName,
                    stack = i.Stack,
                    category = i.Category,
                    isSeed = i.Category == -74,
                    edible = i is StardewValley.Object sobj && sobj.Edibility > -300,
                }).ToList(),
                nearbyChest,
                surroundings
            };
        }
    }
}
