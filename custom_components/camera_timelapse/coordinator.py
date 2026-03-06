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
from homeassistant.helpers.event import async_track_point_in_time, async_track_time_interval
import homeassistant.util.dt as dt_util

from .const import (
    CONF_ASSEMBLY_INTERVAL_MINUTES,
    CONF_CAMERAS,
    CONF_FPS,
    CONF_INTERVAL_MINUTES,
    CONF_MAX_RETENTION_DAYS,
    CONF_MODE,
    CONF_OUTPUT_FORMAT,
    CONF_ROLLING_PERIOD_DAYS,
    CONF_STORAGE_PATH,
    CONF_TIME_END,
    CONF_TIME_RANGE_TYPE,
    CONF_TIME_START,
    FORMAT_APNG,
    FORMAT_GIF,
    FORMAT_MP4,
    MODE_BOTH,
    MODE_DAILY,
    MODE_ROLLING,
    ROLLING_DEBOUNCE_SECONDS,
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

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Read options and schedule capture/assembly tasks for each camera."""
        cameras: dict = self.entry.options.get(CONF_CAMERAS, {})
        for camera_id, cam_config in cameras.items():
            await self._schedule_camera(camera_id, cam_config)

        # Re-schedule when options change (user edits via OptionsFlow)
        self.entry.async_on_unload(
            self.entry.add_update_listener(self._handle_options_update)
        )

        # Startup recovery: assemble yesterday's missing daily timelapses
        await self._recover_missing_daily()

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
                @callback
                def _periodic_assembly_cb(
                    now: datetime, cid: str = camera_id
                ) -> None:
                    self.hass.async_create_task(
                        self.async_assemble_daily(cid, dt_util.now().date())
                    )

                self._periodic_assembly_unsubs[camera_id] = async_track_time_interval(
                    self.hass,
                    _periodic_assembly_cb,
                    timedelta(minutes=assembly_interval),
                )

    def _cancel_camera(self, camera_id: str) -> None:
        """Cancel capture and assembly tasks for a camera."""
        if camera_id in self._periodic_assembly_unsubs:
            self._periodic_assembly_unsubs.pop(camera_id)()
        if camera_id in self._capture_unsubs:
            self._capture_unsubs.pop(camera_id)()
        if camera_id in self._assembly_unsubs:
            self._assembly_unsubs.pop(camera_id)()

    async def _schedule_assembly_trigger(self, camera_id: str, config: dict) -> None:
        """Schedule the daily assembly at end-of-active-window or 23:59."""
        now = dt_util.now()
        time_range_type = config.get(CONF_TIME_RANGE_TYPE, TIME_RANGE_SUNRISE_SUNSET)

        if time_range_type == TIME_RANGE_SUNRISE_SUNSET:
            trigger_time = self._get_sunset_today()
            # If sunset has already passed today, trigger at midnight and reassemble
            if trigger_time is None or trigger_time <= now:
                trigger_time = now.replace(hour=23, minute=59, second=0, microsecond=0)
                if trigger_time <= now:
                    trigger_time = trigger_time + timedelta(days=1)
        elif time_range_type == TIME_RANGE_CUSTOM:
            end_str = config.get(CONF_TIME_END, "19:00:00")
            h, m = int(end_str[:2]), int(end_str[3:5])
            trigger_time = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if trigger_time <= now:
                trigger_time = trigger_time + timedelta(days=1)
        else:
            # TIME_RANGE_ALWAYS → assemble at 23:59
            trigger_time = now.replace(hour=23, minute=59, second=0, microsecond=0)
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
        await self.async_assemble_daily(camera_id, target_date)
        await self.async_cleanup_camera(camera_id)
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

        try:
            from homeassistant.components.camera import async_get_image
            image = await async_get_image(self.hass, camera_id, timeout=10)
        except Exception as err:
            _LOGGER.warning("Frame capture failed for %s: %s", camera_id, err)
            return

        now = dt_util.now()
        camera_slug = _camera_slug(camera_id)
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H%M%S")
        frame_dir = Path(self.storage_path) / "frames" / camera_slug / date_str
        frame_path = frame_dir / f"{time_str}.jpg"
        content = image.content

        def _save() -> None:
            frame_dir.mkdir(parents=True, exist_ok=True)
            frame_path.write_bytes(content)

        await self.hass.async_add_executor_job(_save)
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
            return

        fmt = config.get(CONF_OUTPUT_FORMAT, FORMAT_MP4)
        fps = int(config.get(CONF_FPS, 10))
        output_dir = Path(self.storage_path) / camera_slug
        output_path = output_dir / f"{date_str}.{fmt}"

        lock = self._write_locks.setdefault(camera_id, asyncio.Lock())
        async with lock:
            await self.hass.async_add_executor_job(
                self._write_timelapse, frames, output_path, fps, fmt
            )
        _LOGGER.info("Daily timelapse written: %s", output_path)

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

        lock = self._write_locks.setdefault(camera_id, asyncio.Lock())
        async with lock:
            await self.hass.async_add_executor_job(
                self._write_timelapse, frames, output_path, fps, fmt
            )
        _LOGGER.info("Rolling timelapse written: %s", output_path)

    # ------------------------------------------------------------------
    # Writers (run in executor thread)
    # ------------------------------------------------------------------

    def _write_timelapse(
        self, frames: list[Path], output: Path, fps: int, fmt: str
    ) -> None:
        """Dispatch to the appropriate format writer."""
        output.parent.mkdir(parents=True, exist_ok=True)
        if fmt == FORMAT_GIF:
            self._write_gif(frames, output, fps)
        elif fmt == FORMAT_APNG:
            self._write_apng(frames, output, fps)
        else:
            self._write_mp4(frames, output, fps)

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
    # Cleanup
    # ------------------------------------------------------------------

    async def async_cleanup_camera(self, camera_id: str) -> None:
        """Delete frames and daily timelapse files older than max_retention_days."""
        cameras = self.entry.options.get(CONF_CAMERAS, {})
        config = cameras.get(camera_id, {})
        max_days = int(config.get(CONF_MAX_RETENTION_DAYS, 30))
        if max_days == 0:
            return

        camera_slug = _camera_slug(camera_id)
        cutoff = dt_util.now().date() - timedelta(days=max_days)

        await self.hass.async_add_executor_job(
            self._cleanup_sync, camera_slug, cutoff
        )

    def _cleanup_sync(self, camera_slug: str, cutoff: date) -> None:
        # Remove old frame directories
        frames_root = Path(self.storage_path) / "frames" / camera_slug
        if frames_root.exists():
            for day_dir in frames_root.iterdir():
                if not day_dir.is_dir():
                    continue
                try:
                    dir_date = date.fromisoformat(day_dir.name)
                except ValueError:
                    continue
                if dir_date < cutoff:
                    shutil.rmtree(day_dir, ignore_errors=True)
                    _LOGGER.debug("Removed old frames dir: %s", day_dir)

        # Remove old daily timelapse files (skip rolling_ files)
        output_dir = Path(self.storage_path) / camera_slug
        if not output_dir.exists():
            return
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


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

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
