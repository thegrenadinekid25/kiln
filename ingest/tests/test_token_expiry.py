"""Earthdata token expiry check. No network, no clock: `now` is a parameter."""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from kiln_ingest.cli import (
    TOKEN_RENEWAL_WINDOW,
    check_earthdata_token,
    token_expiry,
)

NOW = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)


def encode_segment(payload: dict) -> str:
    """Base64url with the padding stripped, exactly as a real JWT carries it."""
    raw = json.dumps(payload).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def jwt(payload: dict) -> str:
    return f"{encode_segment({'alg': 'RS256'})}.{encode_segment(payload)}.signature"


def token_expiring_in(delta: timedelta) -> str:
    return jwt({"exp": (NOW + delta).timestamp(), "uid": "kiln"})


# --- reading the claim --------------------------------------------------------------


def test_the_expiry_claim_is_read_back_as_a_utc_datetime():
    expires = token_expiry(token_expiring_in(timedelta(days=30)))
    assert expires == NOW + timedelta(days=30)
    assert expires.tzinfo is timezone.utc


def test_an_integer_expiry_is_read_the_same_as_a_float():
    payload = {"exp": int((NOW + timedelta(days=5)).timestamp())}
    assert token_expiry(jwt(payload)) == NOW + timedelta(days=5)


@pytest.mark.parametrize("token,reason", [
    ("", "empty"),
    ("token", "not a JWT at all"),
    ("header.payload", "two segments"),
    ("a.b.c.d", "four segments"),
    ("header.!!!not-base64!!!.sig", "undecodable segment"),
    (f"header.{base64.urlsafe_b64encode(b'not json').decode().rstrip('=')}.sig", "not JSON"),
])
def test_a_token_that_is_not_a_readable_jwt_yields_no_expiry(token, reason):
    assert token_expiry(token) is None, reason


def test_a_payload_without_an_exp_claim_yields_no_expiry():
    assert token_expiry(jwt({"uid": "kiln"})) is None


def test_a_non_numeric_exp_claim_yields_no_expiry():
    assert token_expiry(jwt({"exp": "next tuesday"})) is None


def test_an_absurd_exp_claim_yields_no_expiry():
    # Far past what datetime can represent; the check must not crash the run.
    assert token_expiry(jwt({"exp": 1e30})) is None


# --- the decision the CLI acts on ---------------------------------------------------


def test_a_comfortable_token_proceeds_without_a_warning(caplog):
    with caplog.at_level(logging.INFO):
        assert check_earthdata_token(token_expiring_in(timedelta(days=60)), NOW)

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert "valid until 2026-10-30" in caplog.text


def test_a_token_inside_the_renewal_window_warns_and_proceeds(caplog):
    with caplog.at_level(logging.INFO):
        assert check_earthdata_token(token_expiring_in(timedelta(days=10)), NOW)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "expires on 2026-09-10" in message
    assert "in 10 days" in message
    assert "urs.earthdata.nasa.gov" in message
    assert "Doppler (kiln/prd)" in message
    assert "GitHub repo secret" in message


def test_the_edge_of_the_renewal_window_warns(caplog):
    with caplog.at_level(logging.INFO):
        check_earthdata_token(token_expiring_in(TOKEN_RENEWAL_WINDOW), NOW)
    assert "expires on" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.INFO):
        check_earthdata_token(
            token_expiring_in(TOKEN_RENEWAL_WINDOW + timedelta(minutes=1)), NOW
        )
    assert "valid until" in caplog.text


def test_an_expired_token_stops_the_run(caplog):
    with caplog.at_level(logging.INFO):
        assert not check_earthdata_token(token_expiring_in(timedelta(days=-1)), NOW)

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    message = errors[0].getMessage()
    assert "expired on 2026-08-30" in message
    assert "urs.earthdata.nasa.gov" in message


def test_a_token_expiring_this_instant_is_treated_as_expired():
    assert not check_earthdata_token(token_expiring_in(timedelta(0)), NOW)


def test_an_unreadable_token_warns_but_never_blocks_the_run(caplog):
    with caplog.at_level(logging.INFO):
        assert check_earthdata_token("an-opaque-token", NOW)

    assert "could not read an expiry date" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_the_check_never_logs_the_token_itself(caplog):
    secret = token_expiring_in(timedelta(days=3))
    with caplog.at_level(logging.INFO):
        check_earthdata_token(secret, NOW)
    assert secret not in caplog.text


# --- the CLI guard ------------------------------------------------------------------


def test_main_refuses_to_start_with_an_expired_token(monkeypatch):
    from kiln_ingest import cli

    monkeypatch.setenv("EARTHDATA_TOKEN", token_expiring_in(timedelta(days=-1)))
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "service-key")

    def unreachable(*args, **kwargs):
        raise AssertionError("the run must stop before any network work")

    monkeypatch.setattr(cli, "run_product", unreachable)

    assert cli.main(["--date", "2026-08-30"]) == 2
