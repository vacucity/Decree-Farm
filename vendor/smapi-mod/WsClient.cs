using System;
using System.Collections.Concurrent;
using System.IO;
using System.Net.WebSockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using StardewModdingAPI;

namespace StardewMCPBridge
{
    /// <summary>
    /// WebSocket client channel to the Python brain (which hosts the server at
    /// ws://localhost:8765). This is the low-latency path; the actions/ file queue
    /// and bridge_data.json stay active as an automatic fallback while the socket
    /// is down.
    ///
    /// Wire protocol:
    ///   Python -> MOD:  a raw action JSON, identical to an actions/*.json file
    ///                   ({"actionType":"player_farm", ...}) — fed straight into
    ///                   the same handler as file-queued commands.
    ///   MOD -> Python:  an envelope {"type":"state","data":{...}} where data is
    ///                   exactly the bridge_data.json payload.
    ///
    /// All socket I/O runs on a background task; the game thread only touches the
    /// two lock-free queues, so a slow/absent server can never hitch rendering.
    /// </summary>
    public class WsClient
    {
        private readonly IMonitor monitor;
        private readonly Uri uri;
        private readonly ConcurrentQueue<string> outbound = new ConcurrentQueue<string>();
        private readonly ConcurrentQueue<string> inbound = new ConcurrentQueue<string>();
        private ClientWebSocket ws;
        private CancellationTokenSource cts;
        private Task loopTask;
        private bool loggedConnect;

        public bool IsConnected => this.ws != null && this.ws.State == WebSocketState.Open;

        public WsClient(IMonitor monitor, string url)
        {
            this.monitor = monitor;
            this.uri = new Uri(url);
        }

        public void Start()
        {
            if (this.loopTask != null)
                return;
            this.cts = new CancellationTokenSource();
            this.loopTask = Task.Run(() => this.ConnectionLoop(this.cts.Token));
        }

        public void Stop()
        {
            try { this.cts?.Cancel(); } catch { }
        }

        /// <summary>Queue a message for sending (thread-safe, never blocks the game thread).</summary>
        public void Send(string json)
        {
            this.outbound.Enqueue(json);
        }

        /// <summary>Dequeue a received command (called from the game thread each tick).</summary>
        public bool TryReceive(out string json) => this.inbound.TryDequeue(out json);

        private async Task ConnectionLoop(CancellationToken ct)
        {
            while (!ct.IsCancellationRequested)
            {
                try
                {
                    using (var sock = new ClientWebSocket())
                    {
                        this.ws = sock;
                        await sock.ConnectAsync(this.uri, ct).ConfigureAwait(false);
                        if (!this.loggedConnect)
                        {
                            this.monitor.Log($"WsClient: connected to {this.uri} (low-latency channel online).", LogLevel.Info);
                            this.loggedConnect = true;
                        }

                        // When either direction ends, tear down and reconnect.
                        await Task.WhenAny(
                            this.ReceiveLoop(sock, ct),
                            this.SendLoop(sock, ct)).ConfigureAwait(false);
                    }
                }
                catch (OperationCanceledException) { break; }
                catch (Exception)
                {
                    // Server not up yet (brain.py not running) — stay quiet and retry.
                }
                finally { this.ws = null; }

                try { await Task.Delay(3000, ct).ConfigureAwait(false); }
                catch (OperationCanceledException) { break; }
            }
        }

        private async Task ReceiveLoop(ClientWebSocket sock, CancellationToken ct)
        {
            var buf = new byte[256 * 1024];
            while (sock.State == WebSocketState.Open && !ct.IsCancellationRequested)
            {
                string msg;
                using (var ms = new MemoryStream())
                {
                    WebSocketReceiveResult result;
                    do
                    {
                        result = await sock.ReceiveAsync(new ArraySegment<byte>(buf), ct).ConfigureAwait(false);
                        if (result.MessageType == WebSocketMessageType.Close)
                            return;
                        ms.Write(buf, 0, result.Count);
                    }
                    while (!result.EndOfMessage);
                    msg = Encoding.UTF8.GetString(ms.ToArray());
                }
                if (!string.IsNullOrWhiteSpace(msg))
                    this.inbound.Enqueue(msg);
            }
        }

        private async Task SendLoop(ClientWebSocket sock, CancellationToken ct)
        {
            while (sock.State == WebSocketState.Open && !ct.IsCancellationRequested)
            {
                if (this.outbound.TryDequeue(out var msg))
                {
                    var bytes = Encoding.UTF8.GetBytes(msg);
                    await sock.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text, true, ct).ConfigureAwait(false);
                }
                else
                {
                    await Task.Delay(100, ct).ConfigureAwait(false);
                }
            }
        }
    }
}
