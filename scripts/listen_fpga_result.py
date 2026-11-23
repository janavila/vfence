"""Display VFence FPGA bench-test results from a dedicated Serial port."""

import argparse
import os
import re
import sys


MAX_LINE_BYTES = 64
GATEWAY_PORT = "/dev/ttyUSB0"
RESULT = re.compile(rb"VFENCE\|P([0-3])\|([0-7])\|([01])")


def read_results(port):
    line = bytearray()
    oversized = False
    while True:
        for byte in port.read(128):
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port", help="Dedicated FPGA port, preferably /dev/serial/by-id/...")
    args = parser.parse_args()
    if os.path.realpath(args.port) == os.path.realpath(GATEWAY_PORT):
        parser.error(f"{GATEWAY_PORT} is reserved for the LoRa Gateway")

    import serial

    try:
        with serial.Serial(args.port, 115200, timeout=0.25, exclusive=True) as port:
            print(f"FPGA em {args.port} (115200 8N1)", flush=True)
            for profile, reason, critical in read_results(port):
                print(f"Perfil=P{profile} | Motivo={reason} | Alerta crítico={critical}", flush=True)
    except KeyboardInterrupt:
        return 0
    except serial.SerialException as exc:
        print(f"Erro na porta da FPGA: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
