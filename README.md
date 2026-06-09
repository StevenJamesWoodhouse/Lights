# Smart Light Discovery

This workspace starts with discovery-only Bluetooth LE scanning. The first goal is
to identify the 12 matching lights before any controller code connects to them or
changes state.

## Scan

```powershell
python .\ble_discover.py --seconds 60
```

The scanner listens for BLE advertisements only. It does not connect to devices
and does not write any characteristics.

Scan results are saved in `scans/` as JSON and CSV. Devices with repeated names,
manufacturer IDs, or service UUIDs are the most useful candidates when looking
for multiple lights of the same brand.

## Next Steps

1. Run scans from a few positions around the property.
2. Compare repeated fingerprints until the 12 lights stand out.
3. Once identified, connect to one test light and inspect GATT services.
4. Add control code only after we know the brand/protocol and have a safe test
   device.

## Read-Only GATT Inspection

```powershell
python .\gatt_inspect.py F8:1D:78:69:B3:A3
```

This connects to one chosen light and lists its services and characteristics. By
default it does not read values and never writes. Add `--read` when you want to
read characteristics that explicitly advertise read support.

## Safe Controller Shell

```powershell
python .\lights_controller.py list
python .\lights_controller.py identify light-01
python .\lights_controller.py identify light-01 --read-custom
```

The controller shell has the 12 discovered `ZenggeMesh` devices in `lights.json`.
State-changing commands are present as disabled placeholders until the mesh
command/security layer is confirmed.

## Colour Monitor

```powershell
python .\monitor_colours.py
python .\monitor_colours.py --once --timeout 6
```

This loops through all 12 lights, reads the known readable custom
characteristics, and redraws the same 12 terminal rows each time. It is read-only:
it connects and reads, but never writes. At the moment the readable custom values
do not expose an obvious RGB colour, so the monitor shows `unknown` plus raw
state bytes until the mesh command/status format is decoded.

## Fast Event Monitor

```powershell
python .\monitor_events.py
python .\monitor_events.py --notify-light light-01
```

The event monitor avoids slow per-light GATT polling. It passively watches BLE
advertisements for all 12 lights and can also subscribe to notifications from one
chosen light. Use this while changing a light from the vendor app to see whether
state changes appear in advertisements or notify packets.

## Authenticated Status Probe

```powershell
python .\auth_status_probe.py light-01
python .\auth_status_probe.py light-01 --credential ZenggeMesh:1234
```

This tries the known Telink/AwoX-style mesh auth flow, then decrypts the status
characteristic and parses mode, RGB, colour brightness, white brightness, and
white temperature. It performs authentication/session writes only and sends no
state-changing control commands.

## Authenticated Zengge Status Monitor

```powershell
python .\monitor_zengge_status.py
python .\monitor_zengge_status.py --gateway light-01 --seconds 30
```

This uses the Zengge default `ZenggeMesh:ZenggeTechnology`, enables encrypted
notifications, and sends status-query packets only. It does not send power,
brightness, colour, mesh reset, or mesh provisioning commands.

For protocol diagnostics:

```powershell
python .\zengge_status_diagnostics.py light-01
```

This tries several safe status/notify request variants and prints raw plus
decrypted characteristic values after each one.

## One-Light Control

```powershell
python .\zengge_control.py rgb light-01 "#FF0000"
python .\zengge_control.py on light-01
python .\zengge_control.py off light-01
```

This authenticates with `ZenggeMesh:ZenggeTechnology` and sends a single command
to the selected light's likely mesh address.

## Hao Deng Cloud Mesh Pull

```powershell
python .\hao_deng_cloud.py "email@example.com" "password" --country GB
```

This logs into the same Hao Deng/Magic Hue cloud API used by the app and saves
mesh keys, mesh passwords, LTKs, and per-device mesh addresses to
`hao_deng_meshes.json`.

## Variant Sweep

```powershell
python .\zengge_variant_sweep.py --response --notify-each --delay 0.8
```

This connects once, then tries labelled command variants with distinct colours.
If a fixture changes, stop the script and note the printed step.

## Restore Remote Control

```powershell
python .\restore_default_mesh.py --aggressive-reset --attempts 5 --scan-timeout 12 --timeout 18
```

This restores the lamps to the default Zengge mesh credentials and then sends
the mesh reset opcode. That reset step is required for the physical remote to
work again; credential restore alone is not enough. By default the reset is sent
as a single broadcast command after all lamps verify on the default mesh, which
is much faster than opening one BLE connection per lamp.

The UI's Start Control path is optimized for the normal post-remote state: it
does one BLE scan, provisions lamps into the Hao Deng app mesh with a small
parallel worker pool, restores mesh addresses from `hao_deng_mesh.json`, and
falls back to a slower mixed-state retry if a previous run was interrupted.

Useful recovery options:

```powershell
python .\restore_default_mesh.py --lamp Lamp8 --attempts 5 --scan-timeout 12 --timeout 18
python .\restore_default_mesh.py --aggressive-reset --per-lamp-reset --attempts 5 --scan-timeout 12 --timeout 18
python .\restore_default_mesh.py --loop --aggressive-reset
```

Use `--lamp` for a flaky or out-of-range fixture. Use `--per-lamp-reset` only
if the fast broadcast reset does not restore remote control.

## Physical Identification

```powershell
python .\identify_physical_light.py
python .\identify_physical_light.py --decode
```

Use this to prove which BLE device maps to which physical fixture. Move the PC
near a fixture or power-cycle a single fixture and watch RSSI, age, and seen
counts change.
