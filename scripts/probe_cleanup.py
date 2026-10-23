"""Cleanup interlocks shared by the disposable installed probes."""

from pathlib import Path

BACKUP_STATE = Path("/var/lib/shinyd-backup")


def backup_interrupted():
    return any(
        (BACKUP_STATE / name).exists() or (BACKUP_STATE / name).is_symlink()
        for name in ("maintenance", "restore-incomplete")
    )


def prepare_runtime_cleanup(policy, expected, run):
    # Cold backup stops this socket. Retiring even removed IDs needs it again.
    # Never bypass a restore interlock or a policy belonging to another installation.
    if backup_interrupted():
        raise RuntimeError("backup_interrupted_preserved")
    if (
        expected is None
        or not policy.is_file()
        or policy.is_symlink()
        or policy.read_bytes() != expected
    ):
        raise RuntimeError("runtime_cleanup_policy_mismatch")
    result = run("systemctl", "start", "shinyd-runtime.socket", check=False)
    if (
        result.returncode
        or run("systemctl", "is-active", "--quiet", "shinyd-runtime.socket", check=False).returncode
    ):
        raise RuntimeError("runtime_cleanup_socket_unavailable")
