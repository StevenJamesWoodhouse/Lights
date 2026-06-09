import argparse
import asyncio
import json
from pathlib import Path

from zengge_control import run as run_zengge


MESH_PATH = Path("hao_deng_mesh.json")


def load_mesh(path):
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_device(mesh, ref):
    ref_lower = ref.lower()
    for device in mesh["devices"]:
        if ref_lower in (
            device["name"].lower(),
            device["address"].lower(),
            f"lamp{device['meshAddress']}".lower(),
            str(device["meshAddress"]),
        ):
            return device
    raise SystemExit(f"Unknown lamp '{ref}'")


def parse_rgb(value):
    if value.startswith("#"):
        value = value[1:]
        if len(value) != 6:
            raise argparse.ArgumentTypeError("Hex RGB must be #RRGGBB")
        return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))
    parts = value.split(",")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("RGB must be #RRGGBB or R,G,B")
    return tuple(int(part) for part in parts)


async def main_async(args):
    mesh = load_mesh(args.mesh)
    device = resolve_device(mesh, args.lamp)
    credential = f"{mesh['meshKey']}:{mesh['meshPassword']}"
    command_args = argparse.Namespace(
        inventory=Path("lights.json"),
        credential=credential.split(":", 1),
        mesh_address=device["meshAddress"],
        device_type=device["deviceType"],
        scan_timeout=args.scan_timeout,
        timeout=args.timeout,
        legacy=False,
        response=args.response,
        enable_notify=args.enable_notify,
        mesh_id=0x0211,
        command=args.command,
        light=device["address"],
        rgb=args.rgb,
        new_address=None,
    )
    await run_zengge(command_args)


def main():
    parser = argparse.ArgumentParser(description="Control lamps using Hao Deng cloud mesh data.")
    parser.add_argument("--mesh", type=Path, default=MESH_PATH)
    parser.add_argument("--response", action="store_true", default=True)
    parser.add_argument("--no-response", action="store_false", dest="response")
    parser.add_argument("--enable-notify", action="store_true")
    parser.add_argument("--scan-timeout", type=float, default=8.0)
    parser.add_argument("--timeout", type=float, default=8.0)
    subparsers = parser.add_subparsers(dest="command", required=True)

    on_parser = subparsers.add_parser("on")
    on_parser.add_argument("lamp")

    off_parser = subparsers.add_parser("off")
    off_parser.add_argument("lamp")

    rgb_parser = subparsers.add_parser("rgb")
    rgb_parser.add_argument("lamp")
    rgb_parser.add_argument("rgb", type=parse_rgb)

    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
