import asyncio
import time
from dataclasses import replace

import pytest

from app.fpga_window import FpgaWindow, await_fpga_decision, validate_fpga_result
from app.metric_protocol import MetricSummary
from app.pdr_protocol import PdrSummary


def records(minute=0, collar=1):
    start = (0xFFFF_FFF0 + minute * 60_000) & 0xFFFF_FFFF
    session = f"collar-{collar}"
    window = str(minute + 1)
    pdr = PdrSummary(collar, "boot", window, session, start, 60_000, 1, 3, 4, 1, 7500, True, "ok")
    radio = MetricSummary(
        "RADIO", "gw", collar, "boot", window, start, 60_000, 5,
        rssi_mean_x100=-5120, snr_mean_x100=-945, rssi_mean_valid=True, snr_mean_valid=True,
    )
    hdop = MetricSummary(
        "HDOP", "gw", collar, session, "different-window", 100, 60_000, 2,
        hdop_mean_x100=120, hdop_mean_valid=True, gateway_session_id="boot",
    )
    return dict(gateway_id="gw", pdr=pdr, radio=radio, hdop=hdop, aligned_hdop_start_ms=start)


def window():
    return FpgaWindow("gw", "boot", 0xFFFF_FFF0, 1, {1: "collar-1", 2: "collar-2"}, deadline=300)


def test_wrap_out_of_order_and_two_collars():
    batch = window()
    assert batch.add(**records(4, 2), now=260)
    assert batch.add(**records(1, 1), now=261)
    assert batch.add(**records(3, 1), now=262)
    assert not batch.add(**records(4, 2), now=263)
    inputs = batch.close_if_due(300)
    assert [(s.minute_index, s.collar_id) for s in inputs] == [(1, 1), (3, 1), (4, 2)]
    assert all(s.valid_vote and s.snr_x100 == -945 and s.rssi_mag == 51 for s in inputs)


def test_missing_data_and_correction_without_fabricating_zero():
    batch = window()
    data = records()
    missing = replace(data["hdop"], sample_count=0, hdop_mean_x100=None, hdop_mean_valid=False)
    assert batch.add(**{**data, "hdop": missing, "radio": None}, now=69)
    one, = batch.close_if_due(300)
    assert (one.hdop_x100, one.snr_x100, one.rssi_mag, one.sample_count) == (None, None, None, None)
    assert one.metrics_valid == 0b0100 and not one.valid_vote
    assert not batch.add(**data, now=70)
    batch = window()
    assert batch.add(**{**data, "hdop": missing, "radio": None}, now=69)
    assert batch.add(**data, now=70)
    corrected, = batch.close_if_due(300)
    assert corrected.valid_vote


@pytest.mark.parametrize("field,value", [
    ("gateway_session_id", "old"), ("collar_session_id", "old"), ("profile_id", 2),
    ("duration_ms", 59_999), ("started_at_ms", 0xFFFF_FFEF),
])
def test_wrong_window_source_and_profile(field, value):
    batch = window()
    data = records()
    assert not batch.add(**{**data, "pdr": replace(data["pdr"], **{field: value})}, now=100)
    assert batch.close_if_due(300) == ()


def test_no_assumed_alignment_and_signed_field_limits():
    batch = window()
    data = records()
    assert batch.add(**{**data, "aligned_hdop_start_ms": None, "radio": replace(
        data["radio"], rssi_mean_x100=-25501, snr_mean_x100=-32769)}, now=90)
    data = records(1)
    assert batch.add(**{**data, "radio": replace(data["radio"], rssi_mean_x100=-25500,
                                              snr_mean_x100=-32768)}, now=150)
    partial, complete = batch.close_if_due(300)
    assert partial.metrics_valid == 0b0100
    assert complete.valid_vote


def test_no_long_window_or_relabelled_pdr_evidence():
    batch = window()
    data = records()
    for duration in (180_000, 300_000):
        longer = replace(data["pdr"], duration_ms=duration)
        assert not batch.add(**{**data, "pdr": longer}, now=100)
    assert batch.add(**data, now=101)
    next_minute = records(1)
    relabelled = replace(next_minute["pdr"], window_id=data["pdr"].window_id)
    assert not batch.add(**{**next_minute, "pdr": relabelled}, now=150)
    assert batch.add(**next_minute, now=151)
    assert batch.add(**records(1, 2), now=152)
    samples = batch.close_if_due(300)
    assert sum(sample.valid_vote for sample in samples) == 3
    assert {sample.minute_index for sample in samples} == {0, 1}


@pytest.mark.parametrize("absent", ["hdop", "radio"])
def test_pdr_with_one_missing_summary_does_not_vote(absent):
    batch = window()
    data = records()
    assert batch.add(**{**data, absent: None}, now=90)
    sample, = batch.close_if_due(300)
    assert sample.pdr_x100 == 7500 and not sample.valid_vote


async def test_timer_closes_without_packets():
    batch = window()
    assert batch.close_if_due(299.999) is None
    assert not batch.add(**records(), now=300)
    assert batch.close_if_due(300) == ()
    batch = FpgaWindow("gw", "boot", 0, 1, {1: "collar-1"}, deadline=time.monotonic() + 0.01)
    assert await asyncio.wait_for(batch.close_at_deadline(), 1) == ()


@pytest.mark.parametrize("profile,reason,critical", [(2, 4, False), (2, 3, True), (1, 7, False), (2, 1, True)])
def test_invalid_fpga_response(profile, reason, critical):
    with pytest.raises(ValueError):
        validate_fpga_result(1, profile, reason, critical)


async def test_fpga_reply_timeout_disconnect_and_wrong_batch_keep_profile():
    async def reply(result):
        return result

    async def disconnected():
        raise OSError("unplugged")

    for operation, status in (
        (asyncio.sleep(1), "timeout"),
        (disconnected(), "communication_error"),
        (reply(("old", 2, 1, False)), "invalid_response"),
    ):
        result = await await_fpga_decision(1, "batch", operation, 0.01)
        assert (result.profile, result.status) == (1, status)
    result = await await_fpga_decision(1, "batch", reply(("batch", 1, 3, True)), 0.1)
    assert (result.profile, result.status, result.critical) == (1, "critical_alert", True)
    result = await await_fpga_decision(1, "batch", reply(("batch", 1, 4, False)), 0.1)
    assert (result.profile, result.status, result.critical) == (1, "insufficient_data", False)
