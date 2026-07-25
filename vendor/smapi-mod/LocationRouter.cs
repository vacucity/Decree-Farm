using System.Collections.Generic;
using Microsoft.Xna.Framework;
using StardewValley;

namespace StardewMCPBridge
{
    /// <summary>
    /// Cross-location routing over the game's own warp network. Builds a directed graph of
    /// location -> location edges (each edge carries the SOURCE warp tile to step onto), then
    /// BFS to find the next hop toward a target. This lets the pilot WALK map-to-map by
    /// stepping onto edge warps (vanilla auto-transition), instead of teleporting.
    ///
    /// Edges come from three vanilla sources:
    ///   - location.warps        (overworld edge warps: Farm<->Backwoods<->Mountain<->Mine...)
    ///   - location.doors        (building-interior doors on a map, e.g. Town -> SeedShop)
    ///   - Farm.buildings         (the farmhouse/cabin/barn human doors -> their interiors)
    /// </summary>
    public static class LocationRouter
    {
        public struct Hop
        {
            public string NextLocation;
            public Vector2 WarpTile;
        }

        private static Dictionary<string, List<(string target, Vector2 tile)>> graph;
        private static int cachedLocationCount = -1;

        /// <summary>(Re)build the graph lazily; the cache is keyed on the location count so
        /// it refreshes when locations are added (e.g. constructed buildings).</summary>
        private static void EnsureGraph()
        {
            if (graph != null && cachedLocationCount == Game1.locations.Count)
                return;

            graph = new Dictionary<string, List<(string, Vector2)>>();
            cachedLocationCount = Game1.locations.Count;

            foreach (var loc in Game1.locations)
            {
                if (loc?.Name == null) continue;
                var edges = GetOrAdd(loc.Name);

                // Overworld / interior edge warps.
                foreach (var w in loc.warps)
                {
                    if (string.IsNullOrEmpty(w.TargetName)) continue;
                    edges.Add((w.TargetName, new Vector2(w.X, w.Y)));
                }

                // Map-defined doors to building interiors (value = target location name).
                foreach (var pair in loc.doors.Pairs)
                {
                    if (string.IsNullOrEmpty(pair.Value)) continue;
                    edges.Add((pair.Value, new Vector2(pair.Key.X, pair.Key.Y)));
                }

                // Farm buildings (farmhouse/cabin/barn...) human doors -> their interiors.
                if (loc is Farm farm)
                {
                    foreach (var building in farm.buildings)
                    {
                        var indoor = building.indoors.Value;
                        if (indoor?.Name == null) continue;
                        var door = building.getPointForHumanDoor();
                        edges.Add((indoor.Name, new Vector2(door.X, door.Y)));
                    }
                }
            }
        }

        /// <summary>BFS from current to target; return the FIRST hop (the next location to
        /// enter and the warp tile in the CURRENT location to step onto), or null if there is
        /// no known route. Recompute after every transition for self-correction.</summary>
        public static Hop? GetNextHop(string current, string target)
        {
            if (string.IsNullOrEmpty(current) || string.IsNullOrEmpty(target) || current == target)
                return null;

            EnsureGraph();
            if (!graph.ContainsKey(current))
                return null;

            var cameFrom = new Dictionary<string, (string prev, Vector2 tile)>
            {
                [current] = (null, Vector2.Zero)
            };
            var queue = new Queue<string>();
            queue.Enqueue(current);

            bool found = false;
            while (queue.Count > 0)
            {
                var node = queue.Dequeue();
                if (node == target) { found = true; break; }
                if (!graph.TryGetValue(node, out var edges)) continue;
                foreach (var (t, tile) in edges)
                {
                    if (t == null || cameFrom.ContainsKey(t)) continue;
                    cameFrom[t] = (node, tile);
                    queue.Enqueue(t);
                }
            }

            if (!found)
                return null;

            // Walk back to the node whose predecessor is `current` -> that is our next hop.
            string cursor = target;
            while (cameFrom[cursor].prev != null && cameFrom[cursor].prev != current)
                cursor = cameFrom[cursor].prev;
            if (cameFrom[cursor].prev != current)
                return null;

            return new Hop { NextLocation = cursor, WarpTile = cameFrom[cursor].tile };
        }

        /// <summary>Return ALL warp tiles from <paramref name="current"/> leading to
        /// <paramref name="nextLocation"/>, sorted by Manhattan distance to <paramref name="playerTile"/>.
        /// Used by DoTravel to cycle through alternatives when the closest warp is unreachable.</summary>
        public static List<Vector2> GetAllWarpsTo(string current, string nextLocation, Vector2 playerTile)
        {
            EnsureGraph();
            var result = new List<Vector2>();
            if (!graph.TryGetValue(current, out var edges))
                return result;

            foreach (var (target, tile) in edges)
            {
                if (string.Equals(target, nextLocation, System.StringComparison.OrdinalIgnoreCase))
                    result.Add(tile);
            }

            // Sort by distance to player so we try the nearest warp first.
            result.Sort((a, b) =>
            {
                float da = System.Math.Abs(a.X - playerTile.X) + System.Math.Abs(a.Y - playerTile.Y);
                float db = System.Math.Abs(b.X - playerTile.X) + System.Math.Abs(b.Y - playerTile.Y);
                return da.CompareTo(db);
            });
            return result;
        }

        private static List<(string, Vector2)> GetOrAdd(string name)
        {
            if (!graph.TryGetValue(name, out var list))
            {
                list = new List<(string, Vector2)>();
                graph[name] = list;
            }
            return list;
        }
    }
}
