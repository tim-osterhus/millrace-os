from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from millrace_os.tmux_provider import (
    DEFAULT_TMUX_EXECUTABLE,
    DEFAULT_TMUX_SHA256,
    TmuxConfig,
    TmuxProvider,
    TmuxServerRestarted,
    TmuxServerUnavailable,
)


@pytest.fixture
def real_tmux(tmp_path: Path):
    executable = Path(os.environ.get("MILLRACE_P03_TMUX", DEFAULT_TMUX_EXECUTABLE))
    if not executable.is_file():
        pytest.skip("pinned stock tmux is not installed")
    # Each test owns a short socket directory, independent of checkout location.
    socket_root = Path(tempfile.mkdtemp(prefix="os-tmux-", dir="/tmp"))
    socket = socket_root / "a.sock"
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    runtime = tmp_path / "runtime"
    work = tmp_path / "work"
    for directory in (home, xdg, runtime, work):
        directory.mkdir()
    environment = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(xdg),
        "XDG_RUNTIME_DIR": str(runtime),
        "TERM": "xterm-256color",
        # These deliberately exercise the provider's clean environment.
        "TMUX": "inherited-value",
        "TMUX_PANE": "%999",
        "TMUX_TMPDIR": str(tmp_path / "unsafe-tmux-tmp"),
    }
    config = TmuxConfig(
        socket_path=socket,
        executable=executable,
        expected_sha256=DEFAULT_TMUX_SHA256
        if executable == Path(DEFAULT_TMUX_EXECUTABLE)
        else hashlib.sha256(executable.read_bytes()).hexdigest(),
        cwd=work,
        environment=environment,
    )

    def tmux(*args: str, check: bool = True) -> str:
        result = subprocess.run(
            [str(executable), "-S", str(socket), *args],
            check=check,
            capture_output=True,
            text=True,
            cwd=work,
            env={
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(xdg),
                "XDG_RUNTIME_DIR": str(runtime),
                "TERM": "xterm-256color",
                "LANG": "C",
                "LC_ALL": "C",
            },
            timeout=5,
        )
        if check:
            result.check_returncode()
        return result.stdout

    def provision() -> None:
        tmux("new-session", "-d", "-s", "millrace", "-n", "same-basename", "sleep 120")
        tmux("set-option", "-w", "-t", "@0", "@millrace_workspace_id", "workspace-a")
        tmux("set-option", "-w", "-t", "@0", "@millrace_instance_id", "instance-a")
        tmux("new-window", "-t", "millrace", "-n", "same-basename", "sleep 120")
        tmux("set-option", "-w", "-t", "@1", "@millrace_workspace_id", "workspace-b")
        tmux("set-option", "-w", "-t", "@1", "@millrace_instance_id", "instance-b")

    try:
        provision()
        provider = TmuxProvider(config)
        yield provider, tmux, socket
    finally:
        subprocess.run(
            [str(executable), "-S", str(socket), "kill-server"],
            check=False,
            capture_output=True,
            cwd=work,
            env={
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(xdg),
                "XDG_RUNTIME_DIR": str(runtime),
                "TERM": "xterm-256color",
                "LANG": "C",
                "LC_ALL": "C",
            },
            timeout=5,
        )
        socket.unlink(missing_ok=True)
        socket_root.rmdir()


def test_real_stock_mapping_and_safe_attach_descriptor(real_tmux) -> None:
    provider, tmux, _ = real_tmux

    health = provider.health()
    inventory = provider.reconcile()
    first = inventory.mapping_for("workspace-a", "instance-a")
    second = inventory.mapping_for("workspace-b", "instance-b")
    descriptor = provider.attach_descriptor("workspace-a", "instance-a")

    assert health.available is True
    assert health.version == tmux("-V").strip()
    assert health.executable_sha256 == hashlib.sha256(
        Path(provider.config.executable).read_bytes()
    ).hexdigest()
    assert first.window_name == second.window_name == "same-basename"
    assert first.window_id != second.window_id
    assert first.pane_id != second.pane_id
    assert descriptor.argv[0] == str(provider.config.executable)
    assert descriptor.argv[1:3] == ("-S", str(provider.config.socket_path))
    assert descriptor.argv[-1] == (
        f"{first.session_id}:{first.window_id}.{first.pane_id}"
    )
    assert descriptor.display.endswith("'$0:@0.%0'") or "attach-session" in (
        descriptor.display
    )


def test_detach_reset_and_fresh_reconcile_leave_panes_alive(real_tmux) -> None:
    provider, tmux, _ = real_tmux
    inventory = provider.reconcile()
    pids = {mapping.pane_pid for mapping in inventory.mappings}

    # There is no client for the detached -d fixture; this command confirms
    # that the provider never needs an attachment or terminal stream.
    tmux("detach-client", "-a", check=False)
    provider.reset()
    assert provider.last_inventory is None
    refreshed = provider.reconcile()
    assert {mapping.pane_pid for mapping in refreshed.mappings} == pids
    for pid in pids:
        os.kill(pid, 0)


def test_dead_pane_retains_exit_fact_without_workflow_claim(real_tmux) -> None:
    provider, tmux, _ = real_tmux
    tmux("set-option", "-w", "-t", "@0", "remain-on-exit", "on")
    tmux("respawn-pane", "-k", "-t", "%0", "sh", "-c", "exit 17")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        inventory = provider.reconcile()
        mapping = inventory.mapping_for("workspace-a", "instance-a")
        if mapping.pane_dead:
            break
        time.sleep(0.05)
    else:
        pytest.fail("fixture pane did not become dead")
    assert mapping.pane_dead_status == 17
    assert mapping.pane_exit_status in (None, 17)
    assert mapping.process_fact == "dead"
    assert not hasattr(mapping, "workflow_state")


def test_server_restart_invalidates_old_descriptor(real_tmux) -> None:
    provider, tmux, socket = real_tmux
    descriptor = provider.attach_descriptor("workspace-a", "instance-a")
    tmux("kill-server")
    assert provider.health().available is False
    with pytest.raises(TmuxServerUnavailable):
        provider.reconcile()

    # The fixture owns this replacement server and recreates the same visible
    # IDs; the new server PID/generation must still fence the old descriptor.
    executable = Path(provider.config.executable)
    tmux("new-session", "-d", "-s", "millrace", "-n", "same-basename", "sleep 120")
    tmux("set-option", "-w", "-t", "@0", "@millrace_workspace_id", "workspace-a")
    tmux("set-option", "-w", "-t", "@0", "@millrace_instance_id", "instance-a")
    with pytest.raises(TmuxServerRestarted):
        provider.validate_attach_descriptor(descriptor)
    assert executable.is_file()
    assert socket.is_absolute()
