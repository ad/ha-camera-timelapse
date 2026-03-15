"""Coordinator for Camera Timelapse: scheduling, capture, assembly, and cleanup."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_point_in_time, async_track_time_interval
from homeassistant.helpers.storage import Store
import homeassistant.util.dt as dt_util

_STORAGE_VERSION = 1

from .const import (
    CONF_ASSEMBLY_INTERVAL_MINUTES,
    CONF_CAMERAS,
    CONF_FPS,
    CONF_FRAME_HEIGHT,
    CONF_FRAME_QUALITY,
    CONF_FRAME_WIDTH,
    CONF_HDR_FRAMES,
    CONF_INTERVAL_MINUTES,
    CONF_KEEP_FRAMES,
    CONF_OVERLAY_FONT_SIZE,
    CONF_OVERLAY_LINE_SPACING,
    CONF_OVERLAY_MARGIN,
    CONF_OVERLAY_POSITION,
    CONF_OVERLAY_SENSORS,
    CONF_OVERLAY_STROKE_COLOR,
    CONF_OVERLAY_STROKE_WIDTH,
    CONF_OVERLAY_TEXT_COLOR,
    CONF_PLACEHOLDER_IMAGE,
    CONF_STABILIZATION,
    DEFAULT_OVERLAY_FONT_SIZE,
    DEFAULT_OVERLAY_LINE_SPACING,
    DEFAULT_OVERLAY_MARGIN,
    DEFAULT_OVERLAY_POSITION,
    DEFAULT_OVERLAY_STROKE_COLOR,
    DEFAULT_OVERLAY_STROKE_WIDTH,
    DEFAULT_OVERLAY_TEXT_COLOR,
    DEFAULT_PLACEHOLDER_IMAGE,
    DEFAULT_STABILIZATION,
    CONF_MAX_RETENTION_DAYS,
    CONF_MODE,
    CONF_OUTPUT_FORMAT,
    CONF_ROLLING_PERIOD_DAYS,
    CONF_STORAGE_PATH,
    CONF_TIME_END,
    CONF_TIME_RANGE_TYPE,
    CONF_TIME_START,
    DEFAULT_FRAME_HEIGHT,
    DEFAULT_FRAME_QUALITY,
    DEFAULT_FRAME_WIDTH,
    DEFAULT_HDR_FRAMES,
    DEFAULT_KEEP_FRAMES,
    DOMAIN,
    EVENT_TIMELAPSE_READY,
    FORMAT_APNG,
    FORMAT_GIF,
    FORMAT_MP4,
    MODE_BOTH,
    MODE_DAILY,
    MODE_ROLLING,
    ROLLING_DEBOUNCE_SECONDS,
    SIGNAL_CAMERAS_UPDATED,
    SIGNAL_SENSOR_UPDATE,
    TIME_RANGE_ALWAYS,
    TIME_RANGE_CUSTOM,
    TIME_RANGE_SUNRISE_SUNSET,
)

_LOGGER = logging.getLogger(__name__)


class TimeLapseCoordinator:
    """Manages all camera timelapse scheduling, capture, and assembly."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.storage_path: str = entry.data[CONF_STORAGE_PATH]
        # Unsub callables keyed by camera entity_id
        self._capture_unsubs: dict[str, Callable] = {}
        self._assembly_unsubs: dict[str, Callable] = {}
        self._periodic_assembly_unsubs: dict[str, Callable] = {}
        # Last rolling assembly timestamp per camera (for debounce)
        self._last_rolling: dict[str, datetime] = {}
        # Serialise write operations per camera
        self._write_locks: dict[str, asyncio.Lock] = {}
        # Track last cleanup date per camera (to run cleanup once per day for rolling mode)
        self._last_cleanup_date: dict[str, date] = {}
        # Sensor state
        self._last_timelapse_info: dict[str, dict] = {}
        self._frame_counts: dict[str, int] = {}
        self._frame_count_dates: dict[str, date] = {}
        self._disk_usage_mb: dict[str, float] = {}
        # Persistent store for last periodic assembly times
        self._store: Store | None = None
        self._last_periodic_assembly: dict[str, datetime] = {}
        # Path of the most recently captured frame per camera (for image entity)
        self._latest_frame_path: dict[str, Path] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Read options and schedule capture/assembly tasks for each camera."""
        # Load persistent state
        self._store = Store(
            self.hass, _STORAGE_VERSION,
            f"{DOMAIN}.{self.entry.entry_id}.periodic_assembly",
        )
        stored: dict = await self._store.async_load() or {}
        for cam_id, ts in stored.items():
            try:
                self._last_periodic_assembly[cam_id] = datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                pass

        # Create default placeholder background if not yet present
        await self.hass.async_add_executor_job(
            _create_default_placeholder, self.storage_path
        )

        cameras: dict = self.entry.options.get(CONF_CAMERAS, {})
        for camera_id, cam_config in cameras.items():
            await self._schedule_camera(camera_id, cam_config)

        # Re-schedule when options change (user edits via OptionsFlow)
        self.entry.async_on_unload(
            self.entry.add_update_listener(self._handle_options_update)
        )

        # Restore frames_today counters from disk
        await self._restore_frame_counts()

        # Startup recovery: assemble yesterday's missing daily timelapses
        await self._recover_missing_daily()

        # Run cleanup for all cameras — catches any missed cleanup windows
        # (e.g. HA was restarted before the daily assembly trigger fired).
        # async_cleanup_camera is idempotent and safe to call at any time.
        for camera_id in cameras:
            await self.async_cleanup_camera(camera_id)

    async def async_unload(self) -> None:
        """Cancel all scheduled tasks."""
        for unsub in list(self._capture_unsubs.values()):
            unsub()
        for unsub in list(self._assembly_unsubs.values()):
            unsub()
        for unsub in list(self._periodic_assembly_unsubs.values()):
            unsub()
        self._capture_unsubs.clear()
        self._assembly_unsubs.clear()
        self._periodic_assembly_unsubs.clear()

    async def _handle_options_update(
        self, hass: HomeAssistant, entry: ConfigEntry
    ) -> None:
        """Called when OptionsFlow saves new data. Re-schedule affected cameras."""
        new_cameras: dict = entry.options.get(CONF_CAMERAS, {})
        old_ids = set(self._capture_unsubs.keys())
        new_ids = set(new_cameras.keys())

        # Remove cameras that were deleted
        for camera_id in old_ids - new_ids:
            self._cancel_camera(camera_id)

        # Add or update cameras
        for camera_id, cam_config in new_cameras.items():
            if camera_id in old_ids:
                self._cancel_camera(camera_id)
            await self._schedule_camera(camera_id, cam_config)

        async_dispatcher_send(hass, SIGNAL_CAMERAS_UPDATED.format(entry.entry_id))

    # ------------------------------------------------------------------
    # Scheduling helpers
    # ------------------------------------------------------------------

    async def _schedule_camera(self, camera_id: str, config: dict) -> None:
        """Register capture interval and optional daily assembly trigger."""
        if camera_id not in self._write_locks:
            self._write_locks[camera_id] = asyncio.Lock()

        interval = timedelta(minutes=int(config[CONF_INTERVAL_MINUTES]))

        @callback
        def _capture_cb(now: datetime, cid: str = camera_id) -> None:
            self.hass.async_create_task(self.async_capture_frame(cid))

        self._capture_unsubs[camera_id] = async_track_time_interval(
            self.hass,
            _capture_cb,
            interval,
        )

        # Daily assembly is not needed for pure rolling mode
        mode = config.get(CONF_MODE, MODE_DAILY)
        if mode in (MODE_DAILY, MODE_BOTH):
            await self._schedule_assembly_trigger(camera_id, config)

            # Periodic mid-day assembly if configured
            assembly_interval = int(
                config.get(CONF_ASSEMBLY_INTERVAL_MINUTES, 0)
            )
            if assembly_interval > 0:
                self._schedule_periodic_assembly(camera_id, config, assembly_interval)

    def _cancel_camera(self, camera_id: str) -> None:
        """Cancel capture and assembly tasks for a camera."""
        if camera_id in self._periodic_assembly_unsubs:
            self._periodic_assembly_unsubs.pop(camera_id)()
        if camera_id in self._capture_unsubs:
            self._capture_unsubs.pop(camera_id)()
        if camera_id in self._assembly_unsubs:
            self._assembly_unsubs.pop(camera_id)()

    def _schedule_periodic_assembly(
        self, camera_id: str, config: dict, interval_minutes: int
    ) -> None:
        """Schedule next periodic assembly via one-shot point-in-time trigger.

        Uses the stored last-run timestamp to compute the next fire time, so
        the interval survives HA restarts. If one or more intervals were missed
        while HA was down, a single catch-up run is fired after a short delay
        instead of running multiple times.
        """
        interval = timedelta(minutes=interval_minutes)
        now = dt_util.now()
        last_run = self._last_periodic_assembly.get(camera_id)

        if last_run is None:
            # First time ever — wait one full interval before assembling
            next_run = now + interval
        else:
            next_run = last_run + interval
            if next_run <= now:
                # Missed one or more intervals: fire once soon, then resume normally
                next_run = now + timedelta(seconds=30)

        # Cancel any existing one-shot for this camera
        if camera_id in self._periodic_assembly_unsubs:
            self._periodic_assembly_unsubs.pop(camera_id)()

        @callback
        def _cb(
            _t: datetime,
            cid: str = camera_id,
            cfg: dict = config,
            mins: int = interval_minutes,
        ) -> None:
            self.hass.async_create_task(self._run_periodic_assembly(cid, cfg, mins))

        self._periodic_assembly_unsubs[camera_id] = async_track_point_in_time(
            self.hass, _cb, next_run
        )
        _LOGGER.debug(
            "Next periodic assembly for %s scheduled at %s", camera_id, next_run
        )

    async def _run_periodic_assembly(
        self, camera_id: str, config: dict, interval_minutes: int
    ) -> None:
        """Execute periodic assembly, persist timestamp, schedule next run."""
        now = dt_util.now()
        self._last_periodic_assembly[camera_id] = now
        await self._save_assembly_times()
        await self.async_assemble_daily(camera_id, now.date())
        self._schedule_periodic_assembly(camera_id, config, interval_minutes)

    async def _save_assembly_times(self) -> None:
        """Persist last periodic assembly timestamps to HA storage."""
        if self._store is None:
            return
        await self._store.async_save(
            {k: v.isoformat() for k, v in self._last_periodic_assembly.items()}
        )

    async def _schedule_assembly_trigger(self, camera_id: str, config: dict) -> None:
        """Schedule the daily assembly at end-of-active-window or 23:59."""
        now = dt_util.now()
        time_range_type = config.get(CONF_TIME_RANGE_TYPE, TIME_RANGE_SUNRISE_SUNSET)

        if time_range_type == TIME_RANGE_SUNRISE_SUNSET:
            trigger_time = self._get_sunset_today()
            # If sunset has already passed today, trigger at midnight and reassemble
            if trigger_time is None or trigger_time <= now:
                trigger_time = now.replace(hour=23, minute=59, second=30, microsecond=0)
                if trigger_time <= now:
                    trigger_time = trigger_time + timedelta(days=1)
        elif time_range_type == TIME_RANGE_CUSTOM:
            end_str = config.get(CONF_TIME_END, "19:00:00")
            h, m = int(end_str[:2]), int(end_str[3:5])
            # +30 s so that any capture firing at hh:mm:00 finishes before assembly reads the dir
            trigger_time = now.replace(hour=h, minute=m, second=30, microsecond=0)
            if trigger_time <= now:
                trigger_time = trigger_time + timedelta(days=1)
        else:
            # TIME_RANGE_ALWAYS → assemble at 23:59:30 (+30 s to let the last capture complete)
            trigger_time = now.replace(hour=23, minute=59, second=30, microsecond=0)
            if trigger_time <= now:
                trigger_time = trigger_time + timedelta(days=1)

        @callback
        def _assembly_cb(t: datetime, cid: str = camera_id) -> None:
            self.hass.async_create_task(self._daily_assembly_and_reschedule(cid))

        self._assembly_unsubs[camera_id] = async_track_point_in_time(
            self.hass,
            _assembly_cb,
            trigger_time,
        )

    async def _daily_assembly_and_reschedule(self, camera_id: str) -> None:
        """Assemble today's timelapse, cleanup old files, then schedule next day."""
        cameras = self.entry.options.get(CONF_CAMERAS, {})
        config = cameras.get(camera_id)
        if config is None:
            return

        target_date = dt_util.now().date()
        try:
            await self.async_assemble_daily(camera_id, target_date)
        except Exception:
            _LOGGER.exception("Assembly failed for %s on %s", camera_id, target_date)

        try:
            await self.async_cleanup_camera(camera_id)
        except Exception:
            _LOGGER.exception("Cleanup failed for %s", camera_id)

        await self._schedule_assembly_trigger(camera_id, config)

    # ------------------------------------------------------------------
    # Time window helpers
    # ------------------------------------------------------------------

    def _is_in_active_window(self, camera_id: str) -> bool:
        """Return True if current time is within the configured capture window."""
        cameras = self.entry.options.get(CONF_CAMERAS, {})
        config = cameras.get(camera_id)
        if config is None:
            return False

        time_range_type = config.get(CONF_TIME_RANGE_TYPE, TIME_RANGE_SUNRISE_SUNSET)
        now = dt_util.now()

        if time_range_type == TIME_RANGE_ALWAYS:
            return True

        if time_range_type == TIME_RANGE_SUNRISE_SUNSET:
            today = now.date()
            sunrise = self._get_astral_event(today, "sunrise")
            sunset = self._get_astral_event(today, "sunset")
            if sunrise is None or sunset is None:
                # Polar day/night fallback: always capture
                return True
            return sunrise <= now <= sunset

        if time_range_type == TIME_RANGE_CUSTOM:
            start_str = config.get(CONF_TIME_START, "07:00:00")
            end_str = config.get(CONF_TIME_END, "19:00:00")
            start = now.replace(
                hour=int(start_str[:2]),
                minute=int(start_str[3:5]),
                second=0,
                microsecond=0,
            )
            end = now.replace(
                hour=int(end_str[:2]),
                minute=int(end_str[3:5]),
                second=0,
                microsecond=0,
            )
            return start <= now <= end

        return True

    def _get_astral_event(self, target_date: date, event: str):
        """Return an aware datetime for a solar event, or None."""
        try:
            from homeassistant.helpers.sun import get_astral_event_date
            result = get_astral_event_date(self.hass, event, target_date)
            return result
        except Exception:
            return None

    def _get_sunset_today(self):
        """Return today's sunset as an aware datetime, or None."""
        return self._get_astral_event(dt_util.now().date(), "sunset")

    # ------------------------------------------------------------------
    # Frame capture
    # ------------------------------------------------------------------

    async def async_capture_frame(self, camera_id: str, bypass_window: bool = False) -> None:
        """Capture one frame from a camera entity."""
        if not bypass_window and not self._is_in_active_window(camera_id):
            return

        cameras = self.entry.options.get(CONF_CAMERAS, {})
        config = cameras.get(camera_id, {})
        n_frames = int(config.get(CONF_HDR_FRAMES, DEFAULT_HDR_FRAMES))

        try:
            from homeassistant.components.camera import async_get_image
            if n_frames > 1:
                raw_frames = []
                for i in range(n_frames):
                    if i > 0:
                        await asyncio.sleep(0.5)
                    img = await async_get_image(self.hass, camera_id, timeout=10)
                    raw_frames.append(img.content)
                content = await self.hass.async_add_executor_job(_average_frames, raw_frames)
            else:
                image = await async_get_image(self.hass, camera_id, timeout=10)
                content = image.content
        except Exception as err:
            _LOGGER.warning("Frame capture failed for %s: %s", camera_id, err)
            await self._update_unavailable_frame(camera_id)
            return

        now = dt_util.now()

        # Update frame counter (reset daily)
        today = now.date()
        if self._frame_count_dates.get(camera_id) != today:
            self._frame_counts[camera_id] = 0
            self._frame_count_dates[camera_id] = today
        self._frame_counts[camera_id] = self._frame_counts.get(camera_id, 0) + 1
        async_dispatcher_send(
            self.hass,
            SIGNAL_SENSOR_UPDATE.format(self.entry.entry_id, camera_id),
        )

        camera_slug = _camera_slug(camera_id)
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H%M%S")
        frame_dir = Path(self.storage_path) / "frames" / camera_slug / date_str
        frame_path = frame_dir / f"{time_str}.jpg"

        # Apply sensor overlay if configured (read states in async context)
        overlay_sensors: list[str] = config.get(CONF_OVERLAY_SENSORS, [])
        sensor_lines: list[str] = []
        for eid in overlay_sensors:
            state = self.hass.states.get(eid)
            if state and state.state not in ("unavailable", "unknown"):
                unit = state.attributes.get("unit_of_measurement", "")
                sensor_lines.append(f"{state.state} {unit}".strip())

        if sensor_lines:
            content = await self.hass.async_add_executor_job(
                self._apply_overlay,
                content,
                sensor_lines,
                config.get(CONF_OVERLAY_POSITION, DEFAULT_OVERLAY_POSITION),
                int(config.get(CONF_OVERLAY_FONT_SIZE, DEFAULT_OVERLAY_FONT_SIZE)),
                tuple(config.get(CONF_OVERLAY_TEXT_COLOR, DEFAULT_OVERLAY_TEXT_COLOR)),
                int(config.get(CONF_OVERLAY_STROKE_WIDTH, DEFAULT_OVERLAY_STROKE_WIDTH)),
                tuple(config.get(CONF_OVERLAY_STROKE_COLOR, DEFAULT_OVERLAY_STROKE_COLOR)),
                int(config.get(CONF_OVERLAY_MARGIN, DEFAULT_OVERLAY_MARGIN)),
                int(config.get(CONF_OVERLAY_LINE_SPACING, DEFAULT_OVERLAY_LINE_SPACING)),
            )

        quality = int(config.get(CONF_FRAME_QUALITY, DEFAULT_FRAME_QUALITY))
        target_w = int(config.get(CONF_FRAME_WIDTH, DEFAULT_FRAME_WIDTH))
        target_h = int(config.get(CONF_FRAME_HEIGHT, DEFAULT_FRAME_HEIGHT))

        def _save() -> None:
            import io
            from PIL import Image
            frame_dir.mkdir(parents=True, exist_ok=True)
            img = Image.open(io.BytesIO(content))
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            if target_w or target_h:
                orig_w, orig_h = img.size
                if target_w and target_h:
                    new_size = (target_w, target_h)
                elif target_w:
                    new_size = (target_w, max(1, round(orig_h * target_w / orig_w)))
                else:
                    new_size = (max(1, round(orig_w * target_h / orig_h)), target_h)
                img = img.resize(new_size, Image.LANCZOS)
            img.save(frame_path, format="JPEG", quality=quality, optimize=True)

        await self.hass.async_add_executor_job(_save)
        self._latest_frame_path[camera_id] = frame_path
        _LOGGER.debug("Frame saved: %s", frame_path)

        # Trigger rolling assembly (debounced)
        cameras = self.entry.options.get(CONF_CAMERAS, {})
        config = cameras.get(camera_id, {})
        mode = config.get(CONF_MODE, MODE_DAILY)
        if mode in (MODE_ROLLING, MODE_BOTH):
            last = self._last_rolling.get(camera_id)
            elapsed = (now - last).total_seconds() if last else ROLLING_DEBOUNCE_SECONDS + 1
            if elapsed >= ROLLING_DEBOUNCE_SECONDS:
                self._last_rolling[camera_id] = now
                self.hass.async_create_task(self.async_assemble_rolling(camera_id))

    # ------------------------------------------------------------------
    # Unavailable-frame placeholder
    # ------------------------------------------------------------------

    async def _update_unavailable_frame(self, camera_id: str) -> None:
        """Point the image entity at the placeholder image when the camera is unavailable."""
        cameras = self.entry.options.get(CONF_CAMERAS, {})
        config = cameras.get(camera_id, {})
        custom_bg = config.get(CONF_PLACEHOLDER_IMAGE, DEFAULT_PLACEHOLDER_IMAGE).strip()
        bg_path = Path(custom_bg) if custom_bg else Path(self.storage_path) / "placeholder.jpg"

        if not bg_path.exists():
            _LOGGER.debug("Placeholder image not found at %s, skipping update", bg_path)
            return

        self._latest_frame_path[camera_id] = bg_path
        async_dispatcher_send(
            self.hass,
            SIGNAL_SENSOR_UPDATE.format(self.entry.entry_id, camera_id),
        )

    # ------------------------------------------------------------------
    # Assembly
    # ------------------------------------------------------------------

    async def async_assemble_daily(self, camera_id: str, target_date: date) -> None:
        """Collect frames for a given date and write a timelapse file."""
        cameras = self.entry.options.get(CONF_CAMERAS, {})
        config = cameras.get(camera_id)
        if config is None:
            _LOGGER.warning("async_assemble_daily: camera %s not configured", camera_id)
            return

        camera_slug = _camera_slug(camera_id)
        date_str = target_date.strftime("%Y-%m-%d")
        frame_dir = Path(self.storage_path) / "frames" / camera_slug / date_str

        frames = await self.hass.async_add_executor_job(
            _collect_frames_day, frame_dir
        )

        if len(frames) < 2:
            _LOGGER.debug("Not enough frames for %s on %s (%d frame(s))", camera_id, date_str, len(frames))
            # In streaming mode, clean up past-day frame dirs even when assembly is skipped
            keep_frames = bool(config.get(CONF_KEEP_FRAMES, DEFAULT_KEEP_FRAMES))
            mode_early = config.get(CONF_MODE, MODE_DAILY)
            if not keep_frames and mode_early == MODE_DAILY and target_date < dt_util.now().date():
                await self.hass.async_add_executor_job(shutil.rmtree, str(frame_dir), True)
                _LOGGER.debug("Streaming mode: removed sparse frame dir %s", frame_dir)
            return

        fmt = config.get(CONF_OUTPUT_FORMAT, FORMAT_MP4)
        fps = int(config.get(CONF_FPS, 10))
        output_dir = Path(self.storage_path) / camera_slug
        output_path = output_dir / f"{date_str}.{fmt}"

        # Streaming mode: append new frames to existing file, then discard frame files.
        # Only valid for daily mode — rolling mode needs frames to rebuild from scratch.
        mode = config.get(CONF_MODE, MODE_DAILY)
        keep_frames = bool(config.get(CONF_KEEP_FRAMES, DEFAULT_KEEP_FRAMES))
        streaming = not keep_frames and mode == MODE_DAILY

        # In streaming mode pass the existing timelapse as append target (if it exists)
        append_to = output_path if streaming and output_path.exists() else None
        stabilize = bool(config.get(CONF_STABILIZATION, DEFAULT_STABILIZATION))

        lock = self._write_locks.setdefault(camera_id, asyncio.Lock())
        async with lock:
            await self.hass.async_add_executor_job(
                self._write_timelapse, frames, output_path, fps, fmt, append_to, stabilize
            )
        _LOGGER.info("Daily timelapse written: %s", output_path)

        # Delete frame directory after successful assembly in streaming mode
        if streaming:
            await self.hass.async_add_executor_job(
                shutil.rmtree, str(frame_dir), True
            )
            _LOGGER.debug("Streaming mode: deleted frame dir %s", frame_dir)

        # Update sensor state
        self._last_timelapse_info[camera_id] = {
            "path": str(output_path),
            "type": MODE_DAILY,
            "assembled_at": dt_util.now().isoformat(),
        }
        mb = await self.hass.async_add_executor_job(
            self._calc_disk_mb, _camera_slug(camera_id)
        )
        self._disk_usage_mb[camera_id] = mb
        async_dispatcher_send(
            self.hass,
            SIGNAL_SENSOR_UPDATE.format(self.entry.entry_id, camera_id),
        )

        # Fire HA event
        self.hass.bus.async_fire(
            EVENT_TIMELAPSE_READY,
            {
                "camera_entity_id": camera_id,
                "type": MODE_DAILY,
                "path": str(output_path),
                "date": date_str,
            },
        )

    async def async_assemble_rolling(self, camera_id: str) -> None:
        """Build a rolling timelapse from the last N days of frames."""
        cameras = self.entry.options.get(CONF_CAMERAS, {})
        config = cameras.get(camera_id)
        if config is None:
            return

        n_days = int(config.get(CONF_ROLLING_PERIOD_DAYS, 7))
        camera_slug = _camera_slug(camera_id)
        frames_root = Path(self.storage_path) / "frames" / camera_slug
        today = dt_util.now().date()

        frames = await self.hass.async_add_executor_job(
            _collect_frames_rolling, frames_root, today, n_days
        )

        if len(frames) < 2:
            return

        fmt = config.get(CONF_OUTPUT_FORMAT, FORMAT_MP4)
        fps = int(config.get(CONF_FPS, 10))
        output_dir = Path(self.storage_path) / camera_slug
        output_path = output_dir / f"rolling_{n_days}d.{fmt}"

        stabilize = bool(config.get(CONF_STABILIZATION, DEFAULT_STABILIZATION))
        lock = self._write_locks.setdefault(camera_id, asyncio.Lock())
        async with lock:
            await self.hass.async_add_executor_job(
                self._write_timelapse, frames, output_path, fps, fmt, None, stabilize
            )
        _LOGGER.info("Rolling timelapse written: %s", output_path)

        # Update sensor state
        self._last_timelapse_info[camera_id] = {
            "path": str(output_path),
            "type": MODE_ROLLING,
            "assembled_at": dt_util.now().isoformat(),
        }
        mb = await self.hass.async_add_executor_job(
            self._calc_disk_mb, _camera_slug(camera_id)
        )
        self._disk_usage_mb[camera_id] = mb
        async_dispatcher_send(
            self.hass,
            SIGNAL_SENSOR_UPDATE.format(self.entry.entry_id, camera_id),
        )

        # Fire HA event
        self.hass.bus.async_fire(
            EVENT_TIMELAPSE_READY,
            {
                "camera_entity_id": camera_id,
                "type": MODE_ROLLING,
                "path": str(output_path),
            },
        )

        # For rolling-only cameras, run cleanup once per day here
        # (daily/both cameras get cleanup via _daily_assembly_and_reschedule)
        cameras = self.entry.options.get(CONF_CAMERAS, {})
        config = cameras.get(camera_id, {})
        if config.get(CONF_MODE) == MODE_ROLLING:
            today = dt_util.now().date()
            if self._last_cleanup_date.get(camera_id) != today:
                self._last_cleanup_date[camera_id] = today
                await self.async_cleanup_camera(camera_id)

    # ------------------------------------------------------------------
    # Writers (run in executor thread)
    # ------------------------------------------------------------------

    def _write_timelapse(
        self,
        frames: list[Path],
        output: Path,
        fps: int,
        fmt: str,
        append_to: Path | None = None,
        stabilize: bool = False,
    ) -> None:
        """Dispatch to the appropriate format writer.

        If *append_to* is provided (streaming mode), new frames are appended to
        that file in-place rather than creating a fresh timelapse from scratch.
        If *stabilize* is True, frames are aligned via phase correlation before encoding.
        """
        output.parent.mkdir(parents=True, exist_ok=True)

        tmp_dir: Path | None = None
        if stabilize and len(frames) >= 2:
            stabilized = self._stabilize_frame_sequence(frames)
            if stabilized is not frames:
                tmp_dir = stabilized[0].parent
                frames = stabilized

        try:
            if append_to is not None and append_to.exists():
                if fmt == FORMAT_GIF:
                    self._append_gif(frames, append_to, fps)
                elif fmt == FORMAT_APNG:
                    self._append_apng(frames, append_to, fps)
                else:
                    self._append_mp4(frames, append_to, fps)
            else:
                if fmt == FORMAT_GIF:
                    self._write_gif(frames, output, fps)
                elif fmt == FORMAT_APNG:
                    self._write_apng(frames, output, fps)
                else:
                    self._write_mp4(frames, output, fps)
        finally:
            if tmp_dir and tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)

    def _write_gif(self, frames: list[Path], output: Path, fps: int) -> None:
        from PIL import Image  # noqa: PLC0415

        images = [Image.open(f).convert("RGB") for f in frames]
        duration_ms = max(1, int(1000 / fps))
        images[0].save(
            output,
            save_all=True,
            append_images=images[1:],
            duration=duration_ms,
            loop=0,
            optimize=True,
        )

    def _write_apng(self, frames: list[Path], output: Path, fps: int) -> None:
        from PIL import Image  # noqa: PLC0415

        images = [Image.open(f).convert("RGBA") for f in frames]
        duration_ms = max(1, int(1000 / fps))
        images[0].save(
            output,
            save_all=True,
            append_images=images[1:],
            duration=duration_ms,
            loop=0,
        )

    def _write_mp4(self, frames: list[Path], output: Path, fps: int) -> None:
        import subprocess  # noqa: PLC0415
        import tempfile  # noqa: PLC0415

        import imageio_ffmpeg  # noqa: PLC0415

        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

        # Build a concat demuxer list file so ffmpeg reads frames in order
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False
        ) as f:
            list_path = f.name
            duration = 1.0 / fps
            for frame in frames:
                f.write(f"file '{frame.absolute()}'\n")
                f.write(f"duration {duration:.6f}\n")

        try:
            subprocess.run(
                [
                    ffmpeg_exe, "-y",
                    "-f", "concat", "-safe", "0",
                    "-i", list_path,
                    # Ensure even dimensions required by yuv420p
                    "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                    "-c:v", "libx264",
                    "-crf", "23",
                    "-preset", "fast",
                    "-pix_fmt", "yuv420p",
                    str(output),
                ],
                check=True,
                capture_output=True,
            )
        finally:
            os.unlink(list_path)

    # ------------------------------------------------------------------
    # Append writers (streaming mode) — run in executor thread
    # ------------------------------------------------------------------

    def _append_mp4(self, new_frames: list[Path], existing: Path, fps: int) -> None:
        """Append *new_frames* to an existing MP4 without re-encoding.

        Strategy: encode new frames into a temporary MP4, then use ffmpeg
        concat with -c copy to join existing + new into a replacement file.
        """
        import subprocess  # noqa: PLC0415
        import tempfile  # noqa: PLC0415

        import imageio_ffmpeg  # noqa: PLC0415

        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        tmp_new = existing.with_name(existing.stem + "._new.mp4")
        tmp_out = existing.with_name(existing.stem + "._out.mp4")
        list_path: str | None = None
        try:
            # Step 1: encode new frames to a temp file (same settings as _write_mp4)
            self._write_mp4(new_frames, tmp_new, fps)

            # Step 2: concat list — existing video followed by new segment
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False
            ) as f:
                list_path = f.name
                f.write(f"file '{existing.absolute()}'\n")
                f.write(f"file '{tmp_new.absolute()}'\n")

            # Step 3: join with stream copy (no quality loss, fast)
            subprocess.run(
                [
                    ffmpeg_exe, "-y",
                    "-f", "concat", "-safe", "0",
                    "-i", list_path,
                    "-c", "copy",
                    str(tmp_out),
                ],
                check=True,
                capture_output=True,
            )

            # Step 4: atomically replace existing file
            tmp_out.replace(existing)
        finally:
            tmp_new.unlink(missing_ok=True)
            tmp_out.unlink(missing_ok=True)
            if list_path:
                os.unlink(list_path)

    def _append_gif(self, new_frames: list[Path], existing: Path, fps: int) -> None:
        """Append new JPEG frames to an existing GIF animation."""
        from PIL import Image  # noqa: PLC0415

        # Extract all frames from the existing GIF
        existing_images: list[Image.Image] = []
        with Image.open(existing) as gif:
            for i in range(getattr(gif, "n_frames", 1)):
                gif.seek(i)
                existing_images.append(gif.copy().convert("RGB"))

        new_images = [Image.open(f).convert("RGB") for f in new_frames]
        all_images = existing_images + new_images
        duration_ms = max(1, int(1000 / fps))
        all_images[0].save(
            existing,
            save_all=True,
            append_images=all_images[1:],
            duration=duration_ms,
            loop=0,
            optimize=True,
        )

    def _append_apng(self, new_frames: list[Path], existing: Path, fps: int) -> None:
        """Append new JPEG frames to an existing APNG animation."""
        from PIL import Image  # noqa: PLC0415

        existing_images: list[Image.Image] = []
        with Image.open(existing) as apng:
            for i in range(getattr(apng, "n_frames", 1)):
                apng.seek(i)
                existing_images.append(apng.copy().convert("RGBA"))

        new_images = [Image.open(f).convert("RGBA") for f in new_frames]
        all_images = existing_images + new_images
        duration_ms = max(1, int(1000 / fps))
        all_images[0].save(
            existing,
            save_all=True,
            append_images=all_images[1:],
            duration=duration_ms,
            loop=0,
        )

    def _calc_disk_mb(self, camera_slug: str) -> float:
        """Return total disk usage in MB for a camera. Runs in executor."""
        total = 0
        for root in (
            Path(self.storage_path) / "frames" / camera_slug,
            Path(self.storage_path) / camera_slug,
        ):
            if root.exists():
                for dirpath, _, filenames in os.walk(root):
                    for fname in filenames:
                        try:
                            total += os.path.getsize(os.path.join(dirpath, fname))
                        except OSError:
                            pass
        return round(total / (1024 * 1024), 2)

    # ------------------------------------------------------------------
    # Stabilization (runs in executor)
    # ------------------------------------------------------------------

    def _stabilize_frame_sequence(self, frames: list[Path]) -> list[Path]:
        """Align frames using numpy FFT phase correlation (translation-only).

        Returns a list of paths to stabilized JPEG copies in a temp directory,
        or the original *frames* list unchanged if no correction was needed.
        The caller is responsible for cleaning up the temp directory.
        """
        import tempfile

        import numpy as np
        from PIL import Image

        ref = np.array(Image.open(frames[0]).convert("L"), dtype=np.float32)
        translations: list[tuple[int, int]] = [(0, 0)]
        for fp in frames[1:]:
            img_arr = np.array(Image.open(fp).convert("L"), dtype=np.float32)
            translations.append(_phase_correlation(ref, img_arr))

        max_dx = max(abs(t[0]) for t in translations)
        max_dy = max(abs(t[1]) for t in translations)
        if max_dx == 0 and max_dy == 0:
            return frames

        ref_img = Image.open(frames[0])
        iw, ih = ref_img.size
        # Cap correction to 10 % of frame dimensions to avoid over-cropping
        cap = min(iw // 10, ih // 10)
        cl = min(int(max_dx), cap)
        ct = min(int(max_dy), cap)
        crop_box = (cl, ct, iw - cl, ih - ct)

        tmp = Path(tempfile.mkdtemp(prefix="timelapse_stab_"))
        result: list[Path] = []
        for idx, (fp, (dx, dy)) in enumerate(zip(frames, translations)):
            dx = max(-cap, min(cap, dx))
            dy = max(-cap, min(cap, dy))
            img = Image.open(fp).convert("RGB")
            # Affine translation matrix (fill edges with black)
            corrected = img.transform(img.size, Image.Transform.AFFINE, (1, 0, -dx, 0, 1, -dy))
            out = tmp / f"{idx:06d}.jpg"
            corrected.crop(crop_box).save(out, format="JPEG", quality=92)
            result.append(out)

        _LOGGER.debug(
            "Stabilization: max shift dx=%d dy=%d, cropped to %s", max_dx, max_dy, crop_box
        )
        return result

    # ------------------------------------------------------------------
    # Sensor overlay (runs in executor)
    # ------------------------------------------------------------------

    def _apply_overlay(
        self,
        image_bytes: bytes,
        sensor_lines: list[str],
        position: str,
        font_size: int,
        text_color: tuple[int, int, int] = (255, 255, 255),
        stroke_width: int = 1,
        stroke_color: tuple[int, int, int] = (0, 0, 0),
        margin: int = 10,
        line_spacing: int = 4,
    ) -> bytes:
        """Draw sensor values on a frame.

        Combines a semi-transparent background rectangle with per-character
        stroke for readability in both bright and dark conditions.
        Runs in executor thread.
        """
        import io

        from PIL import Image, ImageDraw, ImageFont

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        import pathlib as _pathlib
        _bundled = str(
            _pathlib.Path(__file__).parent / "fonts" / "DejaVuSans-Bold.ttf"
        )
        _FONT_PATHS = [
            _bundled,
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        ]
        font = None
        for fp in _FONT_PATHS:
            try:
                font = ImageFont.truetype(fp, font_size)
                break
            except (IOError, OSError):
                continue
        if font is None:
            try:
                font = ImageFont.load_default(size=font_size)  # Pillow >= 10.1
            except TypeError:
                font = ImageFont.load_default()

        pad = 6
        # Measure actual rendered text dimensions for correct block sizing
        bboxes = [
            draw.textbbox((0, 0), line, font=font, stroke_width=stroke_width)
            for line in sensor_lines
        ]
        widths = [b[2] - b[0] for b in bboxes]
        line_heights = [b[3] - b[1] for b in bboxes]
        actual_line_h = max(line_heights) if line_heights else font_size
        line_h = actual_line_h + line_spacing
        block_w = max(widths) + pad * 2
        block_h = len(sensor_lines) * line_h - line_spacing + pad * 2
        iw, ih = img.size

        origins: dict[str, tuple[int, int]] = {
            "top_left":     (margin, margin),
            "top_right":    (iw - block_w - margin, margin),
            "bottom_left":  (margin, ih - block_h - margin),
            "bottom_right": (iw - block_w - margin, ih - block_h - margin),
        }
        ox, oy = origins.get(position, origins["top_left"])

        text_fill = (*text_color, 255)
        stroke_fill = (*stroke_color, 255)
        for i, line in enumerate(sensor_lines):
            draw.text(
                (ox + pad, oy + pad + i * line_h),
                line,
                fill=text_fill,
                font=font,
                stroke_width=stroke_width,
                stroke_fill=stroke_fill,
            )

        composited = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        buf = io.BytesIO()
        composited.save(buf, format="JPEG", quality=90)
        return buf.getvalue()

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def async_cleanup_camera(self, camera_id: str) -> None:
        """Delete frames and daily timelapse files older than max_retention_days.

        Also removes frame dirs for past days whose timelapse has already been
        assembled, provided frames are not needed for rolling assembly.
        """
        cameras = self.entry.options.get(CONF_CAMERAS, {})
        config = cameras.get(camera_id, {})
        max_days = int(config.get(CONF_MAX_RETENTION_DAYS, 30))

        camera_slug = _camera_slug(camera_id)
        cutoff = (
            dt_util.now().date() - timedelta(days=max_days) if max_days > 0 else None
        )

        await self.hass.async_add_executor_job(
            self._cleanup_sync, camera_slug, cutoff, config
        )

        # Refresh disk usage sensor after cleanup
        mb = await self.hass.async_add_executor_job(self._calc_disk_mb, camera_slug)
        self._disk_usage_mb[camera_id] = mb
        async_dispatcher_send(
            self.hass,
            SIGNAL_SENSOR_UPDATE.format(self.entry.entry_id, camera_id),
        )

    def _cleanup_sync(
        self, camera_slug: str, cutoff: date | None, config: dict | None = None
    ) -> None:
        today = dt_util.now().date()
        mode = config.get(CONF_MODE, MODE_DAILY) if config else MODE_DAILY
        keep_frames = (
            bool(config.get(CONF_KEEP_FRAMES, DEFAULT_KEEP_FRAMES)) if config else True
        )
        output_dir = Path(self.storage_path) / camera_slug

        # Rolling window: dates that must be kept for rolling assembly
        rolling_cutoff: date | None = None
        if mode in (MODE_ROLLING, MODE_BOTH) and config:
            n_days = int(config.get(CONF_ROLLING_PERIOD_DAYS, 7))
            rolling_cutoff = today - timedelta(days=n_days)

        # Remove frame directories
        frames_root = Path(self.storage_path) / "frames" / camera_slug
        if frames_root.exists():
            for day_dir in frames_root.iterdir():
                if not day_dir.is_dir():
                    continue
                try:
                    dir_date = date.fromisoformat(day_dir.name)
                except ValueError:
                    continue

                # Never touch frames from the future
                if dir_date > today:
                    continue

                # Always remove dirs beyond max_retention_days
                if cutoff and dir_date < cutoff:
                    shutil.rmtree(day_dir, ignore_errors=True)
                    _LOGGER.debug("Removed old frames dir: %s", day_dir)
                    continue

                # For non-rolling modes: remove past frame dirs when keep_frames
                # is disabled — frames are no longer needed after the assembly
                # window has passed (regardless of whether assembly succeeded).
                if not keep_frames and mode != MODE_ROLLING:
                    # For MODE_BOTH keep frames inside the rolling window
                    if rolling_cutoff and dir_date >= rolling_cutoff:
                        continue

                    if dir_date < today:
                        # Past day: always remove (assembly already had its chance)
                        shutil.rmtree(day_dir, ignore_errors=True)
                        _LOGGER.debug(
                            "Removed past frame dir: %s", day_dir
                        )
                    else:
                        # Today: only remove if timelapse was assembled
                        fmt = (
                            config.get(CONF_OUTPUT_FORMAT, FORMAT_MP4)
                            if config
                            else FORMAT_MP4
                        )
                        assembled = output_dir / f"{dir_date.strftime('%Y-%m-%d')}.{fmt}"
                        if assembled.exists():
                            shutil.rmtree(day_dir, ignore_errors=True)
                            _LOGGER.debug(
                                "Removed assembled frame dir: %s", day_dir
                            )

        # Remove old daily timelapse files (skip rolling_ files)
        if not output_dir.exists():
            return
        if cutoff:
            for tl_file in output_dir.iterdir():
                if not tl_file.is_file():
                    continue
                if tl_file.stem.startswith("rolling_"):
                    continue
                try:
                    file_date = date.fromisoformat(tl_file.stem)
                except ValueError:
                    continue
                if file_date < cutoff:
                    tl_file.unlink(missing_ok=True)
                    _LOGGER.debug("Removed old timelapse: %s", tl_file)

    # ------------------------------------------------------------------
    # Startup recovery
    # ------------------------------------------------------------------

    async def _restore_frame_counts(self) -> None:
        """Restore in-memory sensor state from disk after a restart."""
        cameras: dict = self.entry.options.get(CONF_CAMERAS, {})
        today = dt_util.now().date()
        date_str = today.strftime("%Y-%m-%d")
        for camera_id in cameras:
            camera_slug = _camera_slug(camera_id)

            # frames_today
            frame_dir = Path(self.storage_path) / "frames" / camera_slug / date_str
            count: int = await self.hass.async_add_executor_job(
                lambda d=frame_dir: len(list(d.glob("*.jpg"))) if d.exists() else 0
            )
            if count > 0:
                self._frame_counts[camera_id] = count
                self._frame_count_dates[camera_id] = today
                _LOGGER.debug(
                    "Restored frames_today=%d for %s from disk", count, camera_id
                )

            # disk_usage
            mb: float = await self.hass.async_add_executor_job(
                self._calc_disk_mb, camera_slug
            )
            self._disk_usage_mb[camera_id] = mb

            # last_timelapse — find the most recently modified file in the output dir
            output_dir = Path(self.storage_path) / camera_slug
            info: dict | None = await self.hass.async_add_executor_job(
                _find_latest_timelapse, output_dir
            )
            if info:
                self._last_timelapse_info[camera_id] = info
                _LOGGER.debug(
                    "Restored last_timelapse for %s: %s", camera_id, info["path"]
                )

            # latest_frame — most recent JPEG across all day dirs, or placeholder
            frames_root = Path(self.storage_path) / "frames" / camera_slug
            placeholder_path = Path(self.storage_path) / "placeholder.jpg"
            latest: Path | None = await self.hass.async_add_executor_job(
                _find_latest_frame_any, frames_root, placeholder_path
            )
            if latest:
                self._latest_frame_path[camera_id] = latest

    def get_latest_frame_path(self, camera_id: str) -> Path | None:
        """Return path of the most recently captured frame, or None."""
        return self._latest_frame_path.get(camera_id)

    async def _recover_missing_daily(self) -> None:
        """On startup, assemble any missing daily timelapses from yesterday."""
        cameras: dict = self.entry.options.get(CONF_CAMERAS, {})
        yesterday = dt_util.now().date() - timedelta(days=1)

        for camera_id, config in cameras.items():
            mode = config.get(CONF_MODE, MODE_DAILY)
            if mode not in (MODE_DAILY, MODE_BOTH):
                continue

            camera_slug = _camera_slug(camera_id)
            fmt = config.get(CONF_OUTPUT_FORMAT, FORMAT_MP4)
            output_file = (
                Path(self.storage_path)
                / camera_slug
                / f"{yesterday.strftime('%Y-%m-%d')}.{fmt}"
            )

            if output_file.exists():
                continue

            # Frames exist but file is missing → assemble now
            frames_dir = (
                Path(self.storage_path)
                / "frames"
                / camera_slug
                / yesterday.strftime("%Y-%m-%d")
            )
            has_frames = await self.hass.async_add_executor_job(
                lambda d=frames_dir: d.exists() and bool(list(d.glob("*.jpg"))[:1])
            )
            if has_frames:
                _LOGGER.info(
                    "Recovering missing daily timelapse for %s on %s",
                    camera_id,
                    yesterday,
                )
                await self.async_assemble_daily(camera_id, yesterday)
                await self.async_cleanup_camera(camera_id)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _create_default_placeholder(storage_path: str) -> None:
    """Create a simple dark-grey placeholder JPEG in *storage_path* if absent.

    The file is used as the background when 'Camera unavailable' is overlaid.
    Users can replace it with any JPEG they prefer.
    """
    target = Path(storage_path) / "placeholder.jpg"
    if target.exists():
        return
    from PIL import Image  # noqa: PLC0415

    target.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (640, 360), color=(30, 30, 30))
    img.save(str(target), format="JPEG", quality=85)



def _find_latest_frame_any(frames_root: Path, placeholder_path: Path) -> Path | None:
    """Return the most recently modified JPEG across all day subdirectories.

    Falls back to the placeholder if no real frames exist on disk.
    """
    best: Path | None = None
    best_mtime = 0.0
    if frames_root.exists():
        for day_dir in frames_root.iterdir():
            if not day_dir.is_dir():
                continue
            for f in day_dir.glob("*.jpg"):
                mtime = f.stat().st_mtime
                if mtime > best_mtime:
                    best_mtime = mtime
                    best = f
    if best is None and placeholder_path.exists():
        best = placeholder_path
    return best


def _find_latest_timelapse(output_dir: Path) -> dict | None:
    """Return info dict for the most recently modified timelapse file, or None."""
    if not output_dir.exists():
        return None
    files = [f for f in output_dir.iterdir() if f.is_file()]
    if not files:
        return None
    latest = max(files, key=lambda f: f.stat().st_mtime)
    timelapse_type = MODE_ROLLING if latest.stem.startswith("rolling_") else MODE_DAILY
    assembled_at = dt_util.as_local(
        dt_util.utc_from_timestamp(latest.stat().st_mtime)
    ).isoformat()
    return {"path": str(latest), "type": timelapse_type, "assembled_at": assembled_at}


def _phase_correlation(img1, img2) -> tuple[int, int]:
    """Return (dx, dy) integer translation from img1 to img2 via FFT phase correlation.

    Both inputs must be 2-D numpy float32 arrays of the same shape.
    """
    import numpy as np  # noqa: PLC0415

    f1 = np.fft.fft2(img1)
    f2 = np.fft.fft2(img2)
    cross = f1 * np.conj(f2)
    denom = np.abs(cross)
    denom[denom < 1e-8] = 1e-8
    corr = np.fft.ifft2(cross / denom).real
    y, x = np.unravel_index(np.argmax(corr), corr.shape)
    h, w = img1.shape
    if y > h // 2:
        y -= h
    if x > w // 2:
        x -= w
    return int(x), int(y)


def _camera_slug(entity_id: str) -> str:
    """Convert camera entity_id to a filesystem-safe directory name."""
    slug = entity_id.replace(".", "_")
    if slug.startswith("camera_"):
        slug = slug[len("camera_"):]
    return slug


def _collect_frames_day(frame_dir: Path) -> list[Path]:
    """Collect sorted frame paths for one day. Runs in executor."""
    if not frame_dir.exists():
        return []
    return sorted(frame_dir.glob("*.jpg"))


def _collect_frames_rolling(
    frames_root: Path, today: date, n_days: int
) -> list[Path]:
    """Collect sorted frame paths for the last N days. Runs in executor."""
    frames: list[Path] = []
    for i in range(n_days, -1, -1):
        day = today - timedelta(days=i)
        day_dir = frames_root / day.strftime("%Y-%m-%d")
        if day_dir.exists():
            frames.extend(sorted(day_dir.glob("*.jpg")))
    return frames


def _average_frames(raw_list: list[bytes]) -> bytes:
    """Average N JPEG frames pixel-wise using iterative Pillow blending. Runs in executor."""
    import io  # noqa: PLC0415

    from PIL import Image  # noqa: PLC0415

    images = [Image.open(io.BytesIO(b)).convert("RGB") for b in raw_list]
    result = images[0]
    for i, img in enumerate(images[1:], 2):
        result = Image.blend(result, img, 1.0 / i)
    buf = io.BytesIO()
    result.save(buf, format="JPEG")
    return buf.getvalue()
