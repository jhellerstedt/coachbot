"""Strava token files load app credentials without python-dotenv."""

from __future__ import annotations

import os
from unittest.mock import patch

from strava_token_client import get_strava_client


def test_token_dir_env_loads_when_dotenv_is_missing(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "STRAVA_CLIENT_ID=client-id\nSTRAVA_CLIENT_SECRET=client-secret\n"
    )
    (tmp_path / "strava_token").write_text("access-token\n")
    monkeypatch.delenv("STRAVA_CLIENT_ID", raising=False)
    monkeypatch.delenv("STRAVA_CLIENT_SECRET", raising=False)
    monkeypatch.setattr("strava_token_client.load_dotenv", None)
    with patch("strava_token_client.Client") as client:
        get_strava_client(tmp_path)
    assert os.environ["STRAVA_CLIENT_ID"] == "client-id"
    assert os.environ["STRAVA_CLIENT_SECRET"] == "client-secret"
    assert client.call_args.kwargs["access_token"] == "access-token"
