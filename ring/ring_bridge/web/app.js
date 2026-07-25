/**
 * Focus Agent — 戒指与经营 Agent 控制台
 * 纯原生 JS，零依赖，移动端友好
 */

(function () {
  "use strict";

  // ═══════════════════════════════════════
  //  状态
  // ═══════════════════════════════════════

  const state = {
    connected: false,
    connecting: false,
    activeMac: "",
    activeTab: "agent",
    captureActive: false,
    mode: "rest",
    battery: null,
    charging: null,
    batteryKnown: false,
    imuStreaming: false,
    imuSampleCount: 0,
    imuLastSampleAge: null,
    imuState: "disconnected",
    imuRecoveryCount: 0,
    imuLastError: null,
    protocolDiagnostics: {},
    hardwareImuStreaming: false,
    dataSource: "none",
    showcase: null,
    focusAgent: null,
    deviceMode: "unknown",
    focusState: "light_focus",
    growth: 0,
    motion: 0,
    gestureCounts: { rotate_back: 0, rotate_front: 0, wave: 0, single_click: 0, double_click: 0 },
    accelHistory: [[], [], []],
    gyroHistory: [[], [], []],
    maxHistory: 100,
  };

  // ═══════════════════════════════════════
  //  DOM refs
  // ═══════════════════════════════════════

  const $ = (s) => document.querySelector(s);
  const $$ = (s) => document.querySelectorAll(s);

  const badge = $("#connection-badge");
  const connText = $("#conn-text");

  // ═══════════════════════════════════════
  //  Tabs
  // ═══════════════════════════════════════

  $$(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      $$(".tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      $$(".panel").forEach((p) => p.classList.remove("active"));
      const target = $("#panel-" + tab.dataset.tab);
      if (target) target.classList.add("active");
      state.activeTab = tab.dataset.tab;
      if (state.activeTab === "capture") refreshCaptureStatus();
      // 切换到 IMU tab 时重绘图表
      if (tab.dataset.tab === "imu") drawCharts();
    });
  });

  // ═══════════════════════════════════════
  //  WebSocket
  // ═══════════════════════════════════════

  let ws = null;
  let wsReconnectTimer = null;

  function connectWS() {
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const url = protocol + "//" + location.host + "/ws";
    try {
      ws = new WebSocket(url);
      ws.onopen = () => {
        console.log("WS connected");
        addLog("system", "WebSocket 已连接");
        if (wsReconnectTimer) { clearTimeout(wsReconnectTimer); wsReconnectTimer = null; }
      };
      ws.onmessage = (evt) => {
        try {
          const lines = evt.data.split("\n");
          lines.forEach((line) => {
            if (!line.trim()) return;
            const msg = JSON.parse(line);
            handleMessage(msg);
          });
        } catch (e) { /* ignore */ }
      };
      ws.onclose = () => {
        console.log("WS closed");
        if (!wsReconnectTimer) {
          wsReconnectTimer = setTimeout(connectWS, 3000);
        }
      };
      ws.onerror = () => { ws?.close(); };
    } catch (e) {
      if (!wsReconnectTimer) wsReconnectTimer = setTimeout(connectWS, 3000);
    }
  }

  function handleMessage(msg) {
    switch (msg.type) {
      case "imu":
        updateIMU(msg);
        break;
      case "imu_raw":
        updateRawIMU(msg);
        break;
      case "event":
        handleEvent(msg);
        break;
      case "system":
        handleSystem(msg);
        break;
      case "mappings":
        if (msg.mappings) {
          state.mappings = msg.mappings;
          renderMappingEditor();
        }
        break;
      case "focus_agent":
        renderFocusAgent(msg);
        break;
    }
  }

  function formatBattery(value) {
    return value === null || value === undefined ? "--" : value + "%";
  }

  function updateDeviceMode(mode) {
    const normalized = mode || "unknown";
    state.deviceMode = normalized;
    const labels = {
      recording: "录音模式",
      gesture: "手势模式",
      switching: "切换检测中",
      unknown: "未知",
    };
    const text = labels[normalized] || normalized;
    ["#ring-device-mode", "#imu-device-mode"].forEach((selector) => {
      const element = $(selector);
      if (!element) return;
      element.textContent = state.connected ? text : "未连接";
      element.dataset.mode = state.connected ? normalized : "disconnected";
    });
  }

  function updateIMUStreamStatus(status) {
    const data = status || {};
    if (data.imu_streaming !== undefined) state.imuStreaming = Boolean(data.imu_streaming);
    if (data.hardware_imu_streaming !== undefined) {
      state.hardwareImuStreaming = Boolean(data.hardware_imu_streaming);
    }
    if (data.data_source !== undefined) state.dataSource = data.data_source || "none";
    if (data.imu_sample_count !== undefined) state.imuSampleCount = Number(data.imu_sample_count) || 0;
    if (data.imu_last_sample_age_s !== undefined) state.imuLastSampleAge = data.imu_last_sample_age_s;
    if (data.imu_state !== undefined) state.imuState = data.imu_state;
    if (data.imu_recovery_count !== undefined) state.imuRecoveryCount = Number(data.imu_recovery_count) || 0;
    if (data.imu_last_error !== undefined) state.imuLastError = data.imu_last_error;
    if (data.protocol_diagnostics !== undefined) state.protocolDiagnostics = data.protocol_diagnostics || {};

    const streamState = $("#imu-stream-state");
    const streamDetail = $("#imu-stream-detail");
    const sampleCount = $("#imu-sample-count");
    const packetCount = $("#imu-packet-count");
    const protocolErrors = $("#imu-protocol-errors");
    if (sampleCount) sampleCount.textContent = String(state.imuSampleCount);
    const commands = state.protocolDiagnostics.command_counts || {};
    if (packetCount) packetCount.textContent = String(commands["0x0605"] || 0);
    if (protocolErrors) {
      protocolErrors.textContent = String(state.protocolDiagnostics.protocol_error_count || 0);
    }
    if (!streamState || !streamDetail) return;

    if (state.dataSource === "showcase_fallback") {
      streamState.textContent = "路演保障数据";
      streamState.dataset.tone = "waiting";
      streamDetail.textContent = "真实 0x0605 中断，当前由明确标记的备用演示数据维持展示。";
    } else if (!state.connected) {
      streamState.textContent = "未连接";
      streamState.dataset.tone = "idle";
      streamDetail.textContent = "连接戒指后将自动启动实时六轴数据。";
    } else if (state.imuStreaming) {
      streamState.textContent = "实时采样中";
      streamState.dataset.tone = "live";
      const age = state.imuLastSampleAge;
      streamDetail.textContent = age === null || age === undefined
        ? "正在接收原始加速度与陀螺仪数据。"
        : "最近数据 " + age + " 秒前到达。";
    } else if (state.imuState === "waiting_mode") {
      streamState.textContent = "等待手势模式";
      streamState.dataset.tone = "waiting";
      streamDetail.textContent = "戒指处于录音模式，请单击一次按键切换到手势模式。";
    } else if (state.imuState === "mode_switch") {
      streamState.textContent = "正在确认模式";
      streamState.dataset.tone = "waiting";
      streamDetail.textContent = "已检测到单击，正在确认戒指切换后的工作模式。";
    } else if (state.imuState === "recovering") {
      streamState.textContent = "正在重置数据通道";
      streamState.dataset.tone = "waiting";
      streamDetail.textContent = "正在执行 0x0603 停止 → 0x0601 重新开始。";
    } else if (state.connected && state.imuState === "idle") {
      streamState.textContent = "按需采集待命";
      streamState.dataset.tone = "waiting";
      streamDetail.textContent = "蓝牙控制连接保持稳定；开始标注片段或手动请求时才开启真实六轴数据。";
    } else if (state.imuState === "awaiting_data" && state.imuRecoveryCount > 0) {
      streamState.textContent = "启动成功但无数据";
      streamState.dataset.tone = "error";
      streamDetail.textContent = "戒指已确认启动，但未发送 0x0605；已恢复 "
        + state.imuRecoveryCount + " 次。";
    } else if (state.imuState === "awaiting_data" || state.imuState === "starting") {
      streamState.textContent = "等待首批数据";
      streamState.dataset.tone = "waiting";
      streamDetail.textContent = "手势模式已确认，正在等待戒指发送首批 0x0605 数据。";
    } else {
      streamState.textContent = "数据不可用";
      streamState.dataset.tone = "error";
      streamDetail.textContent = state.imuLastError || "未收到实时六轴数据。";
    }
  }

  function applyStatus(msg) {
    if (msg.connected !== undefined) state.connected = Boolean(msg.connected);
    if (msg.connecting !== undefined) state.connecting = Boolean(msg.connecting);
    if (msg.mode !== undefined) state.mode = msg.mode;
    if (msg.battery !== undefined) state.battery = msg.battery;
    if (msg.charging !== undefined) state.charging = msg.charging;
    if (msg.device_mode !== undefined) state.deviceMode = msg.device_mode;
    if (msg.showcase !== undefined) state.showcase = msg.showcase;
    if (msg.focus_agent !== undefined) state.focusAgent = msg.focus_agent;
    if (msg.battery_known !== undefined) {
      state.batteryKnown = Boolean(msg.battery_known);
    } else if (msg.battery !== undefined) {
      state.batteryKnown = msg.battery !== null;
    }

    if (msg.data_source === "showcase_fallback") {
      badge.className = "badge protected";
      connText.textContent = "保障运行";
    } else if (state.connected && msg.hardware_imu_streaming) {
      badge.className = "badge connected";
      connText.textContent = "路演就绪";
    } else if (state.connected) {
      badge.className = "badge warning";
      connText.textContent = "控制已连接";
    } else {
      badge.className = "badge disconnected";
      connText.textContent = msg.connecting ? "连接中" : "未连接";
    }

    updateConnectionCard(msg);

    if ($("#sys-battery")) $("#sys-battery").textContent = formatBattery(state.battery);
    if ($("#sys-charging")) {
      $("#sys-charging").textContent = state.charging === null || state.charging === undefined
        ? "读取中"
        : state.charging ? "充电中" : "未充电";
    }
    if ($("#sys-mode")) {
      $("#sys-mode").textContent = {
        focus: "🎙️ 专注",
        rest: "☕ 休息",
        sleep: "🌙 睡眠",
        unknown: "❓ 未知",
      }[state.mode] || state.mode;
    }
    updateDeviceMode(state.deviceMode);
    updateIMUStreamStatus(msg);
    renderShowcase(msg.showcase);
    renderFocusAgent(msg.focus_agent);
  }

  function formatAgentElapsed(seconds) {
    const value = Math.max(0, Number(seconds) || 0);
    const minutes = Math.floor(value / 60);
    const rest = Math.floor(value % 60);
    return minutes ? minutes + "分" + rest + "秒" : rest + "秒";
  }

  function renderFocusAgent(agent) {
    if (!agent) return;
    state.focusAgent = agent;
    const active = Boolean(agent.session_active);
    const focused = agent.customer_state === "focused";
    if ($("#agent-session-state")) {
      $("#agent-session-state").textContent = active ? "专注进行中" : "尚未开始";
    }
    if ($("#agent-session-time")) {
      $("#agent-session-time").textContent = active
        ? "已进行 " + formatAgentElapsed(agent.session_elapsed_s)
        : "三击戒指开始 · 四击结束";
    }
    if ($("#agent-customer-state")) {
      $("#agent-customer-state").textContent = focused ? "专注" : "非专注";
    }
    if ($("#agent-customer-card")) {
      $("#agent-customer-card").dataset.state = focused ? "focused" : "distracted";
    }
    if ($("#agent-state-source")) {
      $("#agent-state-source").textContent = agent.state_source === "imu_model"
        ? "已有模型 · 真实 IMU 实时判断"
        : agent.model_ready
          ? "已有模型 · 等待真实 IMU 数据"
          : "分类模型未加载";
    }
    if ($("#agent-action")) $("#agent-action").textContent = agent.agent_action || "等待专注会话";
    if ($("#agent-balance")) $("#agent-balance").textContent = Number(agent.balance || 0).toFixed(1);
    if ($("#agent-harvest")) $("#agent-harvest").textContent = String(agent.harvest || 0);
    if ($("#agent-penalty")) $("#agent-penalty").textContent = String(agent.penalty || 0);
    const growth = Math.max(0, Math.min(100, Number(agent.growth) || 0));
    if ($("#agent-growth-label")) $("#agent-growth-label").textContent = Math.round(growth) + "%";
    if ($("#agent-growth-fill")) $("#agent-growth-fill").style.width = growth + "%";
    if ($("#agent-button-pattern")) {
      $("#agent-button-pattern").textContent =
        (agent.last_button_pattern || "等待三击或四击") +
        " · " + (agent.last_session_trigger || "尚未开始");
    }
    const startButton = $('[data-agent-action="start"]');
    const endButton = $('[data-agent-action="end"]');
    if (startButton) startButton.disabled = active;
    if (endButton) endButton.disabled = !active;
  }

  async function updateFocusAgent(payload) {
    try {
      const resp = await fetch("/api/focus-agent", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || "更新失败");
      renderFocusAgent(data);
    } catch (error) {
      alert("Agent 调试失败：" + error.message);
    }
  }

  function handleSystem(msg) {
    applyStatus(msg);
  }

  function updateConnectionCard(status) {
    const data = status || {};
    const connected = Boolean(data.connected);
    const connecting = Boolean(data.connecting);
    const title = $("#connection-title");
    const message = $("#connection-message");
    const macInput = $("#mac-input");
    const reportedMac = String(data.mac || "").trim().toUpperCase();
    const deviceMode = data.device_mode || state.deviceMode;
    state.connected = connected;
    state.connecting = connecting;
    if (reportedMac) state.activeMac = reportedMac;
    if (title) title.textContent = connecting ? "正在连接" : connected ? "已连接" : "等待连接";
    if (message) {
      if (connecting) {
        message.textContent = "正在建立 BLE 通道，请让戒指保持在附近。";
      } else if (data.data_source === "showcase_fallback") {
        message.textContent = "路演保障数据正在运行；连接真实戒指后会自动尝试交接。";
      } else if (data.imu_state === "device_no_data") {
        message.textContent = "连续 3 次恢复仍无真实 0x0605，已暂停自动命令；请重启戒指后重新连接。";
      } else if (!connected) {
        message.textContent = "可扫描附近设备，或直接输入 MAC 地址。";
      } else if (data.imu_state === "idle") {
        message.textContent = "蓝牙控制已连接；切换页面不会启动高频IMU，开始标注时将按需开启。";
      } else if (deviceMode === "recording") {
        message.textContent = "戒指处于录音模式；如需实时 IMU，请单击一次按键切换到手势模式。";
      } else if (deviceMode === "switching") {
        message.textContent = "已检测到模式变化，后台正在确认戒指工作模式。";
      } else if (deviceMode === "gesture" && data.hardware_imu_streaming === false) {
        message.textContent = "控制通道已连接，但真实 IMU 未就绪；请查看路演预检。";
      } else {
        message.textContent = "蓝牙通道和手势数据流已就绪。";
      }
    }
    if (
      macInput
      && reportedMac
      && document.activeElement !== macInput
      && macInput.dataset.dirty !== "true"
    ) {
      macInput.value = reportedMac;
    }
    if ($("#sys-mac")) $("#sys-mac").textContent = reportedMac || state.activeMac || "--";
    refreshConnectButton();
    if ($("#btn-disconnect")) $("#btn-disconnect").disabled = connecting || !connected;
  }

  function refreshConnectButton() {
    const button = $("#btn-connect");
    const macInput = $("#mac-input");
    if (!button) return;
    const enteredMac = String(macInput?.value || "").trim().toUpperCase();
    const switching = state.connected && enteredMac && enteredMac !== state.activeMac;
    button.disabled = state.connecting || (state.connected && !switching);
    button.textContent = state.connecting ? "连接中..." : switching ? "切换戒指" : "连接";
  }

  function renderShowcase(showcase) {
    if (!showcase) return;
    state.showcase = showcase;
    const card = $("#showcase-card");
    const title = $("#showcase-status");
    const summary = $("#showcase-summary");
    const checks = $("#showcase-checks");
    const toggle = $("#btn-showcase-toggle");
    if (card) card.dataset.grade = showcase.grade || "not_ready";
    if (title) {
      title.textContent = {
        hardware_ready: "真实链路就绪",
        waiting_hardware: "等待首次真实六轴数据",
        protected: showcase.active ? "保障数据接管中" : "保障模式待命",
        not_ready: "尚未通过预检",
      }[showcase.grade] || "等待预检";
    }
    if (summary) summary.textContent = showcase.summary || "";
    if (toggle) {
      toggle.textContent = showcase.enabled ? "关闭保障模式" : "启用保障模式";
      toggle.classList.toggle("primary", !showcase.enabled);
    }
    if (checks) {
      checks.innerHTML = (showcase.checks || []).map((item) =>
        '<div class="showcase-check" data-ok="' + (item.ok ? "true" : "false") + '">' +
          '<span class="showcase-check-icon">' + (item.ok ? "✓" : "!") + '</span>' +
          '<span><strong>' + escapeHtml(item.label) + '</strong><small>' +
          escapeHtml(item.detail) + "</small></span></div>"
      ).join("");
    }
  }

  function handleEvent(msg) {
    const evt = msg.event;
    if (["rotate_back", "rotate_front", "wave"].includes(evt)) {
      state.gestureCounts[evt] = (state.gestureCounts[evt] || 0) + 1;
      updateGestureCounts();
      addGestureLog(evt, msg.ts);
    }
    if (evt === "single_click") {
      state.gestureCounts.single_click = (state.gestureCounts.single_click || 0) + 1;
      $("#count-click").textContent = state.gestureCounts.single_click;
      addLog("click", "🔘 单击");
    }
    if (evt === "double_click") {
      state.gestureCounts.double_click = (state.gestureCounts.double_click || 0) + 1;
      $("#count-double").textContent = state.gestureCounts.double_click;
      addLog("double", "🔘🔘 双击");
    }
  }

  function updateGestureCounts() {
    $("#count-rotate-back").textContent = state.gestureCounts.rotate_back || 0;
    $("#count-rotate-front").textContent = state.gestureCounts.rotate_front || 0;
    $("#count-wave").textContent = state.gestureCounts.wave || 0;
  }

  // ═══════════════════════════════════════
  //  IMU charts
  // ═══════════════════════════════════════

  function updateIMU(msg) {
    // 这里的 msg 是聚合帧，不包含原始采样点
    // 仅更新最新数值显示
    // 如需原始 IMU 数据可视化，需扩展 WS 协议
  }

  function updateRawIMU(msg) {
    state.imuStreaming = true;
    state.imuLastSampleAge = 0;
    if (msg.data_source) state.dataSource = msg.data_source;
    updateIMUStreamStatus({
      imu_streaming: true,
      imu_last_sample_age_s: 0,
      data_source: state.dataSource,
    });
    const accel = [msg.accel_x, msg.accel_y, msg.accel_z].map(Number);
    const gyro = [msg.gyro_x, msg.gyro_y, msg.gyro_z].map(Number);
    accel.forEach((value, index) => {
      state.accelHistory[index].push(value);
      if (state.accelHistory[index].length > state.maxHistory) {
        state.accelHistory[index].shift();
      }
    });
    gyro.forEach((value, index) => {
      state.gyroHistory[index].push(value);
      if (state.gyroHistory[index].length > state.maxHistory) {
        state.gyroHistory[index].shift();
      }
    });
    if ($("#imu-accel")) $("#imu-accel").textContent = accel.join(" / ");
    if ($("#imu-gyro")) $("#imu-gyro").textContent = gyro.join(" / ");
    if ($("#panel-imu")?.classList.contains("active")) drawCharts();
  }

  function drawCharts() {
    drawChart("chart-accel", state.accelHistory, ["#6b9b6f", "#c4965e", "#6b8fc4"], ["AX", "AY", "AZ"]);
    drawChart("chart-gyro", state.gyroHistory, ["#c46b5e", "#8b6b9f", "#5e9b9b"], ["GX", "GY", "GZ"]);
  }

  function drawChart(canvasId, dataArrays, colors, labels) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const w = rect.width;
    const h = rect.height || 130;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    ctx.scale(dpr, dpr);

    ctx.clearRect(0, 0, w, h);

    // 画网格
    ctx.strokeStyle = "#eee";
    ctx.lineWidth = 0.5;
    for (let i = 0; i < 4; i++) {
      const y = (h / 4) * i;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
    }

    // 画数据线
    dataArrays.forEach((data, idx) => {
      if (data.length < 2) return;
      ctx.strokeStyle = colors[idx];
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      const stepX = w / Math.max(1, data.length - 1);
      const mid = h / 2;
      const maxVal = Math.max(1, ...data.map(Math.abs));
      data.forEach((v, i) => {
        const x = i * stepX;
        const y = mid - (v / maxVal) * (h * 0.4);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
    });

    // 图例
    ctx.font = "10px sans-serif";
    labels.forEach((label, i) => {
      ctx.fillStyle = colors[i];
      ctx.fillText(label, 8 + i * 40, 12);
    });
  }

  // ═══════════════════════════════════════
  //  Log helpers
  // ═══════════════════════════════════════

  function addLog(type, msg) {
    const container = $("#event-log");
    if (!container) return;
    if (container.querySelector(".log-empty")) container.innerHTML = "";
    const now = new Date();
    const time = now.toTimeString().slice(0, 8);
    const entry = document.createElement("div");
    entry.className = "log-entry";
    entry.innerHTML = '<span class="log-time">' + time + "</span><span class=\"log-msg\">" + msg + "</span>";
    container.prepend(entry);
    if (container.children.length > 100) container.lastChild.remove();
  }

  function addGestureLog(name, ts) {
    const names = { rotate_back: "↩️ 向后旋转", rotate_front: "↪️ 向前旋转", wave: "👋 挥手" };
    const container = $("#gesture-log");
    if (!container) return;
    if (container.querySelector(".log-empty")) container.innerHTML = "";
    const now = new Date();
    const time = now.toTimeString().slice(0, 8);
    const entry = document.createElement("div");
    entry.className = "log-entry";
    entry.innerHTML = '<span class="log-time">' + time + "</span><span class=\"log-msg\">" + (names[name] || name) + "</span>";
    container.prepend(entry);
    if (container.children.length > 100) container.lastChild.remove();
  }

  // ═══════════════════════════════════════
  //  Buttons
  // ═══════════════════════════════════════

  $("#btn-scan")?.addEventListener("click", async () => {
    $("#device-list").innerHTML = '<div class="log-empty">扫描中...</div>';
    try {
      const resp = await fetch("/api/scan");
      const data = await resp.json();
      if (data.devices && data.devices.length > 0) {
        $("#device-list").innerHTML = data.devices
          .map((d) => {
            const name = d.name || "(无名称)";
            const rssi = d.rssi !== null ? d.rssi + " dBm" : "";
            return '<div class="device-entry">' +
              '<div><div>' + name + '</div><div class="mac">' + d.address + '</div></div>' +
              '<div style="text-align:right"><div>' + rssi + '</div>' +
              '<button class="btn" data-connect-mac="' + d.address + '">连接</button></div>' +
              "</div>";
          })
          .join("");
        $$('[data-connect-mac]').forEach((button) => {
          button.addEventListener("click", () => connectRing(button.dataset.connectMac));
        });
      } else {
        $("#device-list").innerHTML = '<div class="log-empty">未找到设备<br><small>确保戒指在广播中</small></div>';
      }
    } catch (e) {
      $("#device-list").innerHTML = '<div class="log-empty">扫描失败: ' + e.message + "</div>";
    }
  });

  async function connectRing(selectedMac) {
    const mac = String(selectedMac || $("#mac-input")?.value || "").trim().toUpperCase();
    if (!mac) { alert("请输入戒指 MAC 地址"); return; }
    if (!/^([0-9A-F]{2}:){5}[0-9A-F]{2}$/.test(mac)) {
      alert("蓝牙地址格式不正确，请输入类似 AA:BB:CC:DD:EE:FF 的地址");
      return;
    }
    const macInput = $("#mac-input");
    if (macInput) {
      macInput.value = mac;
      macInput.dataset.dirty = "true";
    }
    const button = $("#btn-connect");
    if (button) { button.disabled = true; button.textContent = "连接中..."; }
    updateConnectionCard({ connecting: true, connected: false, mac });
    try {
      const resp = await fetch("/api/connect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mac }),
      });
      const data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || "连接失败");
      state.connected = Boolean(data.connected);
      state.connecting = false;
      state.activeMac = String(data.mac || mac).trim().toUpperCase();
      if (macInput) macInput.dataset.dirty = "false";
      updateConnectionCard(data);
      badge.className = "badge connected";
      connText.textContent = "已连接";
      addLog("system", "戒指蓝牙已连接：" + mac);
    } catch (e) {
      state.connecting = false;
      updateConnectionCard({ connected: false, mac });
      alert("蓝牙连接失败：" + e.message);
    } finally {
      refreshConnectButton();
    }
  }

  async function disconnectRing() {
    const button = $("#btn-disconnect");
    if (button) { button.disabled = true; button.textContent = "断开中..."; }
    try {
      const resp = await fetch("/api/disconnect", { method: "POST" });
      const data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || "断开失败");
      state.connected = false;
      updateConnectionCard(data);
      badge.className = "badge disconnected";
      connText.textContent = "未连接";
      addLog("system", "戒指蓝牙已断开");
    } catch (e) {
      alert("断开失败：" + e.message);
    } finally {
      if (button) { button.disabled = false; button.textContent = "断开"; }
    }
  }

  $("#btn-connect")?.addEventListener("click", () => connectRing());
  $("#mac-input")?.addEventListener("input", (event) => {
    event.target.dataset.dirty = "true";
    event.target.value = event.target.value.toUpperCase().replace(/[^0-9A-F:]/g, "");
    refreshConnectButton();
  });
  $("#mac-input")?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") connectRing();
  });
  $("#btn-disconnect")?.addEventListener("click", disconnectRing);
  $$("[data-agent-action]").forEach((button) => {
    button.addEventListener("click", () => {
      updateFocusAgent({ action: button.dataset.agentAction });
    });
  });

  $("#btn-system-refresh")?.addEventListener("click", async () => {
    try {
      const resp = await fetch("/api/status");
      const data = await resp.json();
      applyStatus(data);
      addLog("system", "状态已刷新");
    } catch (e) {
      addLog("error", "刷新失败: " + e.message);
    }
  });

  $("#btn-imu-retry")?.addEventListener("click", async () => {
    const button = $("#btn-imu-retry");
    if (button) { button.disabled = true; button.textContent = "正在重新请求..."; }
    try {
      const resp = await fetch("/api/imu/start", { method: "POST" });
      const data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || "重新请求失败");
      addLog("system", data.message || "已重新请求真实 IMU");
      const statusResp = await fetch("/api/status");
      applyStatus(await statusResp.json());
    } catch (error) {
      alert("真实 IMU 请求失败：" + error.message);
    } finally {
      if (button) {
        button.disabled = false;
        button.textContent = "红灯已亮，重新请求真实 IMU";
      }
    }
  });

  $("#btn-showcase-preflight")?.addEventListener("click", async () => {
    const button = $("#btn-showcase-preflight");
    if (button) { button.disabled = true; button.textContent = "检查中..."; }
    try {
      const resp = await fetch("/api/status");
      const data = await resp.json();
      applyStatus(data);
      addLog("system", "已完成路演预检：" + (data.showcase?.summary || "未知"));
    } catch (e) {
      alert("预检失败：" + e.message);
    } finally {
      if (button) { button.disabled = false; button.textContent = "运行路演预检"; }
    }
  });

  $("#btn-showcase-toggle")?.addEventListener("click", async () => {
    const enabled = !Boolean(state.showcase?.enabled);
    const warning = enabled
      ? "保障模式只在真实 IMU 中断 3 秒后接管，并会明确标记为备用演示数据。是否启用？"
      : "关闭保障模式后，真实 IMU 中断将直接停止数据展示。是否关闭？";
    if (!confirm(warning)) return;
    try {
      const resp = await fetch("/api/showcase", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
      const data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || "设置失败");
      renderShowcase(data);
      addLog("system", enabled ? "路演保障模式已启用" : "路演保障模式已关闭");
    } catch (e) {
      alert("保障模式设置失败：" + e.message);
    }
  });

  // ═══════════════════════════════════════
  //  初始化
  // ═══════════════════════════════════════

  const captureLabelNames = {
    focused: "专注",
    distracted: "非专注",
    uncertain: "历史标签：不确定",
  };
  let pendingDeleteSessionId = null;
  let pendingDeleteExpiresAt = 0;

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[char]);
  }

  function formatCaptureDuration(ms) {
    const seconds = Math.max(0, Math.floor(Number(ms || 0) / 1000));
    const minutes = Math.floor(seconds / 60);
    return minutes ? minutes + "分" + (seconds % 60) + "秒" : seconds + "秒";
  }

  function renderCaptureStatus(data) {
    const active = data.active;
    state.captureActive = Boolean(active);
    const live = $("#capture-live");
    if (!live) return;
    live.dataset.active = active ? "true" : "false";
    $("#capture-live-title").textContent = active
      ? "正在采集：" + (captureLabelNames[active.label] || active.label)
      : "尚未开始采集";
    $("#capture-live-detail").textContent = active
      ? formatCaptureDuration(active.elapsed_ms) + " · " + active.sample_count + " 个六轴样本"
      : "选择上方一种状态开始记录。";
    $("#btn-capture-stop").disabled = !active;
    $$("[data-capture-label]").forEach((button) => { button.disabled = Boolean(active); });

    ["focused", "distracted"].forEach((label) => {
      const summary = data.summary?.[label] || { samples: 0 };
      const target = $("#capture-total-" + label);
      if (target) target.textContent = Number(summary.samples || 0).toLocaleString();
    });
    if ($("#capture-db-note")) {
      $("#capture-db-note").textContent = "本机数据：" + (data.db_path || "data.db");
    }

    const recent = $("#capture-recent");
    const rows = data.recent || [];
    if (!recent) return;
    if (!rows.length) {
      recent.innerHTML = '<div class="log-empty">暂无采集记录</div>';
      return;
    }
    recent.innerHTML = rows.map((row) => {
      const end = row.ended_at_ms || Date.now();
      const duration = formatCaptureDuration(end - row.started_at_ms);
      const running = row.ended_at_ms ? "" : " · 进行中";
      const deleteDisabled = active?.id === row.id ? " disabled" : "";
      const confirmingDelete =
        pendingDeleteSessionId === row.id && Date.now() < pendingDeleteExpiresAt;
      const deleteClass = confirmingDelete
        ? "capture-delete confirming"
        : "capture-delete";
      const deleteText = confirmingDelete ? "再次点击确认" : "删除片段";
      return '<div class="capture-session">' +
        '<div class="capture-session-main">#' + row.id + " · " +
        escapeHtml(captureLabelNames[row.label] || row.label) + " · " +
        escapeHtml(row.task_type) + '</div>' +
        '<div class="capture-session-meta">' + escapeHtml(row.user_id) + " · " +
        escapeHtml(row.hand === "left" ? "左手" : "右手") + " · " +
        duration + running + '</div>' +
        '<div class="capture-session-count"><strong>' +
        Number(row.sample_count || 0).toLocaleString() +
        '</strong><span>样本</span>' +
        '<button type="button" class="' + deleteClass +
        '" data-delete-capture="' + row.id + '"' +
        deleteDisabled + '>' + deleteText + '</button></div></div>';
    }).join("");
  }

  async function refreshCaptureStatus() {
    try {
      const resp = await fetch("/api/capture");
      const data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || "读取失败");
      renderCaptureStatus(data);
    } catch (error) {
      if ($("#capture-live-detail")) {
        $("#capture-live-detail").textContent = "采集服务不可用：" + error.message;
      }
    }
  }

  async function deleteCaptureSession(sessionId) {
    if (!Number.isInteger(sessionId) || sessionId <= 0) return;
    const now = Date.now();
    if (pendingDeleteSessionId !== sessionId || now >= pendingDeleteExpiresAt) {
      pendingDeleteSessionId = sessionId;
      pendingDeleteExpiresAt = now + 6000;
      const firstButton = $('[data-delete-capture="' + sessionId + '"]');
      if (firstButton) {
        firstButton.classList.add("confirming");
        firstButton.textContent = "再次点击确认";
      }
      return;
    }
    pendingDeleteSessionId = null;
    pendingDeleteExpiresAt = 0;
    const button = $('[data-delete-capture="' + sessionId + '"]');
    if (button) {
      button.disabled = true;
      button.textContent = "删除中";
    }
    try {
      const resp = await fetch("/api/capture/" + sessionId, { method: "DELETE" });
      const data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || "删除失败");
      addLog(
        "capture",
        "已删除片段 #" + sessionId + " 和 " + data.deleted_samples + " 个样本",
      );
      await refreshCaptureStatus();
    } catch (error) {
      alert("无法删除采集片段：" + error.message);
      await refreshCaptureStatus();
    }
  }

  $("#capture-recent")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-delete-capture]");
    if (!button || button.disabled) return;
    deleteCaptureSession(Number(button.dataset.deleteCapture));
  });

  async function startLabeledCapture(label) {
    try {
      const statusResp = await fetch("/api/status");
      const ringStatus = await statusResp.json();
      if (!ringStatus.connected) {
        alert("戒指尚未连接。请先到“系统”页面连接戒指，再开始采集。");
        return;
      }
      const payload = {
        label,
        user_id: $("#capture-user")?.value || "",
        task_type: $("#capture-task")?.value || "",
        hand: $("#capture-hand")?.value || "right",
        orientation: $("#capture-orientation")?.value || "neutral",
        notes: $("#capture-notes")?.value || "",
      };
      const resp = await fetch("/api/capture/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || "开始采集失败");
      addLog("capture", "开始采集：" + captureLabelNames[label]);
      await refreshCaptureStatus();
    } catch (error) {
      alert("无法开始采集：" + error.message);
    }
  }

  async function stopLabeledCapture() {
    const button = $("#btn-capture-stop");
    if (button) button.disabled = true;
    try {
      const resp = await fetch("/api/capture/stop", { method: "POST" });
      const data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || "停止采集失败");
      addLog(
        "capture",
        "标注片段已保存：" + data.session.sample_count + " 个六轴样本",
      );
      await refreshCaptureStatus();
    } catch (error) {
      alert("无法停止采集：" + error.message);
      await refreshCaptureStatus();
    }
  }

  $$("[data-capture-label]").forEach((button) => {
    button.addEventListener("click", () => startLabeledCapture(button.dataset.captureLabel));
  });
  $("#btn-capture-stop")?.addEventListener("click", stopLabeledCapture);
  $("#btn-capture-refresh")?.addEventListener("click", refreshCaptureStatus);

  function init() {
    connectWS();
    drawCharts();
    refreshCaptureStatus();
    setInterval(() => {
      if (state.activeTab === "capture" || state.captureActive) {
        refreshCaptureStatus();
      }
    }, 1000);
    // 定时刷新仪表盘
    setInterval(async () => {
      try {
        const resp = await fetch("/api/status");
        const data = await resp.json();
        applyStatus(data);
      } catch (e) { /* ignore */ }
    }, 5000);
  }

  // 页面加载完毕后初始化
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
