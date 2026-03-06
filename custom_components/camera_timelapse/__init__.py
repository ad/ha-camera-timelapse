"""Camera Timelapse — HACS custom integration."""
from __future__ import annotations

import logging
import os
from datetime import date

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
import homeassistant.helpers.config_validation as cv
import homeassistant.util.dt as dt_util

from .const import (
    CONF_CAMERA_ENTITY_ID,
    CONF_CAMERAS,
    CONF_MODE,
    CONF_STORAGE_PATH,
    DOMAIN,
    MODE_BOTH,
    MODE_DAILY,
    MODE_ROLLING,
    SERVICE_CAPTURE_NOW,
    SERVICE_GENERATE_TIMELAPSE,
)
from .coordinator import TimeLapseCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Camera Timelapse from a config entry."""
    storage_path: str = entry.data[CONF_STORAGE_PATH]

    # Validate that storage path is writable (non-blocking check via executor)
    def _check_path() -> bool:
        os.makedirs(storage_path, exist_ok=True)
        return os.access(storage_path, os.W_OK)

    writable = await hass.async_add_executor_job(_check_path)
    if not writable:
        raise ConfigEntryNotReady(
            f"Storage path '{storage_path}' is not writable. "
            "Check directory permissions and HA media configuration."
        )

    coordinator = TimeLapseCoordinator(hass, entry)
    await coordinator.async_setup()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])

    # --- Service: capture_now ---
    async def handle_capture_now(call: ServiceCall) -> None:
        camera_id: str | None = call.data.get(CONF_CAMERA_ENTITY_ID)
        if camera_id:
            await coordinator.async_capture_frame(camera_id, bypass_window=True)
        else:
            cameras = entry.options.get(CONF_CAMERAS, {})
            for cid in cameras:
                await coordinator.async_capture_frame(cid, bypass_window=True)

    # --- Service: generate_timelapse ---
    async def handle_generate_timelapse(call: ServiceCall) -> None:
        camera_id: str = call.data[CONF_CAMERA_ENTITY_ID]
        mode: str = call.data.get(CONF_MODE, MODE_DAILY)
        date_str: str | None = call.data.get("date")

        if date_str:
            try:
                target_date = date.fromisoformat(date_str)
            except ValueError:
                _LOGGER.error("generate_timelapse: invalid date '%s'", date_str)
                return
        else:
            target_date = dt_util.now().date()

        if mode in (MODE_DAILY, MODE_BOTH):
            await coordinator.async_assemble_daily(camera_id, target_date)
        if mode in (MODE_ROLLING, MODE_BOTH):
            await coordinator.async_assemble_rolling(camera_id)

    if not hass.services.has_service(DOMAIN, SERVICE_CAPTURE_NOW):
        hass.services.async_register(
            DOMAIN,
            SERVICE_CAPTURE_NOW,
            handle_capture_now,
            schema=vol.Schema(
                {
                    vol.Optional(CONF_CAMERA_ENTITY_ID): cv.entity_id,
                }
            ),
        )

    if not hass.services.has_service(DOMAIN, SERVICE_GENERATE_TIMELAPSE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_GENERATE_TIMELAPSE,
            handle_generate_timelapse,
            schema=vol.Schema(
                {
                    vol.Required(CONF_CAMERA_ENTITY_ID): cv.entity_id,
                    vol.Optional(CONF_MODE, default=MODE_DAILY): vol.In(
                        [MODE_DAILY, MODE_ROLLING, MODE_BOTH]
                    ),
                    vol.Optional("date"): cv.string,
                }
            ),
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, ["sensor"]):
        return False

    coordinator: TimeLapseCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
    await coordinator.async_unload()

    # Remove services only if this is the last entry
    if not hass.data[DOMAIN]:
        hass.services.async_remove(DOMAIN, SERVICE_CAPTURE_NOW)
        hass.services.async_remove(DOMAIN, SERVICE_GENERATE_TIMELAPSE)

    return True
