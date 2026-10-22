# Wi-Fi House Walk-Test & Coverage Scanner

A local, real-time Wi-Fi signal strength and home coverage analyzer built for Windows PCs. Designed to run while carrying your laptop around the house to map signal strength, identify dead zones, compare rooms, detect mesh/extender roaming handoffs, and inspect channel congestion.

---

## CLI Script vs. Local Web App — Why a Local Web App?

When carrying a laptop in one hand while walking through rooms, hallways, and corners:

| Criteria | Terminal CLI Script | Local Web App (Chosen Architecture) |
| :--- | :--- | :--- |
| **Readability while walking** | Small scrolling text; hard to read at arm's length | Giant color-coded radial gauge & high-contrast dBm readout |
| **Historical Visualization** | Numbers scroll off screen or require ASCII bars | Smooth real-time scrolling time-series graph (RSSI dBm + Link Speed) |
| **Spatial / House Mapping** | Cannot click on a 2D floorplan | Interactive 2D house floorplan where clicking drops thermal heatmap pins |
| **Room Tagging** | Requires typing commands while holding the laptop | Large one-tap room buttons (`Living Room`, `Kitchen`, `Master Bedroom`, etc.) |
| **Hands-Free Testing** | None | Optional WebAudio Geiger/Tone mode (higher pitch = stronger signal) |
| **Dependencies & Privacy** | 100% Local | **100% Local** (`127.0.0.1:8765`), zero cloud services, zero external pip packages required |

---

## How It Works Under the Hood (What Is Needed on Windows)

Windows exposes Wi-Fi hardware telemetry through three native layers, all implemented in `wifi_engine.py` using Python's built-in `ctypes` library (requiring **zero external `pip` packages**):

1. **Windows Native Wi-Fi API (`wlanapi.dll` via `ctypes`)**
   - Calls `WlanOpenHandle`, `WlanEnumInterfaces`, and `WlanQueryInterface`.
   - Queries opcode `0x10000102` (`wlan_intf_opcode_rssi`) to read **exact hardware RSSI in dBm** (e.g., `-42 dBm`) directly from your Wi-Fi card (MediaTek Wi-Fi 7 MT7925) in `< 2ms`.
   - Queries opcode `8` (`wlan_intf_opcode_channel_number`) for the active 802.11 channel and calculates the frequency band (`2.4 GHz`, `5 GHz`, or `6 GHz`) and center frequency in MHz.
   - Queries opcode `0x10000101` (`wlan_intf_opcode_statistics`) for MAC unicast TX/RX frame counters.
   - Calls `WlanScan` to trigger active multi-channel hardware scans for neighboring networks.

2. **Windows IP Helper API (`iphlpapi.dll` via `ctypes`)**
   - Calls `ConvertInterfaceGuidToLuid` and `GetIfEntry2` (`MIB_IF_ROW2`) to read real-time **Transmit and Receive Link Speeds (Mbps)** (e.g., `1201.0 Mbps`) and the adapter's hardware MAC address in `< 1ms`.

3. **Windows `netsh wlan` & Fallback Profile Engine**
   - **Windows 11 Location Privacy Note**: Starting in Windows 11 (24H2), Microsoft blocks `netsh wlan show interfaces` and `netsh wlan show networks mode=bssid` unless **Windows Location Services** is turned on (`ms-settings:privacy-location`).
   - **Even with Location Services OFF**: This scanner still reads your live **RSSI (dBm)**, **Signal Quality (%)**, **Channel**, **Band**, and **TX/RX Link Speed (Mbps)** directly via `wlanapi.dll` + `iphlpapi.dll`, and resolves your network profile name via `Get-NetConnectionProfile` and router MAC via ARP.
   - **With Location Services ON**: Clicking the **"Open Windows Location Settings"** button in the banner and enabling *Location Services* + *Let desktop apps access your location* unlocks exact connected AP BSSIDs and full neighboring network/channel congestion lists.

---

## Project Structure

```text
Wi-Fi Scanner/
├── app.py               # Local HTTP server (127.0.0.1:8765), background polling thread, REST API & CSV/JSON export
├── wifi_engine.py       # Native Windows Wi-Fi hardware engine (wlanapi.dll, iphlpapi.dll, netsh, Demo Mode)
├── start_scanner.bat    # One-click Windows batch launcher
├── start_scanner.bat    # One-click Windows batch launcher
├── README.md            # Architecture, setup, and walk-test user guide
├── sessions/            # Auto-saved and user-saved walk-test snapshots (JSON + floorplan)
└── static/
    ├── index.html       # Walk-test dashboard UI layout
    ├── styles.css       # High-contrast dark theme & responsive layout
    └── app.js           # Real-time gauges, timeline canvas, floorplan heatmap, TTS & API client
```

---

## Advanced Walk-Test Features Included

1. **Live ICMP Ping, Jitter & Packet Loss Monitor (`iphlpapi.dll` `IcmpSendEcho2Ex`)**
   - Continuously pings both your local Wi-Fi router gateway (bound directly to your Wi-Fi interface IP so it bypasses corporate VPNs) and `1.1.1.1`.
   - Computes real-time **Latency (ms)**, **Jitter (ms)**, and **Packet Loss (%)** per room so you can spot bufferbloat or interference even when signal bars look strong.
2. **Voice Announcements (`Voice TTS: ON`)**
   - Uses browser Speech Synthesis to read out room signal quality when you switch rooms, announce Mesh/AP roaming handoffs, and warn you when entering a dead zone (`<= -80 dBm`) so you don't have to stare at the laptop screen while walking.
3. **On-Demand Room Speed / Throughput Test (`⚡ Test Room Speed`)**
   - Measures real-world HTTP download throughput (`Mbps`) in your current room and compares it against the theoretical Wi-Fi PHY Link Speed (`Mbps`).
4. **Smart Mesh & Router Placement Advisor**
   - Automatically analyzes your room-by-room telemetry to diagnose coverage dead zones, latency spikes, and "sticky client" roaming issues, recommending where to place a mesh node or extender.
5. **Automatic Session Persistence (`sessions/`)**
   - Automatically saves your walk-test history, heatmap pins, speed tests, and uploaded floorplan every 15 seconds to `sessions/autosave_latest.json`, plus lets you save/load named snapshots from the top bar.

---

## How to Run the Scanner

### Option 1: Double-Click Launcher
Double-click **`start_scanner.bat`** in File Explorer. It will launch the Python backend and automatically open your browser to `http://127.0.0.1:8765`.

### Option 2: Run from VS Code Terminal / PowerShell
```powershell
& "C:/Program Files/Python313/python.exe" app.py
```

---

## How to Perform a House Walk-Test

1. **Start in your main room (near the router)**
   - Open the dashboard (`http://127.0.0.1:8765`).
   - Select the room you are currently standing in under **Current Room / Waypoint** (or add a custom room name).
2. **Walk slowly through each room**
   - Watch the **Live Signal Strength** gauge and **Real-Time Signal History** chart.
   - Optional: Click **`Audio Beep: ON`** in the top bar so the laptop emits a gentle pitch-mapped tone (higher pitch = stronger signal, lower pitch = weak signal).
   - Each time you enter a new room, tap that room's button. Dashed vertical markers will appear on the timeline chart, and the **Room Coverage Leaderboard** will compute the Min, Average, and Max dBm and Link Speed for each room.
3. **Stamp Points on the House Floorplan Heatmap**
   - Use the built-in house blueprint or click **Upload Floorplan** to load an image of your own home layout.
   - As you stand in a spot, click that corresponding spot on the floorplan canvas to drop a live Wi-Fi measurement pin and build a color-coded coverage heatmap.
4. **Check Mesh / AP Roaming**
   - If you have a mesh Wi-Fi system or multiple access points, watch the **Mesh / AP Roaming & Band Steering Log** to verify whether your laptop switches cleanly to the closer node or gets stuck on a distant AP ("sticky client").
5. **Export Your Results**
   - Click **Export CSV** or **Export JSON** in the top-right bar to save your full walk-test log.

---

## Understanding Wi-Fi Signal Levels (RSSI in dBm)

Wi-Fi signal strength is measured in **dBm** (decibels relative to 1 milliwatt). Because values are negative, **closer to 0 is stronger** (every `+3 dB` doubles the signal power):

| RSSI Range (dBm) | Rating | Typical Experience |
| :--- | :--- | :--- |
| **`-30` to `-55 dBm`** | **Excellent** | Maximum Wi-Fi 6/7 link speeds, 4K/8K streaming, ultra-low latency gaming |
| **`-56` to `-67 dBm`** | **Good** | Solid reliability for video calls (Teams/Zoom), fast downloads, VoIP |
| **`-68` to `-75 dBm`** | **Fair** | Web browsing works, but link speeds drop and packet retries increase |
| **`-76` to `-82 dBm`** | **Weak** | Frequent buffering, slow throughput, high latency spikes |
| **`-83 dBm` or lower** | **Dead Zone** | Disconnections, failed handshakes — needs a mesh node or AP relocation |
