"""Tests for ha-dev/custom_components/luma_dev_access (the maintainer's dev access broker)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from custom_components.luma_dev_access import keys, tv
from custom_components.luma_dev_access.const import (
    ACTION_APPROVE,
    ACTION_DENY,
    BAD_TOKENS_PER_HOUR,
    DOMAIN,
    MAX_TTL,
    NOTIFICATION_EVENT,
    TOKEN_HEADER,
)
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

TOKEN = "dev-token-for-tests"
OTHER = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOwner owner@desktop"

# ---- keys (no Home Assistant) ------------------------------------------------------------


def test_rewrite_keeps_other_lines_and_drops_removed_and_expired():
    keep = keys.key_line("ssh-ed25519 AAAAkeep", "keep", 2000)
    old = keys.key_line("ssh-ed25519 AAAAold", "old", 1000)
    gone = keys.key_line("ssh-ed25519 AAAAgone", "gone", 5000)
    content = f"{OTHER}\n# comment\n{old}\n{keep}\n{gone}\n"
    new, remaining = keys.rewrite(content, remove={"gone"}, now=1500)
    assert new == f"{OTHER}\n# comment\n{keep}\n"
    assert remaining == {"keep": 2000}


def test_rewrite_adds_only_tagged_lines():
    line = keys.key_line("ssh-ed25519 AAAAnew comment", "abc_-1", 3000)
    assert line == f"{keys.OPTIONS} ssh-ed25519 AAAAnew luma-dev:abc_-1:3000"
    new, remaining = keys.rewrite(OTHER, add=line)
    assert new == f"{OTHER}\n{line}\n"
    assert remaining == {"abc_-1": 3000}
    with pytest.raises(ValueError):
        keys.rewrite(OTHER, add=OTHER)
    with pytest.raises(ValueError):
        keys.key_line("ssh-ed25519 AAAA", "bad id;rm", 1)


def test_untagged_lines_mentioning_the_tag_are_kept():
    tricky = "ssh-ed25519 AAAAx luma-dev:fake:1 trailing"
    new, remaining = keys.rewrite(f"{tricky}\n", now=10**10)
    assert new == f"{tricky}\n" and remaining == {}


def test_tokens():
    assert keys.token_matches(TOKEN, keys.token_hash(TOKEN))
    assert not keys.token_matches("nope", keys.token_hash(TOKEN))
    assert not keys.token_matches(None, keys.token_hash(TOKEN))
    assert len(keys.new_token()) >= 43


# ---- integration -------------------------------------------------------------------------


@pytest.fixture
def notify(hass: HomeAssistant):
    return async_mock_service(hass, "notify", "mobile_app_phone")


@pytest.fixture
def fake_tv():
    with (
        patch.object(tv.Tv, "install", AsyncMock()) as install,
        patch.object(tv.Tv, "remove", AsyncMock(return_value={})) as remove,
        patch.object(tv.Tv, "sweep", AsyncMock(return_value={})) as sweep,
    ):
        yield {"install": install, "remove": remove, "sweep": sweep}


async def _setup(hass: HomeAssistant, cloudflare: bool = True) -> MockConfigEntry:
    MockConfigEntry(
        domain="luma",
        data={"host": "192.0.2.5"},
        options={"motn_api_key": "motn-key-x", "tmdb_read_access_token": "tmdb-y"},
    ).add_to_hass(hass)
    management = await hass.async_add_executor_job(tv.generate_key, "test")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"management_key": management.private_key, "token_hash": keys.token_hash(TOKEN)},
        options={
            "tv_host": "192.0.2.5",
            "tv_port": 22,
            "tunnel_hostname": "tv.example.com",
            "notify_service": "notify.mobile_app_phone",
            "cloudflare_client_id": "cf-id" if cloudflare else "",
            "cloudflare_client_secret": "cf-secret" if cloudflare else "",
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _action(call, title: str) -> str:
    return next(a["action"] for a in call.data["data"]["actions"] if a["title"] == title)


async def test_config_flow_shows_token_once(hass: HomeAssistant, notify):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    form = {
        "tv_host": " 192.0.2.5 ",
        "tv_port": 22,
        "tunnel_hostname": "tv.example.com",
        "notify_service": "notify.mobile_app_phone",
    }
    bad = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**form, "cloudflare_client_id": "only-id"}
    )
    assert bad["errors"] == {"base": "cloudflare_pair"}
    finish = await hass.config_entries.flow.async_configure(result["flow_id"], form)
    assert finish["step_id"] == "finish"
    token = finish["description_placeholders"]["token"]
    line = finish["description_placeholders"]["authorized_keys_line"]
    assert line.startswith(f"{keys.OPTIONS} ssh-ed25519 ") and line.endswith(" luma-dev-manager")
    with patch("custom_components.luma_dev_access.async_setup_entry", return_value=True):
        done = await hass.config_entries.flow.async_configure(finish["flow_id"], {})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert done["options"]["tv_host"] == "192.0.2.5"
    assert done["data"]["token_hash"] == keys.token_hash(token)
    assert "BEGIN OPENSSH PRIVATE KEY" in done["data"]["management_key"]
    assert token not in str(done["data"])


async def test_approved_request_hands_out_once_then_releases(
    hass: HomeAssistant, hass_client_no_auth, notify, fake_tv
):
    await _setup(hass)
    client = await hass_client_no_auth()
    headers = {TOKEN_HEADER: TOKEN}

    resp = await client.post("/api/luma_dev/request", json={"reason": "test search"})
    assert resp.status == 401

    resp = await client.post(
        "/api/luma_dev/request", json={"reason": "test\nsearch", "ttl": 99999}, headers=headers
    )
    assert resp.status == 202
    body = await resp.json()
    request_id = body["request_id"]
    assert body["ttl"] == MAX_TTL
    await hass.async_block_till_done()
    ask = notify[-1]
    assert ask.data["message"].startswith("test search")
    approve = _action(ask, "Approve")
    assert request_id not in approve  # only the phone learns the nonce

    resp = await client.get(f"/api/luma_dev/result/{request_id}", headers=headers)
    assert resp.status == 202 and (await resp.json())["state"] == "pending"

    # A wrong nonce approves nothing.
    hass.bus.async_fire(NOTIFICATION_EVENT, {"action": ACTION_APPROVE + "guess"})
    await hass.async_block_till_done()
    fake_tv["install"].assert_not_called()

    waiting = asyncio.ensure_future(
        client.get(f"/api/luma_dev/result/{request_id}?wait=10", headers=headers)
    )
    await asyncio.sleep(0.05)
    hass.bus.async_fire(NOTIFICATION_EVENT, {"action": approve})
    resp = await waiting
    assert resp.status == 200
    payload = await resp.json()
    assert payload["state"] == "approved"
    assert "BEGIN OPENSSH PRIVATE KEY" in payload["ssh"]["private_key"]
    assert payload["ssh"]["hostname"] == "tv.example.com"
    assert payload["cloudflare"] == {"client_id": "cf-id", "client_secret": "cf-secret"}
    assert payload["streaming"] == {"motn": "motn-key-x", "tmdb": "tmdb-y"}
    generated, installed_id, _expires = fake_tv["install"].call_args.args
    assert installed_id == request_id
    assert generated.private_key == payload["ssh"]["private_key"]

    resp = await client.get(f"/api/luma_dev/result/{request_id}", headers=headers)
    assert resp.status == 410 and (await resp.json())["error"] == "already_delivered"

    resp = await client.post(f"/api/luma_dev/release/{request_id}", headers=headers)
    assert resp.status == 200
    fake_tv["remove"].assert_called_with({request_id})


async def test_denied_request(hass: HomeAssistant, hass_client_no_auth, notify, fake_tv):
    await _setup(hass, cloudflare=False)
    client = await hass_client_no_auth()
    headers = {TOKEN_HEADER: TOKEN}
    resp = await client.post("/api/luma_dev/request", json={}, headers=headers)
    request_id = (await resp.json())["request_id"]
    await hass.async_block_till_done()
    deny = _action(notify[-1], "Deny")
    assert deny.startswith(ACTION_DENY)
    hass.bus.async_fire(NOTIFICATION_EVENT, {"action": deny})
    await hass.async_block_till_done()
    resp = await client.get(f"/api/luma_dev/result/{request_id}", headers=headers)
    assert resp.status == 403
    fake_tv["install"].assert_not_called()
    assert notify[-1].data["message"] == "clear_notification"


async def test_tv_failure_is_reported(hass: HomeAssistant, hass_client_no_auth, notify, fake_tv):
    fake_tv["install"].side_effect = tv.TvError("no route")
    await _setup(hass)
    client = await hass_client_no_auth()
    headers = {TOKEN_HEADER: TOKEN}
    resp = await client.post("/api/luma_dev/request", json={}, headers=headers)
    request_id = (await resp.json())["request_id"]
    await hass.async_block_till_done()
    hass.bus.async_fire(NOTIFICATION_EVENT, {"action": _action(notify[-1], "Approve")})
    resp = await client.get(f"/api/luma_dev/result/{request_id}?wait=5", headers=headers)
    assert resp.status == 502 and (await resp.json())["error"] == "tv_unreachable"


async def test_wrong_tokens_lock_the_endpoints(
    hass: HomeAssistant, hass_client_no_auth, notify, fake_tv
):
    await _setup(hass)
    client = await hass_client_no_auth()
    for _ in range(BAD_TOKENS_PER_HOUR):
        resp = await client.post("/api/luma_dev/request", headers={TOKEN_HEADER: "wrong"})
        assert resp.status == 401
    resp = await client.post("/api/luma_dev/request", headers={TOKEN_HEADER: TOKEN})
    assert resp.status == 429
    await hass.async_block_till_done()
    assert notify[-1].data["title"] == "Luma dev access locked"


async def test_rate_limits(hass: HomeAssistant, hass_client_no_auth, notify, fake_tv):
    await _setup(hass)
    client = await hass_client_no_auth()
    headers = {TOKEN_HEADER: TOKEN}
    statuses = []
    for _ in range(3):
        resp = await client.post("/api/luma_dev/request", json={}, headers=headers)
        statuses.append(resp.status)
    assert statuses == [202, 202, 429]  # two may wait for an answer at once


async def test_startup_sweep_schedules_removal_of_keys_left_on_tv(
    hass: HomeAssistant, notify, fake_tv
):
    import time

    fake_tv["sweep"].return_value = {"leftover": int(time.time()) + 3600}
    entry = await _setup(hass)
    fake_tv["sweep"].assert_called()
    broker = hass.data[DOMAIN]
    assert "leftover" in broker._key_timers
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert broker._key_timers == {}
