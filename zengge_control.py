import argparse
import asyncio
import json
from pathlib import Path

from bleak import BleakClient, BleakScanner

from telink_packets import make_command_packet, make_pair_packet, make_session_key


INVENTORY_PATH = Path("lights.json")
PAIR_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d1914"
CONTROL_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d1912"
NOTIFY_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d1911"

DEFAULT_CREDENTIAL = ("ZenggeMesh", "ZenggeTechnology")
DEFAULT_MESH_ID = 0x0211
DEVICE_TYPE = 0xFF

OPCODE_SET_COLOR = 0xE2
OPCODE_SET_STATE = 0xD0
OPCODE_SET_MESH_ADDRESS = 0xE0
COLORMODE_RGB = 0x60
STATEACTION_POWER = 0x01


def load_inventory(path):
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_light(inventory, light_ref):
    for light in inventory["lights"]:
        if light_ref.lower() in (light["id"].lower(), light["address"].lower()):
            return light
    raise SystemExit(f"Unknown light '{light_ref}'")


def parse_rgb(value):
    if value.startswith("#"):
        value = value[1:]
        if len(value) != 6:
            raise argparse.ArgumentTypeError("Hex RGB must be #RRGGBB")
        return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))
    parts = value.split(",")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("RGB must be #RRGGBB or R,G,B")
    rgb = tuple(int(part) for part in parts)
    if any(channel < 0 or channel > 255 for channel in rgb):
        raise argparse.ArgumentTypeError("RGB channels must be 0..255")
    return rgb


def parse_credential(value):
    if ":" not in value:
        raise argparse.ArgumentTypeError("Credential must be mesh_name:mesh_password")
    return value.split(":", 1)


def default_mesh_address(light):
    return int(light["address"].split(":")[-1], 16)


def mesh_address_from_advertisement(light):
    payload = bytes.fromhex(
        light.get("manufacturer_data", {}).get("529", "")
    ) if light.get("manufacturer_data") else b""
    if len(payload) >= 8:
        return payload[7]
    return None


async def mesh_login(client, mesh_name, mesh_password):
    pair_packet, session_random = make_pair_packet(mesh_name, mesh_password)
    await client.write_gatt_char(PAIR_CHAR_UUID, pair_packet, response=True)
    await asyncio.sleep(0.1)
    reply = bytes(await client.read_gatt_char(PAIR_CHAR_UUID))
    if not reply or reply[0] != 0x0D:
        raise RuntimeError(f"auth rejected: {reply.hex() if reply else 'empty'}")
    return make_session_key(mesh_name, mesh_password, session_random, reply[1:9])


async def send_command(client, session_key, gateway_address, dest, command, data, response):
    packet = make_command_packet(session_key, gateway_address, dest, command, data)
    await client.write_gatt_char(CONTROL_CHAR_UUID, packet, response=response)
    return packet


async def enable_mesh_notify(client, session_key, gateway_address, mesh_id, response):
    packet = make_command_packet(session_key, gateway_address, mesh_id, 0x01, b"")
    await client.write_gatt_char(NOTIFY_CHAR_UUID, packet, response=response)
    await asyncio.sleep(0.3)
    return packet


async def run(args):
    inventory = load_inventory(args.inventory)
    light = resolve_light(inventory, args.light)
    mesh_name, mesh_password = args.credential
    mesh_address = args.mesh_address if args.mesh_address is not None else default_mesh_address(light)

    print(f"Connecting to {light['id']} {light['address']}")
    print(f"Destination mesh address: 0x{mesh_address:02X}")
    async with BleakClient(light["address"], timeout=args.timeout) as client:
        session_key = await mesh_login(client, mesh_name, mesh_password)
        print("Auth ok")
        if args.enable_notify:
            packet = await enable_mesh_notify(
                client,
                session_key,
                light["address"],
                args.mesh_id,
                args.response,
            )
            print(f"Enabled mesh notify: {packet.hex()}")
        if args.command == "rgb":
            r, g, b = args.rgb
            data = (
                bytes([0x04, r, g, b])
                if args.legacy
                else bytes([args.device_type, COLORMODE_RGB, r, g, b])
            )
            packet = await send_command(
                client,
                session_key,
                light["address"],
                mesh_address,
                OPCODE_SET_COLOR,
                data,
                args.response,
            )
            print(f"Sent RGB #{r:02X}{g:02X}{b:02X}: {packet.hex()}")
        elif args.command == "on":
            data = b"\x01" if args.legacy else bytes([args.device_type, STATEACTION_POWER, 1])
            packet = await send_command(
                client,
                session_key,
                light["address"],
                mesh_address,
                OPCODE_SET_STATE,
                data,
                args.response,
            )
            print(f"Sent ON: {packet.hex()}")
        elif args.command == "off":
            data = b"\x00" if args.legacy else bytes([args.device_type, STATEACTION_POWER, 0])
            packet = await send_command(
                client,
                session_key,
                light["address"],
                mesh_address,
                OPCODE_SET_STATE,
                data,
                args.response,
            )
            print(f"Sent OFF: {packet.hex()}")
        elif args.command == "set-address":
            data = args.new_address.to_bytes(2, "little")
            packet = await send_command(
                client,
                session_key,
                light["address"],
                mesh_address,
                OPCODE_SET_MESH_ADDRESS,
                data,
                args.response,
            )
            print(f"Sent SET ADDRESS 0x{args.new_address:04X}: {packet.hex()}")


def main():
    parser = argparse.ArgumentParser(description="Authenticated Zengge light control.")
    parser.add_argument("--inventory", type=Path, default=INVENTORY_PATH)
    parser.add_argument("--credential", type=parse_credential, default=DEFAULT_CREDENTIAL)
    parser.add_argument("--mesh-address", type=lambda value: int(value, 0))
    parser.add_argument("--device-type", type=lambda value: int(value, 0), default=DEVICE_TYPE)
    parser.add_argument("--scan-timeout", type=float, default=8.0)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--legacy", action="store_true", help="Use older AwoX/Telink payload shapes.")
    parser.add_argument("--response", action="store_true", help="Use acknowledged GATT writes.")
    parser.add_argument("--enable-notify", action="store_true", help="Send Zengge notify-enable packet before control.")
    parser.add_argument("--mesh-id", type=lambda value: int(value, 0), default=DEFAULT_MESH_ID)
    subparsers = parser.add_subparsers(dest="command", required=True)

    rgb_parser = subparsers.add_parser("rgb")
    rgb_parser.add_argument("light")
    rgb_parser.add_argument("rgb", type=parse_rgb)

    on_parser = subparsers.add_parser("on")
    on_parser.add_argument("light")

    off_parser = subparsers.add_parser("off")
    off_parser.add_argument("light")

    address_parser = subparsers.add_parser("set-address")
    address_parser.add_argument("light")
    address_parser.add_argument("new_address", type=lambda value: int(value, 0))

    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
