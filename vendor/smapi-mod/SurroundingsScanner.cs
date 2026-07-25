using System;
using System.Collections.Generic;
using System.Linq;
using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Monsters;
using StardewValley.Objects;
using StardewValley.TerrainFeatures;
using SObject = StardewValley.Object;

namespace StardewMCPBridge
{
    /// <summary>
    /// Scans tiles around a companion and builds a structured view of their surroundings.
    /// This is how the AI companion "sees" the world.
    /// </summary>
    public static class SurroundingsScanner
    {
        private static readonly HashSet<string> CompanionNames = new HashSet<string> { "Companion1", "Companion2" };

        /// <summary>Scan tiles around a position and return structured data for the bridge.</summary>
        public static ScanResult Scan(GameLocation location, Vector2 centerTile, int radius = 8)
        {
            var result = new ScanResult
            {
                Location = location.Name,
                CenterX = (int)centerTile.X,
                CenterY = (int)centerTile.Y,
                Radius = radius
            };

            int minX = (int)centerTile.X - radius;
            int maxX = (int)centerTile.X + radius;
            int minY = (int)centerTile.Y - radius;
            int maxY = (int)centerTile.Y + radius;

            for (int x = minX; x <= maxX; x++)
            {
                for (int y = minY; y <= maxY; y++)
                {
                    var tile = new Vector2(x, y);
                    var tileInfo = ScanTile(location, tile);
                    if (tileInfo != null)
                        result.Tiles.Add(tileInfo);
                }
            }

            // Characters (NPCs, monsters) in the area
            foreach (var character in location.characters)
            {
                var charTile = character.Tile;
                if (Math.Abs(charTile.X - centerTile.X) <= radius &&
                    Math.Abs(charTile.Y - centerTile.Y) <= radius)
                {
                    if (character is Monster monster)
                    {
                        result.Monsters.Add(new MonsterInfo
                        {
                            Name = monster.Name,
                            X = (int)charTile.X,
                            Y = (int)charTile.Y,
                            Health = monster.Health,
                            MaxHealth = monster.MaxHealth
                        });
                    }
                    else if (!CompanionNames.Contains(character.Name))
                    {
                        result.Npcs.Add(new NpcInfo
                        {
                            Name = character.Name,
                            X = (int)charTile.X,
                            Y = (int)charTile.Y
                        });
                    }
                }
            }

            return result;
        }

        private static TileInfo ScanTile(GameLocation location, Vector2 tile)
        {
            int x = (int)tile.X;
            int y = (int)tile.Y;

            // Use the pathfinder's real collision check (machines/chests/scarecrows are
            // solid) so digest/brain see the same passability the pilot uses to move.
            bool passable = Pathfinder.IsWalkable(location, x, y);
            bool isWater = location.isWaterTile(x, y);

            string terrainType = null;
            string cropName = null;
            bool cropReady = false;
            int waterState = -1; // -1 = no dirt

            if (location.terrainFeatures.TryGetValue(tile, out var feature))
            {
                if (feature is HoeDirt dirt)
                {
                    terrainType = "hoeDirt";
                    waterState = dirt.state.Value; // 0=dry, 1=watered
                    if (dirt.crop != null)
                    {
                        cropName = dirt.crop.indexOfHarvest.Value;
                        // Try to resolve display name
                        try
                        {
                            var item = ItemRegistry.Create("(O)" + dirt.crop.indexOfHarvest.Value);
                            cropName = item?.DisplayName ?? cropName;
                        }
                        catch { }
                        cropReady = dirt.readyForHarvest();
                    }
                }
                else if (feature is Tree)
                {
                    terrainType = "tree";
                }
                else if (feature is Grass)
                {
                    terrainType = "grass";
                }
                else if (feature is Bush)
                {
                    terrainType = "bush";
                }
            }

            string objectName = null;
            string objectType = null;
            bool breakable = false;
            bool interactable = false;

            if (location.objects.TryGetValue(tile, out var obj))
            {
                objectName = obj.DisplayName ?? obj.Name;
                objectType = GetObjectType(obj);
                breakable = IsBreakable(obj);
                interactable = IsInteractable(obj);
            }

            // Only include tiles that have something interesting or are impassable/water
            bool hasContent = terrainType != null || objectName != null || isWater || !passable;
            if (!hasContent) return null;

            return new TileInfo
            {
                X = x,
                Y = y,
                Passable = passable,
                IsWater = isWater,
                Terrain = terrainType,
                CropName = cropName,
                CropReady = cropReady,
                WaterState = waterState,
                ObjectName = objectName,
                ObjectType = objectType,
                Breakable = breakable,
                Interactable = interactable
            };
        }

        private static string GetObjectType(SObject obj)
        {
            if (obj is Chest) return "chest";
            if (obj.Name != null)
            {
                if (obj.Name.Contains("Stone")) return "stone";
                if (obj.Name.Contains("Weed")) return "weed";
                if (obj.Name.Contains("Twig")) return "twig";
                if (obj.Name.Contains("Ladder") || obj.Name.Contains("Shaft")) return "ladder";
            }
            if (obj.bigCraftable.Value) return "machine";
            return "object";
        }

        private static bool IsBreakable(SObject obj)
        {
            if (obj.Name == null) return false;
            return obj.Name.Contains("Stone") || obj.Name.Contains("Weed") || obj.Name.Contains("Twig")
                || obj.ParentSheetIndex == 294 || obj.ParentSheetIndex == 295
                || obj.ParentSheetIndex == 343 || obj.ParentSheetIndex == 450;
        }

        private static bool IsInteractable(SObject obj)
        {
            if (obj is Chest) return true;
            if (obj.bigCraftable.Value) return true; // machines
            if (obj.Name != null && (obj.Name.Contains("Ladder") || obj.Name.Contains("Shaft"))) return true;
            return false;
        }
    }

    // ==============================
    // Data classes for serialization
    // ==============================

    public class ScanResult
    {
        public string Location { get; set; }
        public int CenterX { get; set; }
        public int CenterY { get; set; }
        public int Radius { get; set; }
        public List<TileInfo> Tiles { get; set; } = new List<TileInfo>();
        public List<MonsterInfo> Monsters { get; set; } = new List<MonsterInfo>();
        public List<NpcInfo> Npcs { get; set; } = new List<NpcInfo>();
    }

    public class TileInfo
    {
        public int X { get; set; }
        public int Y { get; set; }
        public bool Passable { get; set; }
        public bool IsWater { get; set; }
        public string Terrain { get; set; }
        public string CropName { get; set; }
        public bool CropReady { get; set; }
        public int WaterState { get; set; }
        public string ObjectName { get; set; }
        public string ObjectType { get; set; }
        public bool Breakable { get; set; }
        public bool Interactable { get; set; }
    }

    public class MonsterInfo
    {
        public string Name { get; set; }
        public int X { get; set; }
        public int Y { get; set; }
        public int Health { get; set; }
        public int MaxHealth { get; set; }
    }

    public class NpcInfo
    {
        public string Name { get; set; }
        public int X { get; set; }
        public int Y { get; set; }
    }
}
