"""Prepare FPGA inputs without opening a port or requesting a radio change."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field

from app.metric_protocol import MetricSummary
from app.pdr_protocol import PdrSummary


MINUTE_MS = 60_000
WINDOW_MINUTES = 5
_CLOCK_MASK = 0xFFFFFFFF
_RESULT_STATUS = ("maintain", "recommended", "recommended", "critical_alert", "insufficient_data", "tie")


@dataclass(frozen=True)
class FpgaSample:
    collar_id: int
    minute_index: int
    pdr_x100: int | None
    snr_x100: int | None
    rssi_mag: int | None
    hdop_x100: int | None
    sample_count: int | None

    @property
    def metrics_valid(self) -> int:
        return sum(1 << bit for bit, value in enumerate(
            (self.rssi_mag, self.snr_x100, self.pdr_x100, self.hdop_x100)
        ) if value is not None)

    @property
    def valid_vote(self) -> bool:
        return self.metrics_valid == 0b1111 and bool(self.sample_count)


@dataclass
class FpgaWindow:
    gateway_id: str
    gateway_session: str
    started_at_ms: int
    current_profile: int
    collar_sessions: dict[int, str]
    deadline: float = field(default_factory=lambda: time.monotonic() + 60 * WINDOW_MINUTES)
    _samples: dict[tuple[int, int], FpgaSample] = field(default_factory=dict, init=False)
    _pdr_sources: dict[tuple[int, str], int] = field(default_factory=dict, init=False)
    _closed: tuple[FpgaSample, ...] | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.collar_sessions = self.collar_sessions.copy()
        if not (0 <= self.current_profile <= 3 and 0 <= self.started_at_ms <= _CLOCK_MASK
                and 1 <= len(self.collar_sessions) <= 2
                and all(1 <= collar <= 254 and session for collar, session in self.collar_sessions.items())):
            raise ValueError("invalid FPGA evaluation window")

    def add(
        self,
        *,
        gateway_id: str,
        pdr: PdrSummary,
        radio: MetricSummary | None = None,
        hdop: MetricSummary | None = None,
        aligned_hdop_start_ms: int | None = None,
        now: float | None = None,
    ) -> bool:
        """Accept validated summaries. HDOP alignment requires an upstream clock mapping."""
        if self._closed is not None or (time.monotonic() if now is None else now) >= self.deadline:
            return False
        offset = (pdr.started_at_ms - self.started_at_ms) & _CLOCK_MASK
        minute, remainder = divmod(offset, MINUTE_MS)
        collar = pdr.collar_id
        expected_session = self.collar_sessions.get(collar)
        if (gateway_id != self.gateway_id or pdr.gateway_session_id != self.gateway_session
                or expected_session is None or pdr.collar_session_id != expected_session
                or pdr.profile_id != self.current_profile or pdr.duration_ms != MINUTE_MS
                or remainder or minute >= WINDOW_MINUTES):
            return False
        pdr_source = (collar, pdr.window_id)
        if self._pdr_sources.get(pdr_source, minute) != minute:
            return False

        pdr_value = pdr.pdr_x100 if (pdr.valid and pdr.pdr_x100 is not None
                                      and 0 <= pdr.pdr_x100 <= 10_000
                                      and pdr.estimated_tx_count is not None
                                      and 2 <= pdr.received_count <= pdr.estimated_tx_count) else None
        snr = rssi = count = None
        if (radio and radio.kind == "RADIO" and radio.producer_id == gateway_id
                and radio.collar_id == collar and radio.session_id == self.gateway_session
                and radio.window_id == pdr.window_id and radio.started_at_ms == pdr.started_at_ms
                and radio.duration_ms == MINUTE_MS and 1 <= radio.sample_count <= 255):
            count = radio.sample_count
            if radio.snr_mean_valid and radio.snr_mean_x100 is not None and -32768 <= radio.snr_mean_x100 <= 32767:
                snr = radio.snr_mean_x100
            if radio.rssi_mean_valid and radio.rssi_mean_x100 is not None and -25500 <= radio.rssi_mean_x100 <= 0:
                rssi = (-radio.rssi_mean_x100 + 50) // 100

        hdop_value = None
        if (hdop and hdop.kind == "HDOP" and hdop.producer_id == gateway_id and hdop.collar_id == collar
                and hdop.session_id == expected_session and hdop.gateway_session_id == self.gateway_session
                and aligned_hdop_start_ms == pdr.started_at_ms and hdop.duration_ms == MINUTE_MS
                and 1 <= hdop.sample_count <= 60 and hdop.hdop_mean_valid
                and hdop.hdop_mean_x100 is not None and 0 <= hdop.hdop_mean_x100 < 65535):
            hdop_value = hdop.hdop_mean_x100

        sample = FpgaSample(collar, minute, pdr_value, snr, rssi, hdop_value, count)
        key = (minute, collar)
        previous = self._samples.get(key)
        if previous is not None:
            if previous.valid_vote or (previous.pdr_x100 is not None and previous.pdr_x100 != pdr_value):
                return False
            if not sample.valid_vote and (previous.pdr_x100 is not None or pdr_value is None):
                return False
        self._samples[key] = sample
        self._pdr_sources[pdr_source] = minute
        return True

    def close_if_due(self, now: float) -> tuple[FpgaSample, ...] | None:
        if self._closed is None and now >= self.deadline:
            self._closed = tuple(self._samples[key] for key in sorted(self._samples))
        return self._closed

    async def close_at_deadline(self) -> tuple[FpgaSample, ...]:
        while (result := self.close_if_due(time.monotonic())) is None:
            await asyncio.sleep(max(0, self.deadline - time.monotonic()))
        return result


def validate_fpga_result(current: int, profile: int, reason: int, critical: bool) -> None:
    """Validate result semantics after the future transport checks the batch identity."""
    if (any(type(value) is not int for value in (current, profile, reason))
            or not 0 <= current <= 3 or not 0 <= profile <= 3 or type(critical) is not bool):
        raise ValueError("invalid FPGA result fields")
    if critical != (reason == 3) or (
        (reason == 1 and profile != current + 1)
        or (reason == 2 and profile != current - 1)
        or (reason in (0, 3, 4, 5) and profile != current)
        or reason not in range(6)
    ):
        raise ValueError("inconsistent FPGA result")


@dataclass(frozen=True)
class FpgaDecision:
    profile: int
    status: str
    reason: int | None = None
    critical: bool = False


async def await_fpga_decision(
    current: int,
    batch_id: str,
    reply: Awaitable[tuple[str, int, int, bool]],
    timeout: float,
) -> FpgaDecision:
    """Correlate a transport reply. All errors preserve the current profile."""
    try:
        returned_id, profile, reason, critical = await asyncio.wait_for(reply, timeout)
        if returned_id != batch_id:
            raise ValueError("unexpected FPGA batch")
        validate_fpga_result(current, profile, reason, critical)
    except TimeoutError:
        return FpgaDecision(current, "timeout")
    except (OSError, EOFError):
        return FpgaDecision(current, "communication_error")
    except (ValueError, TypeError):
        return FpgaDecision(current, "invalid_response")
    return FpgaDecision(profile, _RESULT_STATUS[reason], reason, critical)
