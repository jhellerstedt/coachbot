from pathlib import Path

import suunto_client
import suunto_sync
from suunto_client import SuuntoCfg, resolve_suuntool_binary
from suunto_sync import sync_suunto_workouts_for_athlete_detailed


def _cfg(path: Path | None) -> SuuntoCfg:
    return SuuntoCfg(
        enabled=True,
        primary=True,
        suuntool_path=path,
        session_file=None,
        indoor_rowing_activity_ids=frozenset({57}),
        gym_activity_ids=frozenset({23}),
    )


def test_resolve_falls_back_when_configured_path_missing(tmp_path: Path, capsys):
    missing = tmp_path / "nope" / "suuntool"
    fallback_dir = tmp_path / "bin"
    fallback_dir.mkdir()
    fallback = fallback_dir / "suuntool"
    fallback.write_text("#!/bin/sh\n")
    fallback.chmod(0o755)
    found = resolve_suuntool_binary(_cfg(missing), tmp_path)
    assert found == fallback.resolve()
    captured = capsys.readouterr()
    assert (
        f"suuntool: configured path missing ({missing.resolve()}); using {found}"
        in captured.err
    )


def test_resolve_falls_back_to_parent_bin_when_base_bin_missing(
    tmp_path: Path, monkeypatch
):
    base = tmp_path / "project"
    base.mkdir()
    missing = base / "nope" / "suuntool"
    parent_bin = base.parent / "bin"
    parent_bin.mkdir()
    fallback = parent_bin / "suuntool"
    fallback.write_text("#!/bin/sh\n")
    fallback.chmod(0o755)

    monkeypatch.setattr(suunto_client.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(suunto_client.shutil, "which", lambda _name: None)

    found = resolve_suuntool_binary(_cfg(missing), base)
    assert found == fallback.resolve()


def test_resolve_raises_when_nothing_exists(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(suunto_client.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(suunto_client.shutil, "which", lambda _name: None)

    try:
        resolve_suuntool_binary(_cfg(tmp_path / "missing"), tmp_path)
    except FileNotFoundError as exc:
        assert "suuntool" in str(exc).lower()
    else:
        raise AssertionError("expected FileNotFoundError")


def test_detailed_sync_returns_suunto_client_error(tmp_path: Path, monkeypatch):
    message = "suuntool not found in configured or fallback paths"

    def raise_missing(*args, **kwargs):
        raise FileNotFoundError(message)

    monkeypatch.setattr(suunto_sync, "SuuntoClient", raise_missing)

    assert sync_suunto_workouts_for_athlete_detailed(
        athlete_id=53603359,
        athlete_label="Jack H",
        cache_dir=tmp_path,
        cfg=_cfg(None),
        config_base=tmp_path,
    ) == (0, message)
