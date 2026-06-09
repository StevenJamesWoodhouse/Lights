import asyncio
import colorsys
import json
import threading
import time
from pathlib import Path

import numpy as np
from bleak import BleakClient
from flask import Flask, jsonify, render_template_string, request

from telink_packets import make_command_packet, make_pair_packet, make_session_key


MESH_PATH = Path("hao_deng_mesh.json")
PAIR_CHAR_UUID    = "00010203-0405-0607-0809-0a0b0c0d1914"
CONTROL_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d1912"
OPCODE_SET_COLOR  = 0xE2
OPCODE_SET_STATE  = 0xD0
COLORMODE_RGB     = 0x60
STATEACTION_POWER = 0x01


# ---------------------------------------------------------------------------
# BLE controller
# ---------------------------------------------------------------------------

class LightController:
    def __init__(self, mesh):
        self._mesh = mesh
        self._gateway = mesh["devices"][0]
        self._client = None
        self._session_key = None
        self._lock = asyncio.Lock()
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        asyncio.run_coroutine_threadsafe(self._connect(), self._loop)

    async def _connect(self):
        async with self._lock:
            await self._do_connect()

    async def _do_connect(self):
        self._client = BleakClient(self._gateway["address"], timeout=10.0)
        await self._client.connect()
        pair_packet, session_random = make_pair_packet(
            self._mesh["meshKey"], self._mesh["meshPassword"]
        )
        await self._client.write_gatt_char(PAIR_CHAR_UUID, pair_packet, response=True)
        await asyncio.sleep(0.1)
        reply = bytes(await self._client.read_gatt_char(PAIR_CHAR_UUID))
        if not reply or reply[0] != 0x0D:
            raise RuntimeError(f"auth rejected: {reply.hex() if reply else 'empty'}")
        self._session_key = make_session_key(
            self._mesh["meshKey"], self._mesh["meshPassword"],
            session_random, reply[1:9],
        )
        print(f"BLE connected → {self._gateway['address']}")

    async def _send_one(self, mesh_address, opcode, data):
        """Must be called while the lock is already held."""
        packet = make_command_packet(
            self._session_key, self._gateway["address"],
            mesh_address, opcode, data,
        )
        await self._client.write_gatt_char(CONTROL_CHAR_UUID, packet, response=False)

    async def _send(self, mesh_address, command, rgb, acknowledged):
        async with self._lock:
            for attempt in range(2):
                try:
                    if not (self._client and self._client.is_connected):
                        await self._do_connect()
                    dt = self._gateway["deviceType"]
                    if command == "on":
                        opcode, data = OPCODE_SET_STATE, bytes([dt, STATEACTION_POWER, 1])
                    elif command == "off":
                        opcode, data = OPCODE_SET_STATE, bytes([dt, STATEACTION_POWER, 0])
                    else:
                        r, g, b = rgb
                        opcode, data = OPCODE_SET_COLOR, bytes([dt, COLORMODE_RGB, r, g, b])
                    await self._send_one(mesh_address, opcode, data)
                    return
                except Exception:
                    self._client = None
                    self._session_key = None
                    if attempt == 1:
                        raise

    async def _send_many(self, commands):
        """Send multiple (mesh_address, opcode, data) with one lock acquisition."""
        async with self._lock:
            for attempt in range(2):
                try:
                    if not (self._client and self._client.is_connected):
                        await self._do_connect()
                    for mesh_address, opcode, data in commands:
                        await self._send_one(mesh_address, opcode, data)
                    return
                except Exception:
                    self._client = None
                    self._session_key = None
                    if attempt == 1:
                        raise

    def send(self, mesh_address, command, rgb=None):
        acknowledged = command in ("on", "off")
        asyncio.run_coroutine_threadsafe(
            self._send(mesh_address, command, rgb, acknowledged), self._loop
        ).result(timeout=12.0)

    def send_many(self, commands):
        asyncio.run_coroutine_threadsafe(
            self._send_many(commands), self._loop
        ).result(timeout=12.0)

    @property
    def connected(self):
        return bool(self._client and self._client.is_connected)


# ---------------------------------------------------------------------------
# Audio reactor
# ---------------------------------------------------------------------------

class AudioReactor:
    """
    Two modes:
      brightness — overall RMS drives all-lamp brightness (original)
      spectrum   — FFT bands drive individual lamps sorted by x-position,
                   each coloured by its frequency (bass=red → treble=violet)
    """

    SPECTRUM_CHUNK  = 2048   # larger window → better low-freq resolution
    BRIGHTNESS_CHUNK = 512

    def __init__(self, controller: LightController, mesh: dict, lamp_layout: list):
        self._controller  = controller
        self._device_type = mesh["devices"][0]["deviceType"]

        # Lamps sorted left→right for frequency mapping
        addr_map = {d["name"]: d["meshAddress"] for d in mesh["devices"]}
        self._bands = [
            {"mesh_address": addr_map[l["name"]], "name": l["name"]}
            for l in sorted(lamp_layout, key=lambda x: x["left"])
        ]
        n = len(self._bands)

        # State
        self.enabled       = False
        self.mode          = "brightness"   # "brightness" | "spectrum"
        self.source        = "loopback"     # "loopback"   | "microphone"
        self.base_color    = (255, 255, 255)
        self.sensitivity   = 1.0
        self.min_brightness = 0.05
        self.smoothing     = 0.15
        self.current_level = 0.0

        self._brightness   = 0.0
        self._band_levels  = np.zeros(n)
        self._error: str | None = None
        self._audio_thread: threading.Thread | None = None
        self._send_thread:  threading.Thread | None = None

    # -- public API ----------------------------------------------------------

    def start(self):
        self._error = None
        self.enabled = True
        self._brightness = 0.0
        self._band_levels[:] = 0.0
        self._audio_thread = threading.Thread(target=self._audio_run, daemon=True)
        self._send_thread  = threading.Thread(target=self._send_run,  daemon=True)
        self._audio_thread.start()
        self._send_thread.start()

    def stop(self):
        self.enabled = False

    @property
    def error(self):
        return self._error

    # -- audio capture -------------------------------------------------------

    @staticmethod
    def _find_loopback(pa):
        import pyaudiowpatch as pyaudio
        info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_out = pa.get_device_info_by_index(info["defaultOutputDevice"])
        for i in range(pa.get_device_count()):
            dev = pa.get_device_info_by_index(i)
            if dev.get("isLoopbackDevice") and default_out["name"] in dev["name"]:
                return i, dev
        raise RuntimeError("WASAPI loopback device not found — ensure a default output device is active")

    def _audio_run(self):
        try:
            import pyaudiowpatch as pyaudio
        except ImportError as exc:
            self._error = f"Missing package: {exc}. Run: pip install pyaudiowpatch"
            self.enabled = False
            return

        pa = pyaudio.PyAudio()
        try:
            if self.source == "microphone":
                dev_info = pa.get_default_input_device_info()
                dev_idx  = int(dev_info["index"])
                sr       = int(dev_info["defaultSampleRate"])
                ch       = min(int(dev_info["maxInputChannels"]), 2)
            else:
                dev_idx, dev_info = self._find_loopback(pa)
                sr = int(dev_info["defaultSampleRate"])
                ch = dev_info["maxInputChannels"]

            chunk  = self.SPECTRUM_CHUNK if self.mode == "spectrum" else self.BRIGHTNESS_CHUNK
            stream = pa.open(
                format=pyaudio.paFloat32,
                channels=ch, rate=sr,
                input=True, input_device_index=dev_idx,
                frames_per_buffer=chunk,
            )

            alpha  = self.smoothing
            db_min, db_max = -45.0, -5.0

            while self.enabled:
                raw     = stream.read(chunk, exception_on_overflow=False)
                samples = np.frombuffer(raw, dtype=np.float32)
                if ch > 1:
                    samples = samples.reshape(-1, ch).mean(axis=1)

                if self.mode == "spectrum":
                    energies = self._compute_bands(samples, sr)
                    self._band_levels = self._band_levels * (1 - alpha) + energies * alpha
                    self.current_level = float(self._band_levels.mean())
                else:
                    rms   = float(np.sqrt(np.mean(samples ** 2)))
                    db    = 20.0 * np.log10(max(rms, 1e-6))
                    norm  = (db - db_min) / (db_max - db_min)
                    norm  = max(0.0, min(1.0, norm * self.sensitivity))
                    self._brightness   = self._brightness * (1 - alpha) + norm * alpha
                    self.current_level = self._brightness

            stream.stop_stream()
            stream.close()
        except Exception as exc:
            self._error = str(exc)
            self.enabled = False
        finally:
            pa.terminate()

    def _compute_bands(self, samples: np.ndarray, sr: int) -> np.ndarray:
        """Log-spaced FFT energy per band, normalised to 0–1."""
        n      = len(samples)
        mag    = np.abs(np.fft.rfft(samples * np.hanning(n))) / n * 2
        freqs  = np.fft.rfftfreq(n, 1.0 / sr)
        n_bands = len(self._bands)
        edges  = np.logspace(np.log10(60), np.log10(16000), n_bands + 1)
        ref    = 0.04  # magnitude at which a band hits full brightness

        result = np.zeros(n_bands)
        for i in range(n_bands):
            mask = (freqs >= edges[i]) & (freqs < edges[i + 1])
            if mask.any():
                result[i] = min(1.0, float(np.mean(mag[mask])) / ref * self.sensitivity)
        return result

    @staticmethod
    def _band_to_rgb(band_idx: int, n_bands: int, brightness: float):
        """Map band index to a rainbow hue (red=bass, violet=treble) at given brightness."""
        hue = (band_idx / max(n_bands - 1, 1)) * 0.75  # 0.0 red → 0.75 violet
        r, g, b = colorsys.hsv_to_rgb(hue, 1.0, brightness)
        return int(r * 255), int(g * 255), int(b * 255)

    # -- BLE send ------------------------------------------------------------

    def _send_run(self):
        dt = self._device_type
        n  = len(self._bands)
        while self.enabled:
            if self.mode == "spectrum":
                commands = []
                for i, band in enumerate(self._bands):
                    level = max(self.min_brightness, float(self._band_levels[i]))
                    r, g, b = self._band_to_rgb(i, n, level)
                    commands.append((
                        band["mesh_address"],
                        OPCODE_SET_COLOR,
                        bytes([dt, COLORMODE_RGB, r, g, b]),
                    ))
                try:
                    self._controller.send_many(commands)
                except Exception:
                    pass
            else:
                br = max(self.min_brightness, self._brightness)
                r0, g0, b0 = self.base_color
                color = (int(r0 * br), int(g0 * br), int(b0 * br))
                try:
                    self._controller.send(0xFFFF, "rgb", color)
                except Exception:
                    pass
            time.sleep(0.08)


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)
_controller:    LightController | None = None
_audio_reactor: AudioReactor    | None = None


def get_controller() -> LightController:
    global _controller
    if _controller is None:
        mesh = json.loads(MESH_PATH.read_text(encoding="utf-8"))
        _controller = LightController(mesh)
    return _controller


def get_audio_reactor() -> AudioReactor:
    global _audio_reactor
    if _audio_reactor is None:
        mesh = json.loads(MESH_PATH.read_text(encoding="utf-8"))
        _audio_reactor = AudioReactor(get_controller(), mesh, LAMP_LAYOUT)
    return _audio_reactor


# ---------------------------------------------------------------------------
# Physical layout
# ---------------------------------------------------------------------------

LAMP_LAYOUT = [
    {"name": "Lamp4",  "left": 114, "top":  19},
    {"name": "Lamp7",  "left": 312, "top":  19},
    {"name": "Lamp8",  "left":  19, "top": 136},
    {"name": "Lamp9",  "left": 189, "top": 136},
    {"name": "Lamp12", "left": 379, "top": 136},
    {"name": "Lamp10", "left": 102, "top": 247},
    {"name": "Lamp5",  "left": 296, "top": 247},
    {"name": "Lamp3",  "left":  27, "top": 379},
    {"name": "Lamp11", "left": 197, "top": 379},
    {"name": "Lamp1",  "left": 379, "top": 379},
    {"name": "Lamp6",  "left": 110, "top": 499},
    {"name": "Lamp2",  "left": 304, "top": 499},
]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(HTML, layout=LAMP_LAYOUT)


@app.route("/api/status")
def status():
    return jsonify({"connected": get_controller().connected})


@app.route("/api/command", methods=["POST"])
def command():
    data = request.get_json(force=True)
    lamp = data.get("lamp")
    cmd  = data.get("command")
    rgb  = data.get("rgb")

    if not lamp or not cmd:
        return jsonify({"error": "lamp and command required"}), 400

    mesh = json.loads(MESH_PATH.read_text(encoding="utf-8"))

    if lamp == "__all__":
        mesh_address = 0xFFFF
    else:
        device = next((d for d in mesh["devices"] if d["name"].lower() == lamp.lower()), None)
        if not device:
            return jsonify({"error": f"Unknown lamp: {lamp}"}), 404
        mesh_address = device["meshAddress"]

    try:
        get_controller().send(mesh_address, cmd, tuple(rgb) if rgb else None)
        ar = get_audio_reactor()
        if lamp == "__all__" and cmd == "rgb" and rgb and ar.enabled:
            ar.base_color = tuple(rgb)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/audio", methods=["POST"])
def audio_control():
    data = request.get_json(force=True)
    ar   = get_audio_reactor()

    ar.sensitivity    = float(data.get("sensitivity",    ar.sensitivity))
    ar.min_brightness = float(data.get("min_brightness", ar.min_brightness))
    ar.smoothing      = float(data.get("smoothing",      ar.smoothing))
    if "base_color" in data:
        ar.base_color = tuple(data["base_color"])

    mode_changed   = "mode"   in data and data["mode"]   != ar.mode
    source_changed = "source" in data and data["source"] != ar.source
    if "mode"   in data: ar.mode   = data["mode"]
    if "source" in data: ar.source = data["source"]

    want_enabled = bool(data.get("enabled", ar.enabled))
    needs_restart = ar.enabled and (mode_changed or source_changed)

    if needs_restart:
        ar.stop()
        time.sleep(0.15)
        ar.start()
    elif want_enabled and not ar.enabled:
        ar.start()
        time.sleep(0.15)
    elif not want_enabled and ar.enabled:
        ar.stop()

    if ar.error:
        return jsonify({"ok": False, "error": ar.error}), 500
    return jsonify({"ok": True, "enabled": ar.enabled})


@app.route("/api/audio/level")
def audio_level():
    ar = get_audio_reactor()
    return jsonify({"level": ar.current_level, "enabled": ar.enabled})


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hao Deng Control</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    background: #0f0f1a; color: #e0e0ff;
    font-family: system-ui, -apple-system, sans-serif;
    padding: 22px;
}
header { display: flex; align-items: center; gap: 12px; margin-bottom: 18px; }
h1 { font-size: 1.2rem; letter-spacing: .05em; color: #9090ee; }
#conn-dot {
    width: 10px; height: 10px; border-radius: 50%;
    background: #555; transition: background .4s;
}
#conn-dot.on  { background: #4caf50; }
#conn-dot.off { background: #e53935; }

.panel {
    display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
    background: #1a1a2e; border: 1px solid #2a2a4a; border-radius: 12px;
    padding: 12px 18px; margin-bottom: 14px;
}
.panel-label {
    font-size: .72rem; font-weight: 700; text-transform: uppercase;
    letter-spacing: .1em; color: #6060a0; white-space: nowrap;
}
.divider { width: 1px; height: 24px; background: #2a2a4a; flex-shrink: 0; }

.swatch-wrap { position: relative; width: 64px; height: 32px; }
.swatch { width: 100%; height: 100%; border-radius: 6px;
          border: 2px solid #2a2a4a; background: #fff; cursor: pointer; }
.color-hidden { position: absolute; inset: 0; opacity: 0;
                width: 100%; height: 100%; cursor: pointer; }
.hex { font-family: monospace; font-size: .75rem; color: #6060a0; }

button {
    padding: 7px 12px; border: none; border-radius: 7px;
    font-size: .75rem; font-weight: 700; cursor: pointer;
    transition: opacity .15s, transform .1s;
}
button:active   { transform: scale(.95); }
button:disabled { opacity: .35; cursor: not-allowed; transform: none; }
.btn-on      { background: #43a047; color: #fff; }
.btn-off     { background: #e53935; color: #fff; }
.btn-toggle  { background: #37474f; color: #aaa; }
.btn-toggle.active { background: #0288d1; color: #fff; }
.btn-opt     { background: #263238; color: #90a4ae; }
.btn-opt.active    { background: #37474f; color: #fff; }

.slider-group { display: flex; align-items: center; gap: 5px; }
.slider-group label { font-size: .7rem; color: #6060a0; white-space: nowrap; }
input[type=range] { width: 80px; accent-color: #9090ee; cursor: pointer; }
.slider-val { font-size: .7rem; color: #9090ee; min-width: 30px; }

.vu-wrap { height: 10px; width: 100px; background: #2a2a4a; border-radius: 5px; overflow: hidden; }
.vu-fill {
    height: 100%; width: 0%;
    background: linear-gradient(to right, #4caf50 0%, #ff9800 70%, #f44336 100%);
    transition: width .08s linear; border-radius: 5px;
}
.err-text { font-size: .7rem; color: #e53935; }

/* room */
#room {
    position: relative; width: 460px; height: 610px;
    background: #1a1a2e; border: 1px solid #2a2a4a; border-radius: 16px;
}
.lamp { position: absolute; display: flex; flex-direction: column; align-items: center; gap: 4px; }
.lamp-circle {
    position: relative; width: 62px; height: 62px; border-radius: 50%;
    background: #ffffff; border: 3px solid #2a2a4a; cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    transition: border-color .2s, box-shadow .2s;
}
.lamp-circle:hover { border-color: #9090ee; box-shadow: 0 0 12px rgba(144,144,238,.4); }
.lamp-circle input[type=color] {
    position: absolute; inset: 0; opacity: 0;
    width: 100%; height: 100%; cursor: pointer; border-radius: 50%;
}
.lamp-num {
    font-size: 15px; font-weight: 700; pointer-events: none; user-select: none;
    color: #fff; position: relative; z-index: 1;
    text-shadow: 0 0 4px #000, 0 0 2px #000, 0 1px 3px rgba(0,0,0,.8);
}
.lamp-btns { display: flex; gap: 3px; }
.lamp-btns button { padding: 3px 8px; font-size: 10px; border-radius: 4px; }
.st { font-size: .7rem; min-height: 16px; }
.st.ok      { color: #4caf50; }
.st.err     { color: #e53935; }
.st.sending { color: #42a5f5; }
</style>
</head>
<body>

<header>
    <div id="conn-dot"></div>
    <h1>Hao Deng Control</h1>
</header>

<!-- master -->
<div class="panel">
    <span class="panel-label">All</span>
    <div class="swatch-wrap">
        <div class="swatch" id="swatch-__all__"></div>
        <input class="color-hidden" type="color" id="color-__all__" value="#ffffff">
    </div>
    <span class="hex" id="hex-__all__">#ffffff</span>
    <button class="btn-on"  onclick="send('__all__','on')">All ON</button>
    <button class="btn-off" onclick="send('__all__','off')">All OFF</button>
    <span class="st" id="st-__all__"></span>
</div>

<!-- audio -->
<div class="panel">
    <span class="panel-label">Audio</span>

    <button class="btn-toggle" id="audio-btn" onclick="toggleAudio()">🔊 Off</button>

    <div class="divider"></div>

    <button class="btn-opt active" id="mode-brightness" onclick="setMode('brightness')">Brightness</button>
    <button class="btn-opt"        id="mode-spectrum"   onclick="setMode('spectrum')">🌈 Spectrum</button>

    <div class="divider"></div>

    <button class="btn-opt active" id="src-loopback"   onclick="setSource('loopback')">🖥 Desktop</button>
    <button class="btn-opt"        id="src-microphone" onclick="setSource('microphone')">🎤 Mic</button>

    <div class="divider"></div>

    <div class="slider-group">
        <label>Sensitivity</label>
        <input type="range" id="sens" min="25" max="400" value="100" oninput="onSlider()">
        <span class="slider-val" id="sens-val">1.0×</span>
    </div>
    <div class="slider-group">
        <label>Min</label>
        <input type="range" id="minbr" min="0" max="50" value="5" oninput="onSlider()">
        <span class="slider-val" id="minbr-val">5%</span>
    </div>
    <div class="slider-group">
        <label>Smooth</label>
        <input type="range" id="smooth" min="5" max="60" value="15" oninput="onSlider()">
        <span class="slider-val" id="smooth-val">0.15</span>
    </div>

    <div class="vu-wrap"><div class="vu-fill" id="vu-fill"></div></div>
    <span class="err-text" id="audio-err"></span>
</div>

<!-- room layout -->
<div id="room"></div>

<script>
const LAYOUT = {{ layout|tojson }};

// build lamp circles
const room = document.getElementById('room');
LAYOUT.forEach(lamp => {
    const el = document.createElement('div');
    el.className = 'lamp';
    el.style.left = lamp.left + 'px';
    el.style.top  = lamp.top  + 'px';
    const num = lamp.name.replace('Lamp', '');
    el.innerHTML = `
        <div class="lamp-circle" id="circle-${lamp.name}">
            <span class="lamp-num">${num}</span>
            <input type="color" id="color-${lamp.name}" value="#ffffff">
        </div>
        <div class="lamp-btns">
            <button class="btn-on"  onclick="send('${lamp.name}','on')">on</button>
            <button class="btn-off" onclick="send('${lamp.name}','off')">off</button>
        </div>
        <span class="st" id="st-${lamp.name}"></span>
    `;
    room.appendChild(el);
});

// colour wiring
const timers = {};

const allInput = document.getElementById('color-__all__');
allInput.addEventListener('input', () => {
    const v = allInput.value;
    document.getElementById('swatch-__all__').style.background = v;
    document.getElementById('hex-__all__').textContent = v;
    LAYOUT.forEach(l => {
        document.getElementById(`color-${l.name}`).value = v;
        document.getElementById(`circle-${l.name}`).style.background = v;
    });
    clearTimeout(timers['__all__']);
    timers['__all__'] = setTimeout(() => send('__all__', 'rgb'), 180);
});

LAYOUT.forEach(l => {
    const input  = document.getElementById(`color-${l.name}`);
    const circle = document.getElementById(`circle-${l.name}`);
    input.addEventListener('input', () => {
        circle.style.background = input.value;
        clearTimeout(timers[l.name]);
        timers[l.name] = setTimeout(() => send(l.name, 'rgb'), 180);
    });
});

// helpers
function hexToRgb(hex) {
    const v = hex.replace('#', '');
    return [parseInt(v.slice(0,2),16), parseInt(v.slice(2,4),16), parseInt(v.slice(4,6),16)];
}
function setSt(id, cls, msg) {
    const el = document.getElementById(`st-${id}`);
    if (!el) return;
    el.className = `st ${cls}`;
    el.textContent = msg;
    if (cls === 'ok') setTimeout(() => { if (el.textContent === msg) el.textContent = ''; }, 2000);
}

// commands
async function send(lamp, command) {
    const rgb = command === 'rgb' ? hexToRgb(document.getElementById(`color-${lamp}`).value) : null;
    setSt(lamp, 'sending', '…');
    try {
        const res  = await fetch('/api/command', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({lamp, command, rgb}),
        });
        const data = await res.json();
        setSt(lamp, data.ok ? 'ok' : 'err', data.ok ? '✓' : (data.error || 'Error'));
    } catch {
        setSt(lamp, 'err', 'Network error');
    }
}

// audio state
let audioEnabled = false;
let audioMode    = 'brightness';
let audioSource  = 'loopback';

async function toggleAudio() {
    audioEnabled = !audioEnabled;
    await pushAudio();
}
function setMode(m) {
    audioMode = m;
    document.getElementById('mode-brightness').className = 'btn-opt' + (m === 'brightness' ? ' active' : '');
    document.getElementById('mode-spectrum').className   = 'btn-opt' + (m === 'spectrum'   ? ' active' : '');
    if (audioEnabled) pushAudio();
}
function setSource(s) {
    audioSource = s;
    document.getElementById('src-loopback').className   = 'btn-opt' + (s === 'loopback'    ? ' active' : '');
    document.getElementById('src-microphone').className = 'btn-opt' + (s === 'microphone'  ? ' active' : '');
    if (audioEnabled) pushAudio();
}
function onSlider() {
    document.getElementById('sens-val').textContent  = (document.getElementById('sens').value  / 100).toFixed(2) + '×';
    document.getElementById('minbr-val').textContent = document.getElementById('minbr').value  + '%';
    document.getElementById('smooth-val').textContent = (document.getElementById('smooth').value / 100).toFixed(2);
    if (audioEnabled) pushAudio();
}
async function pushAudio() {
    try {
        const res  = await fetch('/api/audio', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                enabled:        audioEnabled,
                mode:           audioMode,
                source:         audioSource,
                sensitivity:    document.getElementById('sens').value    / 100,
                min_brightness: document.getElementById('minbr').value   / 100,
                smoothing:      document.getElementById('smooth').value  / 100,
                base_color:     hexToRgb(document.getElementById('color-__all__').value),
            }),
        });
        const data = await res.json();
        audioEnabled = data.enabled ?? audioEnabled;
        const btn = document.getElementById('audio-btn');
        btn.textContent = audioEnabled ? '🔊 On' : '🔊 Off';
        btn.className   = 'btn-toggle' + (audioEnabled ? ' active' : '');
        document.getElementById('audio-err').textContent = data.error || '';
    } catch {
        document.getElementById('audio-err').textContent = 'Network error';
    }
}

// VU meter — in spectrum mode, show per-lamp colours from the server level
(async function pollLevel() {
    try {
        const res  = await fetch('/api/audio/level');
        const data = await res.json();
        document.getElementById('vu-fill').style.width = (data.level * 100).toFixed(1) + '%';
    } catch {}
    setTimeout(pollLevel, 100);
})();

// BLE dot
(async function pollStatus() {
    try {
        const r = await fetch('/api/status');
        const d = await r.json();
        document.getElementById('conn-dot').className = d.connected ? 'on' : 'off';
    } catch {}
    setTimeout(pollStatus, 3000);
})();
</script>
</body>
</html>"""


if __name__ == "__main__":
    get_controller()
    print("http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, threaded=True)
