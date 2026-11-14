"""Tests for Luma's developer access (custom_components/luma/dev_access.py), off by default."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.luma import dev_keys as keys
from custom_components.luma import dev_tv as tv
from custom_components.luma.const import DEV_TOKEN_HEADER, DOMAIN
from custom_components.luma.dev_access import (
    ACTION_APPROVE,
    ACTION_DENY,
    BAD_TOKENS_PER_HOUR,
    DATA,
    MAX_TTL,
    NOTIFICATION_EVENT,
)

HOST = "192.168.1.50"
TV = f"http://{HOST}:5862"
DEVICE = "0123456789abcdef0123456789abcdef"
TOKEN = "dev-token-for-tests"
OTHER = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOwner owner@desktop"
REQUEST = "/api/luma/dev/request"

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


def test_tokens_and_key_pairs():
    assert keys.token_matches(TOKEN, keys.token_hash(TOKEN))
    assert not keys.token_matches("nope", keys.token_hash(TOKEN))
    assert not keys.token_matches(None, keys.token_hash(TOKEN))
    pair = keys.generate_key()
    assert "BEGIN OPENSSH PRIVATE KEY" in pair.private_key
    assert keys.public_key_of(pair.private_key) == pair.public_key
    assert keys.manager_line(pair.private_key).startswith(f"{keys.OPTIONS} ssh-ed25519 ")


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


async def _setup(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    *,
    on: bool = True,
    tunnel: str = "tv.example.com",
) -> MockConfigEntry:
    aioclient_mock.get(f"{TV}/api/status", json={"overlay": None, "overlay_ready": True})
    aioclient_mock.put(f"{TV}/api/streaming-keys", json={"motn": True, "tmdb": True})
    data = {"host": HOST, "port": 5862, "token": "f" * 64, "device_id": DEVICE}
    if on:
        pair = await hass.async_add_executor_job(keys.generate_key)
        data |= {
            "dev_access_management_key": pair.private_key,
            "dev_access_token_hash": keys.token_hash(TOKEN),
        }
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DEVICE,
        title="Living room TV",
        data=data,
        options={
            "motn_api_key": "motn-key-x",
            "tmdb_read_access_token": "tmdb-y",
            "dev_access": on,
            "dev_access_phone": "mobile_app_phone",
            "dev_access_ssh_port": 22,
            "dev_access_tunnel_hostname": tunnel,
            "dev_access_cloudflare_client_id": "cf-id" if tunnel else "",
            "dev_access_cloudflare_client_secret": "cf-secret" if tunnel else "",
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


def _action(call, title: str) -> str:
    return next(a["action"] for a in call.data["data"]["actions"] if a["title"] == title)


async def test_off_by_default_answers_not_found(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock, notify, fake_tv
):
    await _setup(hass, aioclient_mock, on=False)
    assert not hass.data.get(DATA) or not hass.data[DATA].brokers
    fake_tv["sweep"].assert_not_called()
    client = await hass_client_no_auth()
    resp = await client.post(REQUEST, headers={DEV_TOKEN_HEADER: TOKEN})
    assert resp.status == 404


async def test_options_turn_it_on_and_show_the_token_once(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock, notify, fake_tv
):
    entry = await _setup(hass, aioclient_mock, on=False)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "developer_access"}
    )
    assert result["step_id"] == "developer_access"
    form = {"dev_access": True, "dev_access_ssh_port": 22}
    result = await hass.config_entries.options.async_configure(result["flow_id"], form)
    assert result["errors"] == {"dev_access_phone": "phone_required"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {**form, "dev_access_phone": "mobile_app_phone", "dev_access_cloudflare_client_id": "x"},
    )
    assert result["errors"] == {"base": "cloudflare_pair"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**form, "dev_access_phone": "mobile_app_phone"}
    )
    assert result["step_id"] == "developer_token"
    token = result["description_placeholders"]["token"]
    line = result["description_placeholders"]["authorized_keys_line"]
    assert line.endswith(" luma-dev-manager")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.data["dev_access_token_hash"] == keys.token_hash(token)
    assert token not in str(entry.data) and token not in str(entry.options)
    assert entry.entry_id in hass.data[DATA].brokers

    client = await hass_client_no_auth()
    resp = await client.post(REQUEST, json={}, headers={DEV_TOKEN_HEADER: token})
    assert resp.status == 202

    # Turning it off stops it; the endpoints then answer 404 again.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "developer_access"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"dev_access": False, "dev_access_ssh_port": 22}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    resp = await client.post(REQUEST, json={}, headers={DEV_TOKEN_HEADER: token})
    assert resp.status == 404


async def test_approved_request_hands_out_once_then_releases(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock, notify, fake_tv
):
    await _setup(hass, aioclient_mock)
    client = await hass_client_no_auth()
    headers = {DEV_TOKEN_HEADER: TOKEN}

    resp = await client.post(REQUEST, json={"reason": "test search"})
    assert resp.status == 401

    resp = await client.post(
        REQUEST, json={"reason": "test\nsearch", "ttl": 99999}, headers=headers
    )
    assert resp.status == 202
    body = await resp.json()
    request_id = body["request_id"]
    assert body["ttl"] == MAX_TTL
    await hass.async_block_till_done()
    ask = notify[-1]
    assert ask.data["message"].startswith("test search")
    assert "Living room TV" in ask.data["title"]
    approve = _action(ask, "Approve")
    assert request_id not in approve  # only the phone learns the nonce

    resp = await client.get(f"/api/luma/dev/result/{request_id}", headers=headers)
    assert resp.status == 202 and (await resp.json())["state"] == "pending"

    hass.bus.async_fire(NOTIFICATION_EVENT, {"action": ACTION_APPROVE + "guess"})
    await hass.async_block_till_done()
    fake_tv["install"].assert_not_called()

    waiting = asyncio.ensure_future(
        client.get(f"/api/luma/dev/result/{request_id}?wait=10", headers=headers)
    )
    await asyncio.sleep(0.05)
    hass.bus.async_fire(NOTIFICATION_EVENT, {"action": approve})
    resp = await waiting
    assert resp.status == 200
    payload = await resp.json()
    assert payload["state"] == "approved"
    assert payload["tv"] == "Living room TV"
    assert "BEGIN OPENSSH PRIVATE KEY" in payload["ssh"]["private_key"]
    assert payload["ssh"]["hostname"] == "tv.example.com" and payload["ssh"]["tunnel"]
    assert payload["cloudflare"] == {"client_id": "cf-id", "client_secret": "cf-secret"}
    assert payload["streaming"] == {"motn": "motn-key-x", "tmdb": "tmdb-y"}
    generated, installed_id, _expires = fake_tv["install"].call_args.args
    assert installed_id == request_id
    assert generated.private_key == payload["ssh"]["private_key"]

    resp = await client.get(f"/api/luma/dev/result/{request_id}", headers=headers)
    assert resp.status == 410 and (await resp.json())["error"] == "already_delivered"

    resp = await client.post(f"/api/luma/dev/release/{request_id}", headers=headers)
    assert resp.status == 200
    fake_tv["remove"].assert_called_with({request_id})


async def test_without_a_tunnel_sessions_connect_directly(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock, notify, fake_tv
):
    await _setup(hass, aioclient_mock, tunnel="")
    client = await hass_client_no_auth()
    headers = {DEV_TOKEN_HEADER: TOKEN}
    resp = await client.post(REQUEST, json={}, headers=headers)
    request_id = (await resp.json())["request_id"]
    await hass.async_block_till_done()
    hass.bus.async_fire(NOTIFICATION_EVENT, {"action": _action(notify[-1], "Approve")})
    resp = await client.get(f"/api/luma/dev/result/{request_id}?wait=5", headers=headers)
    payload = await resp.json()
    assert payload["ssh"]["hostname"] == HOST and payload["ssh"]["port"] == 22
    assert payload["ssh"]["tunnel"] is False and payload["cloudflare"] is None


async def test_denied_request(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock, notify, fake_tv
):
    await _setup(hass, aioclient_mock)
    client = await hass_client_no_auth()
    headers = {DEV_TOKEN_HEADER: TOKEN}
    resp = await client.post(REQUEST, json={}, headers=headers)
    request_id = (await resp.json())["request_id"]
    await hass.async_block_till_done()
    deny = _action(notify[-1], "Deny")
    assert deny.startswith(ACTION_DENY)
    hass.bus.async_fire(NOTIFICATION_EVENT, {"action": deny})
    await hass.async_block_till_done()
    resp = await client.get(f"/api/luma/dev/result/{request_id}", headers=headers)
    assert resp.status == 403
    fake_tv["install"].assert_not_called()
    assert notify[-1].data["message"] == "clear_notification"


async def test_tv_failure_is_reported(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock, notify, fake_tv
):
    fake_tv["install"].side_effect = tv.TvError("no route")
    await _setup(hass, aioclient_mock)
    client = await hass_client_no_auth()
    headers = {DEV_TOKEN_HEADER: TOKEN}
    resp = await client.post(REQUEST, json={}, headers=headers)
    request_id = (await resp.json())["request_id"]
    await hass.async_block_till_done()
    hass.bus.async_fire(NOTIFICATION_EVENT, {"action": _action(notify[-1], "Approve")})
    resp = await client.get(f"/api/luma/dev/result/{request_id}?wait=5", headers=headers)
    assert resp.status == 502 and (await resp.json())["error"] == "tv_unreachable"


async def test_wrong_tokens_lock_the_endpoints(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock, notify, fake_tv
):
    await _setup(hass, aioclient_mock)
    client = await hass_client_no_auth()
    for _ in range(BAD_TOKENS_PER_HOUR):
        resp = await client.post(REQUEST, headers={DEV_TOKEN_HEADER: "wrong"})
        assert resp.status == 401
    resp = await client.post(REQUEST, headers={DEV_TOKEN_HEADER: TOKEN})
    assert resp.status == 429
    await hass.async_block_till_done()
    assert notify[-1].data["title"] == "Luma developer access locked"


async def test_rate_limits(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock, notify, fake_tv
):
    await _setup(hass, aioclient_mock)
    client = await hass_client_no_auth()
    headers = {DEV_TOKEN_HEADER: TOKEN}
    statuses = []
    for _ in range(3):
        resp = await client.post(REQUEST, json={}, headers=headers)
        statuses.append(resp.status)
    assert statuses == [202, 202, 429]  # two may wait for an answer at once


async def test_startup_sweep_schedules_removal_of_keys_left_on_tv(
    hass: HomeAssistant, aioclient_mock, notify, fake_tv
):
    fake_tv["sweep"].return_value = {"leftover": int(time.time()) + 3600}
    entry = await _setup(hass, aioclient_mock)
    fake_tv["sweep"].assert_called()
    broker = hass.data[DATA].brokers[entry.entry_id]
    assert "leftover" in broker._key_timers
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert broker._key_timers == {}
    assert entry.entry_id not in hass.data[DATA].brokers


async def test_diagnostics_redact_developer_access_secrets(
    hass: HomeAssistant, aioclient_mock, notify, fake_tv
):
    from custom_components.luma.diagnostics import async_get_config_entry_diagnostics

    entry = await _setup(hass, aioclient_mock)
    aioclient_mock.get(f"{TV}/api/diagnostics", json={})
    result = await async_get_config_entry_diagnostics(hass, entry)
    text = str(result)
    assert "BEGIN OPENSSH" not in text and keys.token_hash(TOKEN) not in text
    assert "cf-secret" not in text
