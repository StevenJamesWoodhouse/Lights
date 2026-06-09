import asyncio
import colorsys
import json
import threading
import time
from pathlib import Path

import numpy as np
from bleak import BleakClient, BleakScanner
from flask import Flask, jsonify, render_template_string, request

from telink_packets import encrypt_block, make_command_packet, make_pair_packet, make_session_key


MESH_PATH = Path("hao_deng_mesh.json")
PAIR_CHAR_UUID    = "00010203-0405-0607-0809-0a0b0c0d1914"
CONTROL_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d1912"
OPCODE_SET_COLOR  = 0xE2
OPCODE_SET_STATE  = 0xD0
OPCODE_MESH_RESET = 0xE3
OPCODE_SET_MESH_ADDRESS = 0xE0
COLORMODE_RGB     = 0x60
STATEACTION_POWER = 0x01
CONTROL_DEVICE_TYPE = 0xFF
DEFAULT_MESH = {
    "meshKey": "ZenggeMesh",
    "meshPassword": "ZenggeTechnology",
    "meshLTK": "ZenggeTechnology",
}
PROVISION_SETTLE_SECONDS = 1.0
PROVISION_SCAN_SECONDS = 10.0
PROVISION_CONCURRENCY = 3


def mesh_credentials(mesh):
    return {
        "meshKey": mesh["meshKey"],
        "meshPassword": mesh["meshPassword"],
        "meshLTK": mesh["meshLTK"],
    }


async def login_client(client, credentials):
    pair_packet, session_random = make_pair_packet(
        credentials["meshKey"], credentials["meshPassword"]
    )
    await client.write_gatt_char(PAIR_CHAR_UUID, pair_packet, response=True)
    await asyncio.sleep(0.1)
    reply = bytes(await client.read_gatt_char(PAIR_CHAR_UUID))
    if not reply or reply[0] != 0x0D:
        raise RuntimeError(f"auth rejected: {reply.hex() if reply else 'empty'}")
    return make_session_key(
        credentials["meshKey"],
        credentials["meshPassword"],
        session_random,
        reply[1:9],
    )


async def scan_ble_targets(devices, timeout=PROVISION_SCAN_SECONDS):
    wanted = {device["address"].upper(): device for device in devices}
    found = {}
    discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
    for address, pair in discovered.items():
        if address.upper() in wanted:
            found[address.upper()] = pair[0]
    return found


async def resolve_ble_target(address, timeout=8.0, targets=None):
    if targets and address.upper() in targets:
        return targets[address.upper()]
    device = await BleakScanner.find_device_by_address(address, timeout=timeout)
    return device if device is not None else address


async def try_login(address, credentials, timeout=10.0, targets=None):
    target = await resolve_ble_target(address, timeout=min(timeout, 8.0), targets=targets)
    async with BleakClient(target, timeout=timeout) as client:
        await login_client(client, credentials)


async def write_mesh_field(client, session_key, field_id, value):
    data = encrypt_block(session_key, value.encode("ascii"))
    await client.write_gatt_char(PAIR_CHAR_UUID, bytes([field_id]) + bytes(data))


async def provision_device(device, source_credentials, target_credentials, timeout=12.0, targets=None):
    address = device["address"]
    target = await resolve_ble_target(address, timeout=min(timeout, 8.0), targets=targets)
    async with BleakClient(target, timeout=timeout) as client:
        session_key = await login_client(client, source_credentials)
        await write_mesh_field(client, session_key, 0x04, target_credentials["meshKey"])
        await write_mesh_field(client, session_key, 0x05, target_credentials["meshPassword"])
        await write_mesh_field(client, session_key, 0x06, target_credentials["meshLTK"])
        await asyncio.sleep(PROVISION_SETTLE_SECONDS)
        reply = bytes(await client.read_gatt_char(PAIR_CHAR_UUID))
        if not reply or reply[0] != 0x07:
            raise RuntimeError(f"provision rejected: {reply.hex() if reply else 'empty'}")


def default_mesh_address(device):
    return int(device["address"].split(":")[-1], 16)


async def set_mesh_address(device, credentials, dest, timeout=12.0, targets=None):
    target = await resolve_ble_target(device["address"], timeout=min(timeout, 8.0), targets=targets)
    async with BleakClient(target, timeout=timeout) as client:
        session_key = await login_client(client, credentials)
        packet = make_command_packet(
            session_key,
            device["address"],
            dest,
            OPCODE_SET_MESH_ADDRESS,
            int(device["meshAddress"]).to_bytes(2, "little"),
        )
        await client.write_gatt_char(CONTROL_CHAR_UUID, packet, response=True)
        await asyncio.sleep(0.2)


async def ensure_configured_mesh_address(device, credentials, timeout=12.0, targets=None):
    errors = []
    for dest in (int(device["meshAddress"]), default_mesh_address(device)):
        try:
            await set_mesh_address(device, credentials, dest, timeout, targets)
            return
        except Exception as exc:
            errors.append(f"dest 0x{dest:02X}: {exc}")
    raise RuntimeError("; ".join(errors))


async def ensure_device_mesh(
    device,
    target_credentials,
    fallback_credentials,
    timeout=12.0,
    set_address=False,
    targets=None,
    check_target=True,
):
    address = device["address"]
    if check_target:
        try:
            await try_login(address, target_credentials, timeout, targets)
            if set_address:
                await ensure_configured_mesh_address(device, target_credentials, timeout, targets)
            return "already"
        except Exception:
            pass

    await provision_device(device, fallback_credentials, target_credentials, timeout, targets)
    await asyncio.sleep(0.3)
    await try_login(address, target_credentials, timeout, targets)
    if set_address:
        await ensure_configured_mesh_address(device, target_credentials, timeout, targets)
    return "provisioned"


async def ensure_mesh(
    devices,
    target_credentials,
    fallback_credentials,
    timeout=12.0,
    raise_on_failure=True,
    set_addresses=False,
    concurrency=PROVISION_CONCURRENCY,
    check_target=True,
):
    targets = await scan_ble_targets(devices)
    semaphore = asyncio.Semaphore(concurrency)

    async def worker(index, device):
        async with semaphore:
            return index, await ensure_one(device)

    async def ensure_one(device):
        try:
            action = await ensure_device_mesh(
                device,
                target_credentials,
                fallback_credentials,
                timeout,
                set_addresses,
                targets,
                check_target,
            )
            result = {"name": device["name"], "ok": True, "action": action}
            print(f"{device['name']} {action} -> {target_credentials['meshKey']}")
        except Exception as exc:
            result = {"name": device["name"], "ok": False, "error": str(exc)}
            print(f"{device['name']} provision failed: {exc}")
        return result

    ordered = await asyncio.gather(
        *(worker(index, device) for index, device in enumerate(devices))
    )
    results = [result for _, result in sorted(ordered, key=lambda item: item[0])]
    failures = [item for item in results if not item["ok"]]
    if failures and raise_on_failure:
        names = ", ".join(f"{item['name']} ({item['error']})" for item in failures)
        raise RuntimeError(f"Provisioning failed for: {names}")
    return results


def provision_failures(results):
    return [item for item in results if not item.get("ok")]


async def send_default_mesh_reset(devices, timeout=12.0):
    errors = []
    for device in devices:
        try:
            target = await resolve_ble_target(device["address"], timeout=min(timeout, 8.0))
            async with BleakClient(target, timeout=timeout) as client:
                session_key = await login_client(client, DEFAULT_MESH)
                packet = make_command_packet(
                    session_key,
                    device["address"],
                    0xFFFF,
                    OPCODE_MESH_RESET,
                    b"\x00",
                )
                await client.write_gatt_char(CONTROL_CHAR_UUID, packet, response=True)
                await asyncio.sleep(1.0)
                print(f"Broadcast default mesh reset via {device['name']}")
                return {
                    "name": "__all__",
                    "ok": True,
                    "action": f"broadcast reset via {device['name']}",
                }
        except Exception as exc:
            errors.append(f"{device['name']}: {exc}")
    return {
        "name": "__all__",
        "ok": False,
        "action": "broadcast reset failed",
        "error": "; ".join(errors[-3:]),
    }


async def gateway_accepts_mesh(mesh, credentials, timeout=12.0):
    gateway = mesh["devices"][0]
    await try_login(gateway["address"], credentials, timeout)
    return [{"name": gateway["name"], "ok": True, "action": "gateway already"}]


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
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._last_error = None

    async def _connect(self):
        async with self._lock:
            await self._do_connect()

    async def _do_connect(self):
        if self._client and self._client.is_connected:
            return
        target = await resolve_ble_target(self._gateway["address"])
        self._client = BleakClient(target, timeout=10.0)
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

    async def _disconnect(self):
        async with self._lock:
            client = self._client
            self._client = None
            self._session_key = None
            if client and client.is_connected:
                await client.disconnect()
                print(f"BLE disconnected -> {self._gateway['address']}")

    def connect(self):
        future = asyncio.run_coroutine_threadsafe(self._connect(), self._loop)
        try:
            future.result(timeout=15.0)
            self._last_error = None
        except Exception as exc:
            self._last_error = str(exc)
            try:
                asyncio.run_coroutine_threadsafe(self._disconnect(), self._loop).result(timeout=5.0)
            except Exception:
                pass
            raise

    def disconnect(self):
        future = asyncio.run_coroutine_threadsafe(self._disconnect(), self._loop)
        try:
            future.result(timeout=15.0)
        except Exception as exc:
            self._last_error = str(exc)
            raise

    def shutdown(self):
        try:
            if self._loop.is_running():
                self.disconnect()
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=2.0)

    async def _send(self, mesh_address, command, rgb, acknowledged):
        async with self._lock:
            for attempt in range(2):
                try:
                    if not (self._client and self._client.is_connected):
                        await self._do_connect()
                    if command == "on":
                        opcode, data = OPCODE_SET_STATE, bytes([CONTROL_DEVICE_TYPE, STATEACTION_POWER, 1])
                    elif command == "off":
                        opcode, data = OPCODE_SET_STATE, bytes([CONTROL_DEVICE_TYPE, STATEACTION_POWER, 0])
                    else:
                        r, g, b = rgb
                        opcode, data = OPCODE_SET_COLOR, bytes([CONTROL_DEVICE_TYPE, COLORMODE_RGB, r, g, b])
                    packet = make_command_packet(
                        self._session_key, self._gateway["address"],
                        mesh_address, opcode, data,
                    )
                    await self._client.write_gatt_char(CONTROL_CHAR_UUID, packet, response=acknowledged)
                    return
                except Exception:
                    client = self._client
                    self._client = None
                    self._session_key = None
                    if client and client.is_connected:
                        await client.disconnect()
                    if attempt == 1:
                        self._last_error = "send failed"
                        raise

    def send(self, mesh_address, command, rgb=None):
        acknowledged = command in ("on", "off")
        asyncio.run_coroutine_threadsafe(
            self._send(mesh_address, command, rgb, acknowledged), self._loop
        ).result(timeout=12.0)

    @property
    def connected(self):
        return bool(self._client and self._client.is_connected)

    @property
    def last_error(self):
        return self._last_error


# ---------------------------------------------------------------------------
# Audio reactor
# ---------------------------------------------------------------------------
# All three modes use a single broadcast command (0xFFFF) so every lamp
# updates simultaneously without per-lamp BLE overhead.
#
#  pulse  — RMS → brightness, base colour preserved
#  color  — spectral centroid → hue (bass=red, treble=violet), RMS → brightness
#  beat   — onset detection fires a bright flash whose hue reflects the
#            spectral character; brightness decays until the next hit

class AudioReactor:
    CHUNK = 1024   # ~23 ms at 44100 Hz — snappy response across all modes

    def __init__(self, controller: LightController):
        self._controller = controller

        self.enabled        = False
        self.mode           = "pulse"     # "pulse" | "color" | "beat"
        self.source         = "loopback"  # "loopback" | "microphone"
        self.base_color     = (255, 255, 255)
        self.sensitivity    = 1.0
        self.min_brightness = 0.05
        self.smoothing      = 0.35        # EMA alpha — higher = snappier
        self.current_level  = 0.0        # 0-1 for VU meter

        # shared state written by audio thread, read by send thread
        self._brightness  = 0.0
        self._hue         = 0.0   # color / beat mode
        self._beat_bright = 0.0   # beat mode flash level
        self._beat_avg    = 0.001 # slow-moving RMS reference for onset detection
        self._last_beat   = 0.0

        self._error: str | None = None
        self._audio_thread: threading.Thread | None = None
        self._send_thread:  threading.Thread | None = None

    # -- public ---------------------------------------------------------------

    def start(self):
        self._error       = None
        self.enabled      = True
        self._brightness  = 0.0
        self._beat_bright = 0.0
        self._beat_avg    = 0.001
        self._audio_thread = threading.Thread(target=self._audio_run, daemon=True)
        self._send_thread  = threading.Thread(target=self._send_run,  daemon=True)
        self._audio_thread.start()
        self._send_thread.start()

    def stop(self, join=False):
        self.enabled = False
        if join:
            current = threading.current_thread()
            for thread in (self._audio_thread, self._send_thread):
                if thread and thread.is_alive() and thread is not current:
                    thread.join(timeout=2.0)

    @property
    def error(self):
        return self._error

    # -- audio ----------------------------------------------------------------

    @staticmethod
    def _find_loopback(pa):
        import pyaudiowpatch as pyaudio
        info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_out = pa.get_device_info_by_index(info["defaultOutputDevice"])
        for i in range(pa.get_device_count()):
            dev = pa.get_device_info_by_index(i)
            if dev.get("isLoopbackDevice") and default_out["name"] in dev["name"]:
                return i, dev
        raise RuntimeError("WASAPI loopback not found — ensure a default output device is active")

    @staticmethod
    def _spectral_centroid_hue(samples: np.ndarray, sr: int) -> float:
        """Returns a hue in [0, 0.75] tracking spectral centroid on a log scale."""
        n     = len(samples)
        mag   = np.abs(np.fft.rfft(samples * np.hanning(n)))
        freqs = np.fft.rfftfreq(n, 1.0 / sr)
        total = np.sum(mag) + 1e-10
        centroid = float(np.sum(freqs * mag) / total)
        # log-map 150 Hz (bass) → 0.0 (red) … 8000 Hz (treble) → 0.75 (violet)
        norm = (np.log10(max(centroid, 150)) - np.log10(150)) / (np.log10(8000) - np.log10(150))
        return float(np.clip(norm * 0.75, 0.0, 0.75))

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

            stream = pa.open(
                format=pyaudio.paFloat32,
                channels=ch, rate=sr,
                input=True, input_device_index=dev_idx,
                frames_per_buffer=self.CHUNK,
            )

            DB_MIN, DB_MAX = -45.0, -5.0

            while self.enabled:
                raw     = stream.read(self.CHUNK, exception_on_overflow=False)
                samples = np.frombuffer(raw, dtype=np.float32)
                if ch > 1:
                    samples = samples.reshape(-1, ch).mean(axis=1)

                alpha = self.smoothing
                rms   = float(np.sqrt(np.mean(samples ** 2)))
                db    = 20.0 * np.log10(max(rms, 1e-6))
                vol   = float(np.clip((db - DB_MIN) / (DB_MAX - DB_MIN) * self.sensitivity, 0.0, 1.0))

                if self.mode == "pulse":
                    self._brightness  = self._brightness * (1 - alpha) + vol * alpha
                    self.current_level = self._brightness

                elif self.mode == "color":
                    target_hue = self._spectral_centroid_hue(samples, sr)
                    # hue transitions at half the speed of brightness so it doesn't flicker
                    self._hue        = self._hue       * (1 - alpha * 0.4) + target_hue * (alpha * 0.4)
                    self._brightness = self._brightness * (1 - alpha)       + vol         * alpha
                    self.current_level = self._brightness

                elif self.mode == "beat":
                    # onset: current RMS > 1.5× slow-moving average, min 120 ms gap
                    self._beat_avg = self._beat_avg * 0.97 + rms * 0.03
                    beat = (rms > self._beat_avg * 1.5 and
                            time.time() - self._last_beat > 0.12)
                    if beat:
                        self._last_beat  = time.time()
                        self._beat_hue   = self._spectral_centroid_hue(samples, sr)
                        # max brightness clipped to sensitivity, always vivid on hit
                        self._beat_bright = min(1.0, vol * 1.8 * self.sensitivity)
                    else:
                        # exponential decay between beats — tweak 0.88 for longer/shorter tail
                        self._beat_bright *= 0.88
                    self.current_level = self._beat_bright

            stream.stop_stream()
            stream.close()
        except Exception as exc:
            self._error = str(exc)
            self.enabled = False
        finally:
            pa.terminate()

    # -- send -----------------------------------------------------------------

    def _send_run(self):
        while self.enabled:
            try:
                mode = self.mode
                if mode == "pulse":
                    br = max(self.min_brightness, self._brightness)
                    r0, g0, b0 = self.base_color
                    color = (int(r0 * br), int(g0 * br), int(b0 * br))

                elif mode == "color":
                    br = max(self.min_brightness, self._brightness)
                    r, g, b = colorsys.hsv_to_rgb(self._hue, 1.0, br)
                    color = (int(r * 255), int(g * 255), int(b * 255))

                else:  # beat
                    br = max(self.min_brightness, self._beat_bright)
                    r, g, b = colorsys.hsv_to_rgb(self._beat_hue, 1.0, br)
                    color = (int(r * 255), int(g * 255), int(b * 255))

                self._controller.send(0xFFFF, "rgb", color)
            except Exception:
                pass
            # ~25 fps cap; actual rate is limited by BLE round-trip (~15 fps)
            time.sleep(0.04)


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)
_controller:    LightController | None = None
_audio_reactor: AudioReactor    | None = None
_control_lock = threading.Lock()
_provisioned_active = False
_control_phase = "stopped"
_last_provision = []
_shutdown_started = False


def load_mesh():
    return json.loads(MESH_PATH.read_text(encoding="utf-8"))


def get_controller() -> LightController | None:
    return _controller


def start_control() -> LightController:
    global _controller, _provisioned_active, _control_phase, _last_provision
    with _control_lock:
        mesh = load_mesh()
        active_credentials = mesh_credentials(mesh)
        if not _provisioned_active:
            _control_phase = "checking app mesh"
            try:
                _last_provision = asyncio.run(
                    gateway_accepts_mesh(mesh, active_credentials)
                )
            except Exception:
                _control_phase = "provisioning app mesh"
                try:
                    _last_provision = asyncio.run(
                        ensure_mesh(
                            mesh["devices"],
                            active_credentials,
                            DEFAULT_MESH,
                            set_addresses=True,
                            check_target=False,
                        )
                    )
                except Exception:
                    _control_phase = "provisioning app mesh (mixed state retry)"
                    _last_provision = asyncio.run(
                        ensure_mesh(
                            mesh["devices"],
                            active_credentials,
                            DEFAULT_MESH,
                            set_addresses=True,
                        )
                    )
            _provisioned_active = True
        _control_phase = "connecting"
        if _controller is None:
            _controller = LightController(mesh)
        _controller.connect()
        _control_phase = "active"
        return _controller


def stop_control(force_restore=False):
    global _controller, _audio_reactor, _provisioned_active, _control_phase, _last_provision
    with _control_lock:
        mesh = load_mesh()
        active_credentials = mesh_credentials(mesh)
        if _audio_reactor is not None:
            _control_phase = "stopping audio"
            _audio_reactor.stop(join=True)
            _audio_reactor = None
        if _controller is not None:
            _control_phase = "disconnecting"
            _controller.shutdown()
            _controller = None
        if _provisioned_active or force_restore:
            _control_phase = "restoring default mesh"
            _last_provision = asyncio.run(
                ensure_mesh(
                    mesh["devices"],
                    DEFAULT_MESH,
                    active_credentials,
                    raise_on_failure=False,
                )
            )
            failures = provision_failures(_last_provision)
            if not failures:
                reset_result = asyncio.run(send_default_mesh_reset(mesh["devices"]))
                _last_provision.append(reset_result)
                failures = provision_failures(_last_provision)
            _provisioned_active = bool(failures)
            _control_phase = "restore incomplete" if failures else "stopped"
            return _last_provision
        _control_phase = "stopped"
        return _last_provision


def require_controller() -> LightController:
    controller = get_controller()
    if controller is None or not controller.connected:
        raise RuntimeError("Connection is stopped. Press Start Control first.")
    return controller


def get_audio_reactor() -> AudioReactor:
    global _audio_reactor
    if _audio_reactor is None:
        _audio_reactor = AudioReactor(require_controller())
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
    controller = get_controller()
    return jsonify({
        "active": controller is not None,
        "connected": bool(controller and controller.connected),
        "provisioned": _provisioned_active,
        "phase": _control_phase,
        "provision": _last_provision,
        "error": controller.last_error if controller else None,
    })


@app.route("/api/connect", methods=["POST"])
def connect_control():
    try:
        controller = start_control()
        return jsonify({
            "ok": True,
            "connected": controller.connected,
            "phase": _control_phase,
            "provision": _last_provision,
        })
    except Exception as exc:
        return jsonify({
            "ok": False,
            "connected": False,
            "phase": _control_phase,
            "provision": _last_provision,
            "error": str(exc),
        }), 500


@app.route("/api/disconnect", methods=["POST"])
def disconnect_control():
    try:
        results = stop_control(force_restore=True)
        failures = provision_failures(results)
        return jsonify({
            "ok": not failures,
            "connected": False,
            "phase": _control_phase,
            "provision": results,
            "error": None if not failures else "Restore incomplete: " + ", ".join(
                item["name"] for item in failures
            ),
        })
    except Exception as exc:
        return jsonify({
            "ok": False,
            "connected": False,
            "phase": _control_phase,
            "provision": _last_provision,
            "error": str(exc),
        }), 500


@app.route("/api/command", methods=["POST"])
def command():
    data = request.get_json(force=True)
    lamp = data.get("lamp")
    cmd  = data.get("command")
    rgb  = data.get("rgb")

    if not lamp or not cmd:
        return jsonify({"error": "lamp and command required"}), 400

    mesh = load_mesh()
    if lamp == "__all__":
        mesh_address = 0xFFFF
    else:
        device = next((d for d in mesh["devices"] if d["name"].lower() == lamp.lower()), None)
        if not device:
            return jsonify({"error": f"Unknown lamp: {lamp}"}), 404
        mesh_address = device["meshAddress"]

    try:
        require_controller().send(mesh_address, cmd, tuple(rgb) if rgb else None)
        ar = _audio_reactor
        if ar and lamp == "__all__" and cmd == "rgb" and rgb and ar.enabled:
            ar.base_color = tuple(rgb)
        return jsonify({"ok": True})
    except Exception as exc:
        status_code = 409 if "Connection is stopped" in str(exc) else 500
        return jsonify({"error": str(exc)}), status_code


@app.route("/api/audio", methods=["POST"])
def audio_control():
    data = request.get_json(force=True)
    try:
        ar = get_audio_reactor()
    except Exception as exc:
        return jsonify({"ok": False, "enabled": False, "error": str(exc)}), 409

    ar.sensitivity    = float(data.get("sensitivity",    ar.sensitivity))
    ar.min_brightness = float(data.get("min_brightness", ar.min_brightness))
    ar.smoothing      = float(data.get("smoothing",      ar.smoothing))
    if "base_color" in data:
        ar.base_color = tuple(data["base_color"])

    needs_restart = ar.enabled and (
        ("mode"   in data and data["mode"]   != ar.mode) or
        ("source" in data and data["source"] != ar.source)
    )
    if "mode"   in data: ar.mode   = data["mode"]
    if "source" in data: ar.source = data["source"]

    want = bool(data.get("enabled", ar.enabled))
    if needs_restart:
        ar.stop(); time.sleep(0.15); ar.start()
    elif want and not ar.enabled:
        ar.start(); time.sleep(0.15)
    elif not want and ar.enabled:
        ar.stop()

    if ar.error:
        return jsonify({"ok": False, "error": ar.error}), 500
    return jsonify({"ok": True, "enabled": ar.enabled})


@app.route("/api/audio/level")
def audio_level():
    ar = _audio_reactor
    if ar is None:
        return jsonify({"level": 0.0, "enabled": False})
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
.conn-text { font-size: .75rem; color: #9090ee; min-width: 90px; }
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
.btn-on     { background: #43a047; color: #fff; }
.btn-off    { background: #e53935; color: #fff; }
.btn-toggle { background: #37474f; color: #aaa; }
.btn-toggle.active { background: #0288d1; color: #fff; }
.btn-opt    { background: #263238; color: #90a4ae; }
.btn-opt.active    { background: #37474f; color: #e0e0ff; }

.slider-group { display: flex; align-items: center; gap: 5px; }
.slider-group label { font-size: .7rem; color: #6060a0; white-space: nowrap; }
input[type=range] { width: 80px; accent-color: #9090ee; cursor: pointer; }
.slider-val { font-size: .7rem; color: #9090ee; min-width: 30px; }

.vu-wrap { height: 10px; width: 100px; background: #2a2a4a; border-radius: 5px; overflow: hidden; }
.vu-fill {
    height: 100%; width: 0%;
    background: linear-gradient(to right, #4caf50 0%, #ff9800 70%, #f44336 100%);
    transition: width .06s linear; border-radius: 5px;
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
    transition: background .06s, border-color .2s, box-shadow .2s;
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

/* mode descriptions shown below audio panel */
.mode-hint {
    font-size: .7rem; color: #5050a0; margin-top: -10px; margin-bottom: 14px;
    padding-left: 4px;
}
</style>
</head>
<body>

<header>
    <div id="conn-dot"></div>
    <h1>Hao Deng Control</h1>
</header>

<!-- connection -->
<div class="panel">
    <span class="panel-label">Control</span>
    <button class="btn-on" id="connect-btn" onclick="startControl()">Start Control</button>
    <button class="btn-off" id="disconnect-btn" onclick="stopControl()" disabled>Stop Control</button>
    <span class="conn-text" id="conn-text">Stopped</span>
</div>

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

    <button class="btn-opt active" id="mode-pulse" onclick="setMode('pulse')">Pulse</button>
    <button class="btn-opt"        id="mode-color" onclick="setMode('color')">🎨 Color</button>
    <button class="btn-opt"        id="mode-beat"  onclick="setMode('beat')">⚡ Beat</button>

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
        <input type="range" id="smooth" min="5" max="80" value="35" oninput="onSlider()">
        <span class="slider-val" id="smooth-val">0.35</span>
    </div>

    <div class="vu-wrap"><div class="vu-fill" id="vu-fill"></div></div>
    <span class="err-text" id="audio-err"></span>
</div>
<div class="mode-hint" id="mode-hint">Volume controls brightness. Base colour set by the All picker above.</div>

<!-- room -->
<div id="room"></div>

<script>
const LAYOUT = {{ layout|tojson }};

const MODE_HINTS = {
    pulse: 'Volume controls brightness. Base colour set by the All picker above.',
    color: 'Spectral centroid shifts hue in real time: bass → red/orange, treble → blue/violet.',
    beat:  'Onset detection fires a flash on each beat. Hue reflects the spectral character of the hit.',
};

let controlConnected = false;

function setControlState(connected, error, phase) {
    controlConnected = !!connected;
    document.getElementById('conn-dot').className = controlConnected ? 'on' : 'off';
    document.getElementById('conn-text').textContent = error || phase || (controlConnected ? 'Active' : 'Stopped');
    document.getElementById('connect-btn').disabled = controlConnected;
    document.getElementById('disconnect-btn').disabled = !controlConnected;
    document.querySelectorAll('button').forEach(btn => {
        if (btn.id !== 'connect-btn' && btn.id !== 'disconnect-btn') {
            btn.disabled = !controlConnected;
        }
    });
    document.querySelectorAll('input[type=color]').forEach(input => {
        input.disabled = !controlConnected;
    });
    if (!controlConnected) {
        audioEnabled = false;
        const btn = document.getElementById('audio-btn');
        if (btn) {
            btn.textContent = 'Off';
            btn.className = 'btn-toggle';
        }
        const vu = document.getElementById('vu-fill');
        if (vu) vu.style.width = '0%';
    }
}

async function startControl() {
    document.getElementById('conn-text').textContent = 'Provisioning...';
    try {
        const res = await fetch('/api/connect', {method: 'POST'});
        const data = await res.json();
        setControlState(data.connected, data.error, data.phase);
    } catch {
        setControlState(false, 'Network error');
    }
}

async function stopControl() {
    document.getElementById('conn-text').textContent = 'Restoring...';
    try {
        const res = await fetch('/api/disconnect', {method: 'POST'});
        const data = await res.json();
        setControlState(false, data.error, data.phase);
    } catch {
        setControlState(false, 'Network error');
    }
}

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
    if (!controlConnected) {
        setSt(lamp, 'err', 'Stopped');
        return;
    }
    const rgb = command === 'rgb' ? hexToRgb(document.getElementById(`color-${lamp}`).value) : null;
    setSt(lamp, 'sending', '…');
    try {
        const res  = await fetch('/api/command', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({lamp, command, rgb}),
        });
        const data = await res.json();
        setSt(lamp, data.ok ? 'ok' : 'err', data.ok ? '✓' : (data.error || 'Error'));
    } catch { setSt(lamp, 'err', 'Network error'); }
}

// audio controls
let audioEnabled = false;
let audioMode    = 'pulse';
let audioSource  = 'loopback';

async function toggleAudio() {
    if (!controlConnected) {
        document.getElementById('audio-err').textContent = 'Start Control first';
        return;
    }
    audioEnabled = !audioEnabled;
    await pushAudio();
}
function setMode(m) {
    audioMode = m;
    ['pulse','color','beat'].forEach(id => {
        document.getElementById(`mode-${id}`).className = 'btn-opt' + (m === id ? ' active' : '');
    });
    document.getElementById('mode-hint').textContent = MODE_HINTS[m] || '';
    if (audioEnabled) pushAudio();
}
function setSource(s) {
    audioSource = s;
    ['loopback','microphone'].forEach(id => {
        document.getElementById(`src-${id}`).className = 'btn-opt' + (s === id ? ' active' : '');
    });
    if (audioEnabled) pushAudio();
}
function onSlider() {
    document.getElementById('sens-val').textContent  = (document.getElementById('sens').value   / 100).toFixed(2) + '×';
    document.getElementById('minbr-val').textContent =  document.getElementById('minbr').value  + '%';
    document.getElementById('smooth-val').textContent = (document.getElementById('smooth').value / 100).toFixed(2);
    if (audioEnabled) pushAudio();
}
async function pushAudio() {
    if (!controlConnected) {
        audioEnabled = false;
        document.getElementById('audio-err').textContent = 'Start Control first';
        return;
    }
    try {
        const res  = await fetch('/api/audio', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                enabled:        audioEnabled,
                mode:           audioMode,
                source:         audioSource,
                sensitivity:    document.getElementById('sens').value   / 100,
                min_brightness: document.getElementById('minbr').value  / 100,
                smoothing:      document.getElementById('smooth').value / 100,
                base_color:     hexToRgb(document.getElementById('color-__all__').value),
            }),
        });
        const data = await res.json();
        audioEnabled = data.enabled ?? audioEnabled;
        const btn = document.getElementById('audio-btn');
        btn.textContent = audioEnabled ? '🔊 On' : '🔊 Off';
        btn.className   = 'btn-toggle' + (audioEnabled ? ' active' : '');
        document.getElementById('audio-err').textContent = data.error || '';
    } catch { document.getElementById('audio-err').textContent = 'Network error'; }
}

(async function pollLevel() {
    if (!controlConnected) {
        const vu = document.getElementById('vu-fill');
        if (vu) vu.style.width = '0%';
        setTimeout(pollLevel, 1000);
        return;
    }
    try {
        const res  = await fetch('/api/audio/level');
        const data = await res.json();
        document.getElementById('vu-fill').style.width = (data.level * 100).toFixed(1) + '%';
    } catch {}
    setTimeout(pollLevel, audioEnabled ? 80 : 1000);
})();

(async function pollStatus() {
    try {
        const r = await fetch('/api/status');
        const d = await r.json();
        setControlState(d.connected, d.error, d.phase);
    } catch {}
    setTimeout(pollStatus, 3000);
})();
</script>
</body>
</html>"""


def shutdown_control():
    global _shutdown_started
    if _shutdown_started:
        return
    _shutdown_started = True
    try:
        stop_control()
    except Exception as exc:
        print(f"Failed to release BLE control cleanly: {exc}")


if __name__ == "__main__":
    print("http://127.0.0.1:5000")
    try:
        app.run(host="127.0.0.1", port=5000, threaded=True)
    finally:
        shutdown_control()
