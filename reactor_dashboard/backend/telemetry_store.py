"""
TelemetryStore -- the single owner of all received telemetry.

Every consumer reads through this: the dashboard, analytics, the logger, and
replay. None of them keeps its own copy.

That rule is worth stating plainly because the alternative is so easy to drift
into. A gauge widget that caches "the last pressure it drew", an analytics
class that accumulates its own rolling window, a logger with a pending list --
each is individually reasonable, and together they mean the screen can show
four different answers to the same question. On a reactor HMI the operator has
no way to tell which one is right.

WHAT IT DOES
------------
    - Holds a bounded, chronological window of TelemetryRecords
    - Assigns transport metadata on receipt (arrival, sequence, session)
    - Detects device restarts, via DeviceClock
    - Exposes typed column views for plotting and analytics

WHAT IT DOES NOT DO
-------------------
    - Decode bytes            PacketDecoder does that
    - Judge plausibility      analytics does that
    - Compute health scores   analytics does that
    - Know what a widget is   nothing here imports Qt

STORAGE: deque, NOT numpy
-------------------------
The canonical container is `collections.deque(maxlen=capacity)`. Records go in
as immutable objects and come out unchanged.

numpy appears only in the accessors, which materialise a column on demand for
plotting. It is a *view*, never the storage. A numpy-backed ring buffer would
mean either a struct-of-arrays layout -- which stops a record being a single
object anyone can pass around -- or repeated resizing, and it would put a
numeric type at the centre of a container whose job is to hold domain objects.

deque gives O(1) append and free eviction at capacity. Its weakness is O(n)
indexing in the middle, which never comes up here: consumers either read the
newest record or take a whole column.

Materialising a column is not cached. At the 1 Hz frame rate a full hour is
3600 records, and building one float64 column from that costs a few hundred
microseconds -- a fraction of a percent of one core at a 5 Hz repaint. If
profiling on the Pi ever says otherwise, the fix is a dirty flag invalidated
in append(); it is not worth the complexity in advance.

THREADING
---------
Not internally synchronised, by design. The store is owned by the Qt main
thread; the reader thread hands frames over through a queued signal and never
touches it. Adding a lock would suggest a sharing model that must not exist.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Callable, Iterator

import numpy as np

from backend.device_clock import DeviceClock
from backend.telemetry_frame import TelemetryFrame
from backend.telemetry_source import TelemetrySourceKind
from backend.transport_metadata import TelemetryRecord, TransportMetadata


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TelemetryStore:
    """A bounded, chronological window of received telemetry."""

    def __init__(
        self,
        capacity: int,
        source: TelemetrySourceKind,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        """
        Args:
            capacity: Records retained. Older ones are evicted.
            source: What is producing the stream. Stamped on every record so a
                log or screenshot can always answer "was this real?".
            clock: Wall-clock source, injected so tests can produce
                deterministic arrival times without sleeping.
        """
        if capacity <= 0:
            raise ValueError(f"capacity must be positive, got {capacity}")

        self._records: deque[TelemetryRecord] = deque(maxlen=capacity)
        self._source = source
        self._clock = clock

        self._device_clock = DeviceClock()

        #: Lifetime count, unaffected by eviction. Distinct from len(self),
        #: which is only what is currently retained.
        self._total_received = 0

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------

    def append(self, frame: TelemetryFrame) -> TelemetryRecord:
        """
        Record one received frame and return the record built from it.

        Transport metadata is assigned here rather than by the decoder,
        because the decoder is a pure protocol translator and knows nothing
        about arrival, ordering or sessions.
        """
        reading = self._device_clock.observe(frame.device_uptime_ms)

        metadata = TransportMetadata(
            received_at=self._clock(),
            source=self._source,
            sequence=self._total_received,
            session=reading.session,
            device_gap_ms=reading.gap_ms,
        )

        record = TelemetryRecord(frame=frame, transport=metadata)

        self._records.append(record)
        self._total_received += 1

        return record

    def clear(self) -> None:
        """
        Discard retained records.

        Session numbering and the lifetime counter are deliberately preserved:
        clearing the display window does not un-restart the reactor, and a
        sequence number that went backwards would break any consumer using it
        as a cursor.
        """
        self._records.clear()

    # ------------------------------------------------------------------
    # Size and identity
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[TelemetryRecord]:
        return iter(self._records)

    @property
    def is_empty(self) -> bool:
        return not self._records

    @property
    def capacity(self) -> int:
        return self._records.maxlen or 0

    @property
    def is_full(self) -> bool:
        """True once eviction has begun -- the window is the configured span."""
        return len(self._records) == self.capacity

    @property
    def total_received(self) -> int:
        """Frames received over the process lifetime, including evicted ones."""
        return self._total_received

    @property
    def source(self) -> TelemetrySourceKind:
        return self._source

    @property
    def session(self) -> int:
        """Index of the device's current continuous run."""
        return self._device_clock.session

    @property
    def restarts(self) -> int:
        """How many device restarts have been observed."""
        return self._device_clock.restarts

    # ------------------------------------------------------------------
    # Record access
    #
    # Deliberately narrow. The deque is never handed out -- a consumer holding
    # a reference to it would see it mutate underneath them, and could mutate
    # it back.
    # ------------------------------------------------------------------

    def latest(self) -> TelemetryRecord | None:
        """The most recent record, or None if nothing has arrived."""
        return self._records[-1] if self._records else None

    def latest_frame(self) -> TelemetryFrame | None:
        """
        The most recent frame.

        Offered because the majority of callers want the reactor's values and
        not the delivery details, and `store.latest().frame` forces every one
        of them to handle the None twice.
        """
        record = self.latest()

        return record.frame if record is not None else None

    def records_since(self, sequence: int) -> tuple[TelemetryRecord, ...]:
        """
        Records with a sequence number strictly greater than `sequence`.

        The incremental cursor a logger needs: it asks for everything after
        the last row it wrote. Sequence numbers survive eviction, so a slow
        consumer gets a short answer rather than a wrong one -- what was
        evicted is genuinely gone, and asking again will not bring it back.
        """
        return tuple(
            record
            for record in self._records
            if record.transport.sequence > sequence
        )

    def session_records(self, session: int) -> tuple[TelemetryRecord, ...]:
        """
        Records belonging to one continuous run of the device.

        Used by plotting to break its line at a restart instead of drawing a
        straight segment across a discontinuity that never happened.
        """
        return tuple(
            record for record in self._records if record.transport.session == session
        )

    # ------------------------------------------------------------------
    # Typed column views
    #
    # Every accessor returns a numpy array aligned with every other, so a
    # caller can pair any two without checking. An empty store yields empty
    # arrays rather than None, so plotting code needs no special case for
    # startup -- the commonest source of "works after a minute" bugs.
    #
    # Named in the plural because each returns a series, not a value.
    # ------------------------------------------------------------------

    def timestamps(self) -> np.ndarray:
        """
        Device uptime in seconds -- the x-axis for every trend.

        The device clock, not arrival time, because the firmware's pipeline
        runs at 500 ms while telemetry runs at 1000 ms, so a frame's contents
        can be up to 500 ms older than its transmission
        (docs/PROTOCOL.md section 6.6). Rate maths and trends must use this.

        Note it resets at a device restart. Plot per session, or use
        arrival_times() when a continuous axis matters more than accuracy.
        """
        return self._column(lambda record: record.frame.device_uptime_s)

    def arrival_times(self) -> np.ndarray:
        """
        Host wall clock at receipt, as POSIX seconds.

        Floats rather than datetimes because an object-dtype array is
        unplottable and slow. Monotonic across device restarts, which is what
        makes it the right axis for a session-spanning view.
        """
        return self._column(
            lambda record: record.transport.received_at.timestamp()
        )

    def pressures(self) -> np.ndarray:
        return self._column(lambda record: record.frame.pressure_kpa)

    def pressure_rates(self) -> np.ndarray:
        return self._column(lambda record: record.frame.pressure_rate_kpa_s)

    def temperatures(self) -> np.ndarray:
        """
        Always all zeros with the current firmware -- no sensor exists
        (docs/PROTOCOL.md section 6.1). A caller must render this as NO SENSOR
        rather than as a comfortable 0 degrees.
        """
        return self._column(lambda record: record.frame.temperature_c)

    def temperature_rates(self) -> np.ndarray:
        return self._column(lambda record: record.frame.temperature_rate_c_s)

    def co(self) -> np.ndarray:
        return self._column(lambda record: record.frame.co)

    def co2(self) -> np.ndarray:
        return self._column(lambda record: record.frame.co2)

    def ch4(self) -> np.ndarray:
        return self._column(lambda record: record.frame.ch4)

    def gas_concentrations(self) -> np.ndarray:
        return self._column(lambda record: record.frame.gas_concentration)

    def gas_ratios(self) -> np.ndarray:
        return self._column(lambda record: record.frame.gas_ratio)

    def anomaly_scores(self) -> np.ndarray:
        """Observe-only while the firmware suppresses the verdict (section 6.3)."""
        return self._column(lambda record: record.frame.anomaly_score)

    def states(self) -> np.ndarray:
        """ReactorState values as integers, for banding and shading a trend."""
        return self._column(lambda record: int(record.frame.state), dtype=np.int8)

    def warnings(self) -> np.ndarray:
        return self._column(lambda record: record.frame.warning, dtype=bool)

    def emergencies(self) -> np.ndarray:
        return self._column(lambda record: record.frame.emergency, dtype=bool)

    def sessions(self) -> np.ndarray:
        """
        Session index per sample, so a plot can split at restarts.

        Exposed as a column rather than left for the caller to derive, because
        deriving it means reaching into transport metadata for every record --
        exactly the raw-history access these accessors exist to remove.
        """
        return self._column(lambda record: record.transport.session, dtype=np.int32)

    def device_gaps_ms(self) -> np.ndarray:
        """
        Device-time between consecutive received frames.

        The nominal value is 1000. Anything larger means frames were lost in
        transit, which no single frame can reveal. The first sample of each
        session has no predecessor and is reported as 0.
        """
        return self._column(
            lambda record: record.transport.device_gap_ms or 0, dtype=np.int32
        )

    # ------------------------------------------------------------------
    # Link health
    #
    # Derived from frame arrival, never reported by the transport. A serial
    # port can be open, healthy and completely useless because the Arduino
    # behind it crashed or was unplugged at the far end -- the port says
    # "connected" throughout. What an operator needs is whether fresh
    # telemetry is arriving.
    # ------------------------------------------------------------------

    def seconds_since_last_frame(self, now: datetime | None = None) -> float | None:
        """Age of the newest record, or None if nothing has arrived at all."""
        record = self.latest()

        if record is None:
            return None

        reference = now if now is not None else self._clock()

        return (reference - record.transport.received_at).total_seconds()

    def is_stale(self, timeout_s: float, now: datetime | None = None) -> bool:
        """
        True when no frame has arrived within `timeout_s`.

        An empty store counts as stale: a dashboard that has never received
        anything must show NO SIGNAL, not a hopeful blank.
        """
        age = self.seconds_since_last_frame(now)

        return True if age is None else age > timeout_s

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _column(self, select, dtype=np.float64) -> np.ndarray:
        """Materialise one field across the window as a numpy array."""
        if not self._records:
            return np.empty(0, dtype=dtype)

        return np.fromiter(
            (select(record) for record in self._records),
            dtype=dtype,
            count=len(self._records),
        )
