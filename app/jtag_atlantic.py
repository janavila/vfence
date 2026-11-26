"""Windows JTAG Atlantic transport for the VFence Nios II command interface."""

from __future__ import annotations

import ctypes as c
import multiprocessing
import os
import re
import time
from contextlib import suppress
from pathlib import Path

from app.fpga_window import FpgaSample, validate_fpga_result


_RESULT = re.compile(r"VFENCE\|P([0-3])\|([0-5])\|([01])")


def parse_result(line: str, current: int) -> dict:
    fields = _RESULT.fullmatch(line)
    if not fields:
        raise ValueError("resposta FPGA invalida")
    profile, reason, critical = map(int, fields.groups())
    validate_fpga_result(current, profile, reason, bool(critical))
    return {"profile": profile, "reason": reason, "critical": bool(critical)}


def sample_command(sample: FpgaSample) -> str:
    values = (sample.collar_id, sample.minute_index, sample.pdr_x100, sample.snr_x100,
              sample.rssi_mag, sample.hdop_x100, sample.sample_count, sample.metrics_valid)
    return "SAMPLE " + " ".join(str(value if value is not None else 0) for value in values)


def quartus_directory(configured):
    candidates = [Path(configured)]
    root = os.environ.get("QUARTUS_ROOTDIR")
    if root:
        candidates.append(Path(root) / "bin64")
    for root in (Path(r"C:\altera"), Path(r"C:\intelFPGA"), Path(r"C:\intelFPGA_lite")):
        candidates.extend(sorted(root.glob("*/quartus/bin64"), reverse=True))
    return next((path for path in candidates if all((path / dll).is_file()
                 for dll in ("jtag_client.dll", "jtag_atlantic.dll"))), None)


class JtagAtlantic:
    def __init__(self, settings):
        if os.name != "nt" or c.sizeof(c.c_void_p) != 8:
            raise OSError("JTAG Atlantic requer Python Windows 64 bits")
        directory = quartus_directory(settings.jtag_dll_dir)
        if directory is None:
            raise OSError("DLLs JTAG Atlantic do Quartus indisponíveis")
        directory = directory.resolve()
        self.dll_dir = os.add_dll_directory(str(directory))
        self.handle = None
        self.pending = bytearray()
        self.buffer = c.create_string_buffer(1024)
        try:
            self.client = c.CDLL(str(directory / "jtag_client.dll"))
            self.atlantic = c.CDLL(str(directory / "jtag_atlantic.dll"))
            self._load_symbols()
            self.handle = self.open_handle(settings.jtag_cable.encode("ascii"), settings.jtag_device,
                                           settings.jtag_instance, b"VFence")
            if not self.handle:
                detail = c.c_char_p()
                raise OSError(f"JTAG Atlantic open: {self.get_error(c.byref(detail))}")
            self._drain()
            # Nios discards a partial input line containing NUL at the next LF.
            if self.write(self.handle, b"\x00\n", 2) != 2 or self.flush(self.handle) < 0:
                raise OSError("falha ao resincronizar a entrada JTAG")
            self._drain()
        except BaseException:
            self.close()
            raise

    def _load_symbols(self):
        pointer, string, integer, size = c.c_void_p, c.c_char_p, c.c_int, c.c_uint
        self.open_handle = self._bind("open", "YAPEAUJTAGATLANTIC@@PEBDHH0", [string, integer, integer, string], pointer)
        self.write = self._bind("write", "YAHPEAUJTAGATLANTIC@@PEBDI", [pointer, string, size], integer)
        self.read = self._bind("read", "YAHPEAUJTAGATLANTIC@@PEADI", [pointer, pointer, size], integer)
        self.flush = self._bind("flush", "YAHPEAUJTAGATLANTIC@@", [pointer], integer)
        self.close_handle = self._bind("close", "YAXPEAUJTAGATLANTIC@@", [pointer], None)
        self.get_error = self._bind("get_error", "YA?AW4JATL_ERROR@@PEAPEBD", [c.POINTER(string)], integer)

    def _bind(self, name, signature, args, result):
        symbol = f"jtagatlantic_{name}"
        function = getattr(self.atlantic, f"?{symbol}@@{signature}@Z", None)
        if function is None:
            function = getattr(self.atlantic, symbol)
        function.argtypes = args
        function.restype = result
        return function

    def _read(self):
        count = self.read(self.handle, self.buffer, len(self.buffer))
        if count < 0:
            raise OSError("falha de leitura JTAG")
        return self.buffer.raw[:count]

    def _drain(self):
        # Discard bytes belonging to a previous owner, before sending PING.
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            self._read()
            time.sleep(0.02)

    def command(self, command: str, timeout: float = 5.0) -> str:
        self.last_sent = False
        if "\n" in command or "\r" in command:
            raise ValueError("comando JTAG deve ocupar uma linha")
        data = (command + "\n").encode("ascii")
        deadline = time.monotonic() + timeout
        if len(data) > 127 or self.write(self.handle, data, len(data)) != len(data):
            raise OSError("envio JTAG incompleto")
        if time.monotonic() >= deadline:
            raise TimeoutError("prazo JTAG excedido na escrita")
        if self.flush(self.handle) < 0:
            raise OSError("flush JTAG falhou")
        self.last_sent = True
        while time.monotonic() < deadline:
            self.pending.extend(self._read())
            if time.monotonic() >= deadline:
                break
            if len(self.pending) > 1024:
                raise ValueError("resposta JTAG excedeu o limite")
            while b"\n" in self.pending:
                line, _, rest = self.pending.partition(b"\n")
                self.pending[:] = rest
                line = line.removesuffix(b"\r")
                if line == b"VFENCE|READY" and command != "PING":
                    raise OSError("Nios II reiniciou durante a janela")
                if line == b"ERR|LINE_TOO_LONG" and command == "PING":
                    continue
                if line and line != b"VFENCE|READY":
                    if time.monotonic() >= deadline:
                        raise TimeoutError("resposta JTAG fora do prazo")
                    return line.decode("ascii")
            time.sleep(0.02)
        raise TimeoutError(f"timeout JTAG em {command.split()[0]}")

    def close(self):
        try:
            if self.handle:
                handle, self.handle = self.handle, None
                self.close_handle(handle)
        finally:
            self.dll_dir.close()


class JtagOwnerAlive(OSError):
    """Recovery must not open another handle until the old owner exits."""

    def __init__(self, message, *, owner=None):
        super().__init__(message)
        self.owner = owner

    def released(self):
        if self.owner is None or self.owner.is_alive():
            return False
        self.owner.join(0)
        self.owner.close()
        self.owner = None
        return True


def _jtag_worker(pipe, settings):
    kernel = c.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [c.c_void_p, c.c_int, c.c_wchar_p]
    kernel.CreateMutexW.restype = c.c_void_p
    kernel.CloseHandle.argtypes = [c.c_void_p]
    handle = kernel.CreateMutexW(None, False, r"Global\VFence.JtagAtlantic")
    existing = c.get_last_error() == 183
    driver = None
    try:
        if not handle or existing:
            raise OSError("JTAG ja reservada por outro processo VFence")
        driver = JtagAtlantic(settings)
        pipe.send(("ok", None, False))
        while True:
            try:
                command, timeout = pipe.recv()
            except EOFError:
                break
            if command is None:
                owner, driver = driver, None
                owner.close()
                pipe.send(("ok", None, False))
                break
            try:
                reply = driver.command(command, timeout)
            except Exception as error:
                pipe.send(("timeout" if isinstance(error, TimeoutError) else "error", str(error), driver.last_sent))
                break
            pipe.send(("ok", reply, driver.last_sent))
    except Exception as error:
        pipe.send(("error", str(error), False))
    finally:
        try:
            if driver is not None:
                driver.close()
        finally:
            pipe.close()
            if handle:
                kernel.CloseHandle(handle)


class IsolatedJtagAtlantic:
    """Expose a single, interruptible owner for native calls, including close."""

    def __init__(self, settings, *, worker=_jtag_worker, startup_timeout=8.0):
        context = multiprocessing.get_context("spawn")
        self.pipe, child = context.Pipe()
        self.process = context.Process(target=worker, args=(child, settings), daemon=True)
        self.last_sent = False
        self.broken = False
        try:
            self.process.start()
        except BaseException:
            self.pipe.close()
            raise
        finally:
            child.close()
        try:
            self._receive(time.monotonic() + startup_timeout)
        except BaseException:
            self._reap()
            raise

    def _receive(self, deadline):
        try:
            if not self.pipe.poll(max(0, deadline - time.monotonic())):
                raise TimeoutError("prazo do processo JTAG excedido")
            status, value, sent = self.pipe.recv()
        except (EOFError, OSError):
            self.broken = True
            raise
        self.last_sent = sent
        if status == "timeout" or time.monotonic() >= deadline:
            self.broken = True
            raise TimeoutError("resposta JTAG fora do prazo")
        if status != "ok":
            self.broken = True
            raise OSError(value)
        return value

    def command(self, command, timeout=5.0):
        self.last_sent = False
        if self.broken:
            raise OSError("processo JTAG invalido; reconexao necessaria")
        deadline = time.monotonic() + timeout
        try:
            self.pipe.send((command, timeout))
            return self._receive(deadline)
        except (OSError, EOFError):
            self.broken = True
            raise

    def _reap(self):
        self.pipe.close()
        if self.process.is_alive():
            with suppress(OSError):
                self.process.terminate()
        self.process.join(1)
        if self.process.is_alive():
            with suppress(OSError):
                self.process.kill()
            self.process.join(1)
        if self.process.is_alive():
            raise JtagOwnerAlive("processo JTAG anterior ainda vivo; reconexao bloqueada", owner=self.process)
        self.process.close()

    def close(self):
        try:
            if not self.broken and self.process.is_alive():
                self.pipe.send((None, 0))
                self._receive(time.monotonic() + 1)
                self.process.join(0.2)
        except (OSError, EOFError):
            pass
        finally:
            self._reap()
