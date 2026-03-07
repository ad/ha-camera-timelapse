"""Image platform for Camera Timelapse — shows the latest captured frame."""
from __future__ import annotations

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
import homeassistant.util.dt as dt_util

from .const import (
    CONF_CAMERAS,
    DOMAIN,
    SIGNAL_CAMERAS_UPDATED,
    SIGNAL_SENSOR_UPDATE,
)
from .coordinator import TimeLapseCoordinator, _camera_slug


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Camera Timelapse image entities from a config entry."""
    coordinator: TimeLapseCoordinator = hass.data[DOMAIN][entry.entry_id]

    cameras: dict = entry.options.get(CONF_CAMERAS, {})
    entities = [
        CameraTimelapseLatestFrameImage(coordinator, entry, camera_id)
        for camera_id in cameras
    ]
    async_add_entities(entities)

    known_ids: set[str] = set(cameras.keys())

    @callback
    def _handle_cameras_updated() -> None:
        nonlocal known_ids
        current_ids = set(entry.options.get(CONF_CAMERAS, {}).keys())
        new_ids = current_ids - known_ids
        if new_ids:
            async_add_entities([
                CameraTimelapseLatestFrameImage(coordinator, entry, cid)
                for cid in new_ids
            ])
        known_ids = current_ids

    entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            SIGNAL_CAMERAS_UPDATED.format(entry.entry_id),
            _handle_cameras_updated,
        )
    )


class CameraTimelapseLatestFrameImage(ImageEntity):
    """Image entity showing the most recently captured frame for a camera."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_content_type = "image/jpeg"
    _attr_translation_key = "latest_frame"

    def __init__(
        self,
        coordinator: TimeLapseCoordinator,
        entry: ConfigEntry,
        camera_id: str,
    ) -> None:
        super().__init__(coordinator.hass)
        self._coordinator = coordinator
        self._entry = entry
        self._camera_id = camera_id
        self._camera_slug = _camera_slug(camera_id)
        self._attr_unique_id = (
            f"{entry.entry_id}_{self._camera_slug}_latest_frame"
        )
        # Seed the timestamp from the file on disk (restored at startup)
        path = coordinator.get_latest_frame_path(camera_id)
        if path and path.exists():
            self._attr_image_last_updated = dt_util.utc_from_timestamp(
                path.stat().st_mtime
            )

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"{self._entry.entry_id}_{self._camera_slug}")},
            "name": f"Timelapse {self._camera_slug}",
            "manufacturer": "Camera Timelapse",
            "entry_type": "service",
        }

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_SENSOR_UPDATE.format(self._entry.entry_id, self._camera_id),
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        path = self._coordinator.get_latest_frame_path(self._camera_id)
        if path:
            self._attr_image_last_updated = dt_util.utcnow()
        self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        path = self._coordinator.get_latest_frame_path(self._camera_id)
        if path is None or not path.exists():
            return None
        return await self.hass.async_add_executor_job(path.read_bytes)
