"""Explicit, pinned plugin updates through Hermes's public CLI and PM gates."""
import fcntl
import json
import shutil
import subprocess
import time

from .contracts import Problem, request_id
from .storage import Store

REPOSITORY = "https://github.com/hartlco/klaudimero-hermes-plugin"
PYTHON_DEPENDENCIES = ["cryptography>=50,<51"]


def status(owner, receipt):
    receipt = request_id(receipt)
    with Store().connection() as conn:
        row = conn.execute("SELECT * FROM plugin_updates WHERE id=? AND owner=?", (receipt, owner)).fetchone()
    if not row:
        raise Problem(404, "update_not_found")
    return {"request_id": receipt, "profile_id": row["profile"], "revision": row["revision"],
            "state": row["state"], "error": row["error"], "restart_required": row["state"] == "complete"}


def update(owner, receipt, profile, revision, approved):
    from hermes_cli.profiles import get_profile_dir, list_profile_names
    if approved is not True:
        raise Problem(409, "python_dependency_approval_required")
    receipt = request_id(receipt)
    if profile not in {"default", *list_profile_names()}:
        raise Problem(404, "profile_not_found")
    executable = shutil.which("hermes")
    git = shutil.which("git")
    if not executable or not git:
        raise Problem(503, "hermes_cli_unavailable")
    store = Store()
    with store.connection() as conn:
        row = conn.execute("SELECT * FROM plugin_updates WHERE id=?", (receipt,)).fetchone()
        if row:
            if (row["owner"], row["profile"], row["revision"]) != (owner, profile, revision):
                raise Problem(409, "update_request_id_reused")
            return status(owner, receipt)
    with (store.directory / "plugin-update.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Problem(409, "plugin_update_already_running") from None
        with store.connection() as conn:
            # Recheck under the cross-process lock, before executing anything.
            row = conn.execute("SELECT id FROM plugin_updates WHERE id=?", (receipt,)).fetchone()
            if row:
                return status(owner, receipt)
            conn.execute("INSERT INTO plugin_updates(id,owner,profile,revision,state,created) VALUES(?,?,?,?,?,?)",
                         (receipt, owner, profile, revision, "running", time.time()))
        error = None
        try:
            result = subprocess.run([executable, "-p", profile, "plugins", "install", REPOSITORY,
                "--ref", revision, "--enable", "--force", "--yes-deps"],
                capture_output=True, timeout=120, check=False)
            if result.returncode:
                error = "hermes_plugin_update_refused"
            else:
                # Success means the exact requested tree is actually installed.
                checkout = get_profile_dir(profile) / "plugins" / "klaudimero"
                check = subprocess.run([git, "-C", str(checkout), "rev-parse", "HEAD"],
                    capture_output=True, timeout=10, check=False)
                if check.returncode or check.stdout.decode().strip() != revision:
                    error = "installed_revision_unverified"
        except subprocess.TimeoutExpired:
            error = "plugin_update_timed_out_check_before_retry"
        except UnicodeDecodeError:
            error = "installed_revision_unverified"
        except OSError:
            error = "hermes_cli_unavailable"
        with store.connection() as conn:
            conn.execute("UPDATE plugin_updates SET state=?,error=? WHERE id=?",
                         ("failed" if error else "complete", error, receipt))
        return status(owner, receipt)
