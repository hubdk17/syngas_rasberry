"""
Disturbance -- what a scenario is currently doing to the reactor.

This is the value object passed from ScenarioEngine to the components that
react to it. It lives in its own module so that ReactorPhysics and
TelemetryGenerator do not have to import ScenarioEngine, and ScenarioEngine
does not have to import them. All three depend on this small shared type and
on nothing of each other's.

Two consumers, each reading only what belongs to it:

    ReactorPhysics       process perturbations -- what is happening to the
                         reactor itself
    TelemetryGenerator   sensor faults -- what the instrumentation reports
                         about it

That split matters. A stalled pressure sensor does not stop the reactor's
pressure changing; it stops the Arduino from seeing it change
(docs/PROTOCOL.md section 6.4). Modelling the two separately is what lets the
simulator reproduce that failure honestly.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Disturbance:
    """A scenario's current effect, already scaled by its ramp envelope."""

    # -- Process perturbations, read by ReactorPhysics ---------------------

    #: Shifts the pressure the reactor settles toward. A sustained excursion:
    #: pressure ramps to the new operating point and stays until the scenario
    #: releases it.
    pressure_bias_kpa: float = 0.0

    #: Adds directly to dP/dt. A transient kick rather than a new setpoint --
    #: this is what produces a rate-of-change warning without the absolute
    #: pressure ever leaving its normal band.
    pressure_impulse_kpa_s: float = 0.0

    #: Scales the gas concentrations the reactor settles toward. 1.0 is
    #: nominal; 2.0 doubles output.
    gas_multiplier: float = 1.0

    # -- Sensor faults, read by TelemetryGenerator -------------------------

    #: The pressure sensor stops updating. The reactor keeps evolving; the
    #: reported value freezes at its last reading and, because the firmware
    #: differentiates the reported value, pressure_rate collapses to zero.
    #: This reproduces the firmware's silent stale-value failure exactly.
    stall_pressure_sensor: bool = False

    #: The gas sensors stop updating. Same mechanism, different signals.
    stall_gas_sensors: bool = False

    @property
    def is_quiescent(self) -> bool:
        """True when nothing is being perturbed. Used to skip event logging."""
        return (
            self.pressure_bias_kpa == 0.0
            and self.pressure_impulse_kpa_s == 0.0
            and self.gas_multiplier == 1.0
            and not self.stall_pressure_sensor
            and not self.stall_gas_sensors
        )


#: The reactor running undisturbed. A shared immutable instance rather than a
#: new object every tick -- the frame rate is 1 Hz, but the scenario engine is
#: polled far more often than that.
NO_DISTURBANCE = Disturbance()
