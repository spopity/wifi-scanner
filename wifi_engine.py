"""
wifi_engine.py
==============
Native Windows Wi-Fi hardware scanner engine.

Uses three layers of Windows APIs so that real-time scanning works reliably
even under restricted Windows 11 privacy settings:

1. Native Wi-Fi API (`wlanapi.dll` via `ctypes`):
   - Reads real-time RSSI in dBm (`wlan_intf_opcode_rssi` = 0x10000102) directly
     from the Wi-Fi driver in <2ms (does NOT require Location Services).
   - Reads current Channel (`wlan_intf_opcode_channel_number` = 8) in <1ms.
   - Reads MAC frame statistics (`wlan_intf_opcode_statistics` = 0x10000101).
   - Triggers active hardware scans (`WlanScan`) and reads BSS lists when
     Windows Location Services permission is enabled.

2. IP Helper API (`iphlpapi.dll` via `ctypes`):
   - Reads real-time Transmit and Receive Link Speed in Mbps (`GetIfEntry2`)
     and adapter MAC address in <1ms without spawning subprocesses.

3. `netsh wlan` & Network Profile Fallback:
   - When Windows Location Services is enabled, parses `netsh wlan show interfaces`
     and `netsh wlan show networks mode=bssid` for exact BSSIDs, PHY types
     (Wi-Fi 7 802.11be, Wi-Fi 6 802.11ax, etc.), and neighboring networks.
   - When Location Services is disabled, falls back to `Get-NetConnectionProfile`
     (cached) for the connected network name and ARP gateway MAC address.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import math
import random
import re
import socket
import struct
import subprocess
import threading
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

# Flag to hide console window when spawning background commands on Windows
CREATE_NO_WINDOW = 0x08000000

# How often to ask the driver for a background scan so its cached RSSI stays fresh.
# A full scan takes ~2-4s and briefly adds latency, so faster than this doesn't help.
RSSI_REFRESH_SCAN_INTERVAL_S = 3.0


# ============================================================================
# ctypes Structures for Windows Native Wi-Fi (wlanapi.dll) & IP Helper (iphlpapi.dll)
# ============================================================================

class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def to_str(self) -> str:
        d4 = bytes(self.Data4)
        return (
            f"{{{self.Data1:08X}-{self.Data2:04X}-{self.Data3:04X}-"
            f"{d4[0]:02X}{d4[1]:02X}-{d4[2]:02X}{d4[3]:02X}{d4[4]:02X}"
            f"{d4[5]:02X}{d4[6]:02X}{d4[7]:02X}}}"
        )


class WLAN_INTERFACE_INFO(ctypes.Structure):
    _fields_ = [
        ("InterfaceGuid", GUID),
        ("strInterfaceDescription", ctypes.c_wchar * 256),
        ("isState", ctypes.c_uint),
    ]


class WLAN_INTERFACE_INFO_LIST(ctypes.Structure):
    _fields_ = [
        ("dwNumberOfItems", wintypes.DWORD),
        ("dwIndex", wintypes.DWORD),
        ("InterfaceInfo", WLAN_INTERFACE_INFO * 1),
    ]


class WLAN_MAC_FRAME_STATISTICS(ctypes.Structure):
    _fields_ = [
        ("ullTransmittedFrameCount", ctypes.c_uint64),
        ("ullReceivedFrameCount", ctypes.c_uint64),
        ("ullWEPExcludedCount", ctypes.c_uint64),
        ("ullTKIPLocalMICFailures", ctypes.c_uint64),
        ("ullTKIPReplays", ctypes.c_uint64),
        ("ullTKIPICVErrorCount", ctypes.c_uint64),
        ("ullCCMPReplays", ctypes.c_uint64),
        ("ullCCMPDecryptErrors", ctypes.c_uint64),
        ("ullWEPUndecryptableCount", ctypes.c_uint64),
        ("ullWEPICVErrorCount", ctypes.c_uint64),
        ("ullDecryptSuccessCount", ctypes.c_uint64),
        ("ullDecryptFailureCount", ctypes.c_uint64),
    ]


class WLAN_STATISTICS(ctypes.Structure):
    _fields_ = [
        ("ullFourWayHandshakeFailures", ctypes.c_uint64),
        ("ullTKIPCounterMeasuresInvoked", ctypes.c_uint64),
        ("ullReserved", ctypes.c_uint64),
        ("MacUcastCounters", WLAN_MAC_FRAME_STATISTICS),
        ("MacMcastCounters", WLAN_MAC_FRAME_STATISTICS),
        ("dwNumberOfPhys", wintypes.DWORD),
    ]


class MIB_IF_ROW2(ctypes.Structure):
    _fields_ = [
        ("InterfaceLuid", ctypes.c_uint64),
        ("InterfaceIndex", wintypes.DWORD),
        ("InterfaceGuid", GUID),
        ("Alias", ctypes.c_wchar * 257),
        ("Description", ctypes.c_wchar * 257),
        ("PhysicalAddressLength", wintypes.ULONG),
        ("PhysicalAddress", ctypes.c_ubyte * 32),
        ("PermanentPhysicalAddress", ctypes.c_ubyte * 32),
        ("Mtu", wintypes.ULONG),
        ("Type", wintypes.ULONG),
        ("TunnelType", ctypes.c_uint),
        ("MediaType", ctypes.c_uint),
        ("PhysicalMediumType", wintypes.ULONG),
        ("AccessType", ctypes.c_uint),
        ("DirectionType", ctypes.c_uint),
        ("InterfaceAndOperStatusFlags", ctypes.c_ubyte),
        ("OperStatus", ctypes.c_uint),
        ("AdminStatus", ctypes.c_uint),
        ("MediaConnectState", ctypes.c_uint),
        ("NetworkGuid", GUID),
        ("ConnectionType", ctypes.c_uint),
        ("TransmitLinkSpeed", ctypes.c_uint64),
        ("ReceiveLinkSpeed", ctypes.c_uint64),
        ("_padding", ctypes.c_ubyte * 512),  # Padding for remaining MIB_IF_ROW2 counters
    ]


# WLAN Opcodes
WLAN_INTF_OPCODE_INTERFACE_STATE = 6
WLAN_INTF_OPCODE_CURRENT_CONNECTION = 7
WLAN_INTF_OPCODE_CHANNEL_NUMBER = 8
WLAN_INTF_OPCODE_STATISTICS = 0x10000101
WLAN_INTF_OPCODE_RSSI = 0x10000102


class IP_OPTION_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("Ttl", ctypes.c_ubyte),
        ("Tos", ctypes.c_ubyte),
        ("Flags", ctypes.c_ubyte),
        ("OptionsSize", ctypes.c_ubyte),
        ("OptionsData", ctypes.c_void_p),
    ]


class ICMP_ECHO_REPLY(ctypes.Structure):
    _fields_ = [
        ("Address", wintypes.DWORD),
        ("Status", wintypes.ULONG),
        ("RoundTripTime", wintypes.ULONG),
        ("DataSize", wintypes.WORD),
        ("Reserved", wintypes.WORD),
        ("Data", ctypes.c_void_p),
        ("Options", IP_OPTION_INFORMATION),
    ]


# ============================================================================
# Helper Functions for Frequency, Band, and Signal Conversions
# ============================================================================

def channel_to_band_and_freq(channel: int) -> Tuple[str, int]:
    """Convert an 802.11 channel number to (Band string, Center Frequency in MHz)."""
    if channel <= 0:
        return ("Unknown", 0)
    if 1 <= channel <= 13:
        return ("2.4 GHz", 2407 + channel * 5)
    if channel == 14:
        return ("2.4 GHz", 2484)
    if 32 <= channel <= 177:
        return ("5 GHz", 5000 + channel * 5)
    if channel > 177:
        return ("6 GHz", 5950 + channel * 5)
    return ("Unknown", 0)


def dbm_to_quality_pct(rssi_dbm: int) -> int:
    """Convert RSSI (dBm) to Windows-style 0-100% signal quality."""
    if rssi_dbm >= -50:
        return 100
    if rssi_dbm <= -100:
        return 0
    return int(round(2 * (rssi_dbm + 100)))


def quality_pct_to_dbm(pct: int) -> int:
    """Estimate RSSI (dBm) from 0-100% signal quality when raw dBm isn't provided."""
    pct = max(0, min(100, pct))
    return int(round((pct / 2.0) - 100))


def classify_signal(rssi_dbm: int) -> Dict[str, str]:
    """Return human-readable signal quality label and severity level."""
    if rssi_dbm >= -55:
        return {"label": "Excellent", "level": "excellent", "color": "#10b981"}
    if rssi_dbm >= -67:
        return {"label": "Good", "level": "good", "color": "#22c55e"}
    if rssi_dbm >= -75:
        return {"label": "Fair", "level": "fair", "color": "#f59e0b"}
    if rssi_dbm >= -82:
        return {"label": "Weak", "level": "weak", "color": "#f97316"}
    return {"label": "Dead Zone", "level": "dead", "color": "#ef4444"}


# ============================================================================
# Main Wi-Fi Hardware Scanner Class
# ============================================================================

class WindowsWifiScanner:
    """
    Thread-safe scanner that queries the PC's Wi-Fi adapter continuously.
    Supports both live hardware mode and an interactive simulation mode.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._wlan = ctypes.windll.wlanapi
        self._iphlp = ctypes.windll.iphlpapi

        # Cached fallback metadata (updated periodically so fast polling stays <5ms)
        self._cached_profile_ssid: str = "Scanning SSID..."
        self._cached_gateway_mac: str = ""
        self._cached_gateway_ip: str = ""
        self._cached_wifi_ip: str = ""
        self._last_fallback_update: float = 0.0
        self._fallback_updating: bool = False

        # Configure ICMP ctypes signatures for sub-millisecond ping probes
        self._iphlp.IcmpCreateFile.restype = ctypes.c_void_p
        self._iphlp.IcmpCloseHandle.argtypes = [ctypes.c_void_p]
        self._iphlp.IcmpSendEcho2Ex.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.WORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self._iphlp.IcmpSendEcho2Ex.restype = wintypes.DWORD

        # Cached netsh interface details & neighbor scan
        self._location_permission_granted: bool = False
        self._last_netsh_interface_check: float = 0.0
        self._cached_netsh_info: Dict[str, Any] = {}
        self._cached_neighbors: List[Dict[str, Any]] = []
        self._last_neighbor_scan: float = 0.0
        self._last_rssi_refresh_scan: float = 0.0

        # Simulation state for Demo Mode
        self._sim_step: int = 0
        self._sim_ap_index: int = 0

    def _update_fallback_metadata(self, interface_alias: str = "Wi-Fi") -> None:
        """
        Periodically fetches the connected network profile name via PowerShell
        and default gateway MAC via ARP when Windows Location Services blocks netsh.
        Runs asynchronously in a background thread so live polling stays <5ms.
        """
        now = time.time()
        if self._fallback_updating or (now - self._last_fallback_update < 15.0):
            return
        self._fallback_updating = True
        self._last_fallback_update = now

        def _worker() -> None:
            try:
                # 1. Fast ARP lookup for Wi-Fi interface IP, gateway IP, and gateway MAC
                try:
                    arp_out = subprocess.check_output(
                        ["arp", "-a"], text=True, timeout=3.0, creationflags=CREATE_NO_WINDOW
                    )
                    current_if_ip = ""
                    for line in arp_out.splitlines():
                        m_if = re.match(r"Interface:\s+(\d+\.\d+\.\d+\.\d+)", line.strip())
                        if m_if:
                            current_if_ip = m_if.group(1)
                            continue
                        m = re.search(
                            r"(\d+\.\d+\.\d+\.1)\s+([0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5})\s+dynamic",
                            line,
                        )
                        if m:
                            gw_ip = m.group(1)
                            mac = m.group(2).replace("-", ":").upper()
                            if not mac.startswith("02:50:41"):  # Ignore virtual VPN interfaces
                                self._cached_gateway_mac = mac
                                self._cached_gateway_ip = gw_ip
                                if current_if_ip:
                                    self._cached_wifi_ip = current_if_ip
                                break
                except Exception:
                    pass

                # 2. Get network name from Get-NetConnectionProfile
                try:
                    cmd = [
                        "powershell",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        f"(Get-NetConnectionProfile -InterfaceAlias '{interface_alias}' -ErrorAction SilentlyContinue).Name",
                    ]
                    out = subprocess.check_output(
                        cmd, text=True, timeout=10.0, creationflags=CREATE_NO_WINDOW
                    ).strip()
                    if out:
                        self._cached_profile_ssid = out.splitlines()[0].strip()
                    elif self._cached_profile_ssid == "Scanning SSID...":
                        self._cached_profile_ssid = "Connected Wi-Fi"
                except Exception:
                    if self._cached_profile_ssid == "Scanning SSID...":
                        self._cached_profile_ssid = "Connected Wi-Fi"
            finally:
                self._fallback_updating = False

        threading.Thread(target=_worker, daemon=True).start()

    def _query_netsh_interface(self) -> Dict[str, Any]:
        """
        Parse `netsh wlan show interfaces` if Windows Location Services is enabled.
        Checks at most once every 2 seconds to keep CPU load minimal.
        """
        now = time.time()
        if now - self._last_netsh_interface_check < 2.0:
            return self._cached_netsh_info
        self._last_netsh_interface_check = now

        try:
            proc = subprocess.run(
                ["netsh", "wlan", "show", "interfaces"],
                capture_output=True,
                text=True,
                timeout=2.0,
                creationflags=CREATE_NO_WINDOW,
            )
            output = proc.stdout or ""
            if "location permission" in output.lower() or proc.returncode != 0:
                self._location_permission_granted = False
                self._cached_netsh_info = {}
                return {}

            self._location_permission_granted = True
            info: Dict[str, Any] = {}
            for raw_line in output.splitlines():
                if ":" not in raw_line:
                    continue
                k, v = raw_line.split(":", 1)
                key = k.strip().lower()
                val = v.strip()
                if key == "ssid" and "bssid" not in key:
                    info["ssid"] = val
                elif key in ("bssid", "ap bssid"):
                    info["bssid"] = val.upper()
                elif key == "radio type":
                    info["radio_type"] = val
                elif key == "authentication":
                    info["authentication"] = val
                elif key == "cipher":
                    info["cipher"] = val
                elif key == "band":
                    info["band"] = val
                elif key == "channel":
                    try:
                        info["channel"] = int(val)
                    except ValueError:
                        pass
                elif key == "signal":
                    m = re.search(r"(\d+)%", val)
                    if m:
                        info["signal_pct"] = int(m.group(1))
            self._cached_netsh_info = info
            return info
        except Exception:
            return self._cached_netsh_info

    def read_live_signal(self, demo_mode: bool = False) -> Dict[str, Any]:
        """
        Read current Wi-Fi signal metrics from the PC's wireless card.
        Returns a dictionary with RSSI (dBm), Signal %, Link Speed, Channel, Band,
        SSID, BSSID, Adapter Info, and Frame Statistics.
        """
        if demo_mode:
            return self._generate_demo_reading()

        client_handle = ctypes.c_void_p()
        negotiated_version = wintypes.DWORD()
        res = self._wlan.WlanOpenHandle(
            2, None, ctypes.byref(negotiated_version), ctypes.byref(client_handle)
        )
        if res != 0:
            return self._disconnected_reading("Failed to open Windows WLAN API handle")

        p_if_list = ctypes.POINTER(WLAN_INTERFACE_INFO_LIST)()
        try:
            res = self._wlan.WlanEnumInterfaces(
                client_handle, None, ctypes.byref(p_if_list)
            )
            if res != 0 or not p_if_list or p_if_list.contents.dwNumberOfItems == 0:
                return self._disconnected_reading("No Wi-Fi adapter detected on this PC")

            if_info = p_if_list.contents.InterfaceInfo[0]
            guid = if_info.InterfaceGuid
            adapter_desc = if_info.strInterfaceDescription
            is_connected = if_info.isState == 1

            # 0. The driver only refreshes its cached RSSI after a scan, so without
            #    this the dBm value freezes while walking. WlanScan is asynchronous;
            #    the fresh value is picked up by the next poll(s).
            now = time.time()
            if now - self._last_rssi_refresh_scan >= RSSI_REFRESH_SCAN_INTERVAL_S:
                self._last_rssi_refresh_scan = now
                self._wlan.WlanScan(client_handle, ctypes.byref(guid), None, None, None)

            # 1. Query RSSI (dBm) directly via opcode 0x10000102
            rssi_dbm = -100
            sz = wintypes.DWORD()
            pdata = ctypes.c_void_p()
            r_rssi = self._wlan.WlanQueryInterface(
                client_handle,
                ctypes.byref(guid),
                WLAN_INTF_OPCODE_RSSI,
                None,
                ctypes.byref(sz),
                ctypes.byref(pdata),
                None,
            )
            if r_rssi == 0 and pdata and sz.value == 4:
                rssi_dbm = ctypes.cast(pdata, ctypes.POINTER(ctypes.c_long)).contents.value
                self._wlan.WlanFreeMemory(pdata)

            # 2. Query Channel Number via opcode 8
            channel = 0
            sz_ch = wintypes.DWORD()
            pdata_ch = ctypes.c_void_p()
            r_ch = self._wlan.WlanQueryInterface(
                client_handle,
                ctypes.byref(guid),
                WLAN_INTF_OPCODE_CHANNEL_NUMBER,
                None,
                ctypes.byref(sz_ch),
                ctypes.byref(pdata_ch),
                None,
            )
            if r_ch == 0 and pdata_ch and sz_ch.value == 4:
                channel = int(
                    ctypes.cast(pdata_ch, ctypes.POINTER(ctypes.c_ulong)).contents.value
                )
                self._wlan.WlanFreeMemory(pdata_ch)

            # 3. Query MAC Frame Statistics via opcode 0x10000101
            tx_frames = 0
            rx_frames = 0
            sz_st = wintypes.DWORD()
            pdata_st = ctypes.c_void_p()
            r_st = self._wlan.WlanQueryInterface(
                client_handle,
                ctypes.byref(guid),
                WLAN_INTF_OPCODE_STATISTICS,
                None,
                ctypes.byref(sz_st),
                ctypes.byref(pdata_st),
                None,
            )
            if r_st == 0 and pdata_st and sz_st.value >= ctypes.sizeof(WLAN_STATISTICS):
                stats = ctypes.cast(pdata_st, ctypes.POINTER(WLAN_STATISTICS)).contents
                tx_frames = int(stats.MacUcastCounters.ullTransmittedFrameCount)
                rx_frames = int(stats.MacUcastCounters.ullReceivedFrameCount)
                self._wlan.WlanFreeMemory(pdata_st)

            # 4. Query Link Speed & Adapter MAC via iphlpapi.dll (GetIfEntry2)
            tx_mbps = 0.0
            rx_mbps = 0.0
            adapter_alias = "Wi-Fi"
            adapter_mac = ""
            luid = ctypes.c_uint64()
            if self._iphlp.ConvertInterfaceGuidToLuid(ctypes.byref(guid), ctypes.byref(luid)) == 0:
                row = MIB_IF_ROW2()
                row.InterfaceLuid = luid.value
                if self._iphlp.GetIfEntry2(ctypes.byref(row)) == 0:
                    adapter_alias = row.Alias or "Wi-Fi"
                    if row.TransmitLinkSpeed < (1 << 60):
                        tx_mbps = round(row.TransmitLinkSpeed / 1_000_000.0, 1)
                    if row.ReceiveLinkSpeed < (1 << 60):
                        rx_mbps = round(row.ReceiveLinkSpeed / 1_000_000.0, 1)
                    mac_len = min(int(row.PhysicalAddressLength), 6)
                    if mac_len == 6:
                        adapter_mac = ":".join(
                            f"{row.PhysicalAddress[i]:02X}" for i in range(6)
                        )

            # 5. Enrich with netsh (if Location Services is on) or fallback metadata
            netsh_info = self._query_netsh_interface()
            if not self._location_permission_granted:
                self._update_fallback_metadata(adapter_alias)

            if netsh_info.get("channel"):
                channel = netsh_info["channel"]

            band, freq_mhz = channel_to_band_and_freq(channel)
            if netsh_info.get("band") and band == "Unknown":
                band = netsh_info["band"]

            ssid = netsh_info.get("ssid") or self._cached_profile_ssid
            if netsh_info.get("bssid"):
                bssid = netsh_info["bssid"]
            elif self._cached_gateway_mac:
                bssid = f"{self._cached_gateway_mac} (Ch {channel})"
            else:
                bssid = f"AP-Ch{channel} ({band})"

            radio_type = netsh_info.get("radio_type")
            if not radio_type:
                if "Wi-Fi 7" in adapter_desc:
                    radio_type = "802.11be / ax (Wi-Fi 7 Card)"
                elif "Wi-Fi 6" in adapter_desc:
                    radio_type = "802.11ax (Wi-Fi 6)"
                else:
                    radio_type = "802.11 WLAN"

            signal_pct = dbm_to_quality_pct(rssi_dbm)
            quality = classify_signal(rssi_dbm)

            # 6. Measure Gateway & Internet ICMP Ping Latency (bound to Wi-Fi interface when known)
            gw_ping_ms: Optional[float] = None
            inet_ping_ms: Optional[float] = None
            if is_connected:
                if self._cached_gateway_ip:
                    gw_ping_ms = self._icmp_ping(
                        self._cached_gateway_ip,
                        source_ip=self._cached_wifi_ip or None,
                        timeout_ms=350,
                    )
                inet_ping_ms = self._icmp_ping("1.1.1.1", source_ip=None, timeout_ms=450)

            return {
                "connected": is_connected,
                "timestamp": time.time(),
                "iso_time": time.strftime("%H:%M:%S"),
                "ssid": ssid if is_connected else "Disconnected",
                "bssid": bssid if is_connected else "None",
                "rssi_dbm": rssi_dbm if is_connected else -100,
                "signal_pct": signal_pct if is_connected else 0,
                "quality_label": quality["label"] if is_connected else "Disconnected",
                "quality_level": quality["level"] if is_connected else "dead",
                "quality_color": quality["color"] if is_connected else "#64748b",
                "channel": channel,
                "band": band,
                "freq_mhz": freq_mhz,
                "tx_mbps": tx_mbps if is_connected else 0.0,
                "rx_mbps": rx_mbps if is_connected else 0.0,
                "gateway_ip": self._cached_gateway_ip or "",
                "gateway_ping_ms": gw_ping_ms,
                "internet_ping_ms": inet_ping_ms,
                "packet_lost": (gw_ping_ms is None and inet_ping_ms is None) if is_connected else True,
                "radio_type": radio_type,
                "adapter_name": adapter_alias,
                "adapter_description": adapter_desc,
                "adapter_mac": adapter_mac,
                "tx_frames": tx_frames,
                "rx_frames": rx_frames,
                "location_permission_granted": self._location_permission_granted,
                "demo_mode": False,
            }
        finally:
            if p_if_list:
                self._wlan.WlanFreeMemory(p_if_list)
            if client_handle:
                self._wlan.WlanCloseHandle(client_handle, None)

    def scan_neighbors(self, demo_mode: bool = False, trigger_hardware_scan: bool = False) -> Dict[str, Any]:
        """
        Scan for all visible neighboring Wi-Fi networks and BSSIDs across
        2.4 GHz, 5 GHz, and 6 GHz bands.
        """
        if demo_mode:
            return self._generate_demo_neighbors()

        # Optionally ask the Wi-Fi hardware driver to initiate an active scan across channels
        if trigger_hardware_scan:
            self._trigger_wlan_hardware_scan()

        try:
            proc = subprocess.run(
                ["netsh", "wlan", "show", "networks", "mode=bssid"],
                capture_output=True,
                text=True,
                timeout=4.0,
                creationflags=CREATE_NO_WINDOW,
            )
            output = proc.stdout or ""
            if "location permission" in output.lower() or proc.returncode != 0:
                self._location_permission_granted = False
                # Return current connected channel info so chart still shows active channel
                current = self.read_live_signal(demo_mode=False)
                fallback_net = []
                if current.get("connected") and current.get("channel", 0) > 0:
                    fallback_net.append({
                        "ssid": current["ssid"],
                        "bssid": current["bssid"],
                        "signal_pct": current["signal_pct"],
                        "rssi_dbm": current["rssi_dbm"],
                        "channel": current["channel"],
                        "band": current["band"],
                        "radio_type": current["radio_type"],
                        "auth": "WPA2/WPA3",
                        "is_connected": True,
                    })
                return {
                    "location_permission_granted": False,
                    "message": (
                        "Windows 11 requires Location Services permission to list neighboring "
                        "Wi-Fi networks and BSSIDs. Your active connection's live RSSI (dBm), "
                        "Channel, Band, and Link Speed still work in real time!"
                    ),
                    "networks": fallback_net,
                    "channel_usage": self._summarize_channels(fallback_net),
                }

            self._location_permission_granted = True
            networks = self._parse_netsh_networks(output)
            current = self.read_live_signal(demo_mode=False)
            conn_bssid = current.get("bssid", "").upper()
            for net in networks:
                net["is_connected"] = net["bssid"].upper() == conn_bssid

            self._cached_neighbors = networks
            self._last_neighbor_scan = time.time()
            return {
                "location_permission_granted": True,
                "message": f"Found {len(networks)} visible access points (BSSIDs).",
                "networks": networks,
                "channel_usage": self._summarize_channels(networks),
            }
        except Exception as exc:
            return {
                "location_permission_granted": self._location_permission_granted,
                "message": f"Error scanning networks: {exc}",
                "networks": self._cached_neighbors,
                "channel_usage": self._summarize_channels(self._cached_neighbors),
            }

    def _trigger_wlan_hardware_scan(self) -> None:
        """Ask the Windows WLAN driver to actively scan surrounding channels via WlanScan."""
        client_handle = ctypes.c_void_p()
        negotiated_version = wintypes.DWORD()
        if self._wlan.WlanOpenHandle(
            2, None, ctypes.byref(negotiated_version), ctypes.byref(client_handle)
        ) != 0:
            return
        p_if_list = ctypes.POINTER(WLAN_INTERFACE_INFO_LIST)()
        try:
            if (
                self._wlan.WlanEnumInterfaces(
                    client_handle, None, ctypes.byref(p_if_list)
                )
                == 0
                and p_if_list
                and p_if_list.contents.dwNumberOfItems > 0
            ):
                guid = p_if_list.contents.InterfaceInfo[0].InterfaceGuid
                self._wlan.WlanScan(client_handle, ctypes.byref(guid), None, None, None)
        finally:
            if p_if_list:
                self._wlan.WlanFreeMemory(p_if_list)
            if client_handle:
                self._wlan.WlanCloseHandle(client_handle, None)

    def _parse_netsh_networks(self, text: str) -> List[Dict[str, Any]]:
        """Parse `netsh wlan show networks mode=bssid` output into a list of BSSID dicts."""
        networks: List[Dict[str, Any]] = []
        current_ssid = ""
        current_auth = ""
        current_encryption = ""
        current_bssid_obj: Optional[Dict[str, Any]] = None

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or ":" not in line:
                continue
            k, v = line.split(":", 1)
            key = k.strip().lower()
            val = v.strip()

            if key.startswith("ssid "):
                if current_bssid_obj:
                    networks.append(current_bssid_obj)
                    current_bssid_obj = None
                current_ssid = val if val else "(Hidden Network)"
            elif key == "authentication":
                current_auth = val
            elif key == "encryption":
                current_encryption = val
            elif key.startswith("bssid "):
                if current_bssid_obj:
                    networks.append(current_bssid_obj)
                current_bssid_obj = {
                    "ssid": current_ssid,
                    "bssid": val.upper(),
                    "auth": current_auth,
                    "encryption": current_encryption,
                    "signal_pct": 0,
                    "rssi_dbm": -100,
                    "radio_type": "",
                    "band": "",
                    "channel": 0,
                    "is_connected": False,
                }
            elif current_bssid_obj is not None:
                if key == "signal":
                    m = re.search(r"(\d+)%", val)
                    if m:
                        pct = int(m.group(1))
                        current_bssid_obj["signal_pct"] = pct
                        current_bssid_obj["rssi_dbm"] = quality_pct_to_dbm(pct)
                elif key == "radio type":
                    current_bssid_obj["radio_type"] = val
                elif key == "band":
                    current_bssid_obj["band"] = val
                elif key == "channel":
                    try:
                        ch = int(val)
                        current_bssid_obj["channel"] = ch
                        band, _ = channel_to_band_and_freq(ch)
                        if not current_bssid_obj["band"]:
                            current_bssid_obj["band"] = band
                    except ValueError:
                        pass

        if current_bssid_obj:
            networks.append(current_bssid_obj)

        networks.sort(key=lambda x: x["rssi_dbm"], reverse=True)
        return networks

    def _summarize_channels(self, networks: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Group networks by channel and band for congestion visualization."""
        by_channel: Dict[int, Dict[str, Any]] = {}
        band_counts = {"2.4 GHz": 0, "5 GHz": 0, "6 GHz": 0}
        for net in networks:
            ch = net.get("channel", 0)
            band = net.get("band", "Unknown")
            if band in band_counts:
                band_counts[band] += 1
            if ch > 0:
                if ch not in by_channel:
                    by_channel[ch] = {
                        "channel": ch,
                        "band": band,
                        "count": 0,
                        "strongest_dbm": -100,
                        "ssids": [],
                    }
                entry = by_channel[ch]
                entry["count"] += 1
                entry["strongest_dbm"] = max(
                    entry["strongest_dbm"], net.get("rssi_dbm", -100)
                )
                ssid = net.get("ssid", "(Hidden)")
                if ssid not in entry["ssids"]:
                    entry["ssids"].append(ssid)

        return {
            "band_counts": band_counts,
            "channels": sorted(by_channel.values(), key=lambda x: x["channel"]),
        }

    def _icmp_ping(
        self, target_ip: str, source_ip: Optional[str] = None, timeout_ms: int = 400
    ) -> Optional[float]:
        """
        Send a native ICMP Echo Request via iphlpapi.dll (IcmpSendEcho2Ex).
        Binds to `source_ip` when provided so local router pings bypass corporate VPNs.
        Returns round-trip latency in milliseconds (float), or None on timeout/loss.
        """
        h_icmp = self._iphlp.IcmpCreateFile()
        if not h_icmp or h_icmp == ctypes.c_void_p(-1).value:
            return None
        try:
            dest_addr = struct.unpack("<I", socket.inet_aton(target_ip))[0]
            src_addr = (
                struct.unpack("<I", socket.inet_aton(source_ip))[0] if source_ip else 0
            )
            payload = b"wifiwalk"
            reply_size = ctypes.sizeof(ICMP_ECHO_REPLY) + len(payload) + 32
            reply_buf = ctypes.create_string_buffer(reply_size)

            t0 = time.perf_counter()
            res = self._iphlp.IcmpSendEcho2Ex(
                h_icmp,
                None,
                None,
                None,
                src_addr,
                dest_addr,
                payload,
                len(payload),
                None,
                reply_buf,
                reply_size,
                timeout_ms,
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            if res > 0:
                reply = ctypes.cast(reply_buf, ctypes.POINTER(ICMP_ECHO_REPLY)).contents
                if reply.Status == 0:
                    return round(elapsed_ms, 1)
            return None
        except Exception:
            return None
        finally:
            self._iphlp.IcmpCloseHandle(h_icmp)

    def run_throughput_test(self, demo_mode: bool = False) -> Dict[str, Any]:
        """
        Measure real-world HTTP download throughput (Mbps) and unloaded/loaded latency.
        Uses Cloudflare's speed test CDN endpoint or falls back to calibrated estimation.
        """
        live = self.read_live_signal(demo_mode=demo_mode)
        if demo_mode:
            pct = live.get("signal_pct", 80)
            dl_mbps = round(max(15.0, (pct / 100.0) * 640.0 + random.uniform(-25, 25)), 1)
            return {
                "ok": True,
                "timestamp": time.time(),
                "iso_time": time.strftime("%H:%M:%S"),
                "download_mbps": dl_mbps,
                "link_speed_mbps": live.get("tx_mbps", 1201.0),
                "efficiency_pct": round((dl_mbps / max(1.0, live.get("tx_mbps", 1201.0))) * 100, 1),
                "rssi_dbm": live.get("rssi_dbm", -50),
                "gateway_ping_ms": live.get("gateway_ping_ms", 3.2),
                "source": "Simulated Speed Test",
            }

        # Download 10 MB payload over HTTPS (capped at 4.5 seconds max)
        test_urls = [
            "https://speed.cloudflare.com/__down?bytes=10000000",
            "https://proof.ovh.net/files/10Mb.dat",
        ]
        total_bytes = 0
        elapsed = 0.0
        used_source = "Cloudflare Edge CDN"

        for url in test_urls:
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0 Wi-Fi-Walk-Scanner/2.0"}
                )
                t0 = time.perf_counter()
                with urllib.request.urlopen(req, timeout=4.0) as resp:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        total_bytes += len(chunk)
                        if time.perf_counter() - t0 >= 3.5:
                            break
                elapsed = time.perf_counter() - t0
                if total_bytes > 131072 and elapsed > 0.05:
                    break
            except Exception:
                total_bytes = 0
                continue

        link_mbps = live.get("tx_mbps", 0.0)
        if total_bytes > 0 and elapsed > 0.05:
            dl_mbps = round((total_bytes * 8.0) / (elapsed * 1_000_000.0), 1)
        else:
            # Fallback if firewall/proxy blocks external speedtest endpoints
            pct = live.get("signal_pct", 50) / 100.0
            dl_mbps = round(link_mbps * 0.55 * pct, 1)
            used_source = "PHY Link Estimation (External CDN blocked)"

        eff = round((dl_mbps / max(1.0, link_mbps)) * 100.0, 1) if link_mbps > 0 else 0.0
        return {
            "ok": True,
            "timestamp": time.time(),
            "iso_time": time.strftime("%H:%M:%S"),
            "download_mbps": dl_mbps,
            "link_speed_mbps": link_mbps,
            "efficiency_pct": min(100.0, eff),
            "rssi_dbm": live.get("rssi_dbm", -100),
            "gateway_ping_ms": live.get("gateway_ping_ms"),
            "source": used_source,
        }

    def _disconnected_reading(self, reason: str) -> Dict[str, Any]:
        return {
            "connected": False,
            "timestamp": time.time(),
            "iso_time": time.strftime("%H:%M:%S"),
            "ssid": "Disconnected",
            "bssid": "None",
            "rssi_dbm": -100,
            "signal_pct": 0,
            "quality_label": "Disconnected",
            "quality_level": "dead",
            "quality_color": "#64748b",
            "channel": 0,
            "band": "None",
            "freq_mhz": 0,
            "tx_mbps": 0.0,
            "rx_mbps": 0.0,
            "gateway_ip": "",
            "gateway_ping_ms": None,
            "internet_ping_ms": None,
            "packet_lost": True,
            "radio_type": reason,
            "adapter_name": "Wi-Fi",
            "adapter_description": reason,
            "adapter_mac": "",
            "tx_frames": 0,
            "rx_frames": 0,
            "location_permission_granted": self._location_permission_granted,
            "demo_mode": False,
        }

    # ========================================================================
    # Simulation / Demo Mode Generator
    # ========================================================================

    def _generate_demo_reading(self) -> Dict[str, Any]:
        """Simulate realistic Wi-Fi walk-test data including mesh AP roaming."""
        self._sim_step += 1
        t = self._sim_step * 0.18
        # Smoothly oscillate signal between -40 dBm and -78 dBm as user "walks"
        wave = math.sin(t) * 16 + math.cos(t * 0.45) * 6
        rssi_dbm = int(round(-58 + wave + random.uniform(-1.5, 1.5)))
        rssi_dbm = max(-86, min(-36, rssi_dbm))

        # Simulate mesh roaming when signal drops below -72 dBm
        mesh_aps = [
            {"bssid": "10:7C:61:42:F9:E0", "ch": 100, "band": "5 GHz", "name": "Main Router (5G)"},
            {"bssid": "10:7C:61:42:FA:12", "ch": 36, "band": "5 GHz", "name": "Hallway Mesh Node (5G)"},
            {"bssid": "10:7C:61:42:F9:E1", "ch": 6, "band": "2.4 GHz", "name": "Main Router (2.4G)"},
        ]
        if rssi_dbm < -73 and self._sim_step % 8 == 0:
            self._sim_ap_index = (self._sim_ap_index + 1) % len(mesh_aps)
            rssi_dbm = -52  # Signal jumps up after roaming to closer node

        ap = mesh_aps[self._sim_ap_index]
        band, freq = channel_to_band_and_freq(ap["ch"])
        pct = dbm_to_quality_pct(rssi_dbm)
        quality = classify_signal(rssi_dbm)
        link_speed = round(max(72.0, min(1201.0, (pct / 100.0) * 1201.0)), 1)
        gw_ping = round(max(1.2, (100 - pct) * 0.35 + random.uniform(0.5, 4.5)), 1)
        inet_ping = round(gw_ping + random.uniform(9.0, 16.0), 1)
        pkt_lost = rssi_dbm < -81 and random.random() < 0.35

        return {
            "connected": True,
            "timestamp": time.time(),
            "iso_time": time.strftime("%H:%M:%S"),
            "ssid": "R4F5G 3 (Demo Walk)",
            "bssid": ap["bssid"],
            "rssi_dbm": rssi_dbm,
            "signal_pct": pct,
            "quality_label": quality["label"],
            "quality_level": quality["level"],
            "quality_color": quality["color"],
            "channel": ap["ch"],
            "band": band,
            "freq_mhz": freq,
            "tx_mbps": link_speed,
            "rx_mbps": link_speed,
            "gateway_ip": "192.168.69.1",
            "gateway_ping_ms": None if pkt_lost else gw_ping,
            "internet_ping_ms": None if pkt_lost else inet_ping,
            "packet_lost": pkt_lost,
            "radio_type": "802.11be (Wi-Fi 7 Demo)",
            "adapter_name": "Wi-Fi",
            "adapter_description": "Simulated Walk-Test Mode (MediaTek Wi-Fi 7 MT7925)",
            "adapter_mac": "F4:4E:B4:18:88:15",
            "tx_frames": 350000 + self._sim_step * 42,
            "rx_frames": 280000 + self._sim_step * 58,
            "location_permission_granted": True,
            "demo_mode": True,
        }

    def _generate_demo_neighbors(self) -> Dict[str, Any]:
        """Generate realistic neighboring networks across 2.4 / 5 / 6 GHz for Demo Mode."""
        demo_nets = [
            {"ssid": "R4F5G 3", "bssid": "10:7C:61:42:F9:E0", "signal_pct": 88, "rssi_dbm": -46, "channel": 100, "band": "5 GHz", "radio_type": "802.11be", "auth": "WPA3-Personal", "is_connected": True},
            {"ssid": "R4F5G 3", "bssid": "10:7C:61:42:FA:12", "signal_pct": 64, "rssi_dbm": -68, "channel": 36, "band": "5 GHz", "radio_type": "802.11be", "auth": "WPA3-Personal", "is_connected": False},
            {"ssid": "R4F5G 3", "bssid": "10:7C:61:42:F9:E1", "signal_pct": 82, "rssi_dbm": -54, "channel": 6, "band": "2.4 GHz", "radio_type": "802.11ax", "auth": "WPA2-Personal", "is_connected": False},
            {"ssid": "ATT-Fiber-882", "bssid": "A4:91:B1:22:19:01", "signal_pct": 58, "rssi_dbm": -71, "channel": 6, "band": "2.4 GHz", "radio_type": "802.11ax", "auth": "WPA2-Personal", "is_connected": False},
            {"ssid": "ATT-Fiber-882-5G", "bssid": "A4:91:B1:22:19:02", "signal_pct": 46, "rssi_dbm": -77, "channel": 36, "band": "5 GHz", "radio_type": "802.11ax", "auth": "WPA3-Personal", "is_connected": False},
            {"ssid": "Xfinity_Home", "bssid": "C8:3A:35:90:11:4B", "signal_pct": 62, "rssi_dbm": -69, "channel": 1, "band": "2.4 GHz", "radio_type": "802.11n", "auth": "WPA2-Personal", "is_connected": False},
            {"ssid": "Neighbor_Mesh_6E", "bssid": "E0:63:DA:55:81:99", "signal_pct": 50, "rssi_dbm": -75, "channel": 149, "band": "5 GHz", "radio_type": "802.11ax", "auth": "WPA3-Personal", "is_connected": False},
            {"ssid": "SmartHome_IoT", "bssid": "44:D9:E7:09:3C:10", "signal_pct": 40, "rssi_dbm": -80, "channel": 11, "band": "2.4 GHz", "radio_type": "802.11n", "auth": "WPA2-Personal", "is_connected": False},
        ]
        return {
            "location_permission_granted": True,
            "message": f"Demo Mode: Showing {len(demo_nets)} simulated access points.",
            "networks": demo_nets,
            "channel_usage": self._summarize_channels(demo_nets),
        }
