"""Button platform for Camera Timelapse — per-camera action buttons."""
from __future__ import annotations

import homeassistant.util.dt as dt_util
from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_CAMERAS,
    CONF_MODE,
    DEFAULT_MODE,
    DOMAIN,
    MODE_BOTH,
    MODE_DAILY,
    MODE_ROLLING,
    SIGNAL_CAMERAS_UPDATED,
)
from .coordinator import TimeLapseCoordinator, _camera_slug

_BUTTON_TYPES = ("capture_frame", "generate_timelapse", "cleanup_frames")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Camera Timelapse button entities from a config entry."""
    coordinator: TimeLapseCoordinator = hass.data[DOMAIN][entry.entry_id]

    cameras: dict = entry.options.get(CONF_CAMERAS, {})
    async_add_entities([
        CameraTimelapseButton(coordinator, entry, camera_id, btn_type)
        for camera_id in cameras
        for btn_type in _BUTTON_TYPES
    ])

    known_ids: set[str] = set(cameras.keys())

    @callback
    def _handle_cameras_updated() -> None:
        nonlocal known_ids
        current_ids = set(entry.options.get(CONF_CAMERAS, {}).keys())
        new_ids = current_ids - known_ids
        if new_ids:
            async_add_entities([
                CameraTimelapseButton(coordinator, entry, cid, btn_type)
                for cid in new_ids
                for btn_type in _BUTTON_TYPES
            ])
        known_ids = current_ids

    entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            SIGNAL_CAMERAS_UPDATED.format(entry.entry_id),
            _handle_cameras_updated,
        )
    )


class CameraTimelapseButton(ButtonEntity):
    """A button that triggers a camera timelapse action."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: TimeLapseCoordinator,
        entry: ConfigEntry,
        camera_id: str,
        button_type: str,
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._camera_id = camera_id
        self._camera_slug = _camera_slug(camera_id)
        self._button_type = button_type
        self._attr_translation_key = button_type
        self._attr_unique_id = (
            f"{entry.entry_id}_{self._camera_slug}_{button_type}"
        )

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"{self._entry.entry_id}_{self._camera_slug}")},
            "name": f"Timelapse {self._camera_slug}",
            "manufacturer": "Camera Timelapse",
            "entry_type": "service",
        }

    async def async_press(self) -> None:
        if self._button_type == "capture_frame":
            await self._coordinator.async_capture_frame(
                self._camera_id, bypass_window=True
            )
        elif self._button_type == "generate_timelapse":
            cameras = self._entry.options.get(CONF_CAMERAS, {})
            config = cameras.get(self._camera_id, {})
            mode = config.get(CONF_MODE, DEFAULT_MODE)
            today = dt_util.now().date()
            if mode in (MODE_DAILY, MODE_BOTH):
                await self._coordinator.async_assemble_daily(self._camera_id, today)
            if mode in (MODE_ROLLING, MODE_BOTH):
                await self._coordinator.async_assemble_rolling(self._camera_id)
        elif self._button_type == "cleanup_frames":
            await self._coordinator.async_cleanup_camera(self._camera_id)
