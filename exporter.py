"""Prometheus exporter for Cogeco's Sagemcom GUI JSON-RPC interface."""

from __future__ import annotations

import hashlib
import json
import os
import random
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ADDRESS = os.environ.get("ADDRESS", "192.168.100.1")
# Prefer MODEM_USERNAME: macOS/zsh export USERNAME as the local account name.
USERNAME = os.environ.get("MODEM_USERNAME") or os.environ.get("USERNAME", "admin")
PASSWORD = os.environ.get("PASSWORD", "")
LISTEN_ADDRESS = os.environ.get("LISTEN_ADDRESS", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9488"))
PROBE_TARGETS = tuple(
    target.strip()
    for target in os.environ.get("PROBE_TARGETS", "1.1.1.1:443,8.8.8.8:443").split(",")
    if target.strip()
)
CHECK_API_LOGIN = os.environ.get("CHECK_API_LOGIN", "false").lower() == "true"
COLLECT_DOCSIS = os.environ.get("COLLECT_DOCSIS", "true").lower() == "true"
# Extra modem API groups collected in the same JSON-RPC session as DOCSIS.
COLLECT_ETHERNET = os.environ.get("COLLECT_ETHERNET", "true").lower() == "true"
COLLECT_SYSTEM = os.environ.get("COLLECT_SYSTEM", "true").lower() == "true"

# Cogeco's GUI passes namespace objects, not the "gtw:tr181" option string.
# A string nss still logs in, but every getValue then returns XMO_UNKNOWN_PATH_ERR.
SESSION_NSS = (
    {"name": "gtw", "uri": "http://sagemcom.com/gateway-data"},
    {"name": "tr181", "uri": "http://sagemcom.com/tr181-data"},
)
XMO_NO_ERR = 16777238


def sha512(value: str) -> str:
    return hashlib.sha512(value.encode()).hexdigest()


class ModemClient:
    """Read-only client for the Sagemcom GUI's challenge-response protocol."""

    def __init__(self) -> None:
        self.url = f"https://{ADDRESS}/cgi/json-req"
        self.context = ssl._create_unverified_context()

    def request(self, session_id: str, nonce: str, request_id: int, actions: list[dict]) -> dict:
        cnonce = random.randrange(2**32)
        password_hash = sha512(PASSWORD)
        ha1 = sha512(f"{USERNAME}:{nonce}:{password_hash}")
        auth_key = sha512(f"{ha1}:{request_id}:{cnonce}:JSON:/cgi/json-req")
        payload = {
            "request": {
                "id": request_id,
                "session-id": session_id,
                "priority": True,
                "cnonce": cnonce,
                "auth-key": auth_key,
                "actions": actions,
            }
        }
        request = urllib.request.Request(
            self.url,
            data=urllib.parse.urlencode({"req": json.dumps(payload)}).encode(),
            method="POST",
        )
        with urllib.request.urlopen(request, context=self.context, timeout=30) as response:
            return json.load(response)["reply"]

    def login(self) -> tuple[str, str]:
        reply = self.request(
            "0",
            "",
            0,
            [
                {
                    "id": 0,
                    "method": "logIn",
                    "parameters": {
                        "user": USERNAME,
                        # Cogeco firmware does not return session credentials
                        # for a non-persistent GUI login, even though it accepts
                        # the request. We always log out after the scrape.
                        "persistent": "true",
                        "session-options": {
                            "nss": list(SESSION_NSS),
                            "context-flags": {"get-content-name": True, "local-time": True},
                            "capability-depth": 2,
                            "capability-flags": {
                                "name": True,
                                "default-value": False,
                                "restriction": True,
                                "description": False,
                            },
                            "time-format": "ISO_8601",
                            "jwt-auth": "true",
                        },
                    },
                }
            ],
        )
        action = reply["actions"][0]
        if action["error"]["code"] != XMO_NO_ERR:
            raise ValueError(action["error"]["description"])
        params = action["callbacks"][0]["parameters"]
        return str(params["id"]), str(params["nonce"])

    def logout(self, session_id: str, nonce: str, request_id: int = 1) -> None:
        try:
            self.request(session_id, nonce, request_id, [{"id": 0, "method": "logOut"}])
        except (KeyError, OSError, ValueError, urllib.error.URLError):
            pass

    def get_values(self, session_id: str, nonce: str, request_id: int, paths: tuple[str, ...]) -> list[object]:
        reply = self.request(
            session_id,
            nonce,
            request_id,
            [
                {
                    "id": index,
                    "method": "getValue",
                    "xpath": path,
                    # Required by the GUI's getValuesTree helper.
                    "options": {"capability-flags": {"interface": True}},
                }
                for index, path in enumerate(paths)
            ],
        )
        values: list[object] = []
        for action in reply["actions"]:
            if action["error"]["code"] != XMO_NO_ERR:
                raise ValueError(action["error"]["description"])
            values.append(action["callbacks"][0]["parameters"]["value"])
        return values

    def modem_snapshot(self) -> dict[str, object]:
        """One login: DOCSIS tables plus optional Ethernet and system sensors."""
        paths: list[str] = [
            "Device/Docsis/CableModem/Downstreams",
            "Device/Docsis/CableModem/Upstreams",
            "Device/DeviceInfo/UpTime",
            "Device/Docsis/CableModem/Status",
        ]
        if COLLECT_ETHERNET:
            paths.append("Device/Ethernet/Interfaces")
        if COLLECT_SYSTEM:
            paths.extend(
                (
                    "Device/DeviceInfo/ModelName",
                    "Device/DeviceInfo/SoftwareVersion",
                    "Device/DeviceInfo/HardwareVersion",
                    "Device/DeviceInfo/SerialNumber",
                    "Device/DeviceInfo/RebootCount",
                    "Device/DeviceInfo/MemoryStatus",
                    "Device/DeviceInfo/ProcessStatus",
                    "Device/DeviceInfo/TemperatureStatus",
                    "Device/Docsis/CableModem/ThermalThrottleState",
                )
            )

        session_id, nonce = self.login()
        try:
            values = self.get_values(session_id, nonce, 1, tuple(paths))
        finally:
            # Request ids must increase within a session; login used 0 and getValue used 1.
            self.logout(session_id, nonce, request_id=2)

        snapshot: dict[str, object] = {
            "downstream": values[0],
            "upstream": values[1],
            "uptime": values[2],
            "status": values[3],
        }
        index = 4
        if COLLECT_ETHERNET:
            snapshot["ethernet"] = values[index]
            index += 1
        if COLLECT_SYSTEM:
            snapshot["model"] = values[index]
            snapshot["software"] = values[index + 1]
            snapshot["hardware"] = values[index + 2]
            snapshot["serial"] = values[index + 3]
            snapshot["reboot_count"] = values[index + 4]
            snapshot["memory"] = values[index + 5]
            snapshot["process"] = values[index + 6]
            snapshot["temperature"] = values[index + 7]
            snapshot["thermal_throttle"] = values[index + 8]
        return snapshot

    def check(self) -> tuple[bool, float]:
        started = time.monotonic()
        session_id = nonce = ""
        try:
            session_id, nonce = self.login()
            return True, time.monotonic() - started
        except (KeyError, OSError, ValueError, urllib.error.URLError):
            return False, time.monotonic() - started
        finally:
            if session_id:
                self.logout(session_id, nonce)

    def check_gui(self) -> tuple[bool, float]:
        started = time.monotonic()
        request = urllib.request.Request(f"https://{ADDRESS}/2.0/gui/", method="GET")
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=5) as response:
                return 200 <= response.status < 400, time.monotonic() - started
        except (OSError, urllib.error.URLError):
            return False, time.monotonic() - started


def tcp_probe(target: str) -> tuple[bool, float]:
    host, port_text = target.rsplit(":", 1)
    started = time.monotonic()
    try:
        with socket.create_connection((host, int(port_text)), timeout=5):
            return True, time.monotonic() - started
    except OSError:
        return False, time.monotonic() - started


def metric(name: str, value: float, labels: dict[str, str] | None = None) -> str:
    if not labels:
        return f"{name} {value}\n"
    rendered = ",".join(f'{key}="{value}"' for key, value in labels.items())
    return f"{name}{{{rendered}}} {value}\n"


def numeric(value: object) -> float:
    return float(value) if value not in (None, "") else float("nan")


def unwrap(value: object, key: str) -> object:
    if isinstance(value, dict) and key in value:
        return value[key]
    return value


def interface_list(value: object) -> list[dict]:
    value = unwrap(value, "Interface")
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def docsis_channel_metrics(downstream: object, upstream: object) -> list[str]:
    lines = [
        "# HELP cogeco_sagemcom_docsis_channel_lock DOCSIS channel lock state.\n",
        "# TYPE cogeco_sagemcom_docsis_channel_lock gauge\n",
        "# HELP cogeco_sagemcom_docsis_channel_frequency_hertz DOCSIS channel frequency.\n",
        "# TYPE cogeco_sagemcom_docsis_channel_frequency_hertz gauge\n",
        "# HELP cogeco_sagemcom_docsis_channel_power_dbmv DOCSIS signal power.\n",
        "# TYPE cogeco_sagemcom_docsis_channel_power_dbmv gauge\n",
        "# HELP cogeco_sagemcom_docsis_downstream_snr_db DOCSIS downstream signal-to-noise ratio.\n",
        "# TYPE cogeco_sagemcom_docsis_downstream_snr_db gauge\n",
        "# HELP cogeco_sagemcom_docsis_downstream_codewords DOCSIS downstream codeword counts.\n",
        "# TYPE cogeco_sagemcom_docsis_downstream_codewords gauge\n",
    ]
    for direction, channels in (("downstream", downstream), ("upstream", upstream)):
        if not isinstance(channels, list):
            continue
        for index, channel in enumerate(channels):
            if not isinstance(channel, dict):
                continue
            labels = {
                "direction": direction,
                "channel": str(channel.get("ChannelID", index + 1)),
                "modulation": str(channel.get("Modulation", "unknown")),
            }
            lines.extend(
                [
                    metric(
                        "cogeco_sagemcom_docsis_channel_lock",
                        int(bool(channel.get("LockStatus", True))),
                        labels,
                    ),
                    metric(
                        "cogeco_sagemcom_docsis_channel_frequency_hertz",
                        numeric(channel.get("Frequency")),
                        labels,
                    ),
                    metric(
                        "cogeco_sagemcom_docsis_channel_power_dbmv",
                        numeric(channel.get("PowerLevel")),
                        labels,
                    ),
                ]
            )
            if direction == "downstream":
                lines.append(
                    metric(
                        "cogeco_sagemcom_docsis_downstream_snr_db",
                        numeric(channel.get("SNR")),
                        labels,
                    )
                )
                for source, error_type in (
                    ("CorrectableCodewords", "correctable"),
                    ("UncorrectableCodewords", "uncorrectable"),
                    ("UnerroredCodewords", "unerrored"),
                ):
                    lines.append(
                        metric(
                            "cogeco_sagemcom_docsis_downstream_codewords",
                            numeric(channel.get(source)),
                            labels | {"type": error_type},
                        )
                    )
    return lines


def ethernet_metrics(interfaces: object) -> list[str]:
    lines = [
        "# HELP cogeco_sagemcom_ethernet_up Whether an Ethernet interface is up.\n",
        "# TYPE cogeco_sagemcom_ethernet_up gauge\n",
        "# HELP cogeco_sagemcom_ethernet_speed_mbps Current Ethernet link speed.\n",
        "# TYPE cogeco_sagemcom_ethernet_speed_mbps gauge\n",
        "# HELP cogeco_sagemcom_ethernet_info Ethernet interface metadata.\n",
        "# TYPE cogeco_sagemcom_ethernet_info gauge\n",
        "# HELP cogeco_sagemcom_ethernet_bytes_total Ethernet byte counters.\n",
        "# TYPE cogeco_sagemcom_ethernet_bytes_total counter\n",
        "# HELP cogeco_sagemcom_ethernet_packets_total Ethernet packet counters.\n",
        "# TYPE cogeco_sagemcom_ethernet_packets_total counter\n",
        "# HELP cogeco_sagemcom_ethernet_errors_total Ethernet error counters.\n",
        "# TYPE cogeco_sagemcom_ethernet_errors_total counter\n",
        "# HELP cogeco_sagemcom_ethernet_discards_total Ethernet discard counters.\n",
        "# TYPE cogeco_sagemcom_ethernet_discards_total counter\n",
    ]
    for iface in interface_list(interfaces):
        alias = str(iface.get("Alias") or iface.get("uid") or "unknown")
        labels = {
            "alias": alias,
            "ifc": str(iface.get("IfcName") or ""),
            "role": str(iface.get("Role") or ""),
        }
        status = str(iface.get("Status") or "UNKNOWN")
        stats = iface.get("Stats") if isinstance(iface.get("Stats"), dict) else {}
        lines.extend(
            [
                metric("cogeco_sagemcom_ethernet_up", int(status == "UP"), labels),
                metric("cogeco_sagemcom_ethernet_speed_mbps", numeric(iface.get("CurrentBitRate")), labels),
                metric(
                    "cogeco_sagemcom_ethernet_info",
                    1,
                    labels
                    | {
                        "status": status,
                        "duplex": str(iface.get("DuplexMode") or ""),
                        "mac": str(iface.get("MACAddress") or ""),
                    },
                ),
                metric(
                    "cogeco_sagemcom_ethernet_bytes_total",
                    numeric(stats.get("BytesReceived")),
                    labels | {"direction": "receive"},
                ),
                metric(
                    "cogeco_sagemcom_ethernet_bytes_total",
                    numeric(stats.get("BytesSent")),
                    labels | {"direction": "transmit"},
                ),
                metric(
                    "cogeco_sagemcom_ethernet_packets_total",
                    numeric(stats.get("PacketsReceived")),
                    labels | {"direction": "receive"},
                ),
                metric(
                    "cogeco_sagemcom_ethernet_packets_total",
                    numeric(stats.get("PacketsSent")),
                    labels | {"direction": "transmit"},
                ),
                metric(
                    "cogeco_sagemcom_ethernet_errors_total",
                    numeric(stats.get("ErrorsReceived")),
                    labels | {"direction": "receive"},
                ),
                metric(
                    "cogeco_sagemcom_ethernet_errors_total",
                    numeric(stats.get("ErrorsSent")),
                    labels | {"direction": "transmit"},
                ),
                metric(
                    "cogeco_sagemcom_ethernet_discards_total",
                    numeric(stats.get("DiscardPacketsReceived")),
                    labels | {"direction": "receive"},
                ),
                metric(
                    "cogeco_sagemcom_ethernet_discards_total",
                    numeric(stats.get("DiscardPacketsSent")),
                    labels | {"direction": "transmit"},
                ),
            ]
        )
    return lines


def system_metrics(snapshot: dict[str, object]) -> list[str]:
    lines = [
        "# HELP cogeco_sagemcom_modem_info Modem identity labels.\n",
        "# TYPE cogeco_sagemcom_modem_info gauge\n",
        "# HELP cogeco_sagemcom_reboot_count Modem reboot count from DeviceInfo.\n",
        "# TYPE cogeco_sagemcom_reboot_count gauge\n",
        "# HELP cogeco_sagemcom_memory_bytes Modem memory totals from DeviceInfo.\n",
        "# TYPE cogeco_sagemcom_memory_bytes gauge\n",
        "# HELP cogeco_sagemcom_cpu_usage_percent Modem reported CPU usage.\n",
        "# TYPE cogeco_sagemcom_cpu_usage_percent gauge\n",
        "# HELP cogeco_sagemcom_load_average Modem load averages.\n",
        "# TYPE cogeco_sagemcom_load_average gauge\n",
        "# HELP cogeco_sagemcom_temperature_celsius Modem temperature sensor reading.\n",
        "# TYPE cogeco_sagemcom_temperature_celsius gauge\n",
        "# HELP cogeco_sagemcom_thermal_throttle Modem DOCSIS thermal throttle state.\n",
        "# TYPE cogeco_sagemcom_thermal_throttle gauge\n",
    ]
    lines.append(
        metric(
            "cogeco_sagemcom_modem_info",
            1,
            {
                "model": str(snapshot.get("model") or ""),
                "software": str(snapshot.get("software") or ""),
                "hardware": str(snapshot.get("hardware") or ""),
                "serial": str(snapshot.get("serial") or ""),
            },
        )
    )
    lines.append(metric("cogeco_sagemcom_reboot_count", numeric(snapshot.get("reboot_count"))))
    lines.append(metric("cogeco_sagemcom_thermal_throttle", numeric(snapshot.get("thermal_throttle"))))

    memory = unwrap(snapshot.get("memory"), "MemoryStatus")
    if isinstance(memory, dict):
        # Firmware reports KiB-like integers (e.g. Total=755596).
        for key, label in (("Total", "total"), ("Free", "free")):
            kib = numeric(memory.get(key))
            lines.append(metric("cogeco_sagemcom_memory_bytes", kib * 1024.0, {"type": label}))

    process = unwrap(snapshot.get("process"), "ProcessStatus")
    if isinstance(process, dict):
        lines.append(metric("cogeco_sagemcom_cpu_usage_percent", numeric(process.get("CPUUsage"))))
        load = process.get("LoadAverage")
        if isinstance(load, dict):
            for key, label in (("Load1", "1m"), ("Load5", "5m"), ("Load15", "15m")):
                lines.append(metric("cogeco_sagemcom_load_average", numeric(load.get(key)), {"window": label}))

    temperature = unwrap(snapshot.get("temperature"), "TemperatureStatus")
    sensors = []
    if isinstance(temperature, dict):
        raw = temperature.get("TemperatureSensors", [])
        if isinstance(raw, list):
            sensors = raw
    for sensor in sensors:
        if not isinstance(sensor, dict):
            continue
        value = numeric(sensor.get("Value"))
        # Cogeco leaves several sensors disabled with nonsense placeholders (~-85°C).
        if value != value or value < -40:  # NaN or bogus
            continue
        lines.append(
            metric(
                "cogeco_sagemcom_temperature_celsius",
                value,
                {
                    "sensor": str(sensor.get("Alias") or sensor.get("uid") or "unknown"),
                    "status": str(sensor.get("Status") or ""),
                    "enabled": str(bool(sensor.get("Enable"))).lower(),
                },
            )
        )
    return lines


def modem_api_metrics(client: ModemClient) -> list[str]:
    snapshot = client.modem_snapshot()
    lines = [
        "# HELP cogeco_sagemcom_uptime_seconds Modem uptime; a decrease indicates a restart.\n",
        "# TYPE cogeco_sagemcom_uptime_seconds gauge\n",
        "# HELP cogeco_sagemcom_docsis_registration Current DOCSIS registration state.\n",
        "# TYPE cogeco_sagemcom_docsis_registration gauge\n",
        metric("cogeco_sagemcom_uptime_seconds", numeric(snapshot["uptime"])),
        metric(
            "cogeco_sagemcom_docsis_registration",
            1,
            {"status": str(snapshot["status"])},
        ),
    ]
    lines.extend(docsis_channel_metrics(snapshot["downstream"], snapshot["upstream"]))
    if COLLECT_ETHERNET and "ethernet" in snapshot:
        lines.extend(ethernet_metrics(snapshot["ethernet"]))
    if COLLECT_SYSTEM:
        lines.extend(system_metrics(snapshot))
    return lines


def collect_metrics() -> str:
    lines = [
        "# HELP cogeco_sagemcom_gui_up Whether the modem management interface is reachable.\n",
        "# TYPE cogeco_sagemcom_gui_up gauge\n",
        "# HELP cogeco_sagemcom_gui_request_duration_seconds Modem management interface response time.\n",
        "# TYPE cogeco_sagemcom_gui_request_duration_seconds gauge\n",
    ]
    client = ModemClient()
    success, duration = client.check_gui()
    lines.extend(
        [
            metric("cogeco_sagemcom_gui_up", int(success)),
            metric("cogeco_sagemcom_gui_request_duration_seconds", duration),
            "# HELP cogeco_sagemcom_internet_probe_up TCP reachability from the exporter host.\n",
            "# TYPE cogeco_sagemcom_internet_probe_up gauge\n",
            "# HELP cogeco_sagemcom_internet_probe_duration_seconds TCP connection duration.\n",
            "# TYPE cogeco_sagemcom_internet_probe_duration_seconds gauge\n",
        ]
    )
    if CHECK_API_LOGIN:
        api_success, api_duration = client.check()
        lines.extend(
            [
                "# HELP cogeco_sagemcom_api_up Whether the modem JSON-RPC login succeeded.\n",
                "# TYPE cogeco_sagemcom_api_up gauge\n",
                "# HELP cogeco_sagemcom_api_request_duration_seconds JSON-RPC login duration.\n",
                "# TYPE cogeco_sagemcom_api_request_duration_seconds gauge\n",
                metric("cogeco_sagemcom_api_up", int(api_success)),
                metric("cogeco_sagemcom_api_request_duration_seconds", api_duration),
            ]
        )
    if COLLECT_DOCSIS:
        try:
            lines.extend(modem_api_metrics(client))
            lines.extend(
                [
                    "# HELP cogeco_sagemcom_docsis_scrape_up Whether the modem API scrape succeeded.\n",
                    "# TYPE cogeco_sagemcom_docsis_scrape_up gauge\n",
                    metric("cogeco_sagemcom_docsis_scrape_up", 1),
                ]
            )
        except (KeyError, OSError, ValueError, urllib.error.URLError, TimeoutError):
            lines.extend(
                [
                    "# HELP cogeco_sagemcom_docsis_scrape_up Whether the modem API scrape succeeded.\n",
                    "# TYPE cogeco_sagemcom_docsis_scrape_up gauge\n",
                    metric("cogeco_sagemcom_docsis_scrape_up", 0),
                ]
            )
    for target in PROBE_TARGETS:
        reachable, probe_duration = tcp_probe(target)
        labels = {"target": target}
        lines.append(metric("cogeco_sagemcom_internet_probe_up", int(reachable), labels))
        lines.append(
            metric("cogeco_sagemcom_internet_probe_duration_seconds", probe_duration, labels)
        )
    return "".join(lines)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            return
        if self.path != "/metrics":
            self.send_error(404)
            return
        body = collect_metrics().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _: str, *args: object) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer((LISTEN_ADDRESS, LISTEN_PORT), Handler).serve_forever()
