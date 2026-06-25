# Owlet Cam — sensor reverse-engineering notes

Source: decompiled official Owlet app (`/tmp/jout/sources`, classes7.dex).
These document how the **camera** reports environmental sensors over the
TUTK/Kalay P2P session (the cam's temp/humidity are NOT on the Ayla cloud API).

## Two independent paths

### 1. Embedded in the video FRAME INFO (free — every frame)
The Owlet cam uses an **extended FRAMEINFO struct** (parsed by `c1/c.java`).
Our `tutk_client.py` already receives this via `avRecvFrameData2` (we capture
up to `FINFO_BUF=64` bytes). Layout (little-endian):

| field            | type     | offset | notes                                  |
|------------------|----------|--------|----------------------------------------|
| codec_id         | u16 LE   | 0      |                                        |
| flags            | u8       | 2      | bit0=IFrame, bit1=motion, bit2=alarm, bit3=sound |
| cam_index        | u8       | 3      |                                        |
| onlineNum        | u8       | 4      |                                        |
| resolution_flag  | u8       | 5      | 1=Low 2=Std 3=High 4=Quad else=Off     |
| dasa_enabled     | u8       | 6      | 1=Auto else Manual                     |
| timestamp_ms     | i32 LE   | 8      |                                        |
| timestamp_s      | i32 LE   | 12     |                                        |
| **temperature**  | i32 LE   | **16** | room temp °C (per `k.r` = tempCelsius) |
| camera_utc_time  | i32 LE   | 20     |                                        |
| **audio_db**     | i32 LE   | **24** | noise level (dB)                       |

So **temperature + noise + motion + sound** come for free off the frame info —
no extra request. (`d1/h.java` builds these into `k.r`/`k.s`; `c1/c.java` is the
byte parser.)

### 2. GetRealtimeData IOCTL (adds humidity + brightness)
Request/response classes `x0/m.java` (req) + `x0/n.java` (resp), struct
`c1/d.java$h`:

- **Send:** `avSendIOCtrl(av_index, 960, data, 4)`  (ioType `960` = 0x3C0; payload
  = 4 zero bytes, from `x0/c.java getData()` = `new byte[4]`).
- **Receive:** `avRecvIOCtrl(...)` until type == **`961`** (0x3C1), then parse the
  response struct (little-endian):

| field                       | type   | offset |
|-----------------------------|--------|--------|
| temperature_degrees_celcius | i32 LE | 0      |
| humidity_precent_rh         | i32 LE | 4      |
| noise_db                    | i32 LE | 8      |
| brightness_lux              | i32 LE | 12     |
| wifi_rssi                   | i8     | 16     |

(`v0/g.java handleGetRealtimeDataResponse` maps these into `k.r`/`k.f`/`k.C0221k`/
`k.n`/`k.m` = Temperature/Humidity/Noise/RoomBrightness/RSSI.)

TUTK transport wrappers: `com/owlet/tutk/AndroidTutkSdk.java`
(`sendIoControl` → `AVAPIs.avSendIOCtrl(channelId, requestType, data, data.length)`;
`receiveIoControl` → `AVAPIs.avRecvIOCtrl(channelId, typeHolder, buf, len, timeout)`).

## Implementation in this repo
- Frame-info path: `tutk_client.py` reads offsets 16/24 + flags bit1/bit3 off the
  `finfo` buffer and writes `${OWLET_CAM_SENSORS}` JSON sidecar.
- IOCTL path: a guarded poll thread sends 960 / reads 961 for humidity+brightness.
- `webapp.py` merges the cam sidecar into `/api/vitals`.
- Units: frame-info `temperature` is treated as °C (matches app's `tempCelsius`);
  verify against the app's displayed value and add a scale factor if needed.
