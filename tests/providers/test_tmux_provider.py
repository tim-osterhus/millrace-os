from __future__ import annotations

import shlex
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

import millrace_os.tmux_provider as tmux

SOCKET = "/tmp/millrace-os-p03.sock"
SERVER = tmux.TmuxServerIdentity(
    socket_path=SOCKET,
    pid=101,
    version="3.7c",
    version_sha256="a" * 64,
    session_created=10,
)
SESSION = tmux.TmuxSessionFact(
    session_id="$0",
    name="millrace",
    created=10,
    window_count=2,
    server_generation=SERVER.generation,
)


def _window(
    window_id: str,
    index: int,
    workspace_id: str,
    instance_id: str,
) -> tmux.TmuxWindowFact:
    return tmux.TmuxWindowFact(
        window_id=window_id,
        index=index,
        name="same-basename",
        pane_count=1,
        session_id="$0",
        workspace_id=workspace_id,
        instance_id=instance_id,
    )


def _pane(pane_id: str, window_id: str, *, dead: bool = False) -> tmux.TmuxPaneFact:
    return tmux.TmuxPaneFact(
        pane_id=pane_id,
        index=0,
        pid=202,
        dead=dead,
        dead_status=17 if dead else None,
        exit_status=17 if dead else None,
        window_id=window_id,
        session_id="$0",
    )


def _inventory(
    windows: list[tmux.TmuxWindowFact] | None = None,
    panes: list[tmux.TmuxPaneFact] | None = None,
) -> tmux.TmuxInventory:
    windows = windows or [_window("@0", 0, "workspace-a", "instance-a")]
    panes = panes or [_pane("%0", windows[0].window_id)]
    session = replace(SESSION, window_count=len(windows))
    provider = tmux.TmuxProvider.__new__(tmux.TmuxProvider)
    return provider._build_inventory(SERVER, session, windows, panes, "now")


def test_config_requires_an_explicit_isolated_socket() -> None:
    with pytest.raises(tmux.TmuxConfigurationError):
        tmux.TmuxProvider()
    with pytest.raises(tmux.TmuxConfigurationError):
        tmux.TmuxConfig(socket_path="relative.sock")
    with pytest.raises(tmux.TmuxConfigurationError):
        tmux.TmuxConfig(socket_path=SOCKET, expected_sha256="not-a-digest")


def test_same_basename_windows_resolve_by_stable_ids() -> None:
    windows = [
        _window("@0", 0, "workspace-a", "instance-a"),
        _window("@1", 1, "workspace-b", "instance-b"),
    ]
    inventory = _inventory(windows, [_pane("%0", "@0"), _pane("%1", "@1")])

    first = inventory.mapping_for("workspace-a", "instance-a")
    second = inventory.mapping_for("workspace-b", "instance-b")
    assert first.window_name == second.window_name == "same-basename"
    assert (first.window_id, first.pane_id) == ("@0", "%0")
    assert (second.window_id, second.pane_id) == ("@1", "%1")


def test_duplicate_workspace_or_instance_ids_refuse_reconciliation() -> None:
    duplicate_workspace = [
        _window("@0", 0, "workspace-a", "instance-a"),
        _window("@1", 1, "workspace-a", "instance-b"),
    ]
    with pytest.raises(tmux.TmuxDuplicateMapping):
        _inventory(duplicate_workspace, [_pane("%0", "@0"), _pane("%1", "@1")])

    duplicate_instance = [
        _window("@0", 0, "workspace-a", "instance-a"),
        _window("@1", 1, "workspace-b", "instance-a"),
    ]
    with pytest.raises(tmux.TmuxDuplicateMapping):
        _inventory(duplicate_instance, [_pane("%0", "@0"), _pane("%1", "@1")])


def test_partial_metadata_and_multiple_panes_refuse() -> None:
    partial = replace(_window("@0", 0, "workspace-a", "instance-a"), instance_id=None)
    with pytest.raises(tmux.TmuxMappingError):
        _inventory([partial], [_pane("%0", "@0")])

    two_panes = [_pane("%0", "@0"), _pane("%1", "@0")]
    with pytest.raises(tmux.TmuxTopologyError):
        _inventory([_window("@0", 0, "workspace-a", "instance-a")], two_panes)


def test_dead_pane_fact_does_not_claim_workflow_state() -> None:
    mapping = _inventory(panes=[_pane("%0", "@0", dead=True)]).mappings[0]

    assert mapping.process_fact == "dead"
    assert mapping.pane_dead is True
    assert mapping.pane_exit_status == 17
    assert not hasattr(mapping, "workflow_state")


def test_attach_descriptor_is_shell_escaped_and_contains_exact_identity() -> None:
    inventory = _inventory()
    provider = tmux.TmuxProvider.__new__(tmux.TmuxProvider)
    provider.config = tmux.TmuxConfig(
        socket_path=SOCKET,
        executable="/opt/homebrew/Cellar/tmux/3.7c/bin/tmux",
        expected_sha256=None,
    )
    descriptor = provider._descriptor(inventory.mappings[0], inventory)

    assert descriptor.argv[-1] == "$0:@0.%0"
    assert descriptor.socket_path == SOCKET
    assert descriptor.workspace_id == "workspace-a"
    assert descriptor.instance_id == "instance-a"
    assert descriptor.display == shlex.join(descriptor.argv)
    assert descriptor.argv_list == list(descriptor.argv)
    assert "attach-session" in descriptor.display
    assert descriptor.observation_id.endswith(":workspace-a:instance-a")


def test_descriptor_from_another_server_generation_is_stale() -> None:
    inventory = _inventory()
    provider = tmux.TmuxProvider.__new__(tmux.TmuxProvider)
    provider.config = tmux.TmuxConfig(socket_path=SOCKET, expected_sha256=None)
    descriptor = provider._descriptor(inventory.mappings[0], inventory)
    current = replace(
        inventory,
        server=replace(inventory.server, pid=303),
    )
    provider.reconcile = lambda: current

    with pytest.raises(tmux.TmuxServerRestarted):
        provider.validate_attach_descriptor(descriptor)


def test_reset_and_hints_only_change_replaceable_state() -> None:
    provider = tmux.TmuxProvider.__new__(tmux.TmuxProvider)
    provider._last_inventory = _inventory()
    provider._last_server_generation = SERVER.generation
    provider._server_was_unavailable = False
    provider._hint = None
    provider._state = "healthy"
    provider.config = tmux.TmuxConfig(socket_path=SOCKET, expected_sha256=None)

    hint = provider.hint("hook-change")
    assert hint.requires_reconcile is True
    assert provider.state == "reconciling"
    provider.reset()
    assert provider.last_inventory is None
    assert provider.state == "reconciling"


def test_rows_and_numeric_facts_fail_closed() -> None:
    with pytest.raises(tmux.TmuxMalformedOutput):
        tmux._rows("only|two", 3, "fixture")
    with pytest.raises(tmux.TmuxMalformedOutput):
        tmux._int("not-an-int", "pid")
    with pytest.raises(tmux.TmuxMalformedOutput):
        tmux._bool("yes", "dead")


def test_environment_drops_inherited_tmux_variables(tmp_path: Path) -> None:
    provider = tmux.TmuxProvider(
        socket_path=SOCKET,
        expected_sha256=None,
        cwd=tmp_path,
        environment={"TMUX": "unsafe", "TMUX_PANE": "%9", "EXTRA": "ok"},
    )

    environment = provider._environment()
    assert "TMUX" not in environment
    assert "TMUX_PANE" not in environment
    assert "TMUX_TMPDIR" not in environment
    assert environment["EXTRA"] == "ok"


def test_missing_binary_is_typed_and_health_does_not_fabricate_availability(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "missing-tmux"
    provider = tmux.TmuxProvider(
        socket_path=SOCKET,
        executable=executable,
        expected_sha256=None,
    )

    health = provider.health()
    assert health.available is False
    assert health.reason == tmux.TmuxExecutableMissing.code
    with pytest.raises(tmux.TmuxExecutableMissing):
        provider.reconcile()


def test_subprocess_timeout_and_output_bounds_are_typed(monkeypatch) -> None:
    provider = tmux.TmuxProvider(
        socket_path=SOCKET,
        expected_sha256=None,
        timeout_seconds=0.01,
        max_output_bytes=8,
    )

    class TimeoutProcess:
        returncode = -9

        def communicate(self, *, timeout=None):
            if timeout is None:
                return b"", b""
            raise subprocess.TimeoutExpired(["fixture"], timeout)

        def kill(self):
            return None

    monkeypatch.setattr(
        tmux.subprocess, "Popen", lambda *args, **kwargs: TimeoutProcess()
    )
    with pytest.raises(tmux.TmuxCommandTimeout):
        provider._run(("list-sessions",), command="fixture-timeout")

    class OverflowProcess:
        returncode = 0

        def communicate(self, *, timeout):
            return b"0123456789", b""

    monkeypatch.setattr(
        tmux.subprocess, "Popen", lambda *args, **kwargs: OverflowProcess()
    )
    with pytest.raises(tmux.TmuxOutputOverflow):
        provider._run(("list-sessions",), command="fixture-overflow")


def test_subprocess_is_argv_only_and_has_no_inherited_tmux_state(monkeypatch) -> None:
    provider = tmux.TmuxProvider(
        socket_path=SOCKET,
        expected_sha256=None,
        environment={"TMUX": "bad", "TMUX_PANE": "%9"},
    )
    seen: dict[str, object] = {}

    class Process:
        returncode = 0

        def communicate(self, *, timeout):
            return b"ok\n", b""

    def popen(argv, **kwargs):
        seen["argv"] = argv
        seen.update(kwargs)
        return Process()

    monkeypatch.setattr(tmux.subprocess, "Popen", popen)
    assert provider._run(("list-sessions",), command="fixture") == "ok\n"
    assert seen["shell"] is False
    assert seen["stdin"] is subprocess.DEVNULL
    assert "TMUX" not in seen["env"]
    assert "TMUX_PANE" not in seen["env"]
    assert seen["argv"][:3] == [str(provider.config.executable), "-S", SOCKET]
