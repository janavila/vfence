#!/usr/bin/env python3
import argparse
import json
import threading
import time
import unicodedata

import serial

from app.metric_protocol import METRIC_SUMMARY_KINDS, parse_metric_summary
from app.terminal import RULE, collar_label, event_block, metric_block, position_line, value_text

SCHEMA = "vfence.gateway.serial.v1"
_MAX_SERIAL_LINE_BYTES = 4096
_SERIAL_RESYNC = b"\x00\n"
_NORMAL_GATEWAY_LOG_PREFIXES = (
    "[ERRO", "[FATAL]", "[AVISO]", "[NACK LoRa]", "[RETRY LoRa]", "[ACK SQLITE]",
    "[RX] pacote invalido", "[RX] collarId invalido", "[RX] versao nao suportada",
    "[RX] CONFIG vindo da coleira", "[RX LoRa] pacote maior", "[RX SERIAL] token hexadecimal invalido",
)


class VFenceSerialClient:
    def __init__(self, port: str, baudrate: int = 115200, *, terminal_detail: bool = False):
        self.ser = serial.Serial(port, baudrate=baudrate, timeout=0.2, write_timeout=1.0)
        self.terminal_detail = terminal_detail

        try:
            self.ser.dtr = False
            self.ser.rts = False
        except Exception:
            pass

        self._running = False
        self._reader = None

        self._request_id = 1
        self._ping_token = 1

        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._needs_resync = False

        # Coleiras que o PC já viu diretamente ou recebeu do GET_STATUS.
        # Formato: {collarId: "ONLINE" | "OFFLINE" | "UNKNOWN"}
        self._known_collars = {}

        # Esperas de PONG usadas pelo ping-all.
        # Chave: (collarId, token) -> threading.Event
        self._pending_pongs = {}

    # -------------------------------------------------------------------------
    # IDs locais da interface PC <-> Gateway
    # -------------------------------------------------------------------------

    def next_request_id(self):
        with self._lock:
            rid = self._request_id
            self._request_id += 1
        return rid

    def next_ping_token(self):
        with self._lock:
            token = self._ping_token
            self._ping_token += 1

            # O token do VFENCE PING/PONG é uint16_t.
            if self._ping_token > 0xFFFF:
                self._ping_token = 1

        return token

    # -------------------------------------------------------------------------
    # Envio PC -> Gateway
    # -------------------------------------------------------------------------

    def send(self, command: str, **fields):
        if not isinstance(command, str):
            raise ValueError("comando Serial deve ser texto")
        if unicodedata.normalize("NFKC", command).strip().casefold() == "metric_committed":
            raise ValueError("METRIC_COMMITTED e exclusivo da Base apos COMMIT")
        with self._write_lock:
            msg = {
                "schema": SCHEMA,
                "requestId": self.next_request_id(),
                "command": command,
                **fields,
            }
            line = "[CMD] " + json.dumps(msg, separators=(",", ":")) + "\n"
            wire = line.encode("utf-8")
            if self._needs_resync:
                wire = _SERIAL_RESYNC + wire
            try:
                written = self.ser.write(wire)
                if written != len(wire):
                    raise OSError(f"escrita incompleta ({written}/{len(wire)} bytes)")
                self.ser.flush()
            except OSError as exc:
                self._needs_resync = True
                print(f"[ERRO SERIAL] [PC] {command} requestId={msg['requestId']} | {exc}")
                raise
            self._needs_resync = False

        target = f" [{collar_label(msg['collarId'])}]" if "collarId" in msg else ""
        print(f"[TX SERIAL] [PC]{target} {command} requestId={msg['requestId']}")
        if self.terminal_detail:
            print("[RAW PC]", line.strip())

        return msg["requestId"]

    # -------------------------------------------------------------------------
    # Controle das coleiras conhecidas
    # -------------------------------------------------------------------------

    def _remember_collar(self, collar_id, state=None):
        if type(collar_id) is not int:
            return

        if not (1 <= collar_id <= 254):
            return

        with self._lock:
            previous = self._known_collars.get(collar_id, "UNKNOWN")
            self._known_collars[collar_id] = state or previous

    def known_collars(self):
        with self._lock:
            return dict(self._known_collars)

    def refresh_known_collars(self, wait_s=1.0):
        """
        Solicita GET_STATUS ao gateway e aguarda um pequeno intervalo para que
        as respostas collar_status cheguem pela thread de leitura.
        """
        self.send("GET_STATUS")
        time.sleep(wait_s)
        return self.known_collars()

    # -------------------------------------------------------------------------
    # PING individual com espera de PONG
    # -------------------------------------------------------------------------

    def ping_and_wait(self, collar_id: int, timeout_s: float = 3.0):
        token = self.next_ping_token()
        event = threading.Event()
        key = (collar_id, token)

        with self._lock:
            self._pending_pongs[key] = event

        try:
            self.send("PING", collarId=collar_id, token=token)

            received = event.wait(timeout_s)

            if received:
                print(
                    f"[PING-ALL] collar={collar_id} "
                    f"token={token} -> PONG recebido"
                )
                return True

            print(
                f"[PING-ALL] collar={collar_id} "
                f"token={token} -> TIMEOUT ({timeout_s:.1f}s)"
            )
            return False

        finally:
            with self._lock:
                self._pending_pongs.pop(key, None)

    def ping_all(self, timeout_s: float = 3.0):
        """
        Consulta as coleiras conhecidas pelo gateway e executa PING de forma
        sequencial. Somente passa para a próxima coleira depois de receber
        PONG ou ocorrer timeout.
        """
        print("[PING-ALL] Atualizando lista de coleiras conhecidas...")
        collars = self.refresh_known_collars(wait_s=1.0)

        ids = sorted(collars.keys())

        if not ids:
            print("[PING-ALL] Nenhuma coleira conhecida pelo gateway.")
            return

        print(
            "[PING-ALL] Coleiras encontradas:",
            ", ".join(
                f"{cid}({collars.get(cid, 'UNKNOWN')})"
                for cid in ids
            ),
        )

        ok = []
        failed = []

        for collar_id in ids:
            if self.ping_and_wait(collar_id, timeout_s=timeout_s):
                ok.append(collar_id)
            else:
                failed.append(collar_id)

        print("\n--- RESULTADO PING-ALL ---")
        print(f"Total: {len(ids)}")
        print(f"Responderam: {len(ok)}")
        print(f"Sem resposta: {len(failed)}")

        if ok:
            print("OK:", ", ".join(str(x) for x in ok))

        if failed:
            print("TIMEOUT:", ", ".join(str(x) for x in failed))

    # -------------------------------------------------------------------------
    # Recepção Gateway -> PC
    # -------------------------------------------------------------------------

    def _handle_app_message(self, obj):
        kind = obj.get("kind", "unknown")
        collar_id = obj.get("collarId")

        # Qualquer mensagem válida associada a uma coleira já nos permite
        # registrá-la como conhecida.
        if isinstance(collar_id, int) and 1 <= collar_id <= 254:
            self._remember_collar(collar_id)

        if kind == "telemetry":
            self._remember_collar(collar_id, "ONLINE")
            gps = obj.get("gpsValid")
            zone = None
            if gps is True:
                if obj.get("inside") is False:
                    zone = "FORA"
                elif obj.get("inside") is True:
                    if obj.get("veryNear") is True:
                        zone = "CRITICO"
                    elif obj.get("near") is True:
                        zone = "ATENCAO"
                    else:
                        zone = "SEGURO"
            print(position_line("PC / GATEWAY", collar_id, obj.get("sequence"), gps,
                                obj.get("hdop"), obj.get("rssi"), obj.get("snr"), zone))

        elif isinstance(kind, str) and kind in METRIC_SUMMARY_KINDS:
            try:
                summary = parse_metric_summary(obj, "")
            except (KeyError, TypeError, ValueError) as exc:
                print(f"[ERRO] [PC] [{collar_label(collar_id)}] resumo invalido: {exc}")
            else:
                print(metric_block("PC / GATEWAY", summary, "[RX] Recebido pelo monitor Serial"))

        elif kind == "event":
            self._remember_collar(collar_id, "ONLINE")

            print(event_block("PC / GATEWAY", collar_id, value_text(obj.get("subtype")),
                              f"seq={value_text(obj.get('sequence'))} cerca={value_text(obj.get('fenceVersion'))}"))

        elif kind == "control":
            self._remember_collar(collar_id, "ONLINE")

            # Se for PONG, libera o ping_and_wait correspondente.
            if obj.get("subtype") == "PONG":
                token = obj.get("token")

                if type(collar_id) is int and 1 <= collar_id <= 254 and type(token) is int and 0 <= token <= 65535:
                    key = (collar_id, token)

                    with self._lock:
                        event = self._pending_pongs.get(key)

                    if event:
                        event.set()

            print(f"[CONTROLE] [PC] [{collar_label(collar_id)}] {value_text(obj.get('subtype'))} "
                  f"token={value_text(obj.get('token'))}")

        elif kind == "collar_state":
            state = obj.get("state", "UNKNOWN")
            self._remember_collar(collar_id, state)

            print(event_block("PC / GATEWAY", collar_id, value_text(state)))

        elif kind == "command_response":
            label = "OK" if obj.get("ok") else "ERRO"
            print(f"[{label}] [PC] resposta={value_text(obj.get('command'))} "
                  f"requestId={value_text(obj.get('requestId'))} detalhe={value_text(obj.get('detail'))}")

        elif kind == "fence_transfer":
            print(f"{RULE}\n[CERCA] [PC] [{collar_label(collar_id)}] "
                  f"versao={value_text(obj.get('fenceVersion'))} requestId={value_text(obj.get('requestId'))} "
                  f"estadoGateway={value_text(obj.get('state'))} detalhe={value_text(obj.get('detail'))}\n{RULE}")

        elif kind == "radio_config":
            print(f"[RADIO] [PC] freq={value_text(obj.get('frequencyHz'))}Hz "
                  f"SF={value_text(obj.get('spreadingFactor'))} BW={value_text(obj.get('bandwidthHz'))}Hz "
                  f"CR=4/{value_text(obj.get('codingRateDenominator'))} "
                  f"TX={value_text(obj.get('txPowerDbm'))}dBm preambulo={value_text(obj.get('preambleLength'))} "
                  f"sync={value_text(obj.get('syncWord'))} CRC={value_text(obj.get('crcEnabled'))}")

        elif kind == "collar_status":
            if not obj.get("empty", False):
                state = obj.get("state", "UNKNOWN")
                self._remember_collar(collar_id, state)

            if obj.get("empty", False):
                print("[ESTADO] [PC] Gateway ainda nao viu coleiras")
            else:
                print(f"[ESTADO] [PC] [{collar_label(collar_id)}] {value_text(obj.get('state'))} "
                      f"ultimoContato={value_text(obj.get('lastSeenS'))} s")

        elif kind == "gateway_state":
            print(f"[ESTADO] [PC] Gateway={value_text(obj.get('state'))} "
                  f"transferenciaAtiva={value_text(obj.get('fenceTransferActive'))}")

        else:
            print(f"[RX SERIAL] [PC] [{collar_label(collar_id)}] tipo={value_text(kind)}")

    def _reader_loop(self):
        pending = bytearray()
        discard = False
        try:
            while self._running:
                part = self.ser.read_until(b"\n", _MAX_SERIAL_LINE_BYTES + 1)
                if not self._running:
                    break
                if not part:
                    continue
                terminated = part.endswith(b"\n")
                if discard:
                    discard = not terminated
                    continue
                if len(pending) + len(part) > _MAX_SERIAL_LINE_BYTES:
                    print(f"[ERRO SERIAL] [PC] linha excede {_MAX_SERIAL_LINE_BYTES} bytes")
                    pending.clear()
                    discard = not terminated
                    continue
                pending.extend(part)
                if terminated:
                    self._handle_serial_line(bytes(pending[:-1]).removesuffix(b"\r"))
                    pending.clear()
        except OSError as exc:
            print(f"[ERRO SERIAL] [PC] leitura interrompida | {exc}")
            with self._write_lock:
                try:
                    self.ser.close()
                except OSError as close_error:
                    print(f"[ERRO SERIAL] [PC] fechamento da porta | {close_error}")
        finally:
            self._running = False

    def _handle_serial_line(self, raw: bytes):
        if b"\x00" in raw or b"\r" in raw:
            print("[ERRO SERIAL] [PC] linha descartada por enquadramento invalido")
            return
        if not raw.startswith(b"[APP] "):
            line = raw.decode("utf-8", errors="replace").strip()
            if line and (self.terminal_detail or line.startswith(_NORMAL_GATEWAY_LOG_PREFIXES)):
                print("[ESP]", line)
            return
        try:
            line = raw.decode("utf-8")
            obj = json.loads(line[6:])
            if not isinstance(obj, dict):
                raise ValueError("a mensagem [APP] deve ser um objeto JSON")
            if self.terminal_detail:
                print("[RAW GATEWAY]", line)
            self._handle_app_message(obj)
        except (ValueError, RecursionError) as exc:
            print(f"[ERRO JSON] [PC] {exc}")

    def start(self):
        if self._reader and self._reader.is_alive():
            return
        with self._write_lock:
            if not self.ser.is_open:
                self.ser.open()
                self._needs_resync = True
        self._running = True

        self._reader = threading.Thread(
            target=self._reader_loop,
            daemon=True,
        )

        self._reader.start()

    def close(self):
        self._running = False

        if self._reader:
            self._reader.join(timeout=1.0)

        with self._write_lock:
            self.ser.close()


# =============================================================================
# Terminal interativo
# =============================================================================

def print_help():
    print(
        "\nComandos:\n"
        "  help\n"
        "  status\n"
        "  radio\n"
        "  ping <collarId> [token]\n"
        "  ping-all [timeoutS]\n"
        "  params <id> <veryNearM> <nearM> <telemetryS> "
        "<gpsTimeoutS> <lowBat> <critBat>\n"
        "  fence-new <id> <version> <vertexCount>\n"
        "  fence-point <index> <latitude> <longitude>\n"
        "  fence-send\n"
        "  fence-cancel\n"
        "  quit\n"
        "\n"
        "PING-ALL:\n"
        "  Consulta GET_STATUS no gateway e envia PING individualmente\n"
        "  para cada coleira conhecida. Aguarda PONG ou timeout antes\n"
        "  de passar para a próxima.\n"
        "\n"
        "Exemplos:\n"
        "  ping 1 1234\n"
        "  ping-all\n"
        "  ping-all 5\n"
        "  params 1 10 30 10 60 20 5\n"
        "\n"
    )


def interactive(client: VFenceSerialClient):
    print_help()

    while True:
        try:
            text = input("vfence> ").strip()

        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not text:
            continue

        parts = text.split()
        cmd = parts[0].lower()

        try:
            if cmd in ("quit", "exit"):
                break

            elif cmd == "help":
                print_help()

            elif cmd == "status":
                client.send("GET_STATUS")

            elif cmd == "radio":
                client.send("GET_RADIO")

            elif cmd == "ping":
                collar = int(parts[1], 0)
                fields = {"collarId": collar}

                if len(parts) >= 3:
                    fields["token"] = int(parts[2], 0)

                client.send("PING", **fields)

            elif cmd == "ping-all":
                if len(parts) > 2:
                    raise ValueError(
                        "uso: ping-all [timeoutS]"
                    )

                timeout_s = 3.0

                if len(parts) == 2:
                    timeout_s = float(parts[1])

                    if timeout_s <= 0:
                        raise ValueError(
                            "timeout deve ser maior que zero"
                        )

                client.ping_all(timeout_s=timeout_s)

            elif cmd == "params":
                if len(parts) != 8:
                    raise ValueError(
                        "uso: params <id> <veryNearM> <nearM> "
                        "<telemetryS> <gpsTimeoutS> <lowBat> <critBat>"
                    )

                client.send(
                    "SET_PARAMETERS",
                    collarId=int(parts[1], 0),
                    veryNearM=int(parts[2]),
                    nearM=int(parts[3]),
                    telemetryIntervalS=int(parts[4]),
                    gpsTimeoutS=int(parts[5]),
                    lowBatteryPct=int(parts[6]),
                    criticalBatteryPct=int(parts[7]),
                )

            elif cmd == "fence-new":
                if len(parts) != 4:
                    raise ValueError(
                        "uso: fence-new <id> <version> <vertexCount>"
                    )

                client.send(
                    "FENCE_NEW",
                    collarId=int(parts[1], 0),
                    fenceVersion=int(parts[2], 0),
                    vertexCount=int(parts[3]),
                )

            elif cmd == "fence-point":
                if len(parts) != 4:
                    raise ValueError(
                        "uso: fence-point <index> <latitude> <longitude>"
                    )

                client.send(
                    "FENCE_POINT",
                    index=int(parts[1]),
                    latitude=float(parts[2]),
                    longitude=float(parts[3]),
                )

            elif cmd == "fence-send":
                client.send("FENCE_SEND")

            elif cmd == "fence-cancel":
                client.send("FENCE_CANCEL")

            else:
                print(
                    "Comando desconhecido. "
                    "Digite 'help'."
                )

        except (IndexError, ValueError) as e:
            print("Erro:", e)
        except OSError:
            continue  # send() ja informou a falha da porta.


def main():
    parser = argparse.ArgumentParser(
        description="Cliente Serial VFENCE Gateway <-> PC"
    )

    parser.add_argument(
        "port",
        help="Ex.: /dev/ttyUSB0, /dev/ttyACM0 ou COM3",
    )

    parser.add_argument(
        "--baud",
        type=int,
        default=115200,
    )
    parser.add_argument("--detalhado", action="store_true", help="Mostra JSON bruto e logs humanos do Gateway.")

    args = parser.parse_args()

    try:
        client = VFenceSerialClient(args.port, args.baud, terminal_detail=args.detalhado)
    except OSError as exc:
        parser.exit(1, f"[ERRO SERIAL] [PC] abertura da porta | {exc}\n")

    print(f"{RULE}\n VFENCE | PC | USB SERIAL\n"
          f" Porta: {args.port} | Baud: {args.baud} | Terminal: {'DETALHADO' if args.detalhado else 'NORMAL'}\n"
          f"[USB] Porta aberta, aguardando Gateway\n{RULE}")

    client.start()

    time.sleep(0.5)

    try:
        interactive(client)

    finally:
        client.close()


if __name__ == "__main__":
    main()
