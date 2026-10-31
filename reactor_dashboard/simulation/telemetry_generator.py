"""
TelemetryGenerator -- what the Arduino would report about the reactor.

Responsibility: turn the reactor's true state into the TelemetryFrame the
firmware would have produced from it.

It does not evolve the reactor (ReactorPhysics does) and it does not know what
a byte is (SimulationSource and PacketEncoder do). It produces frames.

IT MIRRORS THE FIRMWARE'S OWN PIPELINE
--------------------------------------
    firmware:   SensorManager -> PhysicsEngine -> ThresholdInference
                              -> DecisionEngine -> ReactorSample

    generator:  sensor faults -> derive rates -> evaluate_thresholds
                              -> ReactorState.derive -> TelemetryFrame

The correspondence is the point. Warning flags and reactor states are
*computed by the same logic the reactor uses* (backend/protocol/limits.py),
never asserted. A scenario says "raise the pressure"; whether that produces a
WARNING is then decided the same way the Arduino decides it.

Asserting the flags instead would make the simulator a description of what we
expect the firmware to do, and the dashboard would be validated against our
expectation rather than against the firmware's actual behaviour. The one place
those two could differ is precisely the place a bug would hide.

TRUE STATE VERSUS REPORTED STATE
--------------------------------
PhysicsState is what the reactor is doing. This class decides what the
instrumentation says about it, and the two diverge whenever a sensor stalls.

The firmware's stale-value failure is reproduced exactly: PressureSensor keeps
its last good reading when a conversion fails (PressureSensor.cpp:91-100) and
PhysicsEngine copies it without checking the validity flag
(PhysicsEngine.cpp:52). Because rates are differentiated from the *reported*
value, a stalled sensor produces a frozen pressure with a rate of exactly
zero -- which looks like a perfectly stable reactor.

That is the failure the dashboard most needs to be able to catch
(docs/PROTOCOL.md section 6.4), so the simulator has to be able to produce it.
"""

from __future__ import annotations

import math
import random

from backend.protocol.limits import (
    MAX_PRESSURE_RATE_KPA_PER_SEC,
    ThresholdVerdict,
    evaluate_thresholds,
)
from backend.protocol.spec import ReactorState
from backend.telemetry_frame import TelemetryFrame
from config.settings import GeneratorConfig
from simulation.disturbance import Disturbance
from simulation.reactor_physics import PhysicsState

#: Guards the gasRatio division, matching PhysicsEngine.cpp:20. Gas readings
#: are non-negative, so anything at or below this means "no CO2 measured".
_MIN_DIVISOR = 1e-6

_MS_PER_SECOND = 1000.0


class TelemetryGenerator:
    """Produces the TelemetryFrame the firmware would send for a given state."""

    def __init__(self, config: GeneratorConfig, rng: random.Random) -> None:
        self._config = config
        self._rng = rng

        # Last values the instrumentation reported. Held so a stalled sensor
        # can repeat them, and so rates can be differentiated from what was
        # reported rather than from what was true.
        self._reported_pressure: float | None = None
        self._reported_co: float | None = None
        self._reported_co2: float | None = None
        self._reported_ch4: float | None = None

        self._previous_pressure: float | None = None
        self._previous_uptime_ms: int | None = None

        # Slow-moving reference for the synthetic anomaly score. Seeded on the
        # first frame so the model does not spend its first minute reporting
        # an anomaly simply because its baseline started at zero.
        self._pressure_reference: float | None = None
        self._gas_reference: float | None = None

    def generate(
        self,
        state: PhysicsState,
        disturbance: Disturbance,
        uptime_ms: int,
    ) -> TelemetryFrame:
        """
        Build the frame the Arduino would transmit for this reactor state.

        Args:
            state: The reactor's true condition.
            disturbance: Active scenario; only its sensor-fault fields are
                read here. The process fields were already applied by
                ReactorPhysics.
            uptime_ms: Simulated millis() since boot.
        """
        pressure = self._report_pressure(state, disturbance)
        co, co2, ch4 = self._report_gases(state, disturbance)

        pressure_rate = self._differentiate_pressure(pressure, uptime_ms)

        gas_concentration = co + co2 + ch4

        # PhysicsEngine.cpp:65-68
        gas_ratio = ch4 / co2 if co2 > _MIN_DIVISOR else 0.0

        # Hard zero, always. No temperature sensor exists in the firmware, so
        # simulating one would build a dashboard around data the real system
        # cannot produce (docs/PROTOCOL.md section 6.1).
        temperature = 0.0
        temperature_rate = 0.0

        verdict = evaluate_thresholds(
            pressure_kpa=pressure,
            pressure_rate_kpa_s=pressure_rate,
            temperature_c=temperature,
            temperature_rate_c_s=temperature_rate,
            co=co,
            co2=co2,
            ch4=ch4,
            gas_concentration=gas_concentration,
        )

        anomaly_score = self._anomaly_score(
            pressure=pressure,
            pressure_rate=pressure_rate,
            gas_concentration=gas_concentration,
        )

        anomaly = self._anomaly_verdict(anomaly_score)

        # The firmware's fusion policy, not a reimplementation of it.
        state_value = ReactorState.derive(
            warning=verdict.warning, emergency=verdict.emergency, anomaly=anomaly
        )

        return TelemetryFrame(
            device_uptime_ms=uptime_ms,
            pressure_kpa=pressure,
            pressure_rate_kpa_s=pressure_rate,
            temperature_c=temperature,
            temperature_rate_c_s=temperature_rate,
            co=co,
            co2=co2,
            ch4=ch4,
            gas_ratio=gas_ratio,
            gas_concentration=gas_concentration,
            anomaly_score=anomaly_score,
            warning=verdict.warning,
            emergency=verdict.emergency,
            anomaly=anomaly,
            state=state_value,
        )

    def last_verdict_for(self, frame: TelemetryFrame) -> ThresholdVerdict:
        """
        Re-derive the granular warning detail for a frame.

        Offered for tests and for the scenario debug panel. Production code
        uses analytics/derived_warnings.py, which does the same reconstruction
        from a received frame without needing the simulator at all.
        """
        return evaluate_thresholds(
            pressure_kpa=frame.pressure_kpa,
            pressure_rate_kpa_s=frame.pressure_rate_kpa_s,
            temperature_c=frame.temperature_c,
            temperature_rate_c_s=frame.temperature_rate_c_s,
            co=frame.co,
            co2=frame.co2,
            ch4=frame.ch4,
            gas_concentration=frame.gas_concentration,
        )

    # ------------------------------------------------------------------
    # Sensor layer
    # ------------------------------------------------------------------

    def _report_pressure(
        self, state: PhysicsState, disturbance: Disturbance
    ) -> float:
        if disturbance.stall_pressure_sensor and self._reported_pressure is not None:
            # Stale value forwarded silently -- the firmware's own behaviour.
            return self._reported_pressure

        self._reported_pressure = state.pressure_kpa

        return state.pressure_kpa

    def _report_gases(
        self, state: PhysicsState, disturbance: Disturbance
    ) -> tuple[float, float, float]:
        stalled = (
            disturbance.stall_gas_sensors
            and self._reported_co is not None
            and self._reported_co2 is not None
            and self._reported_ch4 is not None
        )

        if stalled:
            return self._reported_co, self._reported_co2, self._reported_ch4

        self._reported_co = state.co
        self._reported_co2 = state.co2
        self._reported_ch4 = state.ch4

        return state.co, state.co2, state.ch4

    # ------------------------------------------------------------------
    # Feature derivation -- mirrors PhysicsEngine.cpp:70-109
    # ------------------------------------------------------------------

    def _differentiate_pressure(self, pressure: float, uptime_ms: int) -> float:
        """
        dP/dt from the REPORTED pressure series.

        Differentiating the reported value rather than the true one is what
        makes a stalled sensor produce a rate of exactly zero, matching the
        firmware.
        """
        previous_pressure = self._previous_pressure
        previous_uptime = self._previous_uptime_ms

        self._previous_pressure = pressure
        self._previous_uptime_ms = uptime_ms

        if previous_pressure is None or previous_uptime is None:
            # First frame: nothing to differentiate against. The firmware
            # returns zero here rather than differentiating from a zero
            # baseline, which would emit a large false spike straight into
            # the safety thresholds (PhysicsEngine.cpp:79-90).
            return 0.0

        elapsed_ms = uptime_ms - previous_uptime

        if elapsed_ms <= 0:
            # Called twice within the same millisecond. The firmware leaves
            # the previous rate standing; zero is the closest equivalent for a
            # generator that does not retain it.
            return 0.0

        return (pressure - previous_pressure) / (elapsed_ms / _MS_PER_SECOND)

    # ------------------------------------------------------------------
    # Synthetic anomaly model
    # ------------------------------------------------------------------

    def _anomaly_score(
        self, *, pressure: float, pressure_rate: float, gas_concentration: float
    ) -> float:
        """
        A stand-in for the XGBoost output, in [0, 1].

        The real forest is a hand-written placeholder in firmware, so there is
        no meaningful model to reproduce. What matters is that the score has
        the right *character*: low and quiet when the reactor is behaving,
        rising when it deviates. A constant or purely random score would let
        the dashboard's AI panel be built without ever being exercised.

        Deviation is measured against a slow exponential reference the
        generator maintains itself, rather than against configured baselines.
        That is self-calibrating -- and it is also roughly what an anomaly
        detector genuinely does, which makes the displayed behaviour honest
        about the kind of signal this is.
        """
        config = self._config

        self._pressure_reference = _update_reference(
            self._pressure_reference, pressure, config.anomaly_reference_alpha
        )
        self._gas_reference = _update_reference(
            self._gas_reference, gas_concentration, config.anomaly_reference_alpha
        )

        pressure_deviation = abs(pressure - self._pressure_reference) / max(
            config.anomaly_pressure_scale_kpa, _MIN_DIVISOR
        )

        rate_deviation = abs(pressure_rate) / MAX_PRESSURE_RATE_KPA_PER_SEC

        gas_scale = max(
            self._gas_reference * config.anomaly_gas_scale_fraction, _MIN_DIVISOR
        )
        gas_deviation = abs(gas_concentration - self._gas_reference) / gas_scale

        stress = max(pressure_deviation, rate_deviation, gas_deviation)

        # Saturating rather than linear-then-clamped.
        #
        # A linear map pinned the score at exactly 1.000 for long stretches of
        # any large excursion, which wastes the display: an operator learns
        # nothing from a bar that is hard against its stop for two minutes.
        # The exponential approaches 1 without reaching it, so the score keeps
        # ranking severity even well past the point a linear map saturated.
        headroom = 1.0 - config.anomaly_baseline

        response = 1.0 - math.exp(-config.anomaly_sensitivity * stress)

        score = (
            config.anomaly_baseline
            + headroom * response
            + self._rng.gauss(0.0, config.anomaly_noise)
        )

        return min(1.0, max(0.0, score))

    def _anomaly_verdict(self, score: float) -> bool:
        """
        Whether to raise the anomaly flag.

        False by default, and that is the faithful answer: the firmware
        compiles with ENABLE_XGBOOST_INFERENCE undefined and forces the flag
        false while the forest is a placeholder (DecisionEngine.cpp:50-56).
        The score is still transmitted.

        The config flag exists so the ANOMALY banner and its colour treatment
        can be exercised on demand. It is off by default because a simulator
        that produces states the reactor cannot currently reach would let the
        dashboard be tuned around a condition that never arrives.
        """
        if not self._config.emulate_anomaly_verdict:
            return False

        from backend.protocol.limits import ANOMALY_THRESHOLD

        return score >= ANOMALY_THRESHOLD


def _update_reference(current: float | None, value: float, alpha: float) -> float:
    """Exponential moving average, seeded on first use."""
    if current is None:
        return value

    return current + alpha * (value - current)
