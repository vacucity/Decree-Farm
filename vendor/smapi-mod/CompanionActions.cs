using System;
using System.Collections.Generic;
using System.Linq;
using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;
using StardewValley.TerrainFeatures;
using StardewValley.Objects;
using StardewValley.Menus;
using SObject = StardewValley.Object;

namespace StardewMCPBridge
{
    /// <summary>
    /// Direct game-object manipulation for farm actions.
    /// No Farmer instance needed — we interact with tiles directly.
    /// </summary>
    public static class CompanionActions
    {
        /// <summary>Water a crop at the given tile.</summary>
        public static bool WaterTile(GameLocation location, Vector2 tile, IMonitor monitor)
        {
            if (location.terrainFeatures.TryGetValue(tile, out var feature) && feature is HoeDirt dirt)
            {
                if (dirt.state.Value != 1) // not already watered
                {
                    dirt.state.Value = 1;
                    location.temporarySprites.Add(new TemporaryAnimatedSprite(
                        "TileSheets\\animations", new Rectangle(0, 0, 64, 64),
                        50f, 9, 1, tile * 64f, false, false, 0.01f, 0.01f,
                        Color.White, 1f, 0f, 0f, 0f
                    ));
                    monitor.Log($"Watered tile at ({tile.X}, {tile.Y})", LogLevel.Trace);
                    return true;
                }
            }
            return false;
        }

        /// <summary>Harvest a ready crop at the given tile.</summary>
        public static bool HarvestTile(GameLocation location, Vector2 tile, IMonitor monitor)
        {
            if (location.terrainFeatures.TryGetValue(tile, out var feature) && feature is HoeDirt dirt)
            {
                if (dirt.crop != null && dirt.readyForHarvest())
                {
                    bool success = dirt.crop.harvest((int)tile.X, (int)tile.Y, dirt, null);
                    if (success)
                    {
                        monitor.Log($"Harvested crop at ({tile.X}, {tile.Y})", LogLevel.Trace);
                        return true;
                    }
                }
            }
            return false;
        }

        /// <summary>Clear a debris object (stone, weed, twig) at the given tile.</summary>
        public static bool ClearDebris(GameLocation location, Vector2 tile, IMonitor monitor)
        {
            if (location.objects.TryGetValue(tile, out var obj))
            {
                string name = obj.Name ?? "";
                // Stone, Weeds, Twigs
                if (name.Contains("Stone") || name.Contains("Weed") || name.Contains("Twig")
                    || obj.ParentSheetIndex == 294 || obj.ParentSheetIndex == 295
                    || obj.ParentSheetIndex == 343 || obj.ParentSheetIndex == 450)
                {
                    obj.performRemoveAction();
                    location.objects.Remove(tile);
                    monitor.Log($"Cleared debris at ({tile.X}, {tile.Y}): {name}", LogLevel.Trace);
                    return true;
                }
            }
            return false;
        }

        /// <summary>Find a seed item in the player's inventory (category -74 = Seeds). Optional name filter.</summary>
        public static SObject FindSeed(string seedName = null)
        {
            var seeds = Game1.player.Items.OfType<SObject>().Where(o => o.Category == -74);
            if (string.IsNullOrEmpty(seedName))
                return seeds.FirstOrDefault();
            return seeds.FirstOrDefault(o => o.Name != null
                && o.Name.IndexOf(seedName, StringComparison.OrdinalIgnoreCase) >= 0);
        }

        /// <summary>Plant a seed from the player's inventory onto tilled, unplanted dirt.
        /// Uses the vanilla placement path (Object.placementAction), so the game itself
        /// validates season/location and we consume the item only on success.</summary>
        public static bool PlantTile(GameLocation location, Vector2 tile, IMonitor monitor, string seedName = null)
        {
            if (!(location.terrainFeatures.TryGetValue(tile, out var feature) && feature is HoeDirt dirt))
                return false;
            if (dirt.crop != null)
                return false; // already planted

            SObject seed = FindSeed(seedName);
            if (seed == null)
            {
                monitor.Log("PlantTile: no matching seeds in inventory", LogLevel.Trace);
                return false;
            }

            Game1.player.ActiveItem = seed;
            bool ok = seed.placementAction(location, (int)tile.X * 64, (int)tile.Y * 64, Game1.player);
            if (ok)
            {
                Game1.player.reduceActiveItemByOne();
                monitor.Log($"Planted {seed.Name} at ({tile.X}, {tile.Y})", LogLevel.Trace);
            }
            return ok;
        }

        /// <summary>Hoe the ground at the given tile to create farmable dirt.</summary>
        public static bool HoeTile(GameLocation location, Vector2 tile, IMonitor monitor)
        {
            if (!location.terrainFeatures.ContainsKey(tile)
                && !location.objects.ContainsKey(tile)
                && location.doesTileHaveProperty((int)tile.X, (int)tile.Y, "Diggable", "Back") != null)
            {
                location.terrainFeatures.Add(tile, new HoeDirt(0, location));
                monitor.Log($"Hoed tile at ({tile.X}, {tile.Y})", LogLevel.Trace);
                return true;
            }
            return false;
        }

        /// <summary>Cut/remove a grass tuft at the given tile (direct fallback for the
        /// on-tile case; the normal path is an input-simulated scythe swing).</summary>
        public static bool CutGrassTile(GameLocation location, Vector2 tile, IMonitor monitor)
        {
            if (location.terrainFeatures.TryGetValue(tile, out var feature) && feature is Grass)
            {
                location.terrainFeatures.Remove(tile);
                monitor.Log($"Cut grass at ({tile.X}, {tile.Y})", LogLevel.Trace);
                return true;
            }
            return false;
        }

        /// <summary>Scan a location for tiles that need work and return a prioritized task list.</summary>
        public static List<FarmTask> ScanForTasks(GameLocation location, IMonitor monitor, Vector2? nearTile = null, int hoeRadius = 12)
        {
            var tasks = new List<FarmTask>();
            bool hasSeeds = FindSeed() != null;

            foreach (var pair in location.terrainFeatures.Pairs)
            {
                if (pair.Value is HoeDirt dirt)
                {
                    // Harvest-ready crops (highest priority)
                    if (dirt.crop != null && dirt.readyForHarvest())
                    {
                        tasks.Add(new FarmTask
                        {
                            Type = FarmTaskType.Harvest,
                            Tile = pair.Key,
                            Priority = 10
                        });
                    }
                    // Unwatered crops — watering is the FIRST farm chore (above harvest):
                    // a dry crop loses a growth day, a ripe one just waits.
                    else if (dirt.crop != null && dirt.state.Value != 1 && !Game1.isRaining)
                    {
                        tasks.Add(new FarmTask
                        {
                            Type = FarmTaskType.Water,
                            Tile = pair.Key,
                            Priority = 11
                        });
                    }
                    // Empty tilled dirt → plant if we carry seeds
                    else if (dirt.crop == null && hasSeeds)
                    {
                        tasks.Add(new FarmTask
                        {
                            Type = FarmTaskType.Plant,
                            Tile = pair.Key,
                            Priority = 7
                        });
                    }
                }
            }

            // Debris on the farm — BOUNDED like hoeing: only within 20 tiles of the
            // player, nearest 25 max. Unbounded full-map scans produced ~1000-task
            // queues and turned chained farming into an endless whole-map marathon.
            if (nearTile.HasValue)
            {
                var debris = new List<FarmTask>();
                foreach (var pair in location.objects.Pairs)
                {
                    var obj = pair.Value;
                    string name = obj.Name ?? "";
                    if (!(name.Contains("Stone") || name.Contains("Weed") || name.Contains("Twig")
                        || obj.ParentSheetIndex == 294 || obj.ParentSheetIndex == 295
                        || obj.ParentSheetIndex == 343 || obj.ParentSheetIndex == 450))
                        continue;
                    if (Vector2.Distance(nearTile.Value, pair.Key) > 20f)
                        continue;
                    debris.Add(new FarmTask
                    {
                        Type = FarmTaskType.ClearDebris,
                        Tile = pair.Key,
                        Priority = 3
                    });
                }
                var center = nearTile.Value;
                debris.Sort((a, b) => Vector2.Distance(center, a.Tile).CompareTo(Vector2.Distance(center, b.Tile)));
                for (int i = 0; i < debris.Count && i < 25; i++)
                    tasks.Add(debris[i]);
            }

            // Grass on the farm — BOUNDED like debris: within 20 tiles of the player,
            // nearest 25. Grass is a terrainFeature that blocks hoeing, so it must be
            // cut (scythe) before a tile can be tilled. Priority 4: above debris(3)/hoe(2),
            // below plant(7). Unbounded scans of a grassy farm produce ~1000-task queues.
            if (nearTile.HasValue)
            {
                var grass = new List<FarmTask>();
                foreach (var pair in location.terrainFeatures.Pairs)
                {
                    if (!(pair.Value is Grass))
                        continue;
                    if (Vector2.Distance(nearTile.Value, pair.Key) > 20f)
                        continue;
                    grass.Add(new FarmTask
                    {
                        Type = FarmTaskType.CutGrass,
                        Tile = pair.Key,
                        Priority = 4
                    });
                }
                var gc = nearTile.Value;
                grass.Sort((a, b) => Vector2.Distance(gc, a.Tile).CompareTo(Vector2.Distance(gc, b.Tile)));
                for (int i = 0; i < grass.Count && i < 25; i++)
                    tasks.Add(grass[i]);
            }

            // Tillable open ground near the player (bounded so we don't churn the whole map).
            if (nearTile.HasValue)
            {
                int added = 0;
                int cx = (int)nearTile.Value.X;
                int cy = (int)nearTile.Value.Y;
                for (int x = cx - hoeRadius; x <= cx + hoeRadius && added < 20; x++)
                {
                    for (int y = cy - hoeRadius; y <= cy + hoeRadius && added < 20; y++)
                    {
                        var t = new Vector2(x, y);
                        if (location.terrainFeatures.ContainsKey(t) || location.objects.ContainsKey(t))
                            continue;
                        if (location.doesTileHaveProperty(x, y, "Diggable", "Back") == null)
                            continue;
                        if (!location.isTilePassable(new xTile.Dimensions.Location(x, y), Game1.viewport))
                            continue;
                        tasks.Add(new FarmTask
                        {
                            Type = FarmTaskType.Hoe,
                            Tile = t,
                            Priority = 2
                        });
                        added++;
                    }
                }
            }

            // Sort by priority descending, then distance to center
            tasks.Sort((a, b) => b.Priority.CompareTo(a.Priority));
            return tasks;
        }

        /// <summary>Execute a task at a tile.</summary>
        public static bool ExecuteTask(FarmTask task, GameLocation location, IMonitor monitor)
        {
            switch (task.Type)
            {
                case FarmTaskType.Water:
                    return WaterTile(location, task.Tile, monitor);
                case FarmTaskType.Harvest:
                    return HarvestTile(location, task.Tile, monitor);
                case FarmTaskType.ClearDebris:
                    return ClearDebris(location, task.Tile, monitor);
                case FarmTaskType.Hoe:
                    return HoeTile(location, task.Tile, monitor);
                case FarmTaskType.Plant:
                    return PlantTile(location, task.Tile, monitor);
                case FarmTaskType.CutGrass:
                    return CutGrassTile(location, task.Tile, monitor);
                default:
                    return false;
            }
        }

        /// <summary>Ship every sellable product in the player's inventory into the farm's
        /// shipping bin (vanilla end-of-day settlement, no menu). Excludes seeds (Category
        /// -74) and anything with no store value. Returns true if anything was shipped.</summary>
        public static bool ShipSellables(Farmer who, out int count, out int value)
        {
            count = 0;
            value = 0;
            var bin = Game1.getFarm()?.getShippingBin(who);
            if (bin == null) return false;
            for (int i = 0; i < who.Items.Count; i++)
            {
                if (who.Items[i] is SObject o
                    && o.canBeShipped() && o.sellToStorePrice() > 0 && o.Category != -74)
                {
                    count += o.Stack;
                    value += o.sellToStorePrice() * o.Stack;
                    bin.Add(o);
                    who.Items[i] = null;
                }
            }
            return count > 0;
        }

        /// <summary>Find the nearest water tile within <paramref name="radius"/> of a point
        /// (by-distance), used to walk the player to a water source to refill the can.</summary>
        public static Vector2? FindNearestWaterTile(GameLocation location, Vector2 from, int radius)
        {
            if (location == null) return null;
            Vector2? best = null;
            float bestD = float.MaxValue;
            int cx = (int)from.X, cy = (int)from.Y;
            for (int x = cx - radius; x <= cx + radius; x++)
            {
                for (int y = cy - radius; y <= cy + radius; y++)
                {
                    if (x < 0 || y < 0) continue;
                    if (!location.isWaterTile(x, y)) continue;
                    float d = Vector2.Distance(from, new Vector2(x, y));
                    if (d < bestD) { bestD = d; best = new Vector2(x, y); }
                }
            }
            return best;
        }

        /// <summary>Move non-seed inventory objects into a chest (optionally filtered by name).
        /// Only items the chest fully accepts are removed from the inventory. Returns true if
        /// anything was stored.</summary>
        public static bool StoreToChest(Chest chest, Farmer who, string filter, out int count)
        {
            count = 0;
            if (chest == null) return false;
            for (int i = 0; i < who.Items.Count; i++)
            {
                if (who.Items[i] is SObject o && o.Category != -74
                    && (string.IsNullOrEmpty(filter)
                        || (o.Name != null && o.Name.IndexOf(filter, StringComparison.OrdinalIgnoreCase) >= 0)))
                {
                    int before = o.Stack;
                    Item remainder = chest.addItem(o);
                    if (remainder == null)
                    {
                        count += before;
                        who.Items[i] = null;
                    }
                    else
                    {
                        count += before - remainder.Stack; // chest took a partial stack
                    }
                }
            }
            return count > 0;
        }

        /// <summary>Take items out of a chest into the player's inventory. When
        /// <paramref name="seedsOnly"/> is true, only seeds (Category -74) are taken; otherwise
        /// items whose name contains the optional <paramref name="filter"/> (any item when the
        /// filter is empty). Items leave the chest only to the extent the inventory accepts them
        /// (no item loss when the backpack is full). Returns true if anything was withdrawn.</summary>
        public static bool WithdrawFromChest(Chest chest, Farmer who, string filter, bool seedsOnly, out int count)
        {
            count = 0;
            if (chest == null) return false;
            var items = chest.Items;
            for (int i = 0; i < items.Count; i++)
            {
                var it = items[i];
                if (it == null) continue;
                bool match = seedsOnly
                    ? it.Category == -74
                    : (string.IsNullOrEmpty(filter)
                        || (it.Name != null && it.Name.IndexOf(filter, StringComparison.OrdinalIgnoreCase) >= 0));
                if (!match) continue;
                int before = it.Stack;
                Item leftover = who.addItemToInventory(it);
                if (leftover == null)
                {
                    count += before;
                    items[i] = null;
                }
                else
                {
                    count += before - leftover.Stack; // inventory took a partial stack
                    items[i] = leftover;
                }
            }
            chest.clearNulls();
            return count > 0;
        }

        /// <summary>Buy the cheapest in-season seed from an open ShopMenu, within a budget and a
        /// quantity cap. Reads price/stock from the menu's own data (authoritative), pays real
        /// gold and adds the seeds; refunds any portion the inventory couldn't accept (no loss of
        /// gold or items). Returns true if anything was bought.</summary>
        public static bool BuySeeds(ShopMenu menu, Farmer who, int qtyCap, int budget,
            out int count, out int spent, out string itemName)
        {
            count = 0; spent = 0; itemName = null;
            if (menu == null) return false;

            // Pick the cheapest seed (Category -74) that is in stock. Pierre's forSale is already
            // filtered to the current season, so "cheapest seed" = a cheap in-season seed.
            ISalable best = null; int bestPrice = int.MaxValue; int bestStock = int.MaxValue;
            foreach (var salable in menu.forSale)
            {
                if (!(salable is SObject o) || o.Category != -74) continue;
                if (!menu.itemPriceAndStock.TryGetValue(salable, out var info)) continue;
                int price = info.Price;
                if (price <= 0) continue;
                if (price < bestPrice)
                {
                    bestPrice = price; best = salable; bestStock = info.Stock;
                }
            }
            if (best == null) return false;

            int money = who.Money;
            int spendable = budget > 0 ? Math.Min(budget, money) : (int)(money * 0.6);
            int qty = spendable / bestPrice;
            int cap = qtyCap > 0 ? qtyCap : 30;
            if (qty > cap) qty = cap;
            if (bestStock > 0 && bestStock != int.MaxValue && qty > bestStock) qty = bestStock;
            if (qty <= 0) return false;

            // Add first, then pay only for what the inventory actually accepted.
            var bought = best.GetSalableInstance();
            bought.Stack = qty;
            Item leftover = who.addItemToInventory(bought as Item);
            int taken = qty - (leftover?.Stack ?? 0);
            if (taken <= 0) return false;
            who.Money -= bestPrice * taken;
            count = taken;
            spent = bestPrice * taken;
            itemName = best.DisplayName;
            return true;
        }
    }

    public enum FarmTaskType
    {
        Water,
        Harvest,
        Hoe,
        ClearDebris,
        Plant,
        CutGrass,
        ToolUse,
        Interact,
        RefillCan
    }

    public class FarmTask
    {
        public FarmTaskType Type { get; set; }
        public Vector2 Tile { get; set; }
        public int Priority { get; set; }
        public string ToolTypeName { get; set; }  // for manual player_use_tool tasks
        public string SeedName { get; set; }      // for manual player_plant tasks
    }
}
