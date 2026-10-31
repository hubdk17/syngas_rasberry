"""
TelemetryStore -- the single owner of received telemetry.

Three groups of claims are worth holding down here:

    Storage        a deque bounded at capacity, records immutable, the
                   container never handed out.
    Metadata       arrival, sequence and session are assigned on receipt and
                   kept out of the domain frame.
    Views          typed numpy columns, mutually aligned, empty-safe.

The empty-store cases get more attention than they might seem to deserve. A
dashboard spends its first second with nothing in the store, and an accessor
that returns None instead of an empty array there is the classic source of a
widget that "works after a minute".
"""

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from backend.protocol.spec import ReactorState
from backend.telemetry_frame import TelemetryFrame
from backend.telemetry_source import TelemetrySourceKind
from backend.telemetry_store import TelemetryStore
from backend.transport_metadata import TelemetryRecord, TransportMetadata

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FakeWallClock:
    """Advances only when told to, so arrival times are deterministic."""

    def __init__(self) -> None:
        self.now = EPOCH

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def make_frame(uptime_ms: int = 1000, **overrides) -> TelemetryFrame:
    values = dict(
        device_uptime_ms=uptime_ms,
        pressure_kpa=105.0,
        pressure_rate_kpa_s=0.0,
        temperature_c=0.0,
        temperature_rate_c_s=0.0,
        co=420.0,
        co2=3200.0,
        ch4=1850.0,
        gas_ratio=0.578,
        gas_concentration=5470.0,
        anomaly_score=0.05,
        warning=False,
        emergency=False,
        anomaly=False,
        state=ReactorState.NORMAL,
    )
    values.update(overrides)

    return TelemetryFrame(**values)


def make_store(capacity: int = 10, clock: FakeWallClock | None = None):
    clock = clock or FakeWallClock()

    store = TelemetryStore(
        capacity=capacity,
        source=TelemetrySourceKind.SIMULATION,
        clock=clock,
    )

    return store, clock


def fill(store, count: int, clock: FakeWallClock | None = None, start_ms: int = 1000):
    """Append `count` frames one second apart in device time."""
    for index in range(count):
        if clock is not None:
            clock.advance(1.0)

        store.append(make_frame(uptime_ms=start_ms + index * 1000))


# ==========================================================================
# Storage
# ==========================================================================


def test_new_store_is_empty():
    store, _ = make_store()

    assert len(store) == 0
    assert store.is_empty
    assert store.latest() is None
    assert store.latest_frame() is None


def test_append_returns_the_record_it_stored():
    store, _ = make_store()

    record = store.append(make_frame(uptime_ms=4000))

    assert isinstance(record, TelemetryRecord)
    assert record.frame.device_uptime_ms == 4000
    assert store.latest() is record


def test_records_are_retained_in_arrival_order():
    store, _ = make_store()

    fill(store, 5)

    assert [record.frame.device_uptime_ms for record in store] == [
        1000, 2000, 3000, 4000, 5000
    ]


def test_store_evicts_oldest_at_capacity():
    """
    The deque's maxlen does the eviction. Bounded memory is not optional on a
    512 MB Pi running for weeks.
    """
    store, _ = make_store(capacity=3)

    fill(store, 10)

    assert len(store) == 3
    assert store.capacity == 3
    assert store.is_full

    assert [record.frame.device_uptime_ms for record in store] == [
        8000, 9000, 10000
    ]


def test_total_received_survives_eviction():
    """
    len() is what is retained; total_received is what has arrived. Conflating
    them would make the packet counter on the HMI stop at the window size.
    """
    store, _ = make_store(capacity=3)

    fill(store, 10)

    assert len(store) == 3
    assert store.total_received == 10


def test_capacity_must_be_positive():
    with pytest.raises(ValueError):
        TelemetryStore(capacity=0, source=TelemetrySourceKind.SIMULATION)


def test_clear_discards_records_but_not_counters():
    """
    Clearing the display window does not un-restart the reactor, and a
    sequence number that went backwards would break any consumer using it as
    a cursor.
    """
    store, _ = make_store()

    fill(store, 5)
    store.append(make_frame(uptime_ms=10))  # restart

    store.clear()

    assert store.is_empty
    assert store.total_received == 6
    assert store.restarts == 1
    assert store.session == 1


def test_store_does_not_expose_its_container():
    """
    Handing out the deque would let a consumer see it mutate underneath them,
    or mutate it back. Iteration and typed views are the only ways in.
    """
    store, _ = make_store()
    fill(store, 3)

    for name in ("records", "history", "buffer", "data"):
        assert not hasattr(store, name), f"store exposes raw {name}"


# ==========================================================================
# Transport metadata
# ==========================================================================


def test_metadata_is_assigned_on_receipt():
    clock = FakeWallClock()
    store, _ = make_store(clock=clock)

    clock.advance(5.0)
    record = store.append(make_frame())

    assert record.transport.received_at == EPOCH + timedelta(seconds=5)
    assert record.transport.source is TelemetrySourceKind.SIMULATION
    assert record.transport.sequence == 0
    assert record.transport.session == 0


def test_frame_carries_no_transport_data():
    """
    The separation that build step 4 introduced. A frame is exactly the wire
    payload, so two frames decoded from identical bytes are equal.
    """
    store, _ = make_store()

    record = store.append(make_frame())

    assert not hasattr(record.frame, "received_at")
    assert record.frame == make_frame()


def test_sequence_is_monotonic_and_survives_eviction():
    """The stable cursor an incremental logger relies on."""
    store, _ = make_store(capacity=3)

    fill(store, 10)

    sequences = [record.transport.sequence for record in store]

    assert sequences == [7, 8, 9]


def test_records_since_returns_only_newer_records():
    store, _ = make_store()

    fill(store, 5)

    newer = store.records_since(2)

    assert [record.transport.sequence for record in newer] == [3, 4]


def test_records_since_is_empty_when_caught_up():
    store, _ = make_store()
    fill(store, 3)

    assert store.records_since(2) == ()


def test_records_since_gives_a_short_answer_after_eviction():
    """
    A slow consumer gets what survives, not a wrong answer. What was evicted
    is genuinely gone and asking again will not bring it back.
    """
    store, _ = make_store(capacity=3)

    fill(store, 10)

    assert len(store.records_since(0)) == 3


def test_source_is_recorded_on_every_frame():
    """
    So a CSV row or a screenshot can always answer "was this a real reactor?"
    without relying on someone having noted the configuration at the time.
    """
    store = TelemetryStore(capacity=5, source=TelemetrySourceKind.UART)

    record = store.append(make_frame())

    assert record.transport.source is TelemetrySourceKind.UART
    assert record.transport.is_simulated is False
    assert store.source is TelemetrySourceKind.UART


def test_device_gap_is_recorded():
    store, _ = make_store()

    store.append(make_frame(uptime_ms=1000))
    record = store.append(make_frame(uptime_ms=5000))

    assert record.transport.device_gap_ms == 4000


def test_metadata_is_immutable():
    store, _ = make_store()

    record = store.append(make_frame())

    with pytest.raises(Exception):
        record.transport.sequence = 99


# ==========================================================================
# Device restarts
# ==========================================================================


def test_restart_increments_the_session():
    store, _ = make_store()

    fill(store, 3)
    record = store.append(make_frame(uptime_ms=50))

    assert record.transport.session == 1
    assert record.transport.device_gap_ms is None
    assert store.restarts == 1


def test_session_records_isolates_one_run():
    """
    Plotting reads this to break its line at a restart rather than drawing a
    straight segment across a discontinuity that never happened.
    """
    store, _ = make_store(capacity=20)

    fill(store, 3)
    store.append(make_frame(uptime_ms=100))
    store.append(make_frame(uptime_ms=1100))

    first = store.session_records(0)
    second = store.session_records(1)

    assert [r.frame.device_uptime_ms for r in first] == [1000, 2000, 3000]
    assert [r.frame.device_uptime_ms for r in second] == [100, 1100]


def test_sessions_column_marks_the_boundary():
    store, _ = make_store(capacity=20)

    fill(store, 3)
    store.append(make_frame(uptime_ms=100))

    assert store.sessions().tolist() == [0, 0, 0, 1]


# ==========================================================================
# Typed views
# ==========================================================================


def test_every_accessor_is_empty_safe():
    """
    A dashboard spends its first second with nothing stored. An accessor
    returning None there is the classic cause of a widget that only works
    after a minute.
    """
    store, _ = make_store()

    accessors = [
        store.timestamps, store.arrival_times, store.pressures,
        store.pressure_rates, store.temperatures, store.temperature_rates,
        store.co, store.co2, store.ch4, store.gas_concentrations,
        store.gas_ratios, store.anomaly_scores, store.states,
        store.warnings, store.emergencies, store.sessions,
        store.device_gaps_ms,
    ]

    for accessor in accessors:
        column = accessor()

        assert isinstance(column, np.ndarray), accessor.__name__
        assert len(column) == 0, accessor.__name__


def test_all_columns_are_mutually_aligned():
    """
    A caller must be able to pair any two columns without checking. Plotting
    pressure against a timestamp array of a different length is a crash, and
    a silently truncated one is worse.
    """
    store, clock = make_store(capacity=50)
    fill(store, 20, clock)

    lengths = {
        store.timestamps().size,
        store.arrival_times().size,
        store.pressures().size,
        store.states().size,
        store.warnings().size,
        store.sessions().size,
        store.device_gaps_ms().size,
    }

    assert lengths == {20}


def test_columns_carry_the_stored_values():
    store, _ = make_store()

    store.append(make_frame(uptime_ms=1000, pressure_kpa=101.0))
    store.append(make_frame(uptime_ms=2000, pressure_kpa=160.0, warning=True,
                            state=ReactorState.WARNING))

    assert store.pressures().tolist() == [101.0, 160.0]
    assert store.timestamps().tolist() == [1.0, 2.0]
    assert store.warnings().tolist() == [False, True]
    assert store.states().tolist() == [ReactorState.NORMAL, ReactorState.WARNING]


def test_arrival_times_are_posix_seconds():
    """
    Floats, not datetimes: an object-dtype array is unplottable and slow.
    """
    clock = FakeWallClock()
    store, _ = make_store(clock=clock)

    clock.advance(10.0)
    store.append(make_frame())

    column = store.arrival_times()

    assert column.dtype == np.float64
    assert column[0] == pytest.approx((EPOCH + timedelta(seconds=10)).timestamp())


def test_arrival_times_stay_monotonic_across_a_restart():
    """
    Device timestamps reset at a reboot; host arrival times do not. That is
    what makes arrival the right axis for a session-spanning view.
    """
    clock = FakeWallClock()
    store, _ = make_store(capacity=20, clock=clock)

    fill(store, 3, clock)
    clock.advance(1.0)
    store.append(make_frame(uptime_ms=50))

    arrivals = store.arrival_times()
    devices = store.timestamps()

    assert list(arrivals) == sorted(arrivals)
    assert devices[-1] < devices[-2]  # the device clock went backwards


def test_temperature_column_is_all_zero_with_current_firmware():
    """
    No temperature sensor exists (docs/PROTOCOL.md section 6.1). The column is
    honest about that; rendering it as a comfortable green 0 C is the
    dashboard's mistake to avoid, not the store's to hide.
    """
    store, _ = make_store()
    fill(store, 5)

    assert np.all(store.temperatures() == 0.0)


def test_state_column_uses_a_compact_dtype():
    store, _ = make_store()
    fill(store, 3)

    assert store.states().dtype == np.int8
    assert store.warnings().dtype == np.bool_


def test_device_gaps_report_zero_at_a_session_start():
    """
    The first sample of a session has no predecessor. Zero rather than None
    keeps the column numeric and aligned with every other.
    """
    store, _ = make_store(capacity=20)

    fill(store, 2)
    store.append(make_frame(uptime_ms=50))

    assert store.device_gaps_ms().tolist() == [0, 1000, 0]


def test_columns_reflect_only_the_retained_window():
    store, _ = make_store(capacity=3)

    fill(store, 10)

    assert store.pressures().size == 3
    assert store.timestamps().tolist() == [8.0, 9.0, 10.0]


# ==========================================================================
# Link health
# ==========================================================================


def test_empty_store_is_stale():
    """
    A dashboard that has never received anything must show NO SIGNAL, not a
    hopeful blank.
    """
    store, _ = make_store()

    assert store.seconds_since_last_frame() is None
    assert store.is_stale(timeout_s=3.0) is True


def test_fresh_frame_is_not_stale():
    clock = FakeWallClock()
    store, _ = make_store(clock=clock)

    store.append(make_frame())

    assert store.seconds_since_last_frame() == 0.0
    assert store.is_stale(timeout_s=3.0) is False


def test_link_goes_stale_after_the_timeout():
    clock = FakeWallClock()
    store, _ = make_store(clock=clock)

    store.append(make_frame())

    clock.advance(2.5)
    assert store.is_stale(timeout_s=3.0) is False

    clock.advance(1.0)
    assert store.is_stale(timeout_s=3.0) is True
    assert store.seconds_since_last_frame() == pytest.approx(3.5)


def test_staleness_can_be_evaluated_against_a_supplied_time():
    """
    So a repaint can evaluate every widget against one instant rather than
    letting them disagree by a few milliseconds.
    """
    clock = FakeWallClock()
    store, _ = make_store(clock=clock)

    store.append(make_frame())

    later = EPOCH + timedelta(seconds=30)

    assert store.is_stale(timeout_s=3.0, now=later) is True
    assert store.seconds_since_last_frame(now=later) == pytest.approx(30.0)


# ==========================================================================
# Integration with the real stream
# ==========================================================================


def test_store_ingests_a_real_simulated_stream():
    """
    Source, encoder, decoder, store. The full receive path with nothing
    hand-built.
    """
    from backend.protocol.decoder import PacketDecoder
    from config.settings import load_config
    from simulation.factory import create_simulation_source

    elapsed = [0.0]
    wall = FakeWallClock()

    config = load_config()
    source = create_simulation_source(config.simulation, clock=lambda: elapsed[0])
    decoder = PacketDecoder()

    store = TelemetryStore(
        capacity=config.storage.history_frames,
        source=config.telemetry.source,
        clock=wall,
    )

    for _ in range(500):
        elapsed[0] += 1.0
        wall.advance(1.0)

        for packet in decoder.feed(source.read()):
            if packet.frame is not None:
                store.append(packet.frame)

    assert len(store) == 500
    assert store.total_received == 500
    assert store.restarts == 0

    # Device time advances exactly one frame period per frame -- no gaps on a
    # clean link.
    assert np.all(store.device_gaps_ms()[1:] == 1000)

    assert store.pressures().min() > 0.0
    assert store.timestamps().size == 500


def test_capacity_from_config_matches_the_requested_duration():
    from config.settings import load_config

    storage = load_config().storage

    # 1 Hz cadence, so one record per second of history.
    assert storage.history_frames == storage.history_seconds
    assert storage.stale_after_seconds == pytest.approx(3.0)
