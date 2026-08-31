from pathlib import Path

from suunto_client import SuuntoCfg, resolve_suuntool_binary


def _cfg(path: Path | None) -> SuuntoCfg:
    return SuuntoCfg(
        enabled=True,
        primary=True,
        suuntool_path=path,
        session_file=None,
        indoor_rowing_activity_ids=frozenset({57}),
        gym_activity_ids=frozenset({23}),
    )


def test_resolve_falls_back_when_configured_path_missing(tmp_path: Path):
    missing = tmp_path / "nope" / "suuntool"
    fallback_dir = tmp_path / "bin"
    fallback_dir.mkdir()
    fallback = fallback_dir / "suuntool"
    fallback.write_text("#!/bin/sh\n")
    fallback.chmod(0o755)
    found = resolve_suuntool_binary(_cfg(missing), tmp_path)
    assert found == fallback.resolve()


def test_resolve_raises_when_nothing_exists(tmp_path: Path):
    try:
        resolve_suuntool_binary(_cfg(tmp_path / "missing"), tmp_path)
    except FileNotFoundError as exc:
        assert "suuntool" in str(exc).lower()
    else:
        raise AssertionError("expected FileNotFoundError")
