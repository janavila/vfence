"""Display VFence FPGA bench-test results from Serial or standard input."""

import argparse
import os
import re
import sys


MAX_LINE_BYTES = 64
GATEWAY_PORT = "/dev/ttyUSB0"
RESULT = re.compile(rb"VFENCE\|P([0-3])\|([0-7])\|([01])")


def read_results(port, *, stop_at_eof=False):
    line = bytearray()
    oversized = False
    read_chunk = port.read1 if stop_at_eof else port.read
    while True:
        chunk = read_chunk(128)
        if not chunk and stop_at_eof:
            return
        for byte in chunk:
            if byte == ord("\n"):
                match = RESULT.fullmatch(line.removesuffix(b"\r")) if not oversized else None
                if match:
                    yield tuple(int(field) for field in match.groups())
                line.clear()
                oversized = False
            elif len(line) == MAX_LINE_BYTES:
                oversized = True
            elif not oversized:
                line.append(byte)


def show_results(source, *, stop_at_eof=False):
    for profile, reason, critical in read_results(source, stop_at_eof=stop_at_eof):
        print(f"Perfil=P{profile} | Motivo={reason} | Alerta crítico={critical}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("port", nargs="?", help="Dedicated FPGA port, preferably /dev/serial/by-id/...")
    source.add_argument("--stdin", action="store_true", help="Read FPGA results from a pipe, without opening Serial")
    args = parser.parse_args()
    if args.stdin:
        show_results(sys.stdin.buffer, stop_at_eof=True)
        return 0

    if os.path.realpath(args.port) == os.path.realpath(GATEWAY_PORT):
        parser.error(f"{GATEWAY_PORT} is reserved for the LoRa Gateway")

    import serial

    try:
        with serial.Serial(args.port, 115200, timeout=0.25, exclusive=True) as port:
            print(f"FPGA em {args.port} (115200 8N1)", flush=True)
            show_results(port)
    except KeyboardInterrupt:
        return 0
    except serial.SerialException as exc:
        print(f"Erro na porta da FPGA: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
