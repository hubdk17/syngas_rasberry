"""
ReactorPhysics -- the reactor's true internal state, evolving continuously.

Responsibility: hold the reactor's actual condition and advance it by dt.

That is all. It does not know what a packet is, what a warning is, what a
sensor is, or that anyone is watching. It produces no TelemetryFrame and
imports nothing from the protocol layer. It can be stepped ten thousand times
in a unit test in milliseconds, with no Qt event loop and no clock.

WHY CONTINUOUS STATE RATHER THAN SCRIPTED PLAYBACK
--------------------------------------------------
A recorded or scripted sequence replays the same shapes forever. Every chart
scale, every trend arrow threshold and every stability calculation on the
dashboard would then be tuned against a fixed set of curves, and the first
real reactor data would be the first time the HMI met a shape nobody chose.

A continuous model produces its own shapes. Left running for two days it will
visit combinations nobody staged, which is exactly what endurance testing is
for.

THE MODEL
---------
Each signal is an Ornstein-Uhlenbeck process: mean-reverting drift plus
stochastic noise, integrated Euler-Maruyama.

    x  <-  x + theta*(target - x)*dt + sigma*sqrt(dt)*N(0,1)

Mean reversion is what makes it look like a controlled process rather than a
random walk -- pressure wanders, but a control system keeps pulling it back.
A pure random walk would drift off scale within an hour and would breach the
safety limits for reasons that have nothing to do with reactor behaviour.

The sqrt(dt) on the noise term is not decoration: it is what makes the
statistics independent of step size, so the simulator behaves identically
whether stepped once per second or ten times per second.

Gas species are coupled to pressure, because in a gasifier they physically
are -- higher pressure means more conversion. Without the coupling, gas and
pressure would wander independently and the dashboard's correlation displays
would be showing noise.

Temperature is deliberately absent: the firmware hard-zeroes it because no
sensor exists (PhysicsEngine.cpp:56, docs/PROTOCOL.md section 6.1).
Simulating a temperature the real system cannot report would produce a
dashboard that works beautifully until it meets the reactor.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from config.settings import PhysicsConfig
from simulation.disturbance import Disturbance


@dataclass(frozen=True, slots=True)
class PhysicsState:
    """
    The reactor's true instantaneous condition.

    What is actually happening -- not what the instrumentation reports. The
    two differ whenever a sensor has stalled, which is the point of keeping
    them separate types.
    """

    pressure_kpa: float
    co: float
    co2: float
    ch4: float

    @property
    def gas_concentration(self) -> float:
        """Total measured gas load. PhysicsEngine.cpp:62-63"""
        return self.co + self.co2 + self.ch4


class ReactorPhysics:
    """A continuously evolving reactor. Step it; read its state."""

    def __init__(self, config: PhysicsConfig, rng: random.Random) -> None:
        """
        Args:
            config: Process parameters. Injected rather than hard-coded so the
                operating envelope can be retuned once the real reactor is
                characterised -- see docs/ARCHITECTURE.md open question 1.
            rng: Random source. Injected so a seeded run is byte-for-byte
                reproducible, which is what makes deterministic UI testing and
                repeatable bug reports possible.
        """
        self._config = config
        self._rng = rng

        self._pressure = config.pressure_baseline_kpa
        self._co = config.co_baseline
        self._co2 = config.co2_baseline
        self._ch4 = config.ch4_baseline

    @property
    def state(self) -> PhysicsState:
        return PhysicsState(
            pressure_kpa=self._pressure,
            co=self._co,
            co2=self._co2,
            ch4=self._ch4,
        )

    def step(self, dt_s: float, disturbance: Disturbance) -> PhysicsState:
        """
        Advance the reactor by dt_s seconds and return its new state.

        Raises:
            ValueError: dt_s is not positive. A zero or negative step is
                always a scheduling bug in the caller, and silently accepting
                it would produce a frozen or time-reversed reactor that looks
                like a physics problem.
        """
        if dt_s <= 0.0:
            raise ValueError(f"dt_s must be positive, got {dt_s}")

        self._step_pressure(dt_s, disturbance)
        self._step_gases(dt_s, disturbance)

        return self.state

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _step_pressure(self, dt_s: float, disturbance: Disturbance) -> None:
        config = self._config

        target = config.pressure_baseline_kpa + disturbance.pressure_bias_kpa

        drift = config.pressure_reversion_per_s * (target - self._pressure) * dt_s

        shock = config.pressure_noise_kpa * math.sqrt(dt_s) * self._rng.gauss(0.0, 1.0)

        # The impulse is a rate, so it integrates over the step. This is what
        # a scenario uses to produce a rate-of-change warning without moving
        # the absolute pressure out of its normal band.
        kick = disturbance.pressure_impulse_kpa_s * dt_s

        # Absolute pressure cannot go negative. Clamping rather than
        # reflecting because a reactor pinned at vacuum is a plausible
        # reading, whereas a bounce is not.
        self._pressure = max(0.0, self._pressure + drift + shock + kick)

    def _step_gases(self, dt_s: float, disturbance: Disturbance) -> None:
        config = self._config

        # Fractional pressure deviation from nominal, which is what the gas
        # species respond to. Guarded because a zero baseline would be a
        # configuration error rather than a physical state.
        baseline = config.pressure_baseline_kpa

        deviation = (self._pressure / baseline) - 1.0 if baseline > 0.0 else 0.0

        coupling = 1.0 + config.gas_pressure_coupling * deviation

        # A sufficiently large pressure collapse would otherwise drive the gas
        # target negative, which is not a physical state.
        coupling = max(0.0, coupling)

        scale = disturbance.gas_multiplier * coupling

        self._co = self._step_species(self._co, config.co_baseline, scale, dt_s)
        self._co2 = self._step_species(self._co2, config.co2_baseline, scale, dt_s)
        self._ch4 = self._step_species(self._ch4, config.ch4_baseline, scale, dt_s)

    def _step_species(
        self, value: float, baseline: float, scale: float, dt_s: float
    ) -> float:
        config = self._config

        target = baseline * scale

        drift = config.gas_reversion_per_s * (target - value) * dt_s

        # Noise is proportional to the species baseline, so all three are
        # equally noisy in relative terms despite differing by an order of
        # magnitude in absolute value.
        shock = (
            config.gas_noise_fraction
            * baseline
            * math.sqrt(dt_s)
            * self._rng.gauss(0.0, 1.0)
        )

        return max(0.0, value + drift + shock)
