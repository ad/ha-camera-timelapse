# Camera Timelapse

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/release/ad/ha-camera-timelapse.svg)](https://github.com/ad/ha-camera-timelapse/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Home Assistant custom integration that creates timelapses from your cameras — GIF, APNG, or MP4 — with per-camera schedules, sunrise-to-sunset support, and automatic cleanup.

---

## Features

- **Multiple cameras** — configure each one independently
- **Capture schedule** — set capture interval from 1 minute to 24 hours
- **Time window** — capture only during sunrise–sunset, a custom time range, or 24/7
- **Two timelapse modes**:
  - **Daily** — one file per day, assembled at end of active window
  - **Rolling** — constantly updated file covering the last N days
- **Periodic mid-day assembly** — optionally assemble the timelapse every N minutes during the day
- **Streaming mode** — delete raw frames after each assembly to save disk space (daily mode only)
- **Output formats** — GIF, APNG, or MP4 (H.264, no system ffmpeg required)
- **Auto cleanup** — delete frames and timelapse files older than N days; runs on every startup to catch missed windows
- **HA services** — manual capture and assembly triggers
- **Sensor entities** — frames captured today, last timelapse info, disk usage per camera
- **Media Browser** — files stored in `/media`, accessible via HA Media Browser

---

## Requirements

- Home Assistant 2023.4.0 or newer
- Python packages installed automatically by HA:
  - `Pillow >= 10.0.0`
  - `imageio[ffmpeg] >= 2.28.0` (includes a bundled static ffmpeg binary)

---

## Installation

### Via HACS (recommended)

1. Open HACS → **Integrations** → ⋮ menu → **Custom repositories**
2. Add `https://github.com/ad/ha-camera-timelapse` with category **Integration**
3. Find **Camera Timelapse** in HACS and click **Download**
4. Restart Home Assistant

### Manual

1. Copy `custom_components/camera_timelapse/` to your `config/custom_components/` directory
2. Restart Home Assistant

---

## Setup

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Camera Timelapse**
3. Enter a name and storage path (default: `/media/timelapses`)
4. Click the **Configure** button on the integration card to add cameras

### Adding a camera

In the **Configure** dialog:

1. Select **Add a camera**
2. Pick the camera entity
3. Configure its settings:

| Setting | Description | Default |
|---|---|---|
| Capture interval | How often to grab a frame | 5 min |
| Active time window | `Sunrise to sunset` / `Custom` / `Always` | Sunrise to sunset |
| Start / End time | Used when Custom is selected | 07:00 / 19:00 |
| Timelapse mode | `Daily` / `Rolling` / `Both` | Daily |
| Rolling window | Days of footage in rolling timelapse | 7 days |
| Output format | `GIF` / `APNG` / `MP4` | MP4 |
| FPS | Playback speed of the output file | 10 fps |
| Max retention | Days to keep files (0 = keep forever) | 30 days |
| Assemble every | Also assemble mid-day every N minutes (0 = only at end of day) | 0 |
| Frame averaging | Capture N frames per interval and average them to reduce noise | 0 (off) |
| Keep frame files | When off, raw JPEG frames are deleted after each assembly (streaming mode) | On |

---

## Storage layout

```
/media/timelapses/
├── frames/
│   └── {camera_name}/
│       └── YYYY-MM-DD/
│           ├── 070000.jpg
│           ├── 070500.jpg
│           └── ...
└── {camera_name}/
    ├── 2024-01-15.mp4       ← daily timelapse
    ├── 2024-01-16.mp4
    └── rolling_7d.mp4       ← rolling timelapse (updated after each capture)
```

---

## Services

### `camera_timelapse.capture_now`

Immediately capture a frame, bypassing the active time window.

| Field | Required | Description |
|---|---|---|
| `camera_entity_id` | No | Specific camera, or leave empty for all cameras |

### `camera_timelapse.generate_timelapse`

Manually trigger timelapse assembly without waiting for the scheduled trigger.

| Field | Required | Description |
|---|---|---|
| `camera_entity_id` | Yes | Camera to assemble |
| `mode` | No | `daily` / `rolling` / `both` (default: `daily`) |
| `date` | No | Date in `YYYY-MM-DD` for daily mode (default: today) |

---

## Streaming mode

When **Keep frame files** is disabled, the integration runs in streaming mode:

1. Frames are still saved temporarily to disk during the capture interval.
2. On each assembly (periodic or end-of-day), new frames are **appended** to the existing timelapse and the frame directory is **deleted**.
3. For MP4, appending uses `ffmpeg -c copy` — no re-encoding, no quality loss, very fast.
4. For GIF/APNG, existing animation frames are decoded and combined with the new JPEG frames before saving.

Streaming mode is only available with **Daily** timelapse mode — Rolling mode always needs the raw frames on disk to rebuild the full N-day timelapse.

**Recommended setup for minimum disk usage**: Daily mode + Assemble every 60 min + Keep frame files off.

---

## Notes

- **Storage path on HA OS / HA Green**: you **must** use `/media/...` as the storage path (e.g. `/media/timelapses`). Paths like `/homeassistant/...` or `/config/...` are written inside the HA core container and are not accessible from add-on terminals or the Media Browser. `/media` is the only directory shared across all containers in HA OS.
- **GIF quality**: GIF is limited to 256 colours per frame. For natural outdoor scenes, MP4 or APNG give significantly better results.
- **MP4 dimensions**: frame width and height are automatically rounded down to even numbers (required by H.264/yuv420p). Frames with different resolutions are resized to match the first frame.
- **Polar regions**: if sunrise or sunset data is unavailable (midnight sun / polar night), the integration falls back to capturing all day.
- **HA restart recovery**: on startup the integration checks for missing daily timelapses from yesterday and assembles them automatically if frames are present. Missed cleanup windows are also applied on every startup.
- **Periodic assembly interval**: the interval is counted from the last actual run time (persisted across restarts), not from the clock. If HA was down and one or more intervals were missed, a single catch-up assembly runs shortly after startup.

---

## License

[MIT](LICENSE)
