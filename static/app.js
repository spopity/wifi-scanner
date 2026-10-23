/**
 * app.js
 * ======
 * Frontend logic for the Wi-Fi House Walk-Test & Coverage Scanner.
 */

const state = {
  pollIntervalMs: 1000,
  paused: false,
  demoMode: false,
  audioEnabled: false,
  voiceEnabled: false,
  currentRoom: "Living Room",
  floors: ["Ground Floor"],
  currentFloor: "Ground Floor",
  latest: null,
  history: [],
  roomStats: [],
  advisor: [],
  speedTests: {},
  roamingEvents: [],
  heatmapPoints: [],
  neighbors: [],
  channelUsage: { band_counts: {}, channels: [] },
  floorplanImgs: {}, // floor name -> uploaded Image
  lastRoamCount: 0,
  lastDeadZoneSpokenAt: 0,
};

let pollTimer = null;
let audioCtx = null;

// ============================================================================
// Initialization & Event Listeners
// ============================================================================

window.addEventListener("DOMContentLoaded", () => {
  bindControls();
  restorePersistedFloorplans();
  startPolling();
  fetchNeighbors(false);
});

function bindControls() {
  // Scan Rate selector
  const pollSelect = document.getElementById("pollSelect");
  pollSelect.addEventListener("change", async (e) => {
    const sec = parseFloat(e.target.value);
    state.pollIntervalMs = Math.round(sec * 1000);
    await postJSON("/api/config", { poll_interval: sec });
    startPolling();
  });

  // Pause / Resume button
  const btnPause = document.getElementById("btnPause");
  btnPause.addEventListener("click", async () => {
    state.paused = !state.paused;
    await postJSON("/api/config", { paused: state.paused });
    updateControlButtons();
  });

  // Audio Beep (Geiger tone) button
  const btnAudio = document.getElementById("btnAudio");
  btnAudio.addEventListener("click", () => {
    state.audioEnabled = !state.audioEnabled;
    if (state.audioEnabled && !audioCtx) {
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    }
    updateControlButtons();
  });

  // Voice Announcements (TTS) button
  const btnVoice = document.getElementById("btnVoice");
  btnVoice.addEventListener("click", () => {
    state.voiceEnabled = !state.voiceEnabled;
    updateControlButtons();
    if (state.voiceEnabled) {
      speakText(
        `Voice announcements enabled. Current room ${state.currentRoom}, signal ${
          state.latest ? state.latest.rssi_dbm : -50
        } d B m.`
      );
    }
  });

  // Demo Mode button
  const btnDemo = document.getElementById("btnDemo");
  btnDemo.addEventListener("click", async () => {
    state.demoMode = !state.demoMode;
    await postJSON("/api/config", { demo_mode: state.demoMode });
    updateControlButtons();
    await fetchStatus();
    await fetchNeighbors(false);
  });

  // Save & Load Sessions
  document.getElementById("btnSaveSession").addEventListener("click", async () => {
    const defaultName = `walk_test_${new Date().toTimeString().slice(0, 8).replace(/:/g, "")}`;
    const res = await postJSON("/api/session/save", { name: defaultName });
    if (res && res.ok) {
      speakText("Walk test session saved");
      await fetchStatus();
    }
  });

  document.getElementById("sessionSelect").addEventListener("change", async (e) => {
    const filename = e.target.value;
    if (!filename) return;
    const res = await postJSON("/api/session/load", { filename });
    if (res && res.ok) {
      await restorePersistedFloorplans();
      await fetchStatus();
      speakText(`Loaded session ${filename}`);
    }
    e.target.value = "";
  });

  // Room Speed Test button
  const btnSpeedTest = document.getElementById("btnSpeedTest");
  btnSpeedTest.addEventListener("click", async () => {
    btnSpeedTest.disabled = true;
    const origText = btnSpeedTest.textContent;
    btnSpeedTest.textContent = "⏳ Testing Speed...";
    const banner = document.getElementById("speedTestBanner");
    banner.classList.remove("hidden");
    banner.innerHTML = `<span>Running live HTTP throughput test in <strong>${escapeHtml(state.currentRoom)}</strong>...</span>`;
    try {
      const res = await postJSON("/api/speedtest", {});
      if (res && res.ok) {
        banner.innerHTML = `
          <span><strong>${escapeHtml(res.room)} Speed Test:</strong> <strong style="color:#10b981">${res.download_mbps} Mbps</strong> real download (${res.efficiency_pct}% of ${res.link_speed_mbps} Mbps PHY link)</span>
          <span class="mono" style="color:#94a3b8">${escapeHtml(res.source)}</span>
        `;
        speakText(`${res.room} download speed ${Math.round(res.download_mbps)} megabits per second.`);
        await fetchStatus();
      }
    } finally {
      btnSpeedTest.disabled = false;
      btnSpeedTest.textContent = origText;
    }
  });

  // Clear Session History
  document.getElementById("btnClearSession").addEventListener("click", async () => {
    await postJSON("/api/config", { clear_session: true });
    await fetchStatus();
  });

  // Open Windows 11 Location Settings
  document.getElementById("btnOpenLocationSettings").addEventListener("click", async () => {
    await postJSON("/api/open-location-settings", {});
  });

  // Room Tagging Buttons
  const roomContainer = document.getElementById("roomButtonsContainer");
  roomContainer.addEventListener("click", async (e) => {
    const btn = e.target.closest(".room-btn");
    if (!btn) return;
    await selectRoom(btn.dataset.room);
  });

  // Custom Room Form
  document.getElementById("customRoomForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = document.getElementById("customRoomInput");
    const name = input.value.trim();
    if (!name) return;
    ensureRoomButtonExists(name);
    input.value = "";
    await selectRoom(name);
  });

  // Floorplan Heatmap Canvas Click
  const heatmapCanvas = document.getElementById("heatmapCanvas");
  heatmapCanvas.addEventListener("click", async (e) => {
    const rect = heatmapCanvas.getBoundingClientRect();
    const x = (e.clientX - rect.left) / rect.width;
    const y = (e.clientY - rect.top) / rect.height;
    const res = await postJSON("/api/heatmap/point", {
      action: "add",
      x,
      y,
      room: state.currentRoom,
    });
    if (res && res.points) {
      state.heatmapPoints = res.points;
      drawFloorplanHeatmap();
    }
  });

  // Undo & Clear Heatmap
  document.getElementById("btnUndoPoint").addEventListener("click", async () => {
    const res = await postJSON("/api/heatmap/point", { action: "undo" });
    if (res && res.points) {
      state.heatmapPoints = res.points;
      drawFloorplanHeatmap();
    }
  });

  document.getElementById("btnClearHeatmap").addEventListener("click", async () => {
    const res = await postJSON("/api/heatmap/point", { action: "clear" });
    if (res && res.points) {
      state.heatmapPoints = res.points;
      drawFloorplanHeatmap();
    }
  });

  // Custom Floorplan Upload (applies to the selected floor)
  document.getElementById("floorplanUpload").addEventListener("change", (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const floor = state.currentFloor;
    const reader = new FileReader();
    reader.onload = async (ev) => {
      const dataUrl = ev.target.result;
      const img = new Image();
      img.onload = () => {
        state.floorplanImgs[floor] = img;
        drawFloorplanHeatmap();
      };
      img.src = dataUrl;
      await postJSON("/api/floorplan", { floor, floorplan_data_url: dataUrl });
    };
    reader.readAsDataURL(file);
    e.target.value = "";
  });

  document.getElementById("btnResetFloorplan").addEventListener("click", async () => {
    const floor = state.currentFloor;
    delete state.floorplanImgs[floor];
    drawFloorplanHeatmap();
    await postJSON("/api/floorplan", { floor, floorplan_data_url: null });
  });

  // Floor tabs & management
  document.getElementById("floorTabs").addEventListener("click", async (e) => {
    const btn = e.target.closest(".room-btn");
    if (!btn || btn.dataset.floor === state.currentFloor) return;
    await floorAction({ action: "select", name: btn.dataset.floor });
    speakText(`${state.currentFloor}.`);
  });

  document.getElementById("btnAddFloor").addEventListener("click", async () => {
    const name = (window.prompt("New floor name (e.g. Basement, Attic):") || "").trim();
    if (!name) return;
    const res = await floorAction({ action: "add", name });
    if (res && !res.ok) window.alert(`A floor named "${name}" already exists.`);
  });

  document.getElementById("btnRenameFloor").addEventListener("click", async () => {
    const oldName = state.currentFloor;
    const newName = (window.prompt(`Rename "${oldName}" to:`, oldName) || "").trim();
    if (!newName || newName === oldName) return;
    const res = await floorAction({ action: "rename", name: oldName, new_name: newName });
    if (res && res.ok && state.floorplanImgs[oldName]) {
      state.floorplanImgs[newName] = state.floorplanImgs[oldName];
      delete state.floorplanImgs[oldName];
      drawFloorplanHeatmap();
    }
  });

  document.getElementById("btnRemoveFloor").addEventListener("click", async () => {
    const name = state.currentFloor;
    if (state.floors.length <= 1) {
      window.alert("You need at least one floor.");
      return;
    }
    if (!window.confirm(`Remove "${name}" with its floorplan and heatmap pins? Signal history is kept.`)) return;
    const res = await floorAction({ action: "remove", name });
    if (res && res.ok) delete state.floorplanImgs[name];
    await fetchStatus();
  });

  // Scan Nearby Networks
  document.getElementById("btnScanNeighbors").addEventListener("click", () => {
    fetchNeighbors(true);
  });
}

async function restorePersistedFloorplans() {
  try {
    const res = await fetch("/api/floorplan");
    if (!res.ok) return;
    const data = await res.json();
    state.floorplanImgs = {};
    Object.entries((data && data.floorplans) || {}).forEach(([floor, dataUrl]) => {
      if (!dataUrl) return;
      const img = new Image();
      img.onload = () => {
        state.floorplanImgs[floor] = img;
        drawFloorplanHeatmap();
      };
      img.src = dataUrl;
    });
    drawFloorplanHeatmap();
  } catch (_) {}
}

async function floorAction(payload) {
  const res = await postJSON("/api/floor", payload);
  if (res && res.floors) {
    applyFloorConfig(res.floors, res.current_floor);
    drawFloorplanHeatmap();
  }
  return res;
}

function applyFloorConfig(floors, currentFloor) {
  state.floors = floors;
  state.currentFloor = currentFloor;
  document.getElementById("activeFloorLabel").textContent = currentFloor;
  renderFloorTabs();
}

function renderFloorTabs() {
  const container = document.getElementById("floorTabs");
  const counts = {};
  (state.heatmapPoints || []).forEach((pt) => {
    counts[pt.floor] = (counts[pt.floor] || 0) + 1;
  });
  const html = state.floors
    .map(
      (f) => `<button class="room-btn${f === state.currentFloor ? " active" : ""}" data-floor="${escapeHtml(f)}">${escapeHtml(f)}<span class="floor-tab-count">${counts[f] || 0} pins</span></button>`
    )
    .join("");
  if (container.innerHTML !== html) container.innerHTML = html;
}

function speakText(text) {
  if (!state.voiceEnabled || !("speechSynthesis" in window)) return;
  try {
    window.speechSynthesis.cancel();
    const utter = new SpeechSynthesisUtterance(text);
    utter.rate = 1.05;
    window.speechSynthesis.speak(utter);
  } catch (_) {}
}

function ensureRoomButtonExists(roomName) {
  const container = document.getElementById("roomButtonsContainer");
  const existing = Array.from(container.querySelectorAll(".room-btn")).find(
    (b) => b.dataset.room.toLowerCase() === roomName.toLowerCase()
  );
  if (!existing) {
    const btn = document.createElement("button");
    btn.className = "room-btn";
    btn.dataset.room = roomName;
    btn.textContent = roomName;
    container.appendChild(btn);
  }
}

async function selectRoom(roomName) {
  state.currentRoom = roomName;
  document.getElementById("activeRoomLabel").textContent = roomName;
  document.querySelectorAll(".room-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.room === roomName);
  });
  await postJSON("/api/room", { room: roomName });
  if (state.latest) {
    speakText(
      `${roomName}. Signal ${state.latest.rssi_dbm} d B m, ${state.latest.quality_label}.`
    );
  }
}

function updateControlButtons() {
  const btnPause = document.getElementById("btnPause");
  const liveDot = document.getElementById("liveIndicator");
  btnPause.textContent = state.paused ? "Resume" : "Pause";
  btnPause.classList.toggle("active-toggle", state.paused);
  liveDot.classList.toggle("paused", state.paused);

  const btnAudio = document.getElementById("btnAudio");
  btnAudio.textContent = `Audio Beep: ${state.audioEnabled ? "ON" : "OFF"}`;
  btnAudio.classList.toggle("active-toggle", state.audioEnabled);

  const btnVoice = document.getElementById("btnVoice");
  if (btnVoice) {
    btnVoice.textContent = `Voice TTS: ${state.voiceEnabled ? "ON" : "OFF"}`;
    btnVoice.classList.toggle("active-toggle", state.voiceEnabled);
  }

  const btnDemo = document.getElementById("btnDemo");
  btnDemo.textContent = `Demo Mode: ${state.demoMode ? "ON" : "OFF"}`;
  btnDemo.classList.toggle("active-toggle", state.demoMode);
}

// ============================================================================
// Polling & Data Fetching
// ============================================================================

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  fetchStatus();
  pollTimer = setInterval(fetchStatus, state.pollIntervalMs);
}

async function fetchStatus() {
  try {
    const res = await fetch("/api/status?limit=180");
    if (!res.ok) return;
    const data = await res.json();

    state.latest = data.latest || null;
    state.history = data.history || [];
    state.roomStats = data.room_stats || [];
    state.advisor = data.advisor || [];
    state.speedTests = data.speed_tests || {};
    state.roamingEvents = data.roaming_events || [];
    state.heatmapPoints = data.heatmap_points || [];

    if (data.config) {
      state.paused = data.config.paused;
      state.demoMode = data.config.demo_mode;
      state.currentRoom = data.config.current_room;
      document.getElementById("activeRoomLabel").textContent = state.currentRoom;
      if (data.config.floors) applyFloorConfig(data.config.floors, data.config.current_floor);
      updateControlButtons();
    }

    renderSavedSessionsDropdown(data.saved_sessions || []);
    renderHUD(data.latest, data.session);
    drawGauge(data.latest ? data.latest.rssi_dbm : -100, data.latest ? data.latest.quality_color : "#64748b");
    drawTimelineChart(state.history);
    drawFloorplanHeatmap();
    renderAdvisor(state.advisor);
    renderRoomLeaderboard(state.roomStats);
    renderRoamingLog(state.roamingEvents);

    // Voice TTS alerts for roaming events or sudden dead-zone entry
    if (state.roamingEvents.length > state.lastRoamCount && state.lastRoamCount > 0) {
      const latestRoam = state.roamingEvents[0];
      speakText(`Roaming detected: ${latestRoam.event_type} in ${latestRoam.room}. Signal now ${latestRoam.rssi_after} d B m.`);
    }
    state.lastRoamCount = state.roamingEvents.length;

    if (
      state.voiceEnabled &&
      data.latest &&
      data.latest.connected &&
      data.latest.rssi_dbm <= -80 &&
      Date.now() - state.lastDeadZoneSpokenAt > 20000
    ) {
      state.lastDeadZoneSpokenAt = Date.now();
      speakText(`Warning: Weak signal in ${state.currentRoom}, ${data.latest.rssi_dbm} d B m.`);
    }

    if (state.audioEnabled && !state.paused && data.latest && data.latest.connected) {
      playSignalTone(data.latest.rssi_dbm);
    }
  } catch (err) {
    console.error("Status fetch error:", err);
  }
}

async function fetchNeighbors(triggerScan = false) {
  const btn = document.getElementById("btnScanNeighbors");
  const prevText = btn.textContent;
  btn.textContent = "Scanning Channels...";
  try {
    const res = await fetch(`/api/neighbors?scan=${triggerScan ? "1" : "0"}`);
    if (!res.ok) return;
    const data = await res.json();
    state.neighbors = data.networks || [];
    state.channelUsage = data.channel_usage || { band_counts: {}, channels: [] };
    renderNeighbors(data);
    drawChannelCongestionChart(state.channelUsage.channels || []);
  } catch (err) {
    console.error("Neighbor scan error:", err);
  } finally {
    btn.textContent = prevText;
  }
}

async function postJSON(url, payload) {
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    return await res.json();
  } catch (err) {
    console.error("POST error:", err);
    return null;
  }
}

// ============================================================================
// HUD, Tables, and Audio Rendering
// ============================================================================

function renderHUD(latest, session) {
  if (!latest) return;

  document.getElementById("adapterTitle").textContent =
    `${latest.adapter_description || "Wi-Fi Adapter"} (${latest.adapter_mac || "Local PC"})`;

  const rssiEl = document.getElementById("rssiValue");
  rssiEl.innerHTML = `${latest.rssi_dbm} <span class="unit">dBm</span>`;
  rssiEl.style.color = latest.quality_color || "#f8fafc";

  document.getElementById("pctValue").textContent = `${latest.signal_pct}% Signal Quality`;

  const badge = document.getElementById("qualityBadge");
  badge.textContent = latest.quality_label || "Unknown";
  badge.style.backgroundColor = `${latest.quality_color}22`;
  badge.style.color = latest.quality_color;
  badge.style.border = `1px solid ${latest.quality_color}`;

  document.getElementById("ssidValue").textContent = latest.ssid || "--";
  document.getElementById("bssidValue").textContent = latest.bssid || "--";
  document.getElementById("bandChannelValue").textContent =
    latest.channel > 0
      ? `${latest.band} • Ch ${latest.channel} (${latest.freq_mhz} MHz)`
      : latest.band || "--";
  document.getElementById("linkSpeedValue").textContent =
    `${latest.tx_mbps} / ${latest.rx_mbps} Mbps`;

  const gwPing = latest.gateway_ping_ms !== null && latest.gateway_ping_ms !== undefined ? `${latest.gateway_ping_ms}` : "--";
  const inetPing = latest.internet_ping_ms !== null && latest.internet_ping_ms !== undefined ? `${latest.internet_ping_ms}` : "--";
  document.getElementById("pingValue").textContent = `${gwPing} / ${inetPing} ms`;

  if (session) {
    const jit = session.jitter_ms !== undefined ? `${session.jitter_ms} ms` : "-- ms";
    const loss = session.loss_pct !== undefined ? `${session.loss_pct}%` : "0%";
    const jlEl = document.getElementById("jitterLossValue");
    jlEl.textContent = `${jit} • ${loss}`;
    jlEl.style.color = (session.loss_pct > 2 || session.jitter_ms > 15) ? "#f59e0b" : "#f8fafc";
  }

  document.getElementById("radioTypeValue").textContent = latest.radio_type || "--";

  if (session && session.total_samples > 0) {
    document.getElementById("sessionMinAvgMax").textContent =
      `${session.min_rssi} / ${session.avg_rssi} / ${session.max_rssi} dBm`;
  }

  // Toggle Windows 11 Location Privacy banner
  const banner = document.getElementById("locationBanner");
  if (!latest.location_permission_granted && !latest.demo_mode) {
    banner.classList.remove("hidden");
  } else {
    banner.classList.add("hidden");
  }
}

function renderSavedSessionsDropdown(sessions) {
  const sel = document.getElementById("sessionSelect");
  if (!sel) return;
  const currentOpts = sel.querySelectorAll("option");
  if (currentOpts.length - 1 === sessions.length) return;
  sel.innerHTML = `<option value="">Load Saved (${sessions.length})...</option>` +
    sessions
      .map(
        (s) =>
          `<option value="${escapeHtml(s.filename)}">${
            s.is_autosave ? "🔄 AutoSave" : escapeHtml(s.filename)
          } (${s.samples} pts, ${s.heatmap_pins} pins)</option>`
      )
      .join("");
}

function renderAdvisor(recs) {
  const container = document.getElementById("advisorList");
  if (!container) return;
  if (!recs || recs.length === 0) {
    container.innerHTML = `<div class="advisor-item info">Walk around and tag rooms to generate placement advice.</div>`;
    return;
  }
  container.innerHTML = recs
    .map(
      (r) => `
      <div class="advisor-item ${escapeHtml(r.severity || "info")}">
        <strong>${escapeHtml(r.title)}</strong>
        <span>${escapeHtml(r.detail)}</span>
      </div>
    `
    )
    .join("");
}

function renderRoomLeaderboard(stats) {
  const tbody = document.getElementById("roomStatsBody");
  if (!stats || stats.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty-cell">Walk around and select rooms to compare coverage.</td></tr>`;
    return;
  }

  tbody.innerHTML = stats
    .map((s) => {
      const pingStr = s.avg_ping_ms !== null && s.avg_ping_ms !== undefined ? `${s.avg_ping_ms}ms` : "--";
      const jitStr = s.jitter_ms !== undefined ? `±${s.jitter_ms}ms` : "";
      const lossStr = s.loss_pct > 0 ? ` (${s.loss_pct}% loss)` : "";
      const speedStr = s.measured_dl_mbps
        ? `${s.avg_tx_mbps} / <strong style="color:#38bdf8">${s.measured_dl_mbps} Mbps</strong>`
        : `${s.avg_tx_mbps} Mbps`;
      return `
      <tr>
        <td><strong>${escapeHtml(s.room)}</strong> <span style="color:#64748b;font-size:0.72rem">(${s.samples})</span>${
          state.floors.length > 1 ? `<br><span style="color:#94a3b8;font-size:0.72rem">${escapeHtml(s.floor || "")}</span>` : ""
        }</td>
        <td class="mono" style="color:${s.quality_color};font-weight:700">${s.avg_rssi_dbm} dBm</td>
        <td class="mono">${s.min_rssi_dbm} / ${s.max_rssi_dbm} dBm</td>
        <td class="mono">${pingStr} ${jitStr}${lossStr}</td>
        <td class="mono">${speedStr}</td>
        <td>
          <span style="color:${s.quality_color};font-weight:700">${escapeHtml(s.quality_label)}</span>
        </td>
      </tr>
    `;
    })
    .join("");
}

function renderRoamingLog(events) {
  document.getElementById("roamCountBadge").textContent = `${events.length} Roams`;
  const tbody = document.getElementById("roamingLogBody");
  if (!events || events.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5" class="empty-cell">No AP roaming or channel switches detected yet.</td></tr>`;
    return;
  }

  tbody.innerHTML = events
    .map((ev) => {
      const diff = (ev.rssi_after || 0) - (ev.rssi_before || 0);
      const diffStr = diff >= 0 ? `+${diff} dBm` : `${diff} dBm`;
      const diffColor = diff >= 0 ? "#10b981" : "#f59e0b";
      return `
        <tr>
          <td class="mono">${escapeHtml(ev.iso_time)}</td>
          <td>${escapeHtml(ev.room)}${state.floors.length > 1 && ev.floor ? ` <span style="color:#94a3b8;font-size:0.72rem">(${escapeHtml(ev.floor)})</span>` : ""}</td>
          <td><strong>${escapeHtml(ev.event_type)}</strong></td>
          <td class="mono">${escapeHtml(ev.from_bssid)} &rarr; ${escapeHtml(ev.to_bssid)}</td>
          <td class="mono" style="color:${diffColor}">${ev.rssi_before} &rarr; ${ev.rssi_after} dBm (${diffStr})</td>
        </tr>
      `;
    })
    .join("");
}

function renderNeighbors(data) {
  const counts = (data.channel_usage && data.channel_usage.band_counts) || {};
  document.getElementById("bandCount24").textContent = `2.4 GHz: ${counts["2.4 GHz"] || 0}`;
  document.getElementById("bandCount5").textContent = `5 GHz: ${counts["5 GHz"] || 0}`;
  document.getElementById("bandCount6").textContent = `6 GHz: ${counts["6 GHz"] || 0}`;

  const nets = data.networks || [];
  const tbody = document.getElementById("neighborTableBody");
  if (nets.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty-cell">${escapeHtml(data.message || "No networks found.")}</td></tr>`;
    return;
  }

  tbody.innerHTML = nets
    .map((n) => {
      const color = n.rssi_dbm >= -60 ? "#10b981" : n.rssi_dbm >= -72 ? "#f59e0b" : "#ef4444";
      const activeTag = n.is_connected ? ` <span style="color:#38bdf8;font-size:0.72rem">[CONNECTED]</span>` : "";
      return `
        <tr>
          <td><strong>${escapeHtml(n.ssid)}</strong>${activeTag}</td>
          <td class="mono">${escapeHtml(n.bssid)}</td>
          <td class="mono" style="color:${color};font-weight:700">${n.rssi_dbm} dBm (${n.signal_pct}%)</td>
          <td>${escapeHtml(n.band)}</td>
          <td class="mono">${n.channel}</td>
          <td>${escapeHtml(n.radio_type || "")}</td>
        </tr>
      `;
    })
    .join("");
}

function playSignalTone(rssiDbm) {
  if (!audioCtx) return;
  try {
    // Map -90 dBm .. -35 dBm to 220 Hz .. 880 Hz
    const clamped = Math.max(-90, Math.min(-35, rssiDbm));
    const norm = (clamped + 90) / 55;
    const freq = 220 + norm * 660;

    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.type = "sine";
    osc.frequency.setValueAtTime(freq, audioCtx.currentTime);

    gain.gain.setValueAtTime(0.04, audioCtx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + 0.12);

    osc.connect(gain);
    gain.connect(audioCtx.destination);
    osc.start();
    osc.stop(audioCtx.currentTime + 0.12);
  } catch (_) {}
}

// ============================================================================
// Canvas Visualizations (Gauge, Timeline, Floorplan Heatmap, Spectrum)
// ============================================================================

function drawGauge(rssiDbm, color) {
  const canvas = document.getElementById("gaugeCanvas");
  const ctx = canvas.getContext("2d");
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);

  const cx = w / 2;
  const cy = h - 18;
  const radius = 88;
  const startAngle = Math.PI * 0.95;
  const endAngle = Math.PI * 2.05;

  // Track background arc
  ctx.beginPath();
  ctx.arc(cx, cy, radius, startAngle, endAngle, false);
  ctx.lineWidth = 16;
  ctx.lineCap = "round";
  ctx.strokeStyle = "#1e293b";
  ctx.stroke();

  // Normalized fill (-95 dBm = 0, -35 dBm = 1)
  const norm = Math.max(0, Math.min(1, (rssiDbm + 95) / 60));
  const fillAngle = startAngle + norm * (endAngle - startAngle);

  ctx.beginPath();
  ctx.arc(cx, cy, radius, startAngle, fillAngle, false);
  ctx.lineWidth = 16;
  ctx.lineCap = "round";
  ctx.strokeStyle = color || "#38bdf8";
  ctx.stroke();
}

function drawTimelineChart(history) {
  const canvas = document.getElementById("timelineCanvas");
  const ctx = canvas.getContext("2d");
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);

  const padL = 52;
  const padR = 56;
  const padT = 22;
  const padB = 30;
  const plotW = w - padL - padR;
  const plotH = h - padT - padB;

  const minDbm = -95;
  const maxDbm = -30;
  const dbmToY = (dbm) => {
    const clamped = Math.max(minDbm, Math.min(maxDbm, dbm));
    return padT + ((maxDbm - clamped) / (maxDbm - minDbm)) * plotH;
  };

  // Horizontal reference bands & grid lines
  const refLines = [-40, -55, -67, -75, -85];
  ctx.font = "11px Consolas, monospace";
  refLines.forEach((dbm) => {
    const y = dbmToY(dbm);
    ctx.strokeStyle = dbm === -67 ? "rgba(34,197,94,0.35)" : dbm === -75 ? "rgba(239,68,68,0.35)" : "#1e293b";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(w - padR, y);
    ctx.stroke();

    ctx.fillStyle = "#94a3b8";
    ctx.textAlign = "right";
    ctx.fillText(`${dbm} dBm`, padL - 8, y + 4);
  });

  if (!history || history.length === 0) {
    ctx.fillStyle = "#64748b";
    ctx.textAlign = "center";
    ctx.fillText("Waiting for signal readings...", w / 2, h / 2);
    return;
  }

  const count = history.length;
  const stepX = plotW / Math.max(1, count - 1);

  // Draw room transition markers
  let prevRoom = null;
  let prevBssid = null;
  history.forEach((pt, idx) => {
    const x = padL + idx * stepX;
    if (prevRoom !== null && pt.room !== prevRoom) {
      ctx.save();
      ctx.strokeStyle = "rgba(99, 102, 241, 0.65)";
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(x, padT);
      ctx.lineTo(x, h - padB);
      ctx.stroke();
      ctx.fillStyle = "#a5b4fc";
      ctx.textAlign = "left";
      ctx.fillText(`📍 ${pt.room}`, x + 4, padT + 12);
      ctx.restore();
    } else if (prevBssid !== null && pt.bssid !== prevBssid) {
      ctx.save();
      ctx.strokeStyle = "rgba(245, 158, 11, 0.75)";
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(x, padT);
      ctx.lineTo(x, h - padB);
      ctx.stroke();
      ctx.fillStyle = "#fcd34d";
      ctx.textAlign = "left";
      ctx.fillText(`🔄 Roam`, x + 4, padT + 26);
      ctx.restore();
    }
    prevRoom = pt.room;
    prevBssid = pt.bssid;
  });

  // Draw Link Speed line (right axis 0..1500 Mbps)
  const maxSpeed = 1500;
  ctx.beginPath();
  history.forEach((pt, idx) => {
    const x = padL + idx * stepX;
    const spd = Math.min(maxSpeed, pt.tx_mbps || 0);
    const y = padT + (1 - spd / maxSpeed) * plotH;
    if (idx === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = "rgba(168, 85, 247, 0.65)";
  ctx.lineWidth = 1.8;
  ctx.stroke();

  // Draw RSSI (dBm) area + line
  ctx.beginPath();
  history.forEach((pt, idx) => {
    const x = padL + idx * stepX;
    const y = dbmToY(pt.rssi_dbm);
    if (idx === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = "#38bdf8";
  ctx.lineWidth = 2.6;
  ctx.stroke();

  // Fill gradient under RSSI curve
  const grad = ctx.createLinearGradient(0, padT, 0, h - padB);
  grad.addColorStop(0, "rgba(56, 189, 248, 0.28)");
  grad.addColorStop(1, "rgba(56, 189, 248, 0.01)");
  ctx.lineTo(padL + (count - 1) * stepX, h - padB);
  ctx.lineTo(padL, h - padB);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  // Draw time labels along X axis
  ctx.fillStyle = "#94a3b8";
  ctx.textAlign = "center";
  const labelStride = Math.max(1, Math.floor(count / 6));
  for (let i = 0; i < count; i += labelStride) {
    const x = padL + i * stepX;
    ctx.fillText(history[i].iso_time || "", x, h - 8);
  }
}

const HEATMAP_BASE_WIDTH = 680;
const HEATMAP_DEFAULT_HEIGHT = 420;

// Match the canvas (and its card) to the floorplan image's shape so portrait
// plans aren't squashed into a landscape box. Pins are stored as 0..1 fractions,
// so they stay in place when the shape changes.
function sizeHeatmapCanvas(canvas, floorImg) {
  const ratio = floorImg && floorImg.naturalWidth && floorImg.naturalHeight
    ? floorImg.naturalWidth / floorImg.naturalHeight
    : HEATMAP_BASE_WIDTH / HEATMAP_DEFAULT_HEIGHT;
  const height = Math.round(HEATMAP_BASE_WIDTH / ratio);
  if (canvas.width !== HEATMAP_BASE_WIDTH) canvas.width = HEATMAP_BASE_WIDTH;
  if (canvas.height !== height) canvas.height = height;
  // Cap the on-screen height for tall plans; the wrap narrows to keep the ratio
  canvas.parentElement.style.maxWidth = `calc(75vh * ${ratio.toFixed(4)})`;
}

function drawFloorplanHeatmap() {
  const canvas = document.getElementById("heatmapCanvas");
  const ctx = canvas.getContext("2d");
  const floorImg = state.floorplanImgs[state.currentFloor];
  sizeHeatmapCanvas(canvas, floorImg);
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);

  // 1. Draw the selected floor's uploaded floorplan image or a default blueprint
  const floorIdx = Math.max(0, state.floors.indexOf(state.currentFloor));
  if (floorImg) {
    ctx.drawImage(floorImg, 0, 0, w, h);
    ctx.fillStyle = "rgba(9, 14, 26, 0.35)";
    ctx.fillRect(0, 0, w, h);
  } else {
    drawDefaultHouseBlueprint(ctx, w, h, floorIdx);
  }

  // Floor name badge (bottom-right corner)
  ctx.font = "bold 12px Segoe UI, sans-serif";
  const badge = state.currentFloor;
  const bw = ctx.measureText(badge).width + 16;
  ctx.fillStyle = "rgba(79, 70, 229, 0.85)";
  ctx.fillRect(w - bw - 10, h - 30, bw, 20);
  ctx.fillStyle = "#fff";
  ctx.textAlign = "center";
  ctx.fillText(badge, w - bw / 2 - 10, h - 16);

  // 2. Render radial thermal heatmap blobs for each measurement point on this floor
  const firstFloor = state.floors[0];
  const points = (state.heatmapPoints || []).filter(
    (pt) => (pt.floor || firstFloor) === state.currentFloor
  );
  points.forEach((pt) => {
    const px = pt.x * w;
    const py = pt.y * h;
    const radius = 85;
    const rgb = rssiToRgb(pt.rssi_dbm);

    const radGrad = ctx.createRadialGradient(px, py, 4, px, py, radius);
    radGrad.addColorStop(0, `rgba(${rgb.r}, ${rgb.g}, ${rgb.b}, 0.62)`);
    radGrad.addColorStop(0.55, `rgba(${rgb.r}, ${rgb.g}, ${rgb.b}, 0.28)`);
    radGrad.addColorStop(1, `rgba(${rgb.r}, ${rgb.g}, ${rgb.b}, 0.0)`);

    ctx.fillStyle = radGrad;
    ctx.beginPath();
    ctx.arc(px, py, radius, 0, Math.PI * 2);
    ctx.fill();
  });

  // 3. Draw measurement pins & dBm labels on top
  points.forEach((pt, idx) => {
    const px = pt.x * w;
    const py = pt.y * h;

    ctx.beginPath();
    ctx.arc(px, py, 6, 0, Math.PI * 2);
    ctx.fillStyle = pt.quality_color || "#10b981";
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = "#0b1120";
    ctx.stroke();

    // Label pill
    const text = `${pt.rssi_dbm} dBm`;
    ctx.font = "bold 11px Consolas, monospace";
    const tw = ctx.measureText(text).width + 10;
    ctx.fillStyle = "rgba(15, 23, 42, 0.88)";
    ctx.fillRect(px - tw / 2, py - 24, tw, 16);
    ctx.strokeStyle = pt.quality_color || "#38bdf8";
    ctx.lineWidth = 1;
    ctx.strokeRect(px - tw / 2, py - 24, tw, 16);

    ctx.fillStyle = "#f8fafc";
    ctx.textAlign = "center";
    ctx.fillText(text, px, py - 12);
  });
}

function drawDefaultHouseBlueprint(ctx, w, h, floorIdx = 0) {
  // Subtle blueprint grid
  ctx.strokeStyle = "rgba(56, 189, 248, 0.08)";
  ctx.lineWidth = 1;
  for (let x = 0; x < w; x += 34) {
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, h);
    ctx.stroke();
  }
  for (let y = 0; y < h; y += 34) {
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(w, y);
    ctx.stroke();
  }

  // House rooms layout boxes (ground floor plan, or a generic upper-floor plan)
  const groundRooms = [
    { name: "Living Room", x: 24, y: 24, rw: 260, rh: 185 },
    { name: "Kitchen", x: 284, y: 24, rw: 180, rh: 185 },
    { name: "Patio / Yard", x: 464, y: 24, rw: 192, rh: 185 },
    { name: "Hallway", x: 24, y: 209, rw: 440, rh: 52 },
    { name: "Master Bedroom", x: 24, y: 261, rw: 215, rh: 135 },
    { name: "Bedroom 2", x: 239, y: 261, rw: 145, rh: 135 },
    { name: "Bathroom", x: 384, y: 261, rw: 80, rh: 135 },
    { name: "Home Office / Garage", x: 464, y: 209, rw: 192, rh: 187 },
  ];
  const upperRooms = [
    { name: "Master Bedroom", x: 24, y: 24, rw: 260, rh: 185 },
    { name: "Bathroom", x: 284, y: 24, rw: 120, rh: 185 },
    { name: "Bedroom 2", x: 404, y: 24, rw: 252, rh: 185 },
    { name: "Landing / Stairs", x: 24, y: 209, rw: 632, rh: 52 },
    { name: "Bedroom 3", x: 24, y: 261, rw: 215, rh: 135 },
    { name: "Home Office", x: 239, y: 261, rw: 225, rh: 135 },
    { name: "Balcony", x: 464, y: 261, rw: 192, rh: 135 },
  ];
  const rooms = floorIdx === 0 ? groundRooms : upperRooms;

  ctx.strokeStyle = "rgba(148, 163, 184, 0.45)";
  ctx.lineWidth = 2;
  ctx.font = "600 12px Segoe UI, sans-serif";
  ctx.fillStyle = "rgba(148, 163, 184, 0.55)";
  ctx.textAlign = "left";

  rooms.forEach((r) => {
    ctx.strokeRect(r.x, r.y, r.rw, r.rh);
    ctx.fillText(r.name, r.x + 10, r.y + 20);
  });
}

function rssiToRgb(rssi) {
  if (rssi >= -55) return { r: 16, g: 185, b: 129 }; // Excellent Green
  if (rssi >= -67) return { r: 34, g: 197, b: 94 };  // Good Green
  if (rssi >= -75) return { r: 245, g: 158, b: 11 }; // Fair Amber
  if (rssi >= -82) return { r: 249, g: 115, b: 22 }; // Weak Orange
  return { r: 239, g: 68, b: 68 };                   // Dead Zone Red
}

function drawChannelCongestionChart(channels) {
  const canvas = document.getElementById("channelCanvas");
  const ctx = canvas.getContext("2d");
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);

  if (!channels || channels.length === 0) {
    ctx.fillStyle = "#64748b";
    ctx.font = "12px Segoe UI, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("No channel data available.", w / 2, h / 2);
    return;
  }

  const padL = 42;
  const padR = 20;
  const padT = 24;
  const padB = 38;
  const plotW = w - padL - padR;
  const plotH = h - padT - padB;

  const barW = Math.min(48, Math.max(18, Math.floor(plotW / channels.length) - 12));
  const gap = (plotW - barW * channels.length) / Math.max(1, channels.length + 1);

  channels.forEach((ch, idx) => {
    const x = padL + gap + idx * (barW + gap);
    // Height scaled by strongest RSSI (-100 to -30 dBm)
    const norm = Math.max(0.1, Math.min(1, (ch.strongest_dbm + 100) / 70));
    const barH = norm * plotH;
    const y = padT + (plotH - barH);

    const color =
      ch.band === "2.4 GHz" ? "#38bdf8" : ch.band === "5 GHz" ? "#10b981" : "#a855f7";

    ctx.fillStyle = `${color}44`;
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.fillRect(x, y, barW, barH);
    ctx.strokeRect(x, y, barW, barH);

    ctx.fillStyle = "#f8fafc";
    ctx.font = "bold 11px Consolas, monospace";
    ctx.textAlign = "center";
    ctx.fillText(`${ch.count} AP`, x + barW / 2, y - 6);

    ctx.fillStyle = "#94a3b8";
    ctx.font = "11px Consolas, monospace";
    ctx.fillText(`Ch ${ch.channel}`, x + barW / 2, h - 18);
    ctx.fillText(ch.band, x + barW / 2, h - 5);
  });
}

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
