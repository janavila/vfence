#!/usr/bin/env python3
"""Gerador simples de pacotes VFENCE Protocol v1 para teste sem LoRa.

Ele imprime linhas no formato aceito pelo Monitor Serial da coleira:
    RX 08 01 ...

Cole cada linha individualmente no Monitor Serial.
"""

import struct
import zlib

COLLAR_ID = 0x01
SEQ = 100

TYPE_CONFIG = 0
ACK_REQ = 0x08

FENCE_BEGIN = 0x01
FENCE_CHUNK = 0x02
FENCE_COMMIT = 0x03
PARAMETERS = 0x04


def ctrl(msg_type, ack=False, retry=False, urgent=False, version=0):
    b = ((version & 0x03) << 6) | ((msg_type & 0x03) << 4)
    if ack:
        b |= 0x08
    if retry:
        b |= 0x04
    if urgent:
        b |= 0x02
    return b


def packet(msg_type, collar_id, sequence, payload, ack=False, retry=False, urgent=False):
    return bytes([ctrl(msg_type, ack, retry, urgent), collar_id]) + struct.pack(">H", sequence) + bytes([len(payload)]) + payload


def e7(value):
    return int(round(value * 10_000_000))


def canonical_fence_bytes(version, points):
    data = struct.pack(">HB", version, len(points))
    for lat, lon in points:
        data += struct.pack(">ii", lat, lon)
    return data


def fence_crc(version, points):
    # zlib.crc32 corresponde ao CRC-32/ISO-HDLC usado pela implementacao C++.
    return zlib.crc32(canonical_fence_bytes(version, points)) & 0xFFFFFFFF


def rx_line(data):
    return "RX " + " ".join(f"{b:02X}" for b in data)


def next_seq():
    global SEQ
    value = SEQ
    SEQ = (SEQ + 1) & 0xFFFF
    return value


def main():
    fence_version = 2
    points = [
        (e7(-31.3131703), e7(-54.0868848)),
        (e7(-31.3131703), e7(-54.0867535)),
        (e7(-31.3133945), e7(-54.0867535)),
        (e7(-31.3133945), e7(-54.0868848)),
    ]
    crc = fence_crc(fence_version, points)
    chunk_count = (len(points) + 1) // 2

    begin_payload = bytes([FENCE_BEGIN]) + struct.pack(">HBBI", fence_version, len(points), chunk_count, crc)
    print("# FENCE_BEGIN")
    print(rx_line(packet(TYPE_CONFIG, COLLAR_ID, next_seq(), begin_payload, ack=True)))

    for chunk_index in range(chunk_count):
        first = chunk_index * 2
        chunk_points = points[first:first + 2]
        payload = bytes([FENCE_CHUNK]) + struct.pack(">HBBB", fence_version, chunk_index, first, len(chunk_points))
        for lat, lon in chunk_points:
            payload += struct.pack(">ii", lat, lon)
        print(f"# FENCE_CHUNK {chunk_index}")
        print(rx_line(packet(TYPE_CONFIG, COLLAR_ID, next_seq(), payload)))

    commit_payload = bytes([FENCE_COMMIT]) + struct.pack(">HI", fence_version, crc)
    print("# FENCE_COMMIT")
    print(rx_line(packet(TYPE_CONFIG, COLLAR_ID, next_seq(), commit_payload, ack=True)))

    params_payload = bytes([PARAMETERS]) + struct.pack(">HHIHBB", 10, 30, 10, 60, 20, 5)
    print("# PARAMETERS")
    print(rx_line(packet(TYPE_CONFIG, COLLAR_ID, next_seq(), params_payload, ack=True)))


if __name__ == "__main__":
    main()
