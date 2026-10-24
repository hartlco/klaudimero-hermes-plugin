"""Restart only the systemd unit proven to own this Hermes dashboard.

No caller supplies a command, PID or unit. Unmanaged/container hosts stay manual.
The client confirms a restart by observing a different dashboard process identity.
"""
import fcntl
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from .contracts import Problem, request_id
from .storage import Store


def _read(pid, name):
    return Path(f"/proc/{pid}/{name}").read_bytes()


def _dashboard():
    if sys.platform != "linux":
        return None
    pid = os.getpid()
    for _ in range(16):
        if pid <= 1:
            break
        try:
            stat = _read(pid, "stat").decode().rsplit(")", 1)[1].split()
            argv = [x.decode() for x in _read(pid, "cmdline").split(b"\0") if x]
            # Match a Hermes CLI launch, not arbitrary ancestor services or a
            # process that merely has 'dashboard' in a filename/argument.
            launch = next((i for i, value in enumerate(argv) if Path(value).name == "hermes"), None)
            if launch is not None:
                tail = argv[launch + 1:]
                if tail[:1] in (["-p"], ["--profile"]) and len(tail) >= 3:
                    tail = tail[2:]
                if tail[:1] in (["dashboard"], ["serve"]):
                    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
                    instance = hashlib.sha256(f"{boot}:{pid}:{stat[19]}".encode()).hexdigest()
                    return pid, instance
            pid = int(stat[1])
        except (OSError, ValueError, IndexError, UnicodeError):
            return None
    return None


def _target():
    process = _dashboard()
    if not process:
        return None, None
    pid, instance = process
    executable = shutil.which("systemctl")
    if executable:
        try:
            for line in _read(pid, "cgroup").decode().splitlines():
                if not line.startswith("0::"):
                    continue
                path = line[3:]
                unit = path.rsplit("/", 1)[-1]
                if not re.fullmatch(r"[A-Za-z0-9_.@\\-]+\.service", unit) or unit.startswith("user@"):
                    continue
                scope = ["--user"] if path.startswith("/user.slice/") else [] if path.startswith("/system.slice/") else None
                if scope is None:
                    continue
                command = [executable, *scope, "--no-ask-password"]
                probe = subprocess.run([*command, "show", unit, "--property=MainPID", "--value"],
                                       capture_output=True, timeout=3, check=False)
                if probe.returncode == 0 and probe.stdout.decode().strip() == str(pid):
                    return instance, [*command, "--no-block", "restart", unit]
        except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
            pass
    return instance, None


def plan():
    instance, command = _target()
    return {"protocol": 1, "instance_id": instance, "restart_available": command is not None,
            "reason": None if command else "Restart this dashboard using its host service manager. Automatic restart requires a systemd service whose MainPID is the Hermes dashboard."}


def restart(owner, receipt, expected, confirmed):
    receipt = request_id(receipt)
    if confirmed is not True:
        raise Problem(409, "restart_confirmation_required")
    store = Store()
    with (store.directory / "plugin-update.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Problem(409, "plugin_update_or_restart_running") from None
        with store.connection() as conn:
            prior = conn.execute("SELECT * FROM dashboard_restarts WHERE id=?", (receipt,)).fetchone()
            if prior:
                if prior["owner"] != owner or prior["instance"] != expected:
                    raise Problem(409, "restart_request_id_reused")
                return {"request_id": receipt, "state": prior["state"], "error": prior["error"]}
        instance, command = _target()
        if not command:
            raise Problem(409, "dashboard_restart_unavailable")
        if instance != expected:
            raise Problem(409, "dashboard_already_changed_refresh")
        with store.connection() as conn:
            if conn.execute("SELECT 1 FROM dashboard_restarts WHERE instance=? AND state='requested'", (instance,)).fetchone():
                raise Problem(409, "dashboard_restart_already_requested")
            conn.execute("INSERT INTO dashboard_restarts VALUES(?,?,?,?,?,?)",
                         (receipt, owner, instance, "requested", None, time.time()))
        # systemd owns the queued job, so replacing this dashboard/plugin host
        # cannot kill a detached helper before it gets a chance to execute.
        error = None
        try:
            result = subprocess.run(command, capture_output=True, timeout=5, check=False)
            if result.returncode:
                error = "dashboard_restart_permission_denied_or_failed"
        except subprocess.TimeoutExpired:
            # The job may already be queued. Never replay an uncertain restart.
            pass
        except OSError:
            error = "dashboard_restart_command_unavailable"
        if error:
            with store.connection() as conn:
                conn.execute("UPDATE dashboard_restarts SET state='failed',error=? WHERE id=?", (error, receipt))
        return {"request_id": receipt, "state": "failed" if error else "requested", "error": error}
