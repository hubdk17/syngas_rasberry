"""
TelemetryFrame -- the domain object that flows through the whole application.

One frame is one instant of reactor state as reported by the Arduino. Every
layer above the transport speaks in these: the store holds them, analytics
reads them, the dashboard renders them, the logger writes them.

It is deliberately a *dumb record*. It carries exactly what the firmware sent
and interprets nothing. Health scores, trends, stability and reconstructed
warning detail are all computed in analytics/, because they are Raspberry Pi
opinions rather than reactor measurements, and that distinction must stay
visible in the type system.

NOTHING HERE IS INVENTED BY THE RECEIVER
----------------------------------------
This class carried a `received_at` field until build step 4. It was removed:
the Arduino never sent an arrival time, so holding one made the domain model a
mixture of twelve values measured by a reactor and one invented by the Pi.

Arrival time, source, sequence and session now live in TransportMetadata, and
the pair is a TelemetryRecord (backend/transport_metadata.py). The payoff is
that two frames with equal fields are now genuinely equal, which is what makes
round-trip, replay and regression comparisons mean anything.

Frames are frozen so they can cross the reader-thread boundary without
locking, and so a widget cannot mutate history it merely displays.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.protocol.spec import ReactorState

# --------------------------------------------------------------------------
# A note on field names
#
# Fields carry a unit suffix where the unit is known and trustworthy
# (pressure_kpa, temperature_c). The gas fields deliberately carry NO unit
# suffix: the firmware has not yet applied the datasheet scaling factor, so
# co/co2/ch4/gas_concentration are raw Modbus counts, not ppm
# (PhysicsEngine.h:110-113, docs/PROTOCOL.md section 6.2).
#
# The missing suffix is the point. It should keep looking wrong until the
# firmware applies scaling, at which point these become _ppm.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TelemetryFrame:
    """One decoded reactor sample. See docs/PROTOCOL.md section 4."""

    #: millis() since Arduino boot. NOT wall clock, and wraps at ~49.7 days.
    #: Authoritative for ordering and for rate maths -- arrival time is not,
    #: because the firmware pipeline runs at 500 ms while telemetry runs at
    #: 1000 ms, so a frame's contents can be up to 500 ms older than its
    #: transmission (docs/PROTOCOL.md section 6.6).
    device_uptime_ms: int

    pressure_kpa: float
    pressure_rate_kpa_s: float

    #: Always exactly 0.0: no temperature sensor exists in the firmware
    #: (PhysicsEngine.cpp:56). Must render as NO SENSOR, never as a
    #: comfortable green 0 degrees. See docs/PROTOCOL.md section 6.1.
    temperature_c: float
    temperature_rate_c_s: float

    co: float
    co2: float
    ch4: float

    #: ch4 / co2, or 0.0 when co2 is effectively zero. Dimensionless, and so
    #: unaffected by the missing gas scaling factor -- currently the most
    #: trustworthy gas indicator.
    gas_ratio: float

    #: co + co2 + ch4
    gas_concentration: float

    #: XGBoost anomaly probability in [0, 1]. Always computed and always
    #: transmitted, but the verdict is suppressed in firmware, so this is
    #: OBSERVE-ONLY (docs/PROTOCOL.md section 6.3).
    anomaly_score: float

    #: Rolled-up threshold verdict. The firmware computes five granular
    #: warning bits but transmits only this one; analytics/derived_warnings.py
    #: reconstructs the detail (docs/PROTOCOL.md section 11).
    warning: bool
    emergency: bool

    #: Always False today -- see anomaly_score above.
    anomaly: bool

    state: ReactorState

    @property
    def device_uptime_s(self) -> float:
        """Device clock in seconds. Convenience for plotting and rate maths."""
        return self.device_uptime_ms / 1000.0

    def as_wire_tuple(self) -> tuple:
        """
        The 15 wire fields in serialisation order.

        Exists so the encoder never restates the field order that spec.py
        already fixes -- there is one place to get the layout wrong, not two.
        """
        return (
            self.device_uptime_ms,
            self.pressure_kpa,
            self.pressure_rate_kpa_s,
            self.temperature_c,
            self.temperature_rate_c_s,
            self.co,
            self.co2,
            self.ch4,
            self.gas_ratio,
            self.gas_concentration,
            self.anomaly_score,
            int(self.warning),
            int(self.emergency),
            int(self.anomaly),
            int(self.state),
        )
