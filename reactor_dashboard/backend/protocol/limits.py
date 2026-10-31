"""
The firmware's deterministic safety logic, mirrored in Python.

WHY THIS MODULE EXISTS
----------------------
The Arduino computes five granular warning bits but transmits only the
rolled-up `warning` boolean (ThresholdInference.h:32-43, docs/PROTOCOL.md
section 11). The wire format cannot tell an operator *which* limit tripped.

Every one of those five checks depends solely on values that ARE in the
packet, so the Pi can reconstruct all five exactly -- provided it mirrors the
firmware's constants and comparison logic. That mirror lives here.

Two consumers, neither of which owns it:

    simulation/telemetry_generator.py   produces flags the way the reactor
                                        does, rather than asserting them
    analytics/derived_warnings.py       reconstructs which limit tripped
                                        (build step 7)

Putting the logic here rather than in analytics/ is deliberate: this is not a
Raspberry Pi opinion about the data, it is a restatement of what the reactor
already decided. Placing it in analytics/ would also force simulation/ to
import from a layer above it.

THE DUPLICATION HAZARD
----------------------
These constants exist in two codebases. If firmware changes a limit and this
file does not, the Pi's derived detail silently disagrees with the reactor.

Three things contain that risk:

  1. This is the ONLY place the Pi restates a firmware constant. Nothing else
     in the project may hard-code 150.0 or 0.80.
  2. Every constant cites its firmware source line.
  3. The decoder cross-checks continuously: the OR of the five reconstructed
     bits must equal the transmitted `warning` bit, and any disagreement is
     counted in DecoderStats.state_mismatches and shown on the HMI. Drift
     therefore announces itself in normal operation rather than waiting to be
     discovered.

The long-term fix is a firmware v2 that transmits a warning bitmask, removing
the mirror entirely. That is a firmware decision (docs/PROTOCOL.md section 11).
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Pressure thresholds, kPa -- safety_config.h:21-22
# ---------------------------------------------------------------------------

MAX_PRESSURE_KPA = 150.0
EMERGENCY_PRESSURE_KPA = 180.0

# ---------------------------------------------------------------------------
# Temperature thresholds, degrees C -- safety_config.h:28-29
#
# Unreachable today: the firmware hard-zeroes temperature because no sensor
# exists (PhysicsEngine.cpp:56). Mirrored anyway so that adding the sensor
# needs no change here.
# ---------------------------------------------------------------------------

MAX_TEMPERATURE_C = 550.0
EMERGENCY_TEMPERATURE_C = 650.0

# ---------------------------------------------------------------------------
# Gas thresholds -- safety_config.h:35-39
#
# Nominally ppm. The firmware has not yet applied the datasheet scaling
# factor, so these are currently compared against raw Modbus counts
# (docs/PROTOCOL.md section 6.2). The comparison is still faithful to what the
# reactor does -- which is what matters for mirroring.
# ---------------------------------------------------------------------------

MAX_CO_PPM = 1000.0
MAX_CO2_PPM = 8000.0
MAX_CH4_PPM = 5000.0
MAX_TOTAL_GAS_PPM = 12000.0

# ---------------------------------------------------------------------------
# Rate-of-change thresholds -- safety_config.h:45-46
# ---------------------------------------------------------------------------

MAX_PRESSURE_RATE_KPA_PER_SEC = 5.0
MAX_TEMPERATURE_RATE_C_PER_SEC = 10.0

# ---------------------------------------------------------------------------
# AI threshold -- safety_config.h:61
# ---------------------------------------------------------------------------

ANOMALY_THRESHOLD = 0.80


@dataclass(frozen=True, slots=True)
class ThresholdVerdict:
    """
    The firmware's ThresholdResult (ThresholdInference.h:32-43).

    The five granular bits are the detail the wire format discards; `warning`
    and `emergency` are the two that survive transmission.
    """

    pressure_warning: bool
    temperature_warning: bool
    gas_warning: bool
    pressure_rate_warning: bool
    temperature_rate_warning: bool

    warning: bool
    emergency: bool

    def active_warnings(self) -> tuple[str, ...]:
        """
        Human-readable names of the tripped limits, for the event timeline.

        Returns an empty tuple when nothing is tripped, so a caller can treat
        it as a plain sequence without a None check.
        """
        names = (
            ("Pressure", self.pressure_warning),
            ("Temperature", self.temperature_warning),
            ("Gas", self.gas_warning),
            ("Pressure rate", self.pressure_rate_warning),
            ("Temperature rate", self.temperature_rate_warning),
        )

        return tuple(name for name, tripped in names if tripped)


def evaluate_thresholds(
    *,
    pressure_kpa: float,
    pressure_rate_kpa_s: float,
    temperature_c: float,
    temperature_rate_c_s: float,
    co: float,
    co2: float,
    ch4: float,
    gas_concentration: float,
) -> ThresholdVerdict:
    """
    Reproduce ThresholdInference::update (ThresholdInference.cpp:21-69).

    Keyword-only: eight floats in a row is exactly the signature where a
    transposed argument produces plausible wrong answers forever.

    Comparisons are strictly greater-than, matching the firmware. A value
    sitting exactly on a limit is NOT a warning -- worth preserving, because
    an off-by-one-comparison here would make the Pi disagree with the reactor
    on boundary values and nowhere else, which is the hardest kind of bug to
    notice.
    """
    pressure_warning = pressure_kpa > MAX_PRESSURE_KPA

    temperature_warning = temperature_c > MAX_TEMPERATURE_C

    # Total load, plus any single species over its own limit: one gas can
    # breach well before the sum does (ThresholdInference.cpp:36-40).
    gas_warning = (
        gas_concentration > MAX_TOTAL_GAS_PPM
        or co > MAX_CO_PPM
        or co2 > MAX_CO2_PPM
        or ch4 > MAX_CH4_PPM
    )

    # Magnitude, not signed value. A rapid pressure DROP is as significant as
    # a rapid rise -- both mean loss of control (ThresholdInference.cpp:44-47).
    pressure_rate_warning = abs(pressure_rate_kpa_s) > MAX_PRESSURE_RATE_KPA_PER_SEC

    temperature_rate_warning = (
        abs(temperature_rate_c_s) > MAX_TEMPERATURE_RATE_C_PER_SEC
    )

    warning = (
        pressure_warning
        or temperature_warning
        or gas_warning
        or pressure_rate_warning
        or temperature_rate_warning
    )

    # Note: emergency is NOT a roll-up of the warning bits. It has its own,
    # higher limits and covers pressure and temperature only
    # (ThresholdInference.cpp:66-68).
    emergency = (
        pressure_kpa > EMERGENCY_PRESSURE_KPA
        or temperature_c > EMERGENCY_TEMPERATURE_C
    )

    return ThresholdVerdict(
        pressure_warning=pressure_warning,
        temperature_warning=temperature_warning,
        gas_warning=gas_warning,
        pressure_rate_warning=pressure_rate_warning,
        temperature_rate_warning=temperature_rate_warning,
        warning=warning,
        emergency=emergency,
    )
