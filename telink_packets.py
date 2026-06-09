import struct
from os import urandom

from Crypto.Cipher import AES


def encrypt_block(key, value):
    if len(key) != 16:
        raise ValueError("AES key must be 16 bytes")
    k = bytearray(key)
    val = bytearray(value.ljust(16, b"\x00"))
    k.reverse()
    val.reverse()
    cipher = AES.new(bytes(k), AES.MODE_ECB)
    val = bytearray(cipher.encrypt(bytes(val)))
    val.reverse()
    return val


def make_pair_packet(mesh_name, mesh_password, session_random=None):
    if session_random is None:
        session_random = urandom(8)
    mesh_name = mesh_name.encode("ascii") if isinstance(mesh_name, str) else mesh_name
    mesh_password = mesh_password.encode("ascii") if isinstance(mesh_password, str) else mesh_password
    m_n = bytearray(mesh_name.ljust(16, b"\x00"))
    m_p = bytearray(mesh_password.ljust(16, b"\x00"))
    s_r = session_random.ljust(16, b"\x00")
    name_pass = bytearray(a ^ b for (a, b) in zip(m_n, m_p))
    enc = encrypt_block(s_r, name_pass)
    packet = bytearray(b"\x0c" + session_random)
    packet += enc[0:8]
    return bytes(packet), session_random


def make_session_key(mesh_name, mesh_password, session_random, response_random):
    mesh_name = mesh_name.encode("ascii") if isinstance(mesh_name, str) else mesh_name
    mesh_password = mesh_password.encode("ascii") if isinstance(mesh_password, str) else mesh_password
    random = session_random + response_random
    m_n = bytearray(mesh_name.ljust(16, b"\x00"))
    m_p = bytearray(mesh_password.ljust(16, b"\x00"))
    name_pass = bytearray(a ^ b for (a, b) in zip(m_n, m_p))
    return bytes(encrypt_block(name_pass, random))


def make_checksum(key, nonce, payload):
    base = nonce + bytearray([len(payload)])
    base = base.ljust(16, b"\x00")
    check = encrypt_block(key, base)

    for i in range(0, len(payload), 16):
        check_payload = bytearray(payload[i : i + 16].ljust(16, b"\x00"))
        check = bytearray(a ^ b for (a, b) in zip(check, check_payload))
        check = encrypt_block(key, check)

    return check


def crypt_payload(key, nonce, payload):
    base = bytearray(b"\x00" + nonce)
    base = base.ljust(16, b"\x00")
    result = bytearray()

    for i in range(0, len(payload), 16):
        enc_base = encrypt_block(key, base)
        result += bytearray(
            a ^ b for (a, b) in zip(enc_base, bytearray(payload[i : i + 16]))
        )
        base[0] += 1

    return result


def decrypt_packet(key, address, packet):
    packet = bytearray(packet)
    if len(packet) < 20:
        return None
    address_bytes = bytearray.fromhex(address.replace(":", ""))
    address_bytes.reverse()
    nonce = bytes(address_bytes[0:3] + packet[0:5])
    payload = crypt_payload(key, nonce, packet[7:])
    check = make_checksum(key, nonce, payload)
    if check[0:2] != packet[5:7]:
        return None
    return bytes(packet[0:7] + payload)


def make_command_packet(key, address, dest_id, command, data, command_marker=b"\x11\x02"):
    sequence = urandom(3)
    address_bytes = bytearray.fromhex(address.replace(":", ""))
    address_bytes.reverse()
    nonce = bytes(address_bytes[0:4] + b"\x01" + sequence)
    dest = struct.pack("<H", dest_id)
    payload = (dest + struct.pack("B", command) + command_marker + data).ljust(15, b"\x00")
    check = make_checksum(key, nonce, payload)
    encrypted_payload = crypt_payload(key, nonce, payload)
    return bytes(sequence + check[0:2] + encrypted_payload)


def parse_status_packet(message):
    if not message or len(message) < 19:
        return None
    mesh_id = struct.unpack("B", message[3:4])[0]
    mode = struct.unpack("B", message[12:13])[0]
    if mode >= 40:
        return None
    white_brightness, white_temp = struct.unpack("BB", message[13:15])
    color_brightness, red, green, blue = struct.unpack("BBBB", message[15:19])
    return {
        "mesh_id": mesh_id,
        "mode": mode,
        "on": bool(mode % 2),
        "white_brightness": white_brightness,
        "white_temp": white_temp,
        "color_brightness": color_brightness,
        "red": red,
        "green": green,
        "blue": blue,
        "hex": f"#{red:02X}{green:02X}{blue:02X}",
        "raw_decrypted": message.hex(),
    }


def h255_to_h360(h255):
    if h255 <= 128:
        return round((h255 * 360) / 254)
    return round((h255 * 360) / 255)


def hue_to_rgb(h):
    r = abs(h * 6.0 - 3.0) - 1.0
    g = 2.0 - abs(h * 6.0 - 2.0)
    b = 2.0 - abs(h * 6.0 - 4.0)
    return max(0.0, min(1.0, r)), max(0.0, min(1.0, g)), max(0.0, min(1.0, b))


def hsl_to_rgb(h, s=1, lightness=0.5):
    r, g, b = hue_to_rgb(h / 360)
    c = (1.0 - abs(2.0 * lightness - 1.0)) * s
    values = []
    for value in (r, g, b):
        channel = round(round((value - 0.5) * c + lightness, 4) * 255)
        values.append(255 if channel >= 250 else channel)
    return values


def decode_zengge_colour(value):
    return hsl_to_rgb(h255_to_h360(value))


def parse_zengge_mesh_status(message):
    if not message or len(message) < 20:
        return []
    command = message[7]
    statuses = []
    if command == 0xDB:
        statuses.append(
            {
                "type": "online",
                "mesh_address": message[3],
                "raw_decrypted": message.hex(),
            }
        )
    elif command == 0xDC:
        for offset in (10, 15):
            device_data = message[offset : offset + 5]
            if len(device_data) < 5 or device_data[0] == 0:
                continue
            mesh_address, connected, brightness, mode, colour_value = device_data
            if mesh_address == 255:
                statuses.append(
                    {
                        "type": "bridge",
                        "mesh_address": mesh_address,
                        "state": connected != 0,
                        "raw_decrypted": message.hex(),
                    }
                )
                continue
            if mode in (63, 42):
                color_mode = "rgb"
                rgb = decode_zengge_colour(colour_value)
            else:
                color_mode = "white"
                rgb = [0, 0, 0]
            statuses.append(
                {
                    "type": "status",
                    "mesh_address": mesh_address,
                    "connected": connected != 0,
                    "state": brightness != 0 if connected != 0 else None,
                    "brightness": brightness,
                    "mode": mode,
                    "color_mode": color_mode,
                    "rgb": rgb,
                    "hex": f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}",
                    "white_temperature": colour_value,
                    "raw_decrypted": message.hex(),
                }
            )
    else:
        statuses.append(
            {
                "type": "unknown",
                "command": command,
                "raw_decrypted": message.hex(),
            }
        )
    return statuses
