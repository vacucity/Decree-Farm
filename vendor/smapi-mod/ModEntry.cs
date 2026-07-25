using System;
using System.IO;
using System.Linq;
using System.Text.Json;
using StardewModdingAPI;
using StardewModdingAPI.Events;
using StardewValley;

namespace StardewMCPBridge
{
    /// <summary>
    /// Stardew MCP Bridge — player-pilot edition.
    /// The agent drives the real main player (Game1.player) directly; there are no
    /// companion NPCs. Game state is written to bridge_data.json each half-second and
    /// commands are consumed from the actions/ queue.
    /// </summary>
    public class ModEntry : Mod
    {
        private string bridgePath;
        private string actionDir;
        private PlayerPilot playerPilot;
        private WsClient wsClient;

        public override void Entry(IModHelper helper)
        {
            this.playerPilot = new PlayerPilot(this.Monitor, helper.Input);
            this.bridgePath = Path.Combine(helper.DirectoryPath, "bridge_data.json");
            this.actionDir = Path.Combine(helper.DirectoryPath, "actions");

            // Low-latency channel: connect out to the Python brain's WebSocket server.
            // Silent 3s-retry while the server is down; the file bridge keeps working.
            this.wsClient = new WsClient(this.Monitor, "ws://localhost:8765");
            this.wsClient.Start();

            helper.Events.GameLoop.GameLaunched += this.OnGameLaunched;
            helper.Events.GameLoop.UpdateTicked += this.OnUpdateTicked;
            helper.Events.GameLoop.SaveLoaded += this.OnSaveLoaded;

            this.Monitor.Log("Stardew MCP Bridge initialized (player-pilot mode).", LogLevel.Debug);
        }

        private void OnGameLaunched(object sender, GameLaunchedEventArgs e)
        {
            this.Monitor.Log("Bridge online. Waiting for world. Agent pilots the main player.", LogLevel.Info);
        }

        /// <summary>
        /// Strip any leftover Companion NPCs left behind by earlier companion-based builds,
        /// so old saves load cleanly into the player-pilot mod.
        /// </summary>
        private void OnSaveLoaded(object sender, SaveLoadedEventArgs e)
        {
            try
            {
                int removed = 0;
                foreach (var loc in Game1.locations)
                {
                    var stragglers = loc.characters
                        .Where(c => c.Name == "Companion1" || c.Name == "Companion2")
                        .ToList();
                    foreach (var npc in stragglers)
                    {
                        loc.characters.Remove(npc);
                        removed++;
                    }
                }
                if (removed > 0)
                    this.Monitor.Log($"Removed {removed} legacy companion NPC(s) from the save.", LogLevel.Info);
            }
            catch (Exception ex)
            {
                this.Monitor.Log($"Companion cleanup error: {ex.Message}", LogLevel.Warn);
            }
        }

        private void OnUpdateTicked(object sender, UpdateTickedEventArgs e)
        {
            if (!Context.IsWorldReady) return;

            // Pilot the real main player every frame (drives its pathfinder + autonomous farm).
            this.playerPilot.Tick();

            // Drain WebSocket commands every tick for low latency (same JSON shape as
            // the actions/*.json files; both channels feed the one handler).
            while (this.wsClient.TryReceive(out string wsCmd))
            {
                try
                {
                    this.HandleAction(wsCmd);
                }
                catch (Exception ex)
                {
                    this.Monitor.Log($"WS action handling error: {ex.Message}", LogLevel.Error);
                }
            }

            // Bridge I/O every 30 ticks (~0.5s) to avoid thrashing disk.
            if (e.IsMultipleOf(30))
            {
                this.SyncGameState();
                this.ProcessActions();
            }
        }

        private void SyncGameState()
        {
            try
            {
                var state = new
                {
                    time = Game1.timeOfDay,
                    day = Game1.dayOfMonth,
                    season = Game1.currentSeason,
                    weather = Game1.isLightning ? "storm" : Game1.isRaining ? "rain" : Game1.isSnowing ? "snow" : Game1.isDebrisWeather ? "windy" : "sunny",
                    location = Game1.currentLocation?.Name,
                    player = new
                    {
                        name = Game1.player.Name,
                        health = Game1.player.health,
                        stamina = Game1.player.Stamina,
                        money = Game1.player.Money,
                        position = new { x = Game1.player.Position.X, y = Game1.player.Position.Y }
                    },
                    agentPlayer = this.playerPilot.GetStatus(),
                    npcs = Game1.currentLocation?.characters.Select(c => new
                    {
                        name = c.Name,
                        position = new { x = c.Position.X, y = c.Position.Y }
                    }).ToList(),
                    syncedAt = DateTime.UtcNow.ToString("o")
                };

                string json = JsonSerializer.Serialize(state, new JsonSerializerOptions { WriteIndented = true });
                // Atomic write: temp file then rename, so the MCP side never reads partial JSON.
                string tmpPath = this.bridgePath + ".tmp";
                File.WriteAllText(tmpPath, json);
                File.Move(tmpPath, this.bridgePath, true);

                // Push the same payload over the low-latency socket when it's up.
                if (this.wsClient.IsConnected)
                    this.wsClient.Send("{\"type\":\"state\",\"data\":" + json + "}");
            }
            catch (Exception ex)
            {
                this.Monitor.Log($"Bridge Sync Error: {ex.Message}", LogLevel.Error);
            }
        }

        private void ProcessActions()
        {
            try
            {
                if (!Directory.Exists(this.actionDir))
                    return;

                // Drain the queue oldest-first. Each command is its own file named
                // <timestamp>-<seq>.json, so ordinal filename sort is chronological.
                string[] files = Directory.GetFiles(this.actionDir, "*.json");
                if (files.Length == 0)
                    return;
                Array.Sort(files, StringComparer.Ordinal);

                foreach (string file in files)
                {
                    string json;
                    try
                    {
                        json = File.ReadAllText(file);
                        // Delete immediately so each command is consumed exactly once,
                        // even if handling below throws.
                        File.Delete(file);
                    }
                    catch (Exception ex)
                    {
                        this.Monitor.Log($"Action read error ({Path.GetFileName(file)}): {ex.Message}", LogLevel.Error);
                        continue;
                    }

                    if (string.IsNullOrWhiteSpace(json))
                        continue;

                    try
                    {
                        this.HandleAction(json);
                    }
                    catch (Exception ex)
                    {
                        this.Monitor.Log($"Action handling error: {ex.Message}", LogLevel.Error);
                    }
                }
            }
            catch (Exception ex)
            {
                this.Monitor.Log($"Action Processing Error: {ex.Message}", LogLevel.Error);
            }
        }

        private void HandleAction(string json)
        {
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;

            if (!root.TryGetProperty("actionType", out var actionType))
                return;

            string actionName = actionType.GetString();

            // Agent-piloted main-player commands (player_move_to, player_farm, player_use_tool, ...).
            if (actionName != null && actionName.StartsWith("player_"))
            {
                this.playerPilot.HandleCommand(actionName, root);
                return;
            }

            // In-game chat / narration.
            if (actionName == "chat")
            {
                if (root.TryGetProperty("metadata", out var meta) &&
                    meta.TryGetProperty("message", out var msg))
                {
                    string text = msg.GetString();
                    if (!string.IsNullOrEmpty(text))
                    {
                        Game1.chatBox?.addMessage(text, Microsoft.Xna.Framework.Color.Gold);
                        this.Monitor.Log($"Chat sent: {text}", LogLevel.Info);
                    }
                }
                return;
            }

            this.Monitor.Log($"Ignored unsupported action: {actionName}", LogLevel.Trace);
        }
    }
}
