import asyncio
import getpass
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, List

import serial
from serial.tools import list_ports
import yaml
import paho.mqtt.client as mqtt

from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
WEB_DIR = BASE_DIR / "web"
# Systemd unit name; must match install.sh (xtatech-lora-bridge.service)
SERVICE_NAME = "xtatech-lora-bridge.service"
GITHUB_REPO_URL = "https://github.com/michaelgrenfellbrown/Xtatech-Lora-Bridge.git"
REPO_CLONE_TARGET = Path.home() / "Downloads" / "Xtatech Lora Bridge"
REPO_CLONE_LOG = Path.home() / "Downloads" / "xtatech-lora-bridge-clone.log"
REPO_INSTALL_LOG = Path.home() / "Downloads" / "xtatech-lora-bridge-install.log"
INSTALLED_COMMIT_PATH = BASE_DIR / ".installed_commit"

_RSSI_RE = re.compile(r"rssi[:=]\s*(-?\d+)", re.IGNORECASE)
_KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_-]*)\s*[:=]\s*([^,\s;]+)")
_GIT_PROGRESS_RE = re.compile(r"(Receiving objects|Resolving deltas|Counting objects|Compressing objects):\s+(\d+)%")
BOOT_TS = time.time()
CLONE_PROC: Optional[subprocess.Popen] = None
INSTALL_PROC: Optional[subprocess.Popen] = None

INSTALL_STAGES = [
    ("== Xtatech LoRa Bridge installer", 5, "Starting installer"),
    ("== Install OS dependencies ==", 10, "Installing OS dependencies"),
    ("== Create app directory ==", 20, "Creating app directory"),
    ("== Copy files into", 30, "Copying app files"),
    ("== Create local HTTPS certificate if missing ==", 40, "Checking HTTPS certificate"),
    ("== Create venv and install Python requirements ==", 50, "Installing Python requirements"),
    ("== Configure local terminal token ==", 62, "Configuring terminal"),
    ("== Serial permissions ==", 68, "Updating serial permissions"),
    ("== Disable system sleep targets", 74, "Disabling sleep"),
    ("== Disable USB autosuspend", 80, "Disabling USB autosuspend"),
    ("== Disable Wi-Fi power saving ==", 84, "Disabling Wi-Fi power saving"),
    ("== Allow service user", 88, "Configuring sudo permissions"),
    ("== Install systemd service ==", 94, "Installing service"),
    ("== Installed and started ==", 100, "Install complete"),
]


def run_git_text(args: List[str], cwd: Optional[Path] = None, timeout: int = 30) -> str:
    proc = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(detail or f"git command failed with exit code {proc.returncode}")
    return proc.stdout.strip()


def run_git_ok(args: List[str], cwd: Optional[Path] = None, timeout: int = 30) -> bool:
    proc = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
    )
    return proc.returncode == 0


def github_head_sha(git_bin: str) -> str:
    output = run_git_text([git_bin, "ls-remote", GITHUB_REPO_URL, "HEAD"], timeout=45)
    if not output:
        raise RuntimeError("GitHub did not return a HEAD commit")
    return output.split()[0]


def local_repo_sha(git_bin: str, target: Path) -> str:
    return run_git_text([git_bin, "-C", str(target), "rev-parse", "HEAD"], timeout=15)


def installed_repo_sha(git_bin: str) -> str:
    if INSTALLED_COMMIT_PATH.exists():
        return INSTALLED_COMMIT_PATH.read_text(encoding="utf-8").strip()
    if (BASE_DIR / ".git").exists():
        return local_repo_sha(git_bin, BASE_DIR)
    return ""


def verify_download_repo(git_bin: str) -> Dict[str, Any]:
    target = REPO_CLONE_TARGET.expanduser()
    if not target.exists():
        raise RuntimeError(f"Downloads repo does not exist: {target}")
    if not (target / ".git").exists():
        raise RuntimeError(f"Downloads folder is not a git repository: {target}")

    install_path = target / "install.sh"
    if not install_path.exists():
        raise RuntimeError(f"install.sh is missing from: {target}")

    run_git_text([git_bin, "-C", str(target), "fsck", "--no-progress"], timeout=60)
    local_sha = local_repo_sha(git_bin, target)
    remote_sha = github_head_sha(git_bin)
    if local_sha != remote_sha:
        raise RuntimeError(
            "Downloads repo does not match GitHub HEAD. Run Check / Update GitHub Repo first."
        )

    return {
        "target": target,
        "install_path": install_path,
        "local_head": local_sha,
        "github_head": remote_sha,
    }


def install_eligibility(git_bin: str) -> Dict[str, Any]:
    target = REPO_CLONE_TARGET.expanduser()
    try:
        verified = verify_download_repo(git_bin)
        installed_sha = installed_repo_sha(git_bin)
        download_sha = verified["local_head"]

        if not installed_sha:
            return {
                "ok": True,
                "eligible": False,
                "reason": "Installed version marker is missing",
                "target": str(target),
                "download_head": download_sha,
                "github_head": verified["github_head"],
                "installed_head": "",
            }

        if installed_sha == download_sha:
            return {
                "ok": True,
                "eligible": False,
                "reason": "Downloaded version is already installed",
                "target": str(target),
                "download_head": download_sha,
                "github_head": verified["github_head"],
                "installed_head": installed_sha,
            }

        newer = run_git_ok(
            [git_bin, "-C", str(target), "merge-base", "--is-ancestor", installed_sha, download_sha],
            timeout=20,
        )
        return {
            "ok": True,
            "eligible": newer,
            "reason": "Downloaded version is newer" if newer else "Downloaded version is not newer than installed",
            "target": str(target),
            "download_head": download_sha,
            "github_head": verified["github_head"],
            "installed_head": installed_sha,
        }
    except Exception as e:
        return {
            "ok": True,
            "eligible": False,
            "reason": str(e),
            "target": str(target),
            "download_head": "",
            "github_head": "",
            "installed_head": "",
        }


def now_ts() -> float:
    return time.time()


def now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Missing config.yaml at {CONFIG_PATH}")
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def resolve_local_path(value: Any) -> Optional[str]:
    if not value:
        return None

    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    return str(path)


def validate_config_text(text: str) -> Dict[str, Any]:
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError("config.yaml must contain a YAML mapping/object at the top level")

    required_top = ["mqtt", "serial", "web"]
    for key in required_top:
        if key not in data:
            raise ValueError(f"Missing required top-level config section: {key}")

    return data


def save_config_text(text: str) -> Dict[str, Any]:
    parsed = validate_config_text(text)

    tmp_path = CONFIG_PATH.with_suffix(".yaml.tmp")
    bak_path = CONFIG_PATH.with_suffix(".yaml.bak")

    with tmp_path.open("w", encoding="utf-8") as f:
        f.write(text)

    if CONFIG_PATH.exists():
        shutil.copy2(CONFIG_PATH, bak_path)

    tmp_path.replace(CONFIG_PATH)
    return parsed


def try_extract_json_from_line(line: str) -> Optional[Dict[str, Any]]:
    line = line.strip()
    if not line:
        return None

    if line.startswith("{") and line.endswith("}"):
        try:
            obj = json.loads(line)
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None

    s = line.find("{")
    e = line.rfind("}")
    if s != -1 and e != -1 and e > s:
        candidate = line[s:e + 1]
        try:
            obj = json.loads(candidate)
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None

    return None


def coerce_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return value

    lowered = value.lower()
    if lowered in {"true", "on", "yes"}:
        return True
    if lowered in {"false", "off", "no"}:
        return False

    try:
        if re.fullmatch(r"[-+]?\d+", value):
            return int(value)
        if re.fullmatch(r"[-+]?\d*\.\d+", value):
            return float(value)
    except Exception:
        pass

    return value


def parse_key_value_line(line: str) -> Optional[Dict[str, Any]]:
    pairs = _KV_RE.findall(line)
    if not pairs:
        return None

    payload: Dict[str, Any] = {"raw": line}
    for key, value in pairs:
        payload[key] = coerce_scalar(value)
    return payload


def parse_serial_line(line: str, mode: str) -> Optional[Dict[str, Any]]:
    if mode == "raw":
        return {"raw": line}

    obj = try_extract_json_from_line(line)
    if obj is not None:
        return obj

    if mode == "auto":
        kv = parse_key_value_line(line)
        if kv is not None:
            return kv
        return {"raw": line}

    return None


def extract_rssi(text_line: str) -> Optional[int]:
    m = _RSSI_RE.search(text_line)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def derive_node_id(payload: Dict[str, Any], id_keys: List[str]) -> str:
    lowered = {str(k).lower(): k for k in payload.keys()}
    for wanted in id_keys:
        key = lowered.get(str(wanted).lower())
        if key is not None and payload.get(key) is not None:
            return str(payload[key])
    return "unknown"


def get_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        pass

    try:
        hostname = socket.gethostname()
        for ip in socket.gethostbyname_ex(hostname)[2]:
            if not ip.startswith("127."):
                return ip
    except Exception:
        pass

    return "127.0.0.1"


def web_scheme(cfg: Dict[str, Any]) -> str:
    web = cfg.get("web", {}) or {}
    certfile = resolve_local_path(web.get("ssl_certfile"))
    keyfile = resolve_local_path(web.get("ssl_keyfile"))
    if certfile and keyfile and Path(certfile).exists() and Path(keyfile).exists():
        return "https"
    return "http"


def ssh_service_state() -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "service_name": "",
        "service_active": "",
        "service_enabled": "",
        "service_error": "",
    }

    systemctl = shutil.which("systemctl")
    if not systemctl:
        result["service_error"] = "systemctl not found"
        return result

    for name in ("ssh", "sshd"):
        try:
            active = subprocess.run(
                [systemctl, "is-active", name],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            enabled = subprocess.run(
                [systemctl, "is-enabled", name],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            active_text = active.stdout.strip() or active.stderr.strip()
            enabled_text = enabled.stdout.strip() or enabled.stderr.strip()
            if active.returncode == 0 or enabled.returncode == 0 or active_text not in {"", "inactive", "unknown"}:
                result.update({
                    "service_name": name,
                    "service_active": active_text,
                    "service_enabled": enabled_text,
                    "service_error": "",
                })
                return result
        except Exception as exc:
            result["service_error"] = str(exc)

    if not result["service_error"]:
        result["service_error"] = "ssh/sshd service not found"
    return result


def ssh_status_payload() -> Dict[str, Any]:
    ip = get_ip()
    port = 22
    port_open = False
    port_error = ""

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.5):
            port_open = True
    except Exception as exc:
        port_error = str(exc)

    user = getpass.getuser()
    payload = {
        "hostname": socket.gethostname(),
        "ip": ip,
        "port": port,
        "current_user": user,
        "port_open_local": port_open,
        "port_error": port_error,
        "ssh_command": f"ssh {user}@{ip}",
    }
    payload.update(ssh_service_state())
    return payload


def read_text_file(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def run_command(args: List[str], timeout: float = 2.0) -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }
    except Exception as exc:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": str(exc)}


def cpu_times() -> Optional[List[int]]:
    try:
        first = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0]
        parts = first.split()
        if not parts or parts[0] != "cpu":
            return None
        return [int(value) for value in parts[1:]]
    except Exception:
        return None


def cpu_percent(sample_seconds: float = 0.12) -> Optional[float]:
    first = cpu_times()
    if first is None:
        return None
    time.sleep(sample_seconds)
    second = cpu_times()
    if second is None:
        return None

    idle_1 = first[3] + (first[4] if len(first) > 4 else 0)
    idle_2 = second[3] + (second[4] if len(second) > 4 else 0)
    total_1 = sum(first)
    total_2 = sum(second)
    total_delta = total_2 - total_1
    idle_delta = idle_2 - idle_1
    if total_delta <= 0:
        return None
    return round(max(0.0, min(100.0, (1.0 - idle_delta / total_delta) * 100.0)), 1)


def parse_meminfo() -> Dict[str, int]:
    values: Dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw_value = line.split(":", 1)
            parts = raw_value.strip().split()
            if parts:
                values[key] = int(parts[0]) * 1024
    except Exception:
        pass
    return values


def disk_usage(path: str = "/") -> Dict[str, Any]:
    try:
        usage = shutil.disk_usage(path)
        percent = (usage.used / usage.total * 100.0) if usage.total else 0.0
        return {
            "path": path,
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "used_percent": round(percent, 1),
        }
    except Exception as exc:
        return {"path": path, "error": str(exc)}


def read_temperature_c() -> Optional[float]:
    raw = read_text_file("/sys/class/thermal/thermal_zone0/temp")
    if raw:
        try:
            return round(float(raw) / 1000.0, 1)
        except Exception:
            pass

    vcgencmd = shutil.which("vcgencmd")
    if vcgencmd:
        result = run_command([vcgencmd, "measure_temp"])
        match = re.search(r"temp=([0-9.]+)", result.get("stdout", ""))
        if match:
            return round(float(match.group(1)), 1)
    return None


def throttled_status() -> Dict[str, Any]:
    vcgencmd = shutil.which("vcgencmd")
    if not vcgencmd:
        return {"available": False}

    result = run_command([vcgencmd, "get_throttled"])
    raw = result.get("stdout", "")
    match = re.search(r"0x([0-9a-fA-F]+)", raw)
    if not match:
        return {"available": True, "raw": raw, "error": result.get("stderr", "")}

    value = int(match.group(1), 16)
    flags = {
        "under_voltage_now": bool(value & (1 << 0)),
        "frequency_capped_now": bool(value & (1 << 1)),
        "throttled_now": bool(value & (1 << 2)),
        "soft_temp_limit_now": bool(value & (1 << 3)),
        "under_voltage_seen": bool(value & (1 << 16)),
        "frequency_capped_seen": bool(value & (1 << 17)),
        "throttled_seen": bool(value & (1 << 18)),
        "soft_temp_limit_seen": bool(value & (1 << 19)),
    }
    return {"available": True, "raw": raw, "value": value, **flags}


def network_interfaces() -> List[Dict[str, Any]]:
    interfaces: List[Dict[str, Any]] = []
    try:
        lines = Path("/proc/net/dev").read_text(encoding="utf-8").splitlines()[2:]
        for line in lines:
            name, data = line.split(":", 1)
            parts = data.split()
            interfaces.append({
                "name": name.strip(),
                "rx_bytes": int(parts[0]),
                "rx_packets": int(parts[1]),
                "tx_bytes": int(parts[8]),
                "tx_packets": int(parts[9]),
            })
    except Exception:
        pass
    return interfaces


def top_processes(limit: int = 8) -> List[Dict[str, Any]]:
    ps = shutil.which("ps")
    if not ps:
        return []

    result = run_command([ps, "-eo", "pid,comm,%cpu,%mem", "--sort=-%cpu"], timeout=2.5)
    if not result.get("ok"):
        return []

    rows: List[Dict[str, Any]] = []
    for line in result.get("stdout", "").splitlines()[1:limit + 1]:
        parts = line.split(None, 3)
        if len(parts) != 4:
            continue
        rows.append({
            "pid": parts[0],
            "command": parts[1],
            "cpu_percent": parts[2],
            "mem_percent": parts[3],
        })
    return rows


def build_system_metrics_payload() -> Dict[str, Any]:
    mem = parse_meminfo()
    mem_total = mem.get("MemTotal", 0)
    mem_available = mem.get("MemAvailable", 0)
    mem_used = max(0, mem_total - mem_available) if mem_total else 0
    swap_total = mem.get("SwapTotal", 0)
    swap_free = mem.get("SwapFree", 0)
    swap_used = max(0, swap_total - swap_free) if swap_total else 0
    load_1, load_5, load_15 = os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0)

    return {
        "timestamp": now_ts(),
        "host": {
            "hostname": socket.gethostname(),
            "ip": get_ip(),
            "platform": os.uname().sysname if hasattr(os, "uname") else os.name,
            "kernel": os.uname().release if hasattr(os, "uname") else "",
            "model": read_text_file("/proc/device-tree/model").replace("\x00", ""),
        },
        "cpu": {
            "percent": cpu_percent(),
            "load_1m": round(load_1, 2),
            "load_5m": round(load_5, 2),
            "load_15m": round(load_15, 2),
            "cores": os.cpu_count() or 0,
            "temperature_c": read_temperature_c(),
            "throttled": throttled_status(),
        },
        "memory": {
            "total_bytes": mem_total,
            "available_bytes": mem_available,
            "used_bytes": mem_used,
            "used_percent": round((mem_used / mem_total * 100.0), 1) if mem_total else None,
        },
        "swap": {
            "total_bytes": swap_total,
            "used_bytes": swap_used,
            "free_bytes": swap_free,
            "used_percent": round((swap_used / swap_total * 100.0), 1) if swap_total else None,
        },
        "disk": disk_usage("/"),
        "network": network_interfaces(),
        "processes": top_processes(),
        "bridge": {
            "service_uptime_s": int(now_ts() - BOOT_TS),
            "serial_lines_seen": stats.serial_lines_seen,
            "serial_packets_parsed": stats.serial_packets_parsed,
            "serial_parse_drops": stats.serial_parse_drops,
            "nodes_seen": len(stats.nodes_seen),
        },
    }


@dataclass
class Packet:
    ts: float
    node_id: str
    payload: Dict[str, Any]
    raw_line: str


@dataclass
class LogLine:
    ts: float
    level: str
    msg: str


class PacketStore:
    def __init__(self, maxlen: int = 300):
        self.maxlen = maxlen
        self._buf: List[Packet] = []
        self._lock = asyncio.Lock()

    async def add(self, pkt: Packet) -> None:
        async with self._lock:
            self._buf.append(pkt)
            if len(self._buf) > self.maxlen:
                self._buf = self._buf[-self.maxlen:]

    async def latest(self, n: int = 30) -> List[Dict[str, Any]]:
        async with self._lock:
            data = self._buf[-n:]
            return [
                {
                    "ts": p.ts,
                    "node_id": p.node_id,
                    "payload": p.payload,
                    "raw": p.raw_line,
                }
                for p in data
            ]

    def set_maxlen(self, value: int) -> None:
        self.maxlen = max(10, int(value))


class LogStore:
    def __init__(self, maxlen: int = 800):
        self.maxlen = maxlen
        self._buf: List[LogLine] = []
        self._lock = asyncio.Lock()

    async def add(self, level: str, msg: str) -> None:
        async with self._lock:
            self._buf.append(LogLine(ts=now_ts(), level=level, msg=msg))
            if len(self._buf) > self.maxlen:
                self._buf = self._buf[-self.maxlen:]

    async def latest(self, n: int = 120) -> List[Dict[str, Any]]:
        async with self._lock:
            data = self._buf[-n:]
            return [{"ts": l.ts, "level": l.level, "msg": l.msg} for l in data]

    def set_maxlen(self, value: int) -> None:
        self.maxlen = max(50, int(value))


class RuntimeStats:
    def __init__(self):
        self.last_serial_line_ts: float = 0.0
        self.last_serial_line_iso: str = ""
        self.last_serial_line: str = ""
        self.last_serial_rx_ts: float = 0.0
        self.last_serial_rx_iso: str = ""
        self.last_serial_port_ok: bool = False
        self.last_serial_error: str = ""
        self.active_serial_port: str = ""
        self.serial_candidates: List[str] = []
        self.serial_candidate_details: List[Dict[str, Any]] = []
        self.serial_open_attempts: List[Dict[str, Any]] = []
        self.serial_lines_seen: int = 0
        self.serial_packets_parsed: int = 0
        self.serial_parse_drops: int = 0
        self.nodes_seen: Dict[str, float] = {}

    def mark_serial_open(self, port: str):
        self.last_serial_port_ok = True
        self.last_serial_error = ""
        self.active_serial_port = port

    def mark_serial_line(self, line: str):
        t = now_ts()
        self.last_serial_line_ts = t
        self.last_serial_line_iso = now_iso_utc()
        self.last_serial_line = line[-500:]
        self.last_serial_port_ok = True
        self.last_serial_error = ""
        self.serial_lines_seen += 1

    def mark_serial_rx(self, node_id: str):
        t = now_ts()
        self.last_serial_rx_ts = t
        self.last_serial_rx_iso = now_iso_utc()
        self.last_serial_port_ok = True
        self.last_serial_error = ""
        self.serial_packets_parsed += 1
        if node_id:
            self.nodes_seen[node_id] = t

    def mark_serial_parse_drop(self):
        self.serial_parse_drops += 1

    def mark_serial_error(self, err: str, port_ok: bool):
        self.last_serial_port_ok = port_ok
        self.last_serial_error = err
        if not port_ok:
            self.active_serial_port = ""


class DiscoveryPublisher:
    def __init__(self, cfg: Dict[str, Any], client: mqtt.Client, logs: LogStore):
        self.cfg = cfg
        self.client = client
        self.logs = logs
        disc = cfg.get("mqtt", {}).get("discovery", {}) or {}
        self.enabled = bool(disc.get("enabled", False))
        self.prefix = str(disc.get("prefix", "homeassistant")).strip()
        self.expire_after = int(disc.get("expire_after_s", 0) or 0)
        self.republish_interval = int(disc.get("republish_interval_s", 0) or 0)
        self._last: Dict[str, float] = {}
        self._gw_last: float = 0.0

    def update_cfg(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        disc = cfg.get("mqtt", {}).get("discovery", {}) or {}
        self.enabled = bool(disc.get("enabled", False))
        self.prefix = str(disc.get("prefix", "homeassistant")).strip()
        self.expire_after = int(disc.get("expire_after_s", 0) or 0)
        self.republish_interval = int(disc.get("republish_interval_s", 0) or 0)

    def _topic(self, component: str, object_id: str) -> str:
        return f"{self.prefix}/{component}/{object_id}/config"

    def _log(self, level: str, msg: str) -> None:
        try:
            asyncio.get_running_loop().create_task(self.logs.add(level, msg))
        except RuntimeError:
            pass

    def maybe_publish_gateway(self, gateway_id: str, state_topic: str):
        if not self.enabled:
            return

        now = time.time()
        if self._gw_last and self.republish_interval > 0 and (now - self._gw_last) < self.republish_interval:
            return

        device = {
            "identifiers": [f"lora_gateway_{gateway_id}"],
            "name": f"LoRa Gateway {gateway_id}",
            "manufacturer": "Xtatech",
            "model": "Raspberry Pi Gateway",
        }

        sensors = [
            ("heartbeat", "Heartbeat", None, "timestamp", None, "heartbeat_iso"),
            ("uptime", "Uptime", "s", None, "measurement", "uptime_s"),
            ("nodes_seen", "Nodes Seen", None, None, "measurement", "nodes_seen"),
        ]

        for key, suffix, unit, devcls, stcls, json_key in sensors:
            object_id = f"{gateway_id.lower()}_{key}"
            payload = {
                "name": f"{gateway_id} {suffix}",
                "unique_id": f"lora_gateway_{gateway_id.lower()}_{key}",
                "state_topic": state_topic,
                "value_template": f"{{{{ value_json.{json_key} | default(None) }}}}",
                "device": device,
            }
            if stcls:
                payload["state_class"] = stcls
            if devcls:
                payload["device_class"] = devcls
            if unit:
                payload["unit_of_measurement"] = unit
            if self.expire_after > 0:
                payload["expire_after"] = self.expire_after

            self.client.publish(
                self._topic("sensor", object_id),
                json.dumps(payload, separators=(",", ":")),
                qos=0,
                retain=True,
            )

        bin_sensors = [
            ("mqtt_connected", "MQTT Connected", "mqtt_connected"),
            ("serial_ok", "Serial OK", "serial_ok"),
        ]
        for key, name, json_key in bin_sensors:
            object_id = f"{gateway_id.lower()}_{key}"
            payload = {
                "name": f"{gateway_id} {name}",
                "unique_id": f"lora_gateway_{gateway_id.lower()}_{key}",
                "state_topic": state_topic,
                "value_template": f"{{{{ 'ON' if value_json.{json_key} else 'OFF' }}}}",
                "device": device,
            }
            self.client.publish(
                self._topic("binary_sensor", object_id),
                json.dumps(payload, separators=(",", ":")),
                qos=0,
                retain=True,
            )

        self._gw_last = now
        self._log("INFO", f"MQTT discovery published for gateway: {gateway_id}")

    def maybe_publish_node(self, node_id: str, state_topic: str):
        if not self.enabled:
            return

        now = time.time()
        last = self._last.get(node_id, 0.0)
        if last and self.republish_interval > 0 and (now - last) < self.republish_interval:
            return

        device = {
            "identifiers": [f"lora_{node_id}"],
            "name": f"LoRa {node_id}",
            "manufacturer": "Xtatech",
            "model": "LoRa Sensor Node",
        }

        sensors = [
            ("bat", "Battery", "V", "voltage", "measurement", "bat"),
            ("temp", "Temperature", "°C", "temperature", "measurement", "temp"),
            ("humi", "Humidity", "%", "humidity", "measurement", "humi"),
            ("ph", "pH", "pH", None, "measurement", "PH"),
            ("sleep", "Sleep", "s", None, "measurement", "SLEEP"),
            ("count", "Count", None, None, "total_increasing", "COUNT"),
            ("rssi", "RSSI", "dBm", "signal_strength", "measurement", "rssi"),
            ("last_seen", "Last Seen", None, "timestamp", None, "ts_iso"),
        ]

        for key, suffix, unit, devcls, stcls, json_key in sensors:
            object_id = f"{node_id.lower()}_{key}"
            payload = {
                "name": f"{node_id} {suffix}",
                "unique_id": f"lora_{node_id.lower()}_{key}",
                "state_topic": state_topic,
                "value_template": f"{{{{ value_json.{json_key} | default(None) }}}}",
                "device": device,
            }
            if stcls:
                payload["state_class"] = stcls
            if devcls:
                payload["device_class"] = devcls
            if unit:
                payload["unit_of_measurement"] = unit
            if self.expire_after > 0:
                payload["expire_after"] = self.expire_after

            self.client.publish(
                self._topic("sensor", object_id),
                json.dumps(payload, separators=(",", ":")),
                qos=0,
                retain=True,
            )

        self._last[node_id] = now
        self._log("INFO", f"MQTT discovery published for node: {node_id}")


class MqttPublisher:
    def __init__(self, cfg: Dict[str, Any], logs: LogStore):
        self.cfg = cfg
        self.logs = logs
        self._connected = False

        self.client = mqtt.Client(
            client_id=cfg["mqtt"]["client_id"],
            protocol=mqtt.MQTTv311,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )

        user = cfg["mqtt"].get("username")
        pw = cfg["mqtt"].get("password")
        if user:
            self.client.username_pw_set(user, pw)

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)

        self.discovery = DiscoveryPublisher(cfg, self.client, logs)

    def update_cfg(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        self.discovery.update_cfg(cfg)

    def _log(self, level: str, msg: str) -> None:
        try:
            asyncio.get_running_loop().create_task(self.logs.add(level, msg))
        except RuntimeError:
            pass

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        self._connected = (reason_code == 0)
        self._log("INFO" if self._connected else "WARN", f"MQTT connect result: {reason_code}")

    def _on_disconnect(self, client, userdata, reason_code, properties=None):
        self._connected = False
        self._log("WARN", f"MQTT disconnected: {reason_code}")

    def connect(self) -> None:
        host = self.cfg["mqtt"]["host"]
        port = int(self.cfg["mqtt"]["port"])
        self.client.connect_async(host, port, keepalive=60)
        self.client.loop_start()

    def stop(self) -> None:
        try:
            self.client.loop_stop()
        except Exception:
            pass

    def connected(self) -> bool:
        return self._connected

    def publish_state(self, topic: str, payload: Dict[str, Any], retain: Optional[bool] = None) -> None:
        if not self._connected:
            return
        qos = int(self.cfg["mqtt"].get("qos", 0))
        retain_final = bool(self.cfg["mqtt"].get("retain", False)) if retain is None else bool(retain)
        self.client.publish(topic, json.dumps(payload, separators=(",", ":")), qos=qos, retain=retain_final)

    def status(self) -> Dict[str, Any]:
        return {
            "connected": self._connected,
            "host": self.cfg["mqtt"]["host"],
            "port": int(self.cfg["mqtt"]["port"]),
            "base_topic": self.cfg["mqtt"]["base_topic"],
            "gateway_id": self.cfg["mqtt"].get("gateway_id", ""),
        }


class SerialBridge:
    def __init__(self, cfg: Dict[str, Any], store: PacketStore, mqtt_pub: MqttPublisher, logs: LogStore, stats: RuntimeStats):
        self.cfg = cfg
        self.store = store
        self.mqtt = mqtt_pub
        self.logs = logs
        self.stats = stats
        self._stop = asyncio.Event()

    def update_cfg(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg

    def stop(self) -> None:
        self._stop.set()

    def _candidate_ports(self) -> List[str]:
        return [item["device"] for item in self._candidate_port_details()]

    def _path_info(self, device: str, source: str) -> Dict[str, Any]:
        path = Path(device)
        exists = path.exists()
        resolved = ""
        try:
            resolved = str(path.resolve()) if exists or path.is_symlink() else ""
        except Exception:
            resolved = ""

        return {
            "device": device,
            "source": source,
            "exists": exists,
            "is_symlink": path.is_symlink(),
            "resolved": resolved,
            "readable": os.access(device, os.R_OK) if exists else False,
            "writable": os.access(device, os.W_OK) if exists else False,
        }

    def _candidate_port_details(self) -> List[Dict[str, Any]]:
        details: List[Dict[str, Any]] = []
        seen = set()

        def add(item: Dict[str, Any]) -> None:
            device = item.get("device")
            if not device or device in seen:
                return
            seen.add(device)
            details.append(item)

        serial_by_id_dir = Path("/dev/serial/by-id")
        if serial_by_id_dir.exists():
            for path in sorted(serial_by_id_dir.iterdir()):
                add(self._path_info(str(path), "by-id"))

        usb_ports: List[str] = []
        other_ports: List[str] = []
        for port_info in list_ports.comports():
            device = port_info.device
            if not device:
                continue
            if device.startswith(("/dev/ttyACM", "/dev/ttyUSB")):
                usb_ports.append(device)
            else:
                other_ports.append(device)

        port_infos = {info.device: info for info in list_ports.comports() if info.device}
        for device in sorted(usb_ports) + sorted(other_ports):
            info = port_infos.get(device)
            item = self._path_info(device, "list_ports_usb" if device in usb_ports else "list_ports_other")
            if info:
                item.update({
                    "description": info.description,
                    "hwid": info.hwid,
                    "manufacturer": info.manufacturer,
                    "product": info.product,
                    "serial_number": info.serial_number,
                    "vid": info.vid,
                    "pid": info.pid,
                })
            add(item)

        return details

    def _open_serial_port(self, port: str, baud: int, timeout_s: float):
        return serial.Serial(
            port=port,
            baudrate=baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=timeout_s,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )

    def _resolve_port(self, configured_port: str, baud: int, timeout_s: float) -> str:
        if configured_port and configured_port.lower() != "auto":
            return configured_port

        details = self._candidate_port_details()
        candidates = [item["device"] for item in details]
        self.stats.serial_candidates = candidates
        self.stats.serial_candidate_details = details
        attempts: List[Dict[str, Any]] = []
        for candidate in candidates:
            try:
                ser = self._open_serial_port(candidate, baud, timeout_s)
                ser.close()
                attempts.append({"device": candidate, "ok": True, "selected": True, "error": ""})
                self.stats.serial_open_attempts = attempts
                return candidate
            except serial.SerialException as exc:
                attempts.append({"device": candidate, "ok": False, "selected": False, "error": str(exc)})
                continue

        self.stats.serial_open_attempts = attempts
        checked = ", ".join(candidates) if candidates else "none"
        raise serial.SerialException(
            f"serial.port is set to auto, but no usable USB serial port was found. Checked: {checked}"
        )

    async def run_forever(self) -> None:
        delay = float(self.cfg["serial"].get("reconnect_delay_s", 2.0))
        await self.logs.add("INFO", f"Serial starting: {self.cfg['serial']['port']} @ {self.cfg['serial']['baud']}")

        while not self._stop.is_set():
            try:
                await self._run_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                port = self.cfg["serial"]["port"]
                port_ok = Path(port).exists() if port.startswith("/dev/") else True
                self.stats.mark_serial_error(repr(e), port_ok)
                await self.logs.add("WARN", f"Serial error: {e!r} (retry in {delay}s)")
                await asyncio.sleep(delay)

    async def _run_once(self) -> None:
        configured_port = str(self.cfg["serial"]["port"])
        baud = int(self.cfg["serial"]["baud"])
        timeout_s = float(self.cfg["serial"].get("timeout_s", 1.0))
        max_len = int(self.cfg.get("parsing", {}).get("max_line_length", 4096))
        mode = (self.cfg.get("parsing", {}).get("mode", "auto") or "auto").lower()
        id_keys = self.cfg.get("topics", {}).get("id_keys", ["ID"])

        if configured_port.lower() == "auto":
            try:
                port = await asyncio.to_thread(self._resolve_port, configured_port, baud, timeout_s)
                await self.logs.add("INFO", f"Auto-selected LoRa serial port: {port}")
            except serial.SerialException as e:
                msg = str(e)
                self.stats.mark_serial_error(msg, port_ok=False)
                await self.logs.add("WARN", f"{msg} (waiting; app will keep running)")
                await asyncio.sleep(float(self.cfg["serial"].get("reconnect_delay_s", 2.0)))
                return
        else:
            port = configured_port

        if port.startswith("/dev/") and not Path(port).exists():
            msg = f"Configured LoRa serial port does not exist yet: {port}"
            self.stats.mark_serial_error(msg, port_ok=False)
            await self.logs.add("WARN", f"{msg} (waiting; app will keep running)")
            await asyncio.sleep(float(self.cfg["serial"].get("reconnect_delay_s", 2.0)))
            return

        def _open_serial():
            return self._open_serial_port(port, baud, timeout_s)

        ser = await asyncio.to_thread(_open_serial)
        await self.logs.add("INFO", f"Serial opened: {port} @ {baud} (8N1, no flow control)")
        self.stats.mark_serial_open(port)

        base_topic = self.cfg["mqtt"]["base_topic"].rstrip("/")

        try:
            while not self._stop.is_set():
                raw = await asyncio.to_thread(ser.readline)
                if not raw:
                    continue

                line = raw.decode("utf-8", errors="ignore")
                if len(line) > max_len:
                    line = line[:max_len]
                line = line.strip()
                if not line:
                    continue

                self.stats.mark_serial_line(line)
                await self.logs.add("SERIAL", line)

                payload = parse_serial_line(line, mode)
                if payload is None:
                    self.stats.mark_serial_parse_drop()
                    await self.logs.add("WARN", f"Serial line ignored by {mode} parser: {line[:200]}")
                    continue
                node_id = "raw" if mode == "raw" else derive_node_id(payload, id_keys)

                payload.setdefault("ts", now_ts())
                payload.setdefault("ts_iso", now_iso_utc())

                if "rssi" not in payload:
                    rssi = extract_rssi(line)
                    if rssi is not None:
                        payload["rssi"] = rssi

                self.stats.mark_serial_rx(node_id)

                node_state_topic = f"{base_topic}/{node_id}/state"
                self.mqtt.discovery.maybe_publish_node(node_id, node_state_topic)
                self.mqtt.publish_state(node_state_topic, payload)
                self.mqtt._log("INFO", f"MQTT published: {node_state_topic}")

                pkt = Packet(ts=float(payload["ts"]), node_id=node_id, payload=payload, raw_line=line)
                await self.store.add(pkt)

        finally:
            await asyncio.to_thread(ser.close)
            await self.logs.add("WARN", f"Serial closed: {port}")


ws_payload_clients: List[WebSocket] = []
ws_log_clients: List[WebSocket] = []
ws_payload_lock = asyncio.Lock()
ws_log_lock = asyncio.Lock()

runtime: Dict[str, Any] = {
    "cfg": None,
    "stop_event": None,
    "mqtt": None,
    "serial": None,
    "tasks": [],
}

store = PacketStore()
logs = LogStore()
stats = RuntimeStats()


def auth_ok(request: Request) -> bool:
    cfg = runtime.get("cfg") or {}
    web = cfg.get("web", {}) or {}
    if not web.get("require_token", False):
        return True
    token = str(web.get("token", ""))
    return request.query_params.get("token") == token


def no_cache_html(text: str) -> HTMLResponse:
    return HTMLResponse(
        text,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


async def ws_payload_broadcaster(store: PacketStore, stop_event: asyncio.Event):
    while not stop_event.is_set():
        await asyncio.sleep(0.75)
        msg = json.dumps(await store.latest(30), separators=(",", ":"))
        async with ws_payload_lock:
            dead = []
            for ws in ws_payload_clients:
                try:
                    await ws.send_text(msg)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                if ws in ws_payload_clients:
                    ws_payload_clients.remove(ws)


async def ws_log_broadcaster(logs: LogStore, stop_event: asyncio.Event):
    while not stop_event.is_set():
        await asyncio.sleep(0.75)
        msg = json.dumps(await logs.latest(120), separators=(",", ":"))
        async with ws_log_lock:
            dead = []
            for ws in ws_log_clients:
                try:
                    await ws.send_text(msg)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                if ws in ws_log_clients:
                    ws_log_clients.remove(ws)


async def heartbeat_task(cfg: Dict[str, Any], mqtt_pub: MqttPublisher, logs: LogStore, stats: RuntimeStats, stop_event: asyncio.Event):
    hb = cfg.get("heartbeat", {}) or {}
    wd = cfg.get("watchdog", {}) or {}
    enabled = bool(hb.get("enabled", True))
    interval_s = int(hb.get("interval_s", 30))
    hb_base = str(hb.get("topic", f"{cfg['mqtt']['base_topic'].rstrip('/')}/_gateway")).rstrip("/")

    gw_id = str(cfg["mqtt"].get("gateway_id") or socket.gethostname())
    state_topic = f"{hb_base}/{gw_id}/state"

    while not stop_event.is_set():
        if mqtt_pub.connected():
            mqtt_pub.discovery.maybe_publish_gateway(gw_id, state_topic)
            break
        await asyncio.sleep(1)

    await logs.add("INFO", f"Heartbeat task started: every {interval_s}s -> {state_topic}")

    while not stop_event.is_set():
        current_cfg = runtime.get("cfg") or cfg
        hb = current_cfg.get("heartbeat", {}) or {}
        wd = current_cfg.get("watchdog", {}) or {}
        enabled = bool(hb.get("enabled", True))
        interval_s = int(hb.get("interval_s", 30))
        hb_base = str(hb.get("topic", f"{current_cfg['mqtt']['base_topic'].rstrip('/')}/_gateway")).rstrip("/")
        gw_id = str(current_cfg["mqtt"].get("gateway_id") or socket.gethostname())
        state_topic = f"{hb_base}/{gw_id}/state"

        if enabled and mqtt_pub.connected():
            uptime = int(now_ts() - BOOT_TS)
            nodes_seen = len(stats.nodes_seen)
            payload = {
                "gateway_id": gw_id,
                "heartbeat_ts": now_ts(),
                "heartbeat_iso": now_iso_utc(),
                "uptime_s": uptime,
                "ip": get_ip(),
                "mqtt_connected": mqtt_pub.connected(),
                "serial_ok": bool(stats.last_serial_port_ok),
                "last_serial_rx_ts": stats.last_serial_rx_ts or None,
                "last_serial_rx_iso": stats.last_serial_rx_iso or None,
                "nodes_seen": nodes_seen,
                "last_serial_error": stats.last_serial_error or "",
            }
            mqtt_pub.discovery.maybe_publish_gateway(gw_id, state_topic)
            mqtt_pub.publish_state(state_topic, payload, retain=False)

        if bool(wd.get("enabled", False)) and bool(wd.get("reboot", False)):
            no_data_s = int(wd.get("no_serial_data_s", 900))
            require_port_missing = bool(wd.get("require_port_missing", False))
            last_rx = stats.last_serial_rx_ts
            port = str(current_cfg.get("serial", {}).get("port", ""))

            if last_rx > 0 and (now_ts() - last_rx) > no_data_s:
                port_missing = port.startswith("/dev/") and not Path(port).exists()
                if (not require_port_missing) or port_missing:
                    await logs.add("ERROR", f"WATCHDOG: no serial data for {int(now_ts() - last_rx)}s. Rebooting...")
                    try:
                        subprocess.run(
                            ["/usr/bin/sudo", "/usr/bin/systemctl", "reboot"],
                            check=False,
                        )
                    except Exception as e:
                        await logs.add("ERROR", f"WATCHDOG reboot failed: {e!r}")

        await asyncio.sleep(interval_s)


def apply_runtime_config(new_cfg: Dict[str, Any]) -> None:
    runtime["cfg"] = new_cfg
    store.set_maxlen(new_cfg.get("storage", {}).get("ring_buffer_size", 300))
    logs.set_maxlen(new_cfg.get("storage", {}).get("log_buffer_size", 800))

    if runtime.get("mqtt"):
        runtime["mqtt"].update_cfg(new_cfg)

    if runtime.get("serial"):
        runtime["serial"].update_cfg(new_cfg)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config()
    apply_runtime_config(cfg)

    runtime["stop_event"] = asyncio.Event()
    runtime["tasks"] = []

    await logs.add("INFO", "Bridge starting up")

    mqtt_pub = MqttPublisher(cfg, logs)
    mqtt_pub.connect()
    runtime["mqtt"] = mqtt_pub

    serial_bridge = SerialBridge(cfg, store, mqtt_pub, logs, stats)
    runtime["serial"] = serial_bridge

    runtime["tasks"].append(asyncio.create_task(serial_bridge.run_forever()))
    runtime["tasks"].append(asyncio.create_task(ws_payload_broadcaster(store, runtime["stop_event"])))
    runtime["tasks"].append(asyncio.create_task(ws_log_broadcaster(logs, runtime["stop_event"])))
    runtime["tasks"].append(asyncio.create_task(heartbeat_task(cfg, mqtt_pub, logs, stats, runtime["stop_event"])))

    try:
        yield
    finally:
        await logs.add("WARN", "Bridge shutting down")
        runtime["stop_event"].set()

        if runtime.get("serial"):
            runtime["serial"].stop()

        for t in runtime.get("tasks", []):
            try:
                t.cancel()
            except Exception:
                pass

        if runtime.get("mqtt"):
            runtime["mqtt"].stop()


app = FastAPI(lifespan=lifespan)

if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


async def build_status_payload() -> Dict[str, Any]:
    cfg = runtime.get("cfg") or {}
    web = cfg.get("web", {}) or {}
    ip = get_ip()
    port = int(web.get("port", 0) or 0)
    scheme = web_scheme(cfg)
    nodes_seen = [
        {"node_id": node_id, "last_seen_ts": ts, "age_s": int(now_ts() - ts)}
        for node_id, ts in sorted(stats.nodes_seen.items(), key=lambda item: item[1], reverse=True)
    ]

    return {
        "controller": {
            "hostname": socket.gethostname(),
            "ip": ip,
            "ui_host": web.get("host", ""),
            "ui_port": port,
            "scheme": scheme,
            "https_enabled": scheme == "https",
            "url": f"{scheme}://{ip}:{port}" if port else "",
        },
        "mqtt": runtime["mqtt"].status() if runtime.get("mqtt") else {},
        "serial": cfg.get("serial", {}),
        "parsing": cfg.get("parsing", {}),
        "topics": cfg.get("topics", {}),
        "web": cfg.get("web", {}),
        "runtime": {
            "last_serial_line_ts": stats.last_serial_line_ts,
            "last_serial_line_iso": stats.last_serial_line_iso,
            "last_serial_line": stats.last_serial_line,
            "last_serial_rx_ts": stats.last_serial_rx_ts,
            "last_serial_rx_iso": stats.last_serial_rx_iso,
            "serial_ok": stats.last_serial_port_ok,
            "active_serial_port": stats.active_serial_port,
            "serial_candidates": stats.serial_candidates,
            "serial_candidate_details": stats.serial_candidate_details,
            "serial_open_attempts": stats.serial_open_attempts,
            "serial_lines_seen": stats.serial_lines_seen,
            "serial_packets_parsed": stats.serial_packets_parsed,
            "serial_parse_drops": stats.serial_parse_drops,
            "nodes_seen": len(stats.nodes_seen),
            "nodes_seen_detail": nodes_seen[:100],
            "last_serial_error": stats.last_serial_error,
            "uptime_s": int(now_ts() - BOOT_TS),
        },
    }


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if not auth_ok(request):
        return HTMLResponse("Unauthorized", status_code=401)

    index = WEB_DIR / "index.html"
    if not index.exists():
        return HTMLResponse("Missing web/index.html", status_code=500)

    return no_cache_html(index.read_text(encoding="utf-8"))


@app.get("/diagnostics", response_class=HTMLResponse)
async def diagnostics_page(request: Request):
    if not auth_ok(request):
        return HTMLResponse("Unauthorized", status_code=401)

    page = WEB_DIR / "diagnostics.html"
    if not page.exists():
        return HTMLResponse("Missing web/diagnostics.html", status_code=500)
    return no_cache_html(page.read_text(encoding="utf-8"))


@app.get("/services", response_class=HTMLResponse)
async def services_page(request: Request):
    if not auth_ok(request):
        return HTMLResponse("Unauthorized", status_code=401)

    page = WEB_DIR / "services.html"
    if not page.exists():
        return HTMLResponse("Missing web/services.html", status_code=500)
    return no_cache_html(page.read_text(encoding="utf-8"))


@app.get("/metrics", response_class=HTMLResponse)
async def metrics_page(request: Request):
    if not auth_ok(request):
        return HTMLResponse("Unauthorized", status_code=401)

    page = WEB_DIR / "metrics.html"
    if not page.exists():
        return HTMLResponse("Missing web/metrics.html", status_code=500)
    return no_cache_html(page.read_text(encoding="utf-8"))


@app.get("/ssh", response_class=HTMLResponse)
async def ssh_page(request: Request):
    if not auth_ok(request):
        return HTMLResponse("Unauthorized", status_code=401)

    page = WEB_DIR / "ssh.html"
    if not page.exists():
        return HTMLResponse("Missing web/ssh.html", status_code=500)
    return no_cache_html(page.read_text(encoding="utf-8"))


@app.get("/terminal", response_class=HTMLResponse)
async def terminal_page(request: Request):
    if not auth_ok(request):
        return HTMLResponse("Unauthorized", status_code=401)

    page = WEB_DIR / "terminal.html"
    if not page.exists():
        return HTMLResponse("Missing web/terminal.html", status_code=500)
    return no_cache_html(page.read_text(encoding="utf-8"))


@app.get("/xtatech.png")
async def logo_png():
    logo = WEB_DIR / "xtatech.png"
    if not logo.exists():
        return PlainTextResponse("missing logo", status_code=404)
    return FileResponse(logo)


@app.get("/api/status")
async def api_status(request: Request):
    if not auth_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    return await build_status_payload()


@app.get("/api/diagnostics")
async def api_diagnostics(request: Request):
    if not auth_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    return {
        "status": await build_status_payload(),
        "recent_packets": await store.latest(100),
        "recent_logs": await logs.latest(200),
    }


@app.get("/api/ssh/status")
async def api_ssh_status(request: Request):
    if not auth_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    return ssh_status_payload()


@app.get("/api/system/metrics")
async def api_system_metrics(request: Request):
    if not auth_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    return await asyncio.to_thread(build_system_metrics_payload)


@app.get("/api/latest")
async def api_latest(request: Request):
    if not auth_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await store.latest(300)


@app.delete("/api/nodes/{node_id}")
async def api_delete_node(node_id: str, request: Request):
    if not auth_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    existed = node_id in stats.nodes_seen
    if existed:
        del stats.nodes_seen[node_id]
        await logs.add("INFO", f"Node removed from seen list: {node_id}")
    return {"ok": True, "removed": existed, "node_id": node_id}


@app.get("/api/logs")
async def api_logs(request: Request, tail: int = 200):
    if not auth_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    tail = max(10, min(int(tail), 800))
    return await logs.latest(tail)


@app.get("/api/config", response_class=PlainTextResponse)
async def api_get_config(request: Request):
    if not auth_ok(request):
        return PlainTextResponse("unauthorized", status_code=401)

    try:
        return CONFIG_PATH.read_text(encoding="utf-8")
    except Exception as e:
        return PlainTextResponse(str(e), status_code=500)


@app.post("/api/config", response_class=PlainTextResponse)
async def api_save_config(request: Request):
    if not auth_ok(request):
        return PlainTextResponse("unauthorized", status_code=401)

    try:
        body = await request.body()
        text = body.decode("utf-8")
        save_config_text(text)
        await logs.add("INFO", "Configuration saved to config.yaml")
        return "OK"
    except ValueError as e:
        return PlainTextResponse(str(e), status_code=400)
    except Exception as e:
        return PlainTextResponse(str(e), status_code=500)


@app.post("/api/config/apply")
async def api_apply_config(request: Request):
    if not auth_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        new_cfg = load_config()
        apply_runtime_config(new_cfg)
        await logs.add("INFO", "Configuration applied from config.yaml")
        return {
            "ok": True,
            "message": "Config applied. Some changes may still require a service restart.",
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/service/restart")
async def api_restart_service(request: Request):
    if not auth_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        await logs.add("WARN", "Service restart requested from web UI")
        subprocess.Popen(
            ["/usr/bin/sudo", "/usr/bin/systemctl", "restart", SERVICE_NAME],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"ok": True, "message": f"Restart triggered for {SERVICE_NAME}"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/repo/clone")
async def api_clone_repo(request: Request):
    global CLONE_PROC

    if not auth_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        if CLONE_PROC and CLONE_PROC.poll() is None:
            return JSONResponse({"error": "A GitHub clone is already running"}, status_code=409)
        if CLONE_PROC and CLONE_PROC.poll() is not None:
            CLONE_PROC = None

        git_bin = shutil.which("git")
        if not git_bin:
            return JSONResponse({"error": "git is not installed on this Raspberry Pi"}, status_code=500)

        target = REPO_CLONE_TARGET.expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        log_path = REPO_CLONE_LOG.expanduser()

        remote_sha = github_head_sha(git_bin)
        local_sha = ""
        if target.exists():
            if not (target / ".git").exists():
                local_sha = "not-a-git-repository"
            else:
                local_sha = local_repo_sha(git_bin, target)
                if local_sha == remote_sha:
                    CLONE_PROC = None
                    log_path.write_text(
                        f"Repository is already current.\nLocal HEAD: {local_sha}\nGitHub HEAD: {remote_sha}\n",
                        encoding="utf-8",
                    )
                    await logs.add("INFO", f"GitHub clone skipped; Downloads repo is already current: {target}")
                    return {
                        "ok": True,
                        "message": "Downloads repo is already current",
                        "repo_url": GITHUB_REPO_URL,
                        "target": str(target),
                        "log": str(log_path),
                        "local_head": local_sha,
                        "github_head": remote_sha,
                        "current": True,
                    }

            shutil.rmtree(target)
            await logs.add(
                "WARN",
                (
                    "Downloads repo was missing, older, or not comparable and was removed "
                    f"before re-clone: {local_sha or 'none'} -> {remote_sha}"
                )
            )

        log_file = log_path.open("wb")
        log_file.write(
            (
                f"GitHub HEAD: {remote_sha}\n"
                f"Local HEAD: {local_sha or 'none'}\n"
                f"Cloning because the Downloads folder is missing or older than GitHub.\n"
            ).encode("utf-8")
        )
        log_file.flush()
        try:
            CLONE_PROC = subprocess.Popen(
                [git_bin, "clone", "--progress", GITHUB_REPO_URL, str(target)],
                cwd=str(target.parent),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log_file.close()
        await logs.add("INFO", f"GitHub clone requested from web UI: {GITHUB_REPO_URL} -> {target}")
        return {
            "ok": True,
            "message": "GitHub clone started",
            "repo_url": GITHUB_REPO_URL,
            "target": str(target),
            "log": str(log_path),
            "pid": CLONE_PROC.pid,
            "local_head": local_sha,
            "github_head": remote_sha,
            "current": False,
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/repo/clone/status")
async def api_clone_repo_status(request: Request):
    if not auth_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        target = REPO_CLONE_TARGET.expanduser()
        log_path = REPO_CLONE_LOG.expanduser()
        text = ""
        if log_path.exists():
            text = log_path.read_text(encoding="utf-8", errors="replace")

        returncode = CLONE_PROC.poll() if CLONE_PROC else None
        running = CLONE_PROC is not None and returncode is None

        stage = "Waiting"
        percent = 0
        download_percent = None
        for match in _GIT_PROGRESS_RE.finditer(text):
            stage = match.group(1)
            value = max(0, min(int(match.group(2)), 100))
            if stage == "Receiving objects":
                download_percent = value
            percent = value

        if download_percent is not None:
            percent = download_percent
            stage = "Receiving objects"
        elif "Resolving deltas" in text:
            percent = 100
            stage = "Download complete; resolving deltas"
        elif "Repository is already current." in text:
            percent = 100
            stage = "Already current"
        elif "Cloning into" in text:
            stage = "Starting download"

        complete = target.exists() and (
            (returncode == 0)
            or ("done." in text and not running)
            or ("Repository is already current." in text)
        )
        failed = returncode is not None and returncode != 0
        if complete:
            percent = 100
            if "done." in text or "Resolving deltas" in text:
                stage = "Complete"
        elif failed:
            stage = "Clone failed"

        tail = text[-4000:] if text else ""
        return {
            "ok": True,
            "target": str(target),
            "log": str(log_path),
            "stage": stage,
            "percent": percent,
            "running": running,
            "complete": complete,
            "failed": failed,
            "log_tail": tail,
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/repo/install/eligibility")
async def api_install_download_repo_eligibility(request: Request):
    if not auth_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        git_bin = shutil.which("git")
        if not git_bin:
            return JSONResponse(
                {
                    "ok": True,
                    "eligible": False,
                    "reason": "git is not installed on this Raspberry Pi",
                    "target": str(REPO_CLONE_TARGET.expanduser()),
                    "download_head": "",
                    "github_head": "",
                    "installed_head": "",
                }
            )
        return install_eligibility(git_bin)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/repo/install")
async def api_install_download_repo(request: Request):
    global INSTALL_PROC

    if not auth_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        if INSTALL_PROC and INSTALL_PROC.poll() is None:
            return JSONResponse({"error": "An install is already running"}, status_code=409)
        if INSTALL_PROC and INSTALL_PROC.poll() is not None:
            INSTALL_PROC = None

        git_bin = shutil.which("git")
        if not git_bin:
            return JSONResponse({"error": "git is not installed on this Raspberry Pi"}, status_code=500)

        eligibility = install_eligibility(git_bin)
        if not eligibility.get("eligible"):
            return JSONResponse(
                {"error": eligibility.get("reason") or "Downloaded version is not newer than installed"},
                status_code=409,
            )

        verified = verify_download_repo(git_bin)
        install_path = verified["install_path"]
        log_path = REPO_INSTALL_LOG.expanduser()

        log_file = log_path.open("wb")
        log_file.write(
            (
                "Verified Downloads repository before install.\n"
                f"Local HEAD: {verified['local_head']}\n"
                f"GitHub HEAD: {verified['github_head']}\n"
                f"Installer: {install_path}\n"
            ).encode("utf-8")
        )
        log_file.flush()
        try:
            INSTALL_PROC = subprocess.Popen(
                ["/usr/bin/sudo", "-n", "/bin/bash", str(install_path)],
                cwd=str(verified["target"]),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log_file.close()

        await logs.add("WARN", f"Verified Downloads repo install requested from web UI: {install_path}")
        return {
            "ok": True,
            "message": "Install started from verified Downloads repo",
            "target": str(verified["target"]),
            "installer": str(install_path),
            "log": str(log_path),
            "pid": INSTALL_PROC.pid,
            "local_head": verified["local_head"],
            "github_head": verified["github_head"],
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/repo/install/status")
async def api_install_download_repo_status(request: Request):
    if not auth_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        log_path = REPO_INSTALL_LOG.expanduser()
        text = ""
        if log_path.exists():
            text = log_path.read_text(encoding="utf-8", errors="replace")

        returncode = INSTALL_PROC.poll() if INSTALL_PROC else None
        running = INSTALL_PROC is not None and returncode is None
        percent = 0
        stage = "Waiting"

        if "Verified Downloads repository before install." in text:
            percent = 3
            stage = "Verified download"

        for marker, marker_percent, marker_stage in INSTALL_STAGES:
            if marker in text:
                percent = marker_percent
                stage = marker_stage

        complete = "== Installed and started ==" in text or returncode == 0
        failed = returncode is not None and returncode != 0

        if complete:
            percent = 100
            stage = "Install complete"
        elif failed:
            stage = "Install failed"

        tail = text[-6000:] if text else ""
        return {
            "ok": True,
            "log": str(log_path),
            "stage": stage,
            "percent": percent,
            "running": running,
            "complete": complete,
            "failed": failed,
            "log_tail": tail,
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/system/reboot")
async def api_reboot_system(request: Request):
    if not auth_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        await logs.add("ERROR", "Raspberry Pi reboot requested from web UI")
        subprocess.Popen(
            ["/usr/bin/sudo", "/usr/bin/systemctl", "reboot"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"ok": True, "message": "Raspberry Pi reboot requested"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.websocket("/ws")
async def ws_payload(ws: WebSocket):
    await ws.accept()
    async with ws_payload_lock:
        ws_payload_clients.append(ws)

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        async with ws_payload_lock:
            if ws in ws_payload_clients:
                ws_payload_clients.remove(ws)


@app.websocket("/ws/logs")
async def ws_logs(ws: WebSocket):
    await ws.accept()
    async with ws_log_lock:
        ws_log_clients.append(ws)

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        async with ws_log_lock:
            if ws in ws_log_clients:
                ws_log_clients.remove(ws)


def terminal_cfg() -> Dict[str, Any]:
    cfg = runtime.get("cfg") or {}
    return cfg.get("terminal", {}) or {}


def terminal_allowed(token: str) -> bool:
    term_cfg = terminal_cfg()
    expected = str(term_cfg.get("token", ""))
    return bool(term_cfg.get("enabled")) and bool(expected) and token == expected


@app.websocket("/ws/terminal")
async def websocket_terminal(ws: WebSocket):
    token = ws.query_params.get("token", "")
    await ws.accept()

    if not terminal_allowed(token):
        await ws.send_text("Terminal access denied. Check terminal.enabled and terminal.token in config.yaml.\r\n")
        await ws.close()
        return

    if os.name != "posix":
        await ws.send_text("Terminal is only available on Linux/POSIX hosts.\r\n")
        await ws.close()
        return

    import pty

    term_cfg = terminal_cfg()
    shell = str(term_cfg.get("shell") or os.environ.get("SHELL") or "/bin/bash")
    cwd = str(term_cfg.get("cwd") or str(BASE_DIR))
    max_session_seconds = int(term_cfg.get("max_session_seconds", 3600))

    if not Path(shell).exists():
        await ws.send_text(f"Configured shell does not exist: {shell}\r\n")
        await ws.close()
        return

    master_fd, slave_fd = pty.openpty()
    env = os.environ.copy()
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("COLORTERM", "truecolor")
    env.setdefault("PS1", "\\u@\\h:\\w $ ")

    proc = subprocess.Popen(
        shlex.split(shell),
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd=cwd if Path(cwd).exists() else str(BASE_DIR),
        env=env,
        close_fds=True,
    )
    os.close(slave_fd)

    started = time.time()

    async def read_pty():
        while proc.poll() is None:
            try:
                data = await asyncio.to_thread(os.read, master_fd, 4096)
                if not data:
                    break
                await ws.send_text(data.decode("utf-8", errors="replace"))
            except Exception:
                break

    reader = asyncio.create_task(read_pty())
    try:
        await ws.send_text(f"Connected to local shell as PID {proc.pid}. Session limit: {max_session_seconds}s.\r\n")
        while proc.poll() is None:
            if time.time() - started > max_session_seconds:
                await ws.send_text("\r\nSession limit reached. Closing terminal.\r\n")
                break
            try:
                text = await asyncio.wait_for(ws.receive_text(), timeout=1)
            except asyncio.TimeoutError:
                continue
            os.write(master_fd, text.encode("utf-8"))
    except WebSocketDisconnect:
        pass
    finally:
        reader.cancel()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        try:
            os.close(master_fd)
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn

    cfg = load_config()
    web_cfg = cfg["web"]
    ssl_certfile = resolve_local_path(web_cfg.get("ssl_certfile"))
    ssl_keyfile = resolve_local_path(web_cfg.get("ssl_keyfile"))
    uvicorn_kwargs = {
        "host": web_cfg["host"],
        "port": int(web_cfg["port"]),
        "reload": False,
    }

    if ssl_certfile or ssl_keyfile:
        if ssl_certfile and ssl_keyfile and Path(ssl_certfile).exists() and Path(ssl_keyfile).exists():
            uvicorn_kwargs["ssl_certfile"] = ssl_certfile
            uvicorn_kwargs["ssl_keyfile"] = ssl_keyfile
        else:
            print(
                "HTTPS is configured, but the TLS certificate or key file is missing. "
                "Starting without HTTPS."
            )

    uvicorn.run(app, **uvicorn_kwargs)
