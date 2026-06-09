import argparse
import asyncio
import json
from pathlib import Path

from bleak import BleakClient, BleakScanner

from telink_packets import encrypt_block, make_command_packet, make_pair_packet, make_session_key


MESH_PATH = Path("hao_deng_mesh.json")
PAIR_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d1914"
CONTROL_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d1912"
OPCODE_MESH_RESET = 0xE3

DEFAULT_MESH = {
    "name": "default",
    "meshKey": "ZenggeMesh",
    "meshPassword": "ZenggeTechnology",
    "meshLTK": "ZenggeTechnology",
}


def load_mesh(path):
    mesh = json.loads(path.read_text(encoding="utf-8"))
    mesh["name"] = mesh.get("displayName") or "app"
    return mesh


def credentials_from_mesh(mesh):
    return {
        "name": mesh.get("name") or mesh.get("displayName") or "mesh",
        "meshKey": mesh["meshKey"],
        "meshPassword": mesh["meshPassword"],
        "meshLTK": mesh["meshLTK"],
    }


def label(credentials):
    return f"{credentials['name']}:{credentials['meshKey']}/{credentials['meshPassword']}"


async def resolve_ble_target(address, timeout):
    device = await BleakScanner.find_device_by_address(address, timeout=timeout)
    return device if device is not None else address


async def login(client, credentials):
    pair_packet, session_random = make_pair_packet(
        credentials["meshKey"], credentials["meshPassword"]
    )
    await client.write_gatt_char(PAIR_CHAR_UUID, pair_packet, response=True)
    await asyncio.sleep(0.15)
    reply = bytes(await client.read_gatt_char(PAIR_CHAR_UUID))
    if not reply or reply[0] != 0x0D:
        raise RuntimeError(f"auth rejected: {reply.hex() if reply else 'empty'}")
    return make_session_key(
        credentials["meshKey"],
        credentials["meshPassword"],
        session_random,
        reply[1:9],
    )


async def try_login(address, credentials, scan_timeout, timeout):
    target = await resolve_ble_target(address, scan_timeout)
    async with BleakClient(target, timeout=timeout) as client:
        await login(client, credentials)


async def find_current_credentials(address, candidates, scan_timeout, timeout):
    errors = []
    for credentials in candidates:
        for attempt in range(1, find_current_credentials.attempts + 1):
            try:
                await try_login(address, credentials, scan_timeout, timeout)
                return credentials, errors
            except Exception as exc:
                errors.append(f"{label(credentials)} attempt {attempt} -> {exc}")
                await asyncio.sleep(0.4)
    return None, errors


find_current_credentials.attempts = 1


async def write_mesh_field(client, session_key, field_id, value):
    encrypted = encrypt_block(session_key, value.encode("ascii"))
    await client.write_gatt_char(PAIR_CHAR_UUID, bytes([field_id]) + bytes(encrypted))


async def set_mesh_credentials(device, source, target, scan_timeout, timeout):
    target_device = await resolve_ble_target(device["address"], scan_timeout)
    async with BleakClient(target_device, timeout=timeout) as client:
        session_key = await login(client, source)
        await write_mesh_field(client, session_key, 0x04, target["meshKey"])
        await write_mesh_field(client, session_key, 0x05, target["meshPassword"])
        await write_mesh_field(client, session_key, 0x06, target["meshLTK"])
        await asyncio.sleep(1.0)
        reply = bytes(await client.read_gatt_char(PAIR_CHAR_UUID))
        if not reply or reply[0] != 0x07:
            raise RuntimeError(f"provision rejected: {reply.hex() if reply else 'empty'}")


async def send_mesh_reset(device, source, scan_timeout, timeout):
    target_device = await resolve_ble_target(device["address"], scan_timeout)
    async with BleakClient(target_device, timeout=timeout) as client:
        session_key = await login(client, source)
        packet = make_command_packet(
            session_key,
            device["address"],
            device["meshAddress"],
            OPCODE_MESH_RESET,
            b"\x00",
        )
        await client.write_gatt_char(CONTROL_CHAR_UUID, packet, response=True)
        await asyncio.sleep(1.0)


async def send_broadcast_mesh_reset(devices, source, scan_timeout, timeout):
    errors = []
    for device in devices:
        try:
            target_device = await resolve_ble_target(device["address"], scan_timeout)
            async with BleakClient(target_device, timeout=timeout) as client:
                session_key = await login(client, source)
                packet = make_command_packet(
                    session_key,
                    device["address"],
                    0xFFFF,
                    OPCODE_MESH_RESET,
                    b"\x00",
                )
                await client.write_gatt_char(CONTROL_CHAR_UUID, packet, response=True)
                await asyncio.sleep(1.0)
                return {
                    "name": "__all__",
                    "address": "broadcast",
                    "ok": True,
                    "action": f"broadcast reset sent via {device['name']}",
                }
        except Exception as exc:
            errors.append(f"{device['name']} -> {exc}")
    return {
        "name": "__all__",
        "address": "broadcast",
        "ok": False,
        "action": "broadcast reset failed",
        "error": "; ".join(errors[-3:]),
    }


async def restore_one(device, candidates, args):
    address = device["address"]
    current, errors = await find_current_credentials(
        address,
        candidates,
        args.scan_timeout,
        args.timeout,
    )
    if current is None:
        return {
            "name": device["name"],
            "address": address,
            "ok": False,
            "action": "not found/auth failed",
            "error": "; ".join(errors[-2:]),
        }

    if current["meshKey"] == DEFAULT_MESH["meshKey"] and current["meshPassword"] == DEFAULT_MESH["meshPassword"]:
        return {
            "name": device["name"],
            "address": address,
            "ok": True,
            "action": "already default",
            "source": label(current),
        }

    try:
        last_error = None
        for attempt in range(1, args.attempts + 1):
            try:
                await set_mesh_credentials(
                    device,
                    current,
                    DEFAULT_MESH,
                    args.scan_timeout,
                    args.timeout,
                )
                await asyncio.sleep(0.4)
                await try_login(address, DEFAULT_MESH, args.scan_timeout, args.timeout)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(0.8)
        if last_error is not None:
            raise last_error
        return {
            "name": device["name"],
            "address": address,
            "ok": True,
            "action": "restored default",
            "source": label(current),
        }
    except Exception as exc:
        return {
            "name": device["name"],
            "address": address,
            "ok": False,
            "action": "restore failed",
            "source": label(current),
            "error": str(exc),
        }


async def reset_one(device, args):
    last_error = None
    for attempt in range(1, args.attempts + 1):
        try:
            await send_mesh_reset(device, DEFAULT_MESH, args.scan_timeout, args.timeout)
            await asyncio.sleep(0.5)
            await try_login(device["address"], DEFAULT_MESH, args.scan_timeout, args.timeout)
            last_error = None
            break
        except Exception as exc:
            last_error = exc
            await asyncio.sleep(0.8)
    if last_error is None:
        return {
            "name": device["name"],
            "address": device["address"],
            "ok": True,
            "action": "reset command sent",
        }
    return {
        "name": device["name"],
        "address": device["address"],
        "ok": False,
        "action": "reset failed",
        "error": str(last_error),
    }


async def restore_all(devices, candidates, args):
    results = []
    for index, device in enumerate(devices, start=1):
        print(f"[{index:02}/{len(devices):02}] {device['name']} {device['address']} ...", flush=True)
        result = await restore_one(device, candidates, args)
        results.append(result)
        print_result(result)
    return results


async def reset_all(devices, args):
    if not args.per_lamp_reset:
        print("[all] broadcast reset ...", flush=True)
        result = await send_broadcast_mesh_reset(
            devices,
            DEFAULT_MESH,
            args.scan_timeout,
            args.timeout,
        )
        print_result(result)
        return [result]

    results = []
    for index, device in enumerate(devices, start=1):
        print(f"[{index:02}/{len(devices):02}] {device['name']} reset ...", flush=True)
        result = await reset_one(device, args)
        results.append(result)
        print_result(result)
    return results


def print_result(result):
    status = "OK" if result["ok"] else "FAIL"
    source = f" from {result['source']}" if result.get("source") else ""
    error = f" | {result['error']}" if result.get("error") else ""
    print(f"  {status}: {result['action']}{source}{error}", flush=True)


def print_summary(results):
    failures = [item for item in results if not item["ok"]]
    print()
    print(f"Summary: {len(results) - len(failures)}/{len(results)} ok")
    if failures:
        print("Failures:")
        for item in failures:
            print(f"  - {item['name']} {item['address']}: {item.get('error', item['action'])}")
    print()
    return failures


def ask(prompt):
    try:
        return input(prompt).strip().lower()
    except EOFError:
        return ""


async def main_async(args):
    mesh = load_mesh(args.mesh)
    app_credentials = credentials_from_mesh(mesh)
    candidates = [DEFAULT_MESH, app_credentials]
    devices = mesh["devices"]
    if args.lamp:
        refs = {value.strip().lower() for value in args.lamp.split(",")}
        devices = [
            device for device in devices
            if device["name"].lower() in refs
            or device["address"].lower() in refs
            or str(device["meshAddress"]) in refs
        ]
        if not devices:
            raise SystemExit(f"No devices matched --lamp {args.lamp!r}")

    while True:
        results = await restore_all(devices, candidates, args)
        failures = print_summary(results)

        if args.aggressive_reset:
            reset_results = await reset_all(devices, args)
            reset_failures = print_summary(reset_results)
            failures = failures or reset_failures

        if failures and not args.loop:
            raise SystemExit(1)

        if args.loop:
            answer = ask("Test the remote now. Is it working? [y/N/r=retry/a=aggressive reset] ")
            if answer in ("y", "yes"):
                return
            if answer in ("a", "aggressive"):
                reset_results = await reset_all(devices, args)
                print_summary(reset_results)
                answer = ask("Test the remote again. Is it working? [y/N] ")
                if answer in ("y", "yes"):
                    return
            retry = ask("Retry restore pass? [Y/n] ")
            if retry in ("n", "no"):
                raise SystemExit(1)
        else:
            return


def main():
    parser = argparse.ArgumentParser(
        description="Restore all Hao Deng/Zengge lamps to default Zengge mesh credentials."
    )
    parser.add_argument("--mesh", type=Path, default=MESH_PATH)
    parser.add_argument("--scan-timeout", type=float, default=10.0)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--lamp", help="Restore only one lamp or a comma-separated list of names, addresses, or mesh addresses.")
    parser.add_argument("--aggressive-reset", action="store_true", help="After restoring credentials, send the mesh reset opcode too.")
    parser.add_argument("--per-lamp-reset", action="store_true", help="Use the slower one-connection-per-lamp reset instead of one broadcast reset.")
    parser.add_argument("--loop", action="store_true", help="Prompt for remote test and retry until confirmed.")
    args = parser.parse_args()
    find_current_credentials.attempts = max(1, args.attempts)
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
