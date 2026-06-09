import asyncio
import json
import threading
from pathlib import Path

from bleak import BleakClient
from flask import Flask, jsonify, render_template_string, request

from telink_packets import make_command_packet, make_pair_packet, make_session_key


MESH_PATH = Path("hao_deng_mesh.json")
PAIR_CHAR_UUID  = "00010203-0405-0607-0809-0a0b0c0d1914"
CONTROL_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d1912"
OPCODE_SET_COLOR  = 0xE2
OPCODE_SET_STATE  = 0xD0
COLORMODE_RGB     = 0x60
STATEACTION_POWER = 0x01


class LightController:
    """Persistent BLE connection to the first device; forwards commands over the mesh."""

    def __init__(self, mesh):
        self._mesh = mesh
        self._gateway = mesh["devices"][0]
        self._client = None
        self._session_key = None
        self._lock = asyncio.Lock()
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        asyncio.run_coroutine_threadsafe(self._connect(), self._loop)

    # -- connection ----------------------------------------------------------

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
            session_random, reply[1:9]
        )
        print(f"BLE connected → {self._gateway['address']}")

    # -- send ----------------------------------------------------------------

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
                    packet = make_command_packet(
                        self._session_key, self._gateway["address"],
                        mesh_address, opcode, data,
                    )
                    await self._client.write_gatt_char(CONTROL_CHAR_UUID, packet, response=acknowledged)
                    return
                except Exception:
                    self._client = None
                    self._session_key = None
                    if attempt == 1:
                        raise

    def send(self, mesh_address, command, rgb=None):
        acknowledged = command in ("on", "off")
        future = asyncio.run_coroutine_threadsafe(
            self._send(mesh_address, command, rgb, acknowledged),
            self._loop,
        )
        future.result(timeout=12.0)

    @property
    def connected(self):
        return bool(self._client and self._client.is_connected)


app = Flask(__name__)
_controller: LightController | None = None


def get_controller() -> LightController:
    global _controller
    if _controller is None:
        mesh = json.loads(MESH_PATH.read_text(encoding="utf-8"))
        _controller = LightController(mesh)
    return _controller


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
        device = next(
            (d for d in mesh["devices"] if d["name"].lower() == lamp.lower()), None
        )
        if not device:
            return jsonify({"error": f"Unknown lamp: {lamp}"}), 404
        mesh_address = device["meshAddress"]

    try:
        get_controller().send(mesh_address, cmd, tuple(rgb) if rgb else None)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Physical layout — pixel positions match the room map
# (left, top) = top-left corner of each 62px circle
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
# UI
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
    background: #0f0f1a;
    color: #e0e0ff;
    font-family: system-ui, -apple-system, sans-serif;
    padding: 22px;
}
header {
    display: flex; align-items: center; gap: 12px;
    margin-bottom: 18px;
}
h1 { font-size: 1.2rem; letter-spacing: .05em; color: #9090ee; }
#conn-dot {
    width: 10px; height: 10px; border-radius: 50%;
    background: #555; transition: background .4s;
}
#conn-dot.on  { background: #4caf50; }
#conn-dot.off { background: #e53935; }

/* master strip */
.master {
    display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
    background: #1a1a2e; border: 1px solid #2a2a4a; border-radius: 12px;
    padding: 12px 18px; margin-bottom: 24px;
}
.master-label { font-size: .72rem; font-weight: 700; text-transform: uppercase;
                letter-spacing: .1em; color: #6060a0; }
.master-swatch-wrap { position: relative; width: 64px; height: 32px; }
.master-swatch { width: 100%; height: 100%; border-radius: 6px;
                 border: 2px solid #2a2a4a; background: #fff; cursor: pointer; }
.color-input-hidden { position: absolute; inset: 0; opacity: 0;
                      width: 100%; height: 100%; cursor: pointer; }
.hex { font-family: monospace; font-size: .75rem; color: #6060a0; }
button {
    padding: 7px 14px; border: none; border-radius: 7px;
    font-size: .75rem; font-weight: 700; cursor: pointer;
    transition: opacity .15s, transform .1s;
}
button:active  { transform: scale(.95); }
button:disabled { opacity: .35; cursor: not-allowed; transform: none; }
.btn-on  { background: #43a047; color: #fff; }
.btn-off { background: #e53935; color: #fff; }
.st { font-size: .7rem; min-height: 16px; }
.st.ok      { color: #4caf50; }
.st.err     { color: #e53935; }
.st.sending { color: #42a5f5; }

/* physical layout */
#room {
    position: relative;
    width: 460px;
    height: 610px;
    background: #1a1a2e;
    border: 1px solid #2a2a4a;
    border-radius: 16px;
}

/* each lamp */
.lamp {
    position: absolute;
    display: flex; flex-direction: column; align-items: center; gap: 4px;
}
.lamp-circle {
    position: relative;
    width: 62px; height: 62px; border-radius: 50%;
    background: #ffffff;
    border: 3px solid #2a2a4a;
    cursor: pointer;
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
    color: #fff;
    text-shadow: 0 0 4px #000, 0 0 2px #000, 0 1px 3px rgba(0,0,0,.8);
    position: relative; z-index: 1;
}
.lamp-btns { display: flex; gap: 3px; }
.lamp-btns button { padding: 3px 8px; font-size: 10px; border-radius: 4px; }
</style>
</head>
<body>

<header>
    <div id="conn-dot"></div>
    <h1>Hao Deng Control</h1>
</header>

<div class="master">
    <span class="master-label">All</span>
    <div class="master-swatch-wrap">
        <div class="master-swatch" id="swatch-__all__"></div>
        <input class="color-input-hidden" type="color" id="color-__all__" value="#ffffff">
    </div>
    <span class="hex" id="hex-__all__">#ffffff</span>
    <button class="btn-on"  onclick="send('__all__','on')">All ON</button>
    <button class="btn-off" onclick="send('__all__','off')">All OFF</button>
    <span class="st" id="st-__all__"></span>
</div>

<div id="room"></div>

<script>
const LAYOUT = {{ layout|tojson }};

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

// wire every colour input → circle background + hex + debounced send
const timers = {};
function wireColor(id, circleId) {
    const input  = document.getElementById(`color-${id}`);
    const circle = document.getElementById(circleId || `circle-${id}`);
    const hex    = document.getElementById(`hex-${id}`);
    input.addEventListener('input', () => {
        if (circle) circle.style.background = input.value;
        const swatch = document.getElementById(`swatch-${id}`);
        if (swatch) swatch.style.background = input.value;
        if (hex) hex.textContent = input.value;
        clearTimeout(timers[id]);
        timers[id] = setTimeout(() => send(id, 'rgb'), 180);
    });
}
wireColor('__all__');
LAYOUT.forEach(l => wireColor(l.name));

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

async function send(lamp, command) {
    const rgb = command === 'rgb' ? hexToRgb(document.getElementById(`color-${lamp}`).value) : null;
    setSt(lamp, 'sending', '…');
    try {
        const res = await fetch('/api/command', {
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

async function pollStatus() {
    try {
        const r = await fetch('/api/status');
        const d = await r.json();
        document.getElementById('conn-dot').className = d.connected ? 'on' : 'off';
    } catch {}
    setTimeout(pollStatus, 3000);
}
pollStatus();
</script>
</body>
</html>"""


if __name__ == "__main__":
    get_controller()  # connect on startup, not on first request
    print("http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, threaded=True)
