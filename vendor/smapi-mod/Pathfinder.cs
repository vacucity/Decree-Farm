using System;
using System.Collections.Generic;
using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Locations;
using xTile.Dimensions;

namespace StardewMCPBridge
{
    /// <summary>
    /// A* pathfinding for navigating Stardew Valley maps.
    /// Ported from Hunter-Thompson/stardew-mcp (MIT), with full vanilla walkability
    /// checks: map passability, objects, terrain features (incl. HoeDirt rules),
    /// resource clumps, farm buildings, furniture and water.
    /// </summary>
    public class Pathfinder
    {
        private const int MaxIterations = 50000;

        /// <summary>Find a path from start to goal using A*. Returns tile waypoints
        /// (excluding the start tile), or null if unreachable.
        /// If the start tile has no walkable neighbor (warp/door landing), a BFS escape
        /// is attempted first to reach open ground.</summary>
        public List<Vector2> FindPath(GameLocation location, Vector2 start, Vector2 goal)
        {
            if (location == null)
                return null;

            // Quick check: if goal is not walkable, no path possible.
            if (!IsTileWalkable(location, (int)goal.X, (int)goal.Y))
                return null;

            if (start == goal)
                return new List<Vector2>();

            // Warp-escape: if start has no fully-walkable neighbor (common for farmhouse
            // door, mine entrance, etc. where building footprint blocks adjacent tiles),
            // BFS outward with relaxed walkability to find the nearest open tile, then A*.
            bool hasWalkableNeighbor = false;
            foreach (var n in GetNeighbors(start))
            {
                if (IsTileWalkable(location, (int)n.X, (int)n.Y))
                { hasWalkableNeighbor = true; break; }
            }

            if (!hasWalkableNeighbor)
            {
                var escape = FindEscapeRoute(location, start);
                if (escape == null) return null;

                var (escapeTile, prefix) = escape.Value;
                if (escapeTile == goal) return prefix;

                var suffix = FindPathCore(location, escapeTile, goal);
                if (suffix == null) return null;

                prefix.AddRange(suffix);
                return prefix;
            }

            return FindPathCore(location, start, goal);
        }

        /// <summary>Core A* pathfinding (assumes start has at least one walkable neighbor).</summary>
        private List<Vector2> FindPathCore(GameLocation location, Vector2 start, Vector2 goal)
        {
            if (start == goal)
                return new List<Vector2>();

            var openSet = new PriorityQueue<Vector2, float>();
            var cameFrom = new Dictionary<Vector2, Vector2>();
            var gScore = new Dictionary<Vector2, float> { [start] = 0 };
            var fScore = new Dictionary<Vector2, float> { [start] = Heuristic(start, goal) };

            openSet.Enqueue(start, fScore[start]);
            var inOpenSet = new HashSet<Vector2> { start };

            int iterations = 0;

            while (openSet.Count > 0 && iterations < MaxIterations)
            {
                iterations++;
                var current = openSet.Dequeue();
                inOpenSet.Remove(current);

                if (current == goal)
                    return ReconstructPath(cameFrom, current);

                foreach (var neighbor in GetNeighbors(current))
                {
                    int nx = (int)neighbor.X;
                    int ny = (int)neighbor.Y;

                    if (!IsTileWalkable(location, nx, ny))
                        continue;

                    float tentativeGScore = gScore[current] + 1;

                    if (!gScore.ContainsKey(neighbor) || tentativeGScore < gScore[neighbor])
                    {
                        cameFrom[neighbor] = current;
                        gScore[neighbor] = tentativeGScore;
                        fScore[neighbor] = tentativeGScore + Heuristic(neighbor, goal);

                        if (!inOpenSet.Contains(neighbor))
                        {
                            openSet.Enqueue(neighbor, fScore[neighbor]);
                            inOpenSet.Add(neighbor);
                        }
                    }
                }
            }

            return null; // no path found
        }

        /// <summary>Check if a tile is walkable for the player, using the game's own rules.
        /// Instance wrapper — delegates to the static <see cref="IsWalkable"/> so callers that
        /// hold a Pathfinder instance and code that only has a location share one truth.</summary>
        public bool IsTileWalkable(GameLocation location, int x, int y)
        {
            return IsWalkable(location, x, y);
        }

        /// <summary>Check if a tile is walkable for the player, using the game's own rules.
        /// Static so the surroundings scanner reports the same passability the pathfinder uses
        /// (machines/chests/scarecrows are solid even though the map layer marks them passable).</summary>
        public static bool IsWalkable(GameLocation location, int x, int y)
        {
            if (location == null || x < 0 || y < 0)
                return false;

            var map = location.Map;
            if (map == null || map.Layers.Count == 0)
                return false;

            var layer = map.Layers[0];
            if (x >= layer.LayerWidth || y >= layer.LayerHeight)
                return false;

            var tileLocation = new Location(x, y);
            var tileVector = new Vector2(x, y);

            // Map-layer passability (vanilla check).
            if (!location.isTilePassable(tileLocation, Game1.viewport))
                return false;

            // Objects (stones, weeds, machines, fences...) block unless passable.
            if (location.objects.TryGetValue(tileVector, out var obj) && !obj.isPassable())
                return false;

            // Terrain features: trees block; HoeDirt is walkable except trellis crops
            // (the feature's own isPassable() implements the vanilla rule).
            if (location.terrainFeatures.TryGetValue(tileVector, out var feature) && !feature.isPassable())
                return false;

            // Large resource clumps (boulders, stumps...).
            foreach (var clump in location.resourceClumps)
            {
                if (clump.occupiesTile(x, y))
                    return false;
            }

            // Farm buildings.
            if (location is Farm farm)
            {
                foreach (var building in farm.buildings)
                {
                    if (building.occupiesTile(tileVector))
                        return false;
                }
            }

            // Furniture.
            foreach (var furniture in location.furniture)
            {
                if (furniture.TileLocation == tileVector ||
                    furniture.boundingBox.Value.Contains(x * 64 + 32, y * 64 + 32))
                    return false;
            }

            // Large terrain features (bushes) block movement even though the map layer
            // marks the tile passable. These live in largeTerrainFeatures (NOT terrainFeatures),
            // so the checks above miss them — the crowded-farm "stuck at one tile" root cause.
            var tileBox = new Microsoft.Xna.Framework.Rectangle(x * 64, y * 64, 64, 64);
            foreach (var ltf in location.largeTerrainFeatures)
            {
                if (ltf.getBoundingBox().Intersects(tileBox))
                    return false;
            }

            // Water (can't walk on water).
            if (location.isWaterTile(x, y) && !location.isTilePassable(tileLocation, Game1.viewport))
                return false;

            return true;
        }

        /// <summary>Get the 4-directional neighbors of a tile.</summary>
        private IEnumerable<Vector2> GetNeighbors(Vector2 tile)
        {
            yield return new Vector2(tile.X + 1, tile.Y);
            yield return new Vector2(tile.X - 1, tile.Y);
            yield return new Vector2(tile.X, tile.Y + 1);
            yield return new Vector2(tile.X, tile.Y - 1);
        }

        private float Heuristic(Vector2 a, Vector2 b)
        {
            return Math.Abs(a.X - b.X) + Math.Abs(a.Y - b.Y);
        }

        /// <summary>Reconstruct start-to-goal path, excluding the start tile.</summary>
        private List<Vector2> ReconstructPath(Dictionary<Vector2, Vector2> cameFrom, Vector2 current)
        {
            var path = new List<Vector2> { current };
            while (cameFrom.ContainsKey(current))
            {
                current = cameFrom[current];
                path.Add(current);
            }
            path.Reverse();
            if (path.Count > 0)
                path.RemoveAt(0); // we're already standing on the first tile
            return path;
        }

        // ======================
        // WARP-ESCAPE: BFS from a stuck start to the nearest fully-walkable tile.
        // Uses relaxed walkability (vanilla isTilePassable only, skipping building/
        // furniture checks that may over-block near doors and warps).
        // ======================

        /// <summary>BFS outward from <paramref name="start"/> through relaxed-walkable tiles
        /// until a fully-walkable tile is found. Returns (escapeTile, pathFromStart), or null.</summary>
        private (Vector2 escapeTile, List<Vector2> path)? FindEscapeRoute(GameLocation location, Vector2 start, int maxRadius = 5)
        {
            var queue = new Queue<Vector2>();
            var cameFrom = new Dictionary<Vector2, Vector2>();
            var visited = new HashSet<Vector2> { start };
            queue.Enqueue(start);

            while (queue.Count > 0)
            {
                var current = queue.Dequeue();
                if (Vector2.Distance(start, current) > maxRadius)
                    continue;

                foreach (var n in GetNeighbors(current))
                {
                    if (visited.Contains(n)) continue;
                    visited.Add(n);

                    int nx = (int)n.X, ny = (int)n.Y;
                    if (!IsRelaxedWalkable(location, nx, ny))
                        continue;

                    cameFrom[n] = current;

                    // Found a tile that passes the FULL walkability check → escape target.
                    if (IsTileWalkable(location, nx, ny))
                    {
                        var path = new List<Vector2>();
                        var c = n;
                        while (cameFrom.ContainsKey(c))
                        {
                            path.Add(c);
                            c = cameFrom[c];
                        }
                        path.Reverse();
                        return (n, path);
                    }

                    queue.Enqueue(n);
                }
            }
            return null;
        }

        /// <summary>Relaxed walkability: uses only the vanilla map-layer passability check
        /// (skips building/furniture/object checks that may over-block near doors).
        /// Still rejects water and resource clumps (physical obstacles).</summary>
        internal static bool IsRelaxedWalkable(GameLocation location, int x, int y)
        {
            if (location == null || x < 0 || y < 0)
                return false;
            var map = location.Map;
            if (map == null || map.Layers.Count == 0)
                return false;
            var layer = map.Layers[0];
            if (x >= layer.LayerWidth || y >= layer.LayerHeight)
                return false;

            var tileLocation = new Location(x, y);
            if (!location.isTilePassable(tileLocation, Game1.viewport))
                return false;
            if (location.isWaterTile(x, y))
                return false;

            // Resource clumps are physical boulders — still block.
            foreach (var clump in location.resourceClumps)
            {
                if (clump.occupiesTile(x, y))
                    return false;
            }
            return true;
        }
    }
}
