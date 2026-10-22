"""
app.py
======
Local Web Application server and continuous background Wi-Fi walk-test controller.

Features:
- Continuous background polling of the PC's Wi-Fi card via `WindowsWifiScanner`
- Real-time time-series signal history (RSSI dBm, Signal %, Link Speed, Channel, Band)
- Room / Waypoint tagging & automatic per-room coverage aggregation (Min/Avg/Max dBm)
- Automatic Mesh / AP Roaming detection (logs BSSID, Band, or Channel handoffs)
- Interactive 2D Floorplan / House Grid Heatmap point storage
- Neighboring networks & channel congestion scanner (2.4 GHz / 5 GHz / 6 GHz)
- One-click CSV & JSON session export
- Zero external pip dependencies required (uses Python's built-in `ThreadingHTTPServer`)
"""

from __future__ import annotations

import csv
import io
import json
import mimetypes
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

from wifi_engine import WindowsWifiScanner, classify_signal

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
SESSIONS_DIR = BASE_DIR / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)
AUTOSAVE_FILE = SESSIONS_DIR / "autosave_latest.json"


class WalkTestSession:
    """
    Manages the state of the active Wi-Fi walk test:
    - Live signal readings history
    - Active room tag & per-room statistics
    - Roaming events log (BSSID / Channel / Band changes)
    - Floorplan heatmap points
    """

    def __init__(self, scanner: WindowsWifiScanner) -> None:
        self.scanner = scanner
        self.lock = threading.Lock()

        # Configuration
        self.poll_interval: float = 1.0  # seconds
        self.paused: bool = False
        self.demo_mode: bool = False
        self.current_room: str = "Living Room"
        self.max_history: int = 1800  # 30 minutes at 1s interval

        # State collections
        self.latest_reading: Dict[str, Any] = {}
        self.history: List[Dict[str, Any]] = []
        self.roaming_events: List[Dict[str, Any]] = []
        self.heatmap_points: List[Dict[str, Any]] = []
        self.room_events: List[Dict[str, Any]] = []
        self.speed_tests: Dict[str, Dict[str, Any]] = {}
        self.floorplan_data_url: Optional[str] = None

        # Track previous AP state for roaming detection
        self._prev_bssid: Optional[str] = None
        self._prev_channel: Optional[int] = None
        self._prev_band: Optional[str] = None
        self._prev_rssi: Optional[int] = None
        self._last_autosave: float = time.time()

        # Restore previous autosave if present
        self._load_from_file(AUTOSAVE_FILE, silent=True)

        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def _poll_loop(self) -> None:
        """Background thread that continuously polls the Wi-Fi hardware."""
        while self._running:
            start = time.time()
            if not self.paused:
                try:
                    reading = self.scanner.read_live_signal(demo_mode=self.demo_mode)
                    self._record_reading(reading)
                    if time.time() - self._last_autosave >= 15.0:
                        self._last_autosave = time.time()
                        self.save_snapshot("autosave_latest", is_auto=True)
                except Exception as exc:
                    print(f"[Scanner Error] {exc}", file=sys.stderr)

            elapsed = time.time() - start
            sleep_for = max(0.1, self.poll_interval - elapsed)
            time.sleep(sleep_for)

    def _record_reading(self, reading: Dict[str, Any]) -> None:
        with self.lock:
            reading["room"] = self.current_room
            self.latest_reading = reading

            # Check for AP / Band / Channel roaming handoff
            if reading.get("connected"):
                cur_bssid = reading.get("bssid", "")
                cur_ch = reading.get("channel", 0)
                cur_band = reading.get("band", "")
                cur_rssi = reading.get("rssi_dbm", -100)

                if (
                    self._prev_bssid is not None
                    and self._prev_bssid != "None"
                    and cur_bssid != "None"
                    and (cur_bssid != self._prev_bssid or cur_ch != self._prev_channel)
                ):
                    event_type = "BSSID Roam" if cur_bssid != self._prev_bssid else "Channel Switch"
                    if cur_band != self._prev_band:
                        event_type = f"Band Steering ({self._prev_band} → {cur_band})"

                    self.roaming_events.insert(
                        0,
                        {
                            "timestamp": reading["timestamp"],
                            "iso_time": reading["iso_time"],
                            "room": self.current_room,
                            "event_type": event_type,
                            "from_bssid": self._prev_bssid,
                            "to_bssid": cur_bssid,
                            "from_channel": self._prev_channel,
                            "to_channel": cur_ch,
                            "from_band": self._prev_band,
                            "to_band": cur_band,
                            "rssi_before": self._prev_rssi,
                            "rssi_after": cur_rssi,
                        },
                    )
                    # Keep latest 100 roaming events
                    if len(self.roaming_events) > 100:
                        self.roaming_events.pop()

                self._prev_bssid = cur_bssid
                self._prev_channel = cur_ch
                self._prev_band = cur_band
                self._prev_rssi = cur_rssi

            self.history.append(reading)
            if len(self.history) > self.max_history:
                self.history.pop(0)

    def set_room(self, room_name: str) -> None:
        room_name = room_name.strip() or "Unlabeled"
        with self.lock:
            if room_name != self.current_room:
                self.current_room = room_name
                self.room_events.append(
                    {
                        "timestamp": time.time(),
                        "iso_time": time.strftime("%H:%M:%S"),
                        "room": room_name,
                    }
                )
                if self.latest_reading:
                    self.latest_reading["room"] = room_name

    def add_heatmap_point(self, x: float, y: float, label: str = "") -> Dict[str, Any]:
        """Record a floorplan heatmap sample at normalized coordinates (0.0 - 1.0)."""
        with self.lock:
            live = self.latest_reading or self.scanner.read_live_signal(demo_mode=self.demo_mode)
            rssi = live.get("rssi_dbm", -70)
            q = classify_signal(rssi)
            pt = {
                "id": int(time.time() * 1000),
                "x": round(max(0.0, min(1.0, float(x))), 4),
                "y": round(max(0.0, min(1.0, float(y))), 4),
                "room": label.strip() or self.current_room,
                "rssi_dbm": rssi,
                "signal_pct": live.get("signal_pct", 0),
                "tx_mbps": live.get("tx_mbps", 0.0),
                "channel": live.get("channel", 0),
                "band": live.get("band", ""),
                "bssid": live.get("bssid", ""),
                "quality_label": q["label"],
                "quality_color": q["color"],
                "iso_time": time.strftime("%H:%M:%S"),
            }
            self.heatmap_points.append(pt)
            return pt

    def remove_last_heatmap_point(self) -> None:
        with self.lock:
            if self.heatmap_points:
                self.heatmap_points.pop()

    def clear_heatmap(self) -> None:
        with self.lock:
            self.heatmap_points.clear()

    def clear_session(self) -> None:
        with self.lock:
            self.history.clear()
            self.roaming_events.clear()
            self.room_events.clear()
            self.speed_tests.clear()
            self._prev_bssid = None
            self._prev_channel = None
            self._prev_band = None
            self._prev_rssi = None
        self.save_snapshot("autosave_latest", is_auto=True)

    def run_room_speed_test(self) -> Dict[str, Any]:
        """Run an on-demand HTTP throughput speed test for the active room."""
        res = self.scanner.run_throughput_test(demo_mode=self.demo_mode)
        with self.lock:
            room = self.current_room
            res["room"] = room
            self.speed_tests[room] = res
        self.save_snapshot("autosave_latest", is_auto=True)
        return res

    def save_snapshot(self, name: str = "", is_auto: bool = False) -> Dict[str, Any]:
        """Persist current walk-test session (history, heatmap, floorplan, speed tests) to disk."""
        clean_name = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in name.strip())
        if not clean_name:
            clean_name = f"walk_test_{time.strftime('%Y%m%d_%H%M%S')}"
        if not clean_name.endswith(".json"):
            clean_name += ".json"

        target_path = AUTOSAVE_FILE if is_auto else (SESSIONS_DIR / clean_name)
        with self.lock:
            payload = {
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "current_room": self.current_room,
                "history": self.history[-1800:],
                "roaming_events": self.roaming_events,
                "heatmap_points": self.heatmap_points,
                "speed_tests": self.speed_tests,
                "floorplan_data_url": self.floorplan_data_url,
            }
        try:
            target_path.write_text(json.dumps(payload), encoding="utf-8")
            return {"ok": True, "filename": target_path.name, "saved_at": payload["saved_at"]}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _load_from_file(self, path: Path, silent: bool = False) -> bool:
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            with self.lock:
                self.current_room = data.get("current_room", self.current_room)
                self.history = data.get("history", [])
                self.roaming_events = data.get("roaming_events", [])
                self.heatmap_points = data.get("heatmap_points", [])
                self.speed_tests = data.get("speed_tests", {})
                if data.get("floorplan_data_url"):
                    self.floorplan_data_url = data.get("floorplan_data_url")
            return True
        except Exception as exc:
            if not silent:
                print(f"[Load Session Error] {exc}", file=sys.stderr)
            return False

    def list_saved_sessions(self) -> List[Dict[str, Any]]:
        files: List[Dict[str, Any]] = []
        for p in sorted(SESSIONS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                files.append(
                    {
                        "filename": p.name,
                        "saved_at": raw.get("saved_at", ""),
                        "samples": len(raw.get("history", [])),
                        "heatmap_pins": len(raw.get("heatmap_points", [])),
                        "is_autosave": p.name == AUTOSAVE_FILE.name,
                    }
                )
            except Exception:
                continue
        return files

    def compute_room_stats(self) -> List[Dict[str, Any]]:
        """Aggregate signal, ping, jitter, packet loss, and throughput statistics by room."""
        with self.lock:
            grouped: Dict[str, List[Dict[str, Any]]] = {}
            for item in self.history:
                if not item.get("connected"):
                    continue
                rm = item.get("room", "Unlabeled")
                grouped.setdefault(rm, []).append(item)
            speed_map = dict(self.speed_tests)

        stats: List[Dict[str, Any]] = []
        for rm, samples in grouped.items():
            rssis = [s["rssi_dbm"] for s in samples]
            txs = [s.get("tx_mbps", 0.0) for s in samples]
            bssids = sorted(list({s.get("bssid", "") for s in samples if s.get("bssid")}))
            bands = sorted(list({s.get("band", "") for s in samples if s.get("band")}))

            # Ping / Jitter / Loss calculation (prefer gateway ping, fallback to internet ping)
            pings = [
                (s.get("gateway_ping_ms") if s.get("gateway_ping_ms") is not None else s.get("internet_ping_ms"))
                for s in samples
            ]
            valid_pings = [float(p) for p in pings if p is not None]
            lost_count = sum(1 for s in samples if s.get("packet_lost"))
            loss_pct = round((lost_count / len(samples)) * 100.0, 1) if samples else 0.0
            avg_ping = round(sum(valid_pings) / len(valid_pings), 1) if valid_pings else None
            jitter = 0.0
            if len(valid_pings) >= 2:
                diffs = [abs(valid_pings[i] - valid_pings[i - 1]) for i in range(1, len(valid_pings))]
                jitter = round(sum(diffs) / len(diffs), 1)

            avg_rssi = int(round(sum(rssis) / len(rssis)))
            q = classify_signal(avg_rssi)
            sp = speed_map.get(rm)
            stats.append(
                {
                    "room": rm,
                    "samples": len(samples),
                    "avg_rssi_dbm": avg_rssi,
                    "min_rssi_dbm": min(rssis),
                    "max_rssi_dbm": max(rssis),
                    "avg_tx_mbps": round(sum(txs) / len(txs), 1),
                    "avg_ping_ms": avg_ping,
                    "jitter_ms": jitter,
                    "loss_pct": loss_pct,
                    "measured_dl_mbps": sp["download_mbps"] if sp else None,
                    "quality_label": q["label"],
                    "quality_level": q["level"],
                    "quality_color": q["color"],
                    "bssids": bssids,
                    "bands": bands,
                }
            )
        stats.sort(key=lambda x: x["avg_rssi_dbm"], reverse=True)
        return stats

    def compute_advisor_recommendations(
        self, room_stats: List[Dict[str, Any]], roaming_events: List[Dict[str, Any]]
    ) -> List[Dict[str, str]]:
        """
        Analyze walk-test telemetry across rooms to generate actionable mesh/router
        placement and configuration recommendations.
        """
        recs: List[Dict[str, str]] = []
        if not room_stats:
            return [
                {
                    "severity": "info",
                    "title": "Walk Around to Generate Coverage Insights",
                    "detail": "Select rooms as you walk through your home. The advisor will analyze signal drop-offs, latency jitter, sticky AP roaming, and band steering.",
                }
            ]

        dead_rooms = [r for r in room_stats if r["avg_rssi_dbm"] <= -76]
        fair_rooms = [r for r in room_stats if -75 <= r["avg_rssi_dbm"] <= -68]
        best_rooms = [r for r in room_stats if r["avg_rssi_dbm"] >= -60]
        high_jitter_rooms = [r for r in room_stats if (r.get("jitter_ms") or 0) > 12.0 or (r.get("loss_pct") or 0) > 5.0]

        if dead_rooms:
            names = ", ".join(f"{r['room']} ({r['avg_rssi_dbm']} dBm)" for r in dead_rooms)
            midway = fair_rooms[0]["room"] if fair_rooms else (best_rooms[-1]["room"] if best_rooms else "a central hallway")
            recs.append(
                {
                    "severity": "critical",
                    "title": f"Coverage Dead Zone Detected in { len(dead_rooms) } Room(s)",
                    "detail": f"Weak signal measured in {names}. Place a Mesh Node or Wi-Fi Extender halfway between your main router and {dead_rooms[0]['room']} (ideally near {midway} where signal is still above -65 dBm).",
                }
            )

        if fair_rooms and not dead_rooms:
            names = ", ".join(f"{r['room']} ({r['avg_rssi_dbm']} dBm)" for r in fair_rooms)
            recs.append(
                {
                    "severity": "warning",
                    "title": "Moderate Signal Drop-Off in Fringe Rooms",
                    "detail": f"{names} have fair signal (-68 to -75 dBm). For 4K streaming or low-latency video calls in these rooms, elevate your router off the floor or enable 80MHz/160MHz channel width on 5 GHz.",
                }
            )

        if high_jitter_rooms:
            names = ", ".join(
                f"{r['room']} (Jitter: {r['jitter_ms']}ms, Loss: {r['loss_pct']}%)"
                for r in high_jitter_rooms
            )
            recs.append(
                {
                    "severity": "warning",
                    "title": "Latency Spikes / Packet Loss Detected",
                    "detail": f"Unstable ping detected in {names}. This can cause Teams/Zoom audio dropouts even when signal bars look okay. Check the Channel Congestion chart below for co-channel interference.",
                }
            )

        # Sticky Client check: signal dropped below -75 dBm without any roaming event
        overall_min = min(r["min_rssi_dbm"] for r in room_stats)
        if overall_min <= -75 and len(roaming_events) == 0:
            recs.append(
                {
                    "severity": "info",
                    "title": "No Mesh Roaming Triggered ('Sticky Client' Check)",
                    "detail": f"Signal dropped to {overall_min} dBm without switching BSSIDs or bands. If you have a single router, this is normal. If you have a Mesh system, enable 802.11k/v/r Fast Roaming or lower your Wi-Fi card's Roaming Aggressiveness threshold in Device Manager.",
                }
            )

        if not recs:
            best = room_stats[0]
            recs.append(
                {
                    "severity": "good",
                    "title": "Strong Whole-Home Wi-Fi Coverage!",
                    "detail": f"All {len(room_stats)} tested room(s) maintain healthy signal levels (best: {best['room']} at {best['avg_rssi_dbm']} dBm, {best['avg_tx_mbps']} Mbps link speed) with low jitter.",
                }
            )

        return recs

    def get_status_payload(self, history_limit: int = 180) -> Dict[str, Any]:
        with self.lock:
            recent = self.history[-history_limit:]
            latest = self.latest_reading
            roaming = list(self.roaming_events[:25])
            heatmap = list(self.heatmap_points)
            total_samples = len(self.history)
            connected_items = [h for h in self.history if h.get("connected")]
            all_rssis = [h["rssi_dbm"] for h in connected_items]
            pings = [
                (h.get("gateway_ping_ms") if h.get("gateway_ping_ms") is not None else h.get("internet_ping_ms"))
                for h in connected_items[-60:]
            ]
            valid_pings = [float(p) for p in pings if p is not None]
            lost_recent = sum(1 for h in connected_items[-60:] if h.get("packet_lost"))
            recent_count = len(connected_items[-60:])

        jitter_ms = 0.0
        if len(valid_pings) >= 2:
            diffs = [abs(valid_pings[i] - valid_pings[i - 1]) for i in range(1, len(valid_pings))]
            jitter_ms = round(sum(diffs) / len(diffs), 1)

        loss_pct = round((lost_recent / recent_count) * 100.0, 1) if recent_count > 0 else 0.0
        avg_ping_ms = round(sum(valid_pings) / len(valid_pings), 1) if valid_pings else None

        room_stats = self.compute_room_stats()
        advisor = self.compute_advisor_recommendations(room_stats, roaming)

        session_summary = {
            "total_samples": total_samples,
            "min_rssi": min(all_rssis) if all_rssis else -100,
            "max_rssi": max(all_rssis) if all_rssis else -100,
            "avg_rssi": int(round(sum(all_rssis) / len(all_rssis))) if all_rssis else -100,
            "avg_ping_ms": avg_ping_ms,
            "jitter_ms": jitter_ms,
            "loss_pct": loss_pct,
            "roam_count": len(roaming),
            "heatmap_count": len(heatmap),
        }

        return {
            "config": {
                "poll_interval": self.poll_interval,
                "paused": self.paused,
                "demo_mode": self.demo_mode,
                "current_room": self.current_room,
            },
            "latest": latest,
            "session": session_summary,
            "room_stats": room_stats,
            "advisor": advisor,
            "speed_tests": self.speed_tests,
            "roaming_events": roaming,
            "heatmap_points": heatmap,
            "history": recent,
            "saved_sessions": self.list_saved_sessions(),
        }

    def export_csv(self) -> str:
        with self.lock:
            rows = list(self.history)

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "Timestamp",
                "Time",
                "Room",
                "SSID",
                "BSSID",
                "RSSI (dBm)",
                "Signal (%)",
                "Quality",
                "Band",
                "Channel",
                "Freq (MHz)",
                "TX Link Speed (Mbps)",
                "RX Link Speed (Mbps)",
                "Gateway Ping (ms)",
                "Internet Ping (ms)",
                "Packet Lost",
                "Radio Type",
                "Adapter",
            ]
        )
        for r in rows:
            writer.writerow(
                [
                    round(r.get("timestamp", 0), 2),
                    r.get("iso_time", ""),
                    r.get("room", ""),
                    r.get("ssid", ""),
                    r.get("bssid", ""),
                    r.get("rssi_dbm", ""),
                    r.get("signal_pct", ""),
                    r.get("quality_label", ""),
                    r.get("band", ""),
                    r.get("channel", ""),
                    r.get("freq_mhz", ""),
                    r.get("tx_mbps", ""),
                    r.get("rx_mbps", ""),
                    r.get("gateway_ping_ms", ""),
                    r.get("internet_ping_ms", ""),
                    r.get("packet_lost", False),
                    r.get("radio_type", ""),
                    r.get("adapter_description", ""),
                ]
            )
        return output.getvalue()


# ============================================================================
# HTTP Request Handler
# ============================================================================

scanner_engine = WindowsWifiScanner()
walk_session = WalkTestSession(scanner_engine)


class WifiScannerHTTPHandler(BaseHTTPRequestHandler):
    """Serves the static frontend dashboard and JSON REST API endpoints."""

    def log_message(self, format: str, *args: Any) -> None:
        # Keep terminal output clean during continuous 1s polling
        pass

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/api/status":
            limit = int(query.get("limit", ["180"])[0])
            self._send_json(walk_session.get_status_payload(history_limit=limit))
            return

        if path == "/api/neighbors":
            trigger = query.get("scan", ["0"])[0] == "1"
            res = scanner_engine.scan_neighbors(
                demo_mode=walk_session.demo_mode, trigger_hardware_scan=trigger
            )
            self._send_json(res)
            return

        if path == "/api/floorplan":
            self._send_json({"floorplan_data_url": walk_session.floorplan_data_url})
            return

        if path == "/api/sessions":
            self._send_json({"sessions": walk_session.list_saved_sessions()})
            return

        if path == "/api/export/csv":
            csv_data = walk_session.export_csv().encode("utf-8")
            filename = f"wifi_walk_test_{time.strftime('%Y%m%d_%H%M%S')}.csv"
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(csv_data)))
            self.end_headers()
            self.wfile.write(csv_data)
            return

        if path == "/api/export/json":
            payload = walk_session.get_status_payload(history_limit=5000)
            raw = json.dumps(payload, indent=2).encode("utf-8")
            filename = f"wifi_walk_test_{time.strftime('%Y%m%d_%H%M%S')}.json"
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        # Serve static files
        if path == "/" or path == "":
            file_path = STATIC_DIR / "index.html"
        else:
            rel = path.lstrip("/")
            file_path = (STATIC_DIR / rel).resolve()
            if not str(file_path).startswith(str(STATIC_DIR.resolve())):
                self.send_error(403, "Forbidden")
                return

        if not file_path.exists() or not file_path.is_file():
            self.send_error(404, "File not found")
            return

        mime, _ = mimetypes.guess_type(str(file_path))
        content = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = self._read_json_body()

        if path == "/api/room":
            room = str(body.get("room", "Living Room"))
            walk_session.set_room(room)
            self._send_json({"ok": True, "current_room": walk_session.current_room})
            return

        if path == "/api/config":
            if "poll_interval" in body:
                val = float(body["poll_interval"])
                walk_session.poll_interval = max(0.25, min(10.0, val))
            if "paused" in body:
                walk_session.paused = bool(body["paused"])
            if "demo_mode" in body:
                walk_session.demo_mode = bool(body["demo_mode"])
            if body.get("clear_session"):
                walk_session.clear_session()
            self._send_json(
                {
                    "ok": True,
                    "config": {
                        "poll_interval": walk_session.poll_interval,
                        "paused": walk_session.paused,
                        "demo_mode": walk_session.demo_mode,
                        "current_room": walk_session.current_room,
                    },
                }
            )
            return

        if path == "/api/heatmap/point":
            action = body.get("action", "add")
            if action == "clear":
                walk_session.clear_heatmap()
                self._send_json({"ok": True, "points": []})
            elif action == "undo":
                walk_session.remove_last_heatmap_point()
                self._send_json({"ok": True, "points": walk_session.heatmap_points})
            else:
                pt = walk_session.add_heatmap_point(
                    x=float(body.get("x", 0.5)),
                    y=float(body.get("y", 0.5)),
                    label=str(body.get("room", "")),
                )
                self._send_json({"ok": True, "point": pt, "points": walk_session.heatmap_points})
            return

        if path == "/api/open-location-settings":
            try:
                os.startfile("ms-settings:privacy-location")
                self._send_json({"ok": True})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=500)
            return

        if path == "/api/speedtest":
            res = walk_session.run_room_speed_test()
            self._send_json(res)
            return

        if path == "/api/session/save":
            name = str(body.get("name", ""))
            res = walk_session.save_snapshot(name=name, is_auto=False)
            self._send_json(res)
            return

        if path == "/api/session/load":
            filename = os.path.basename(str(body.get("filename", "")))
            target = SESSIONS_DIR / filename
            ok = walk_session._load_from_file(target)
            self._send_json({"ok": ok, "filename": filename})
            return

        if path == "/api/floorplan":
            walk_session.floorplan_data_url = body.get("floorplan_data_url")
            walk_session.save_snapshot("autosave_latest", is_auto=True)
            self._send_json({"ok": True})
            return

        self.send_error(404, "Endpoint not found")


def main() -> None:
    host = "127.0.0.1"
    port = int(os.environ.get("WIFI_SCANNER_PORT", "8765"))
    url = f"http://{host}:{port}"

    server = ThreadingHTTPServer((host, port), WifiScannerHTTPHandler)
    print("=" * 64)
    print("  Wi-Fi House Walk-Test & Coverage Scanner")
    print("=" * 64)
    print(f"  Dashboard URL : {url}")
    print("  Hardware API  : Windows Native Wi-Fi (wlanapi.dll + iphlpapi.dll)")
    print("  Press Ctrl+C in this terminal to stop the server.")
    print("=" * 64)

    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down Wi-Fi Scanner server...")
        server.server_close()


if __name__ == "__main__":
    main()
