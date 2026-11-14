"""Luma dev access: hand cloud development sessions a short-lived TV SSH key, after the owner
approves each request on their phone. See ha-dev/readme.md in the Luma repository."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .broker import Broker
from .const import DOMAIN
from .views import VIEWS

_VIEWS_REGISTERED = f"{DOMAIN}_views"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    broker = Broker(hass, entry)
    await broker.async_start()
    hass.data[DOMAIN] = broker
    # Views cannot be unregistered; they find the current broker in hass.data.
    if not hass.data.get(_VIEWS_REGISTERED):
        for view in VIEWS:
            hass.http.register_view(view())
        hass.data[_VIEWS_REGISTERED] = True
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    broker: Broker | None = hass.data.pop(DOMAIN, None)
    if broker is not None:
        await broker.async_stop()
    return True


async def _options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
