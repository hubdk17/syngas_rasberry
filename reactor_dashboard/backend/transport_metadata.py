"""
Transport metadata -- what the Pi knows about a delivery, as opposed to what
the reactor said.

WHY THIS IS SEPARATE FROM TelemetryFrame
----------------------------------------
TelemetryFrame previously carried a `received_at` field. That was wrong: the
Arduino never sent an arrival time, and putting one inside the frame made the
domain model a mixture of two unrelated things -- twelve values measured by a
reactor, and one value invented by the receiver.

The distinction is not academic. It decides what a CSV column means, what a
replay file has to contain, and whether an analytics function that takes a
TelemetryFrame is reasoning about the reactor or about the link. Keeping them
apart means:

    TelemetryFrame      is exactly the wire payload, and nothing else. Two
                        frames with equal fields ARE equal, which is what
                        makes round-trip and replay comparisons meaningful.

    TransportMetadata   is everything the receiver observed about getting it.

A TelemetryRecord is the pair. The store holds records; analytics that cares
only about the reactor reads `record.frame` and is structurally unable to
depend on delivery details.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from backend.telemetry_frame import TelemetryFrame
from backend.telemetry_source import TelemetrySourceKind


@dataclass(frozen=True, slots=True)
class TransportMetadata:
    """How one frame arrived. Assigned by the store on receipt."""

    #: Host wall clock at receipt, timezone-aware and UTC.
    #:
    #: This is the only clock that can be compared with anything off-device.
    #: The reactor's own timestamp is uptime since boot, so it cannot date a
    #: CSV row, cannot be compared across a reboot, and cannot answer "how
    #: long ago was this?".
    received_at: datetime

    #: What produced the stream. Recorded per frame so a log, a replay file or
    #: a screenshot can always answer "was this a real reactor?" without
    #: relying on someone having noted the configuration at the time.
    source: TelemetrySourceKind

    #: Monotonic receive ordinal, from zero, across the process lifetime.
    #:
    #: Survives ring-buffer eviction and device restarts, so it is a stable
    #: cursor for incremental consumers -- a logger asks for everything after
    #: the last sequence it wrote.
    sequence: int

    #: Which continuous run of the device this frame belongs to. Increments
    #: whenever the device clock goes backwards. See backend/device_clock.py.
    session: int

    #: Device-time elapsed since the previously received frame, or None at the
    #: start of a session where there is nothing to measure from.
    #:
    #: This is how packet loss becomes visible. Frames arrive at 1 Hz, so a
    #: gap of 4000 ms means three frames never made it -- something no single
    #: frame can reveal on its own, and something the reactor cannot report
    #: because it does not know what failed to arrive.
    device_gap_ms: int | None

    @property
    def is_simulated(self) -> bool:
        return self.source.is_simulated


@dataclass(frozen=True, slots=True)
class TelemetryRecord:
    """
    One received frame together with how it arrived.

    The unit the store holds and the unit the logger writes. Deliberately
    offers no passthrough properties: a caller writes `record.frame.pressure_kpa`
    rather than `record.pressure_kpa`, so it is always plain at the call site
    whether the value came from the reactor or from the receiver.
    """

    frame: TelemetryFrame
    transport: TransportMetadata
