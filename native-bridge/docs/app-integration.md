# Owlet Bridge — app integration guide

Everything your native app needs to talk to the bridge directly (instead of
through Frigate) for **sub-second WebRTC video** plus **all the sock + camera
data**. Base host below is your bridge, e.g. `http://192.168.1.50`.

| Service | Port | What |
|---|---|---|
| Control API + go2rtc | `1984` | WebRTC/MSE/HLS, snapshots, stream list |
| RTSP | `8554` | `rtsp://<host>:8554/<camera>` |
| WebRTC media (UDP/TCP) | `8555` | ICE — must be reachable for sub-second |
| Bridge UI + REST | `8088` | `/api/vitals`, talk, sounds, config |

> Talk to the bridge **directly**, not via Frigate. Frigate restreams our RTSP,
> which adds a hop + its own jitter buffer. Going straight to our go2rtc WebRTC
> is the lowest latency path.

---

## 1. Video — sub-second WebRTC (go2rtc, WHEP)

Each camera publishes two streams:
- `**<camera>**` — clean H.264 copy (no transcode). **Use this for the app.**
- `**<camera>_overlay**` — same feed with the glass HUD burned in (on-demand,
  transcoded). Use only if you want a server-rendered HUD; otherwise draw the
  overlay yourself from `/api/vitals` (crisper, lower latency).

List streams: `GET http://<host>:1984/api/streams`

### WHEP (recommended for iOS/Swift)
go2rtc speaks **WHEP** (WebRTC-HTTP Egress Protocol) — the modern standard.

```
POST http://<host>:1984/api/webrtc?src=<camera>
Content-Type: application/sdp
Body: <your WebRTC offer SDP>
→ 201 Created, body = answer SDP
```

Swift flow (using Google's `WebRTC` framework / `stasel/WebRTC`):
1. Create `RTCPeerConnection` (recvonly video+audio).
2. `offer(...)` → set local description.
3. POST the offer SDP to the URL above; get the answer SDP.
4. Set remote description = answer. Frames start flowing.

Minimal example:
```swift
var mc = RTCMediaConstraints(mandatoryConstraints:
  ["OfferToReceiveVideo":"true","OfferToReceiveAudio":"true"], optionalConstraints: nil)
pc.offer(for: mc) { sdp, _ in
  pc.setLocalDescription(sdp!) { _ in
    var req = URLRequest(url: URL(string: "http://\(host):1984/api/webrtc?src=owlet")!)
    req.httpMethod = "POST"
    req.setValue("application/sdp", forHTTPHeaderField: "Content-Type")
    req.httpBody = sdp!.sdp.data(using: .utf8)
    URLSession.shared.dataTask(with: req) { data, _, _ in
      let answer = RTCSessionDescription(type: .answer,
        sdp: String(data: data!, encoding: .utf8)!)
      pc.setRemoteDescription(answer) { _ in }
    }.resume()
  }
}
```

### Getting it actually sub-second
WebRTC needs a reachable ICE candidate. Inside Docker, go2rtc may advertise the
wrong IP. Set the bridge env var:
```
OWLET_WEBRTC_CANDIDATE=<bridge-LAN-ip>:8555
```
and make sure host port **8555 (UDP and TCP)** is published. Then the offer/answer
completes immediately and you get ~0.2–0.5 s glass-to-glass on LAN. (You can pass
multiple comma-separated candidates, e.g. LAN + VPN IP.)

### Fallbacks
- **MSE** (low latency, very reliable, great for a quick WKWebView): 
  `ws://<host>:1984/api/ws?src=<camera>` (go2rtc MSE/WebSocket).
- **HLS** (higher latency, universal): `http://<host>:1984/api/stream.m3u8?src=<camera>`
- **RTSP** (for VLC/ffmpeg): `rtsp://<host>:8554/<camera>`

---

## 2. Snapshots (poster / thumbnails)
```
GET http://<host>:1984/api/frame.jpeg?src=<camera>      # current frame JPEG
GET http://<host>:8088/api/snapshot/<camera>            # bridge passthrough
```

---

## 3. Data — `GET http://<host>:8088/api/vitals`

The single feed for your overlay. Add `?units=metric` for °C (default is °F/US).

```json
{
  "ts": 1750000000.0,
  "units": "us",
  "devices": [
    {
      "dsn": "X1234...", "name": "sock", "kind": "sock", "model": "Dream Sock",
      "sensors": {
        "heart_rate": 124, "oxygen": 98, "oxygen_avg": 97,
        "skin_temperature": 99, "sleep_state": 8, "movement": 2,
        "battery": 74, "battery_minutes": 410, "signal_strength": -52,
        "base_station_on": 1, "charging": 0
      }
    },
    {
      "dsn": "owlet", "name": "owlet", "kind": "cam", "model": "Owlet Cam",
      "ts": 1750000000.0,
      "sensors": {
        "temperature": 72, "humidity": 48, "noise": 39,
        "brightness": 150, "motion": 0, "sound": 0, "wifi_rssi": -55
      }
    }
  ]
}
```

Poll every 2–5 s. Values update server-side (~15 s sock, ~10 s cam). `kind` is
`sock`, `cam`, or `device`. Match a sock to a camera by your own pairing (e.g.
let the user assign in-app).

### Field reference

**Sock (`kind:"sock"`)**
| key | meaning | unit / values |
|---|---|---|
| `heart_rate` | heart rate | bpm (null while charging/off-foot) |
| `oxygen` | SpO₂ | % (null while charging/off-foot) |
| `oxygen_avg` | 10-min average SpO₂ | % |
| `skin_temperature` | skin temperature | °F (°C with `units=metric`) |
| `sleep_state` | sleep state | `1`=awake, `8`=light, `15`=deep, `0`=unknown |
| `movement` | movement level | integer |
| `battery` | sock battery | % |
| `battery_minutes` | runtime remaining | minutes |
| `signal_strength` | sock RF signal | dBm |
| `base_station_on` | base powered | `0`/`1` |
| `charging` | on charger | `0`/`1` (when `1`, vitals are null) |

**Camera (`kind:"cam"`)** — read live off the TUTK stream
| key | meaning | unit / values |
|---|---|---|
| `temperature` | room temperature | °F (°C with `units=metric`) |
| `humidity` | room humidity | % RH |
| `noise` | room noise | dB |
| `brightness` | room brightness | lux |
| `motion` | motion detected | `0`/`1` |
| `sound` | sound detected | `0`/`1` |
| `wifi_rssi` | camera WiFi | dBm |

> `temperature`, `noise`, `motion`, `sound` come from the video frame metadata
> (instant). `humidity`, `brightness`, `wifi_rssi` come from a sensor poll
> (~10 s).

### Live cameras + their stream URLs
```
GET http://<host>:8088/api/cameras
```
returns each camera with its `name`, status, and RTSP/WebRTC/HLS URLs.

---

## 4. Two-way audio (talk + sounds)
- **Play a sound out the camera speaker:**
  `POST http://<host>:8088/api/play/<camera>`  body `{"file":"lullaby.mp3"}`
- **Push live audio (walkie-talkie):** `POST http://<host>:8088/api/talk/<camera>`
  with an audio clip (multipart) — re-encoded to the camera's AAC and sent to the
  speaker. `POST /api/talk/<camera>/stop` to stop.
- List/upload/delete sounds: `GET/POST /api/sounds`, `DELETE /api/sounds/<name>`.

(WebRTC bidirectional mic → camera is possible later; today the talk path is the
HTTP endpoint above.)

---

## 5. Home Assistant
Configure in the bridge UI (🏠 Home Assistant card) or via env
(`OWLET_MQTT_HOST`, …). Every sock vital + cam sensor is published with MQTT
discovery, so HA auto-creates the entities (temps in °F). Your app can also just
read HA, but `/api/vitals` is the most direct.

---

## TL;DR for the app
1. Video: `POST http://<host>:1984/api/webrtc?src=<camera>` (WHEP) + set
   `OWLET_WEBRTC_CANDIDATE` for sub-second.
2. Data: poll `GET http://<host>:8088/api/vitals` (°F) and draw your glass HUD.
3. Talk/sounds: `POST /api/talk/<camera>` and `/api/play/<camera>`.
