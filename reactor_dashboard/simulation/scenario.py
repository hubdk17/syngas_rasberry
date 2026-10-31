"""
ScenarioEngine -- decides what is being done to the reactor, and when.

Responsibility: own the lifecycle of process upsets and report the current
Disturbance. It does not apply the disturbance; ReactorPhysics and
TelemetryGenerator do that. It does not know what a packet is.

SCENARIOS ARE DATA, NOT CODE
----------------------------
A scenario is a ScenarioDefinition loaded from app.toml. Adding "the CH4 line
partially blocks for four minutes" is a new [[simulation.scenario]] table, not
a new class, not a subclass, and not an edit to this file.

That constraint is what keeps this module small. The alternative -- a class
per scenario behind a common interface -- would mean every new failure mode a
commissioning engineer wants to reproduce requires a Python change, a review
and a redeploy. Here it requires editing a config file and restarting.

The cost is that scenarios can only express what ScenarioDefinition can
express: sustained pressure bias, rate impulse, gas scaling, and sensor
stalls. That covers every failure mode the current firmware can actually
report. When a scenario genuinely needs something outside that vocabulary,
the honest fix is a new field on ScenarioDefinition -- one edit, in one place,
still data-driven.

TWO MODES
---------
    RANDOM  Upsets arrive on their own as a Poisson process. The default,
            and the mode for long-duration endurance runs: leave it going
            overnight and the HMI meets combinations nobody staged.

    MANUAL  Nothing happens unless triggered. For development, demos and
            deterministic UI testing, where waiting several minutes for a
            random pressure ramp is not a workflow.

Both modes share the same definitions and the same execution path. Mode
changes only who decides to start a scenario, which is why there is no
behavioural difference between a randomly-fired and a manually-fired upset.

RAMP SHAPES
-----------
Disturbances ease in and out on a raised-cosine envelope rather than switching
on as a step. A step change in pressure bias would produce an instantaneous
dP/dt spike far beyond anything physical, tripping the rate warning as an
artefact of the simulation rather than as a property of the scenario.
"""

from __future__ import annotations

import math
import random

from config.settings import ScenarioDefinition, ScenarioMode, ScenarioTiming
from simulation.disturbance import NO_DISTURBANCE, Disturbance

# ScenarioDefinition, ScenarioMode and ScenarioTiming are defined in
# config/settings.py rather than here. They are the typed representation of
# app.toml tables and are read from nowhere else, so keeping them with the
# other configuration types gives one dependency direction -- simulation
# imports config, never the reverse. This module owns their *behaviour*.


class ScenarioEngine:
    """Runs at most one scenario at a time and reports its current effect."""

    def __init__(
        self,
        definitions: tuple[ScenarioDefinition, ...],
        mode: ScenarioMode,
        timing: ScenarioTiming,
        rng: random.Random,
    ) -> None:
        self._definitions = {definition.name: definition for definition in definitions}
        self._mode = mode
        self._timing = timing
        self._rng = rng

        self._active: ScenarioDefinition | None = None
        self._elapsed_s = 0.0

        self._idle_remaining_s = self._next_idle_gap()

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    @property
    def mode(self) -> ScenarioMode:
        return self._mode

    @property
    def available(self) -> tuple[str, ...]:
        """Names that can be triggered. Sorted, for a stable UI listing."""
        return tuple(sorted(self._definitions))

    @property
    def active_name(self) -> str | None:
        """The running scenario, or None when the reactor is undisturbed."""
        return self._active.name if self._active is not None else None

    @property
    def active_progress(self) -> float:
        """
        How far through the active scenario, in [0, 1]. Zero when idle.

        Exposed so a debug panel can show a progress bar without having to
        track the scenario's start time itself.
        """
        if self._active is None or self._active.duration_s <= 0.0:
            return 0.0

        return min(1.0, self._elapsed_s / self._active.duration_s)

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    def trigger(self, name: str) -> None:
        """
        Start a scenario immediately, replacing any that is running.

        Operator intent outranks a randomly-scheduled upset: someone
        demonstrating the emergency banner should not have to wait for an
        unrelated gas excursion to finish.

        Works in both modes. MANUAL only stops the engine *starting* scenarios
        on its own; it does not make it inert.

        Raises:
            ValueError: No such scenario. The message lists what is available,
                because the caller is usually a person typing a name.
        """
        definition = self._definitions.get(name)

        if definition is None:
            available = ", ".join(self.available) or "none defined"

            raise ValueError(
                f"Unknown scenario {name!r}. Available: {available}"
            )

        self._active = definition
        self._elapsed_s = 0.0

    def cancel(self) -> None:
        """Stop the active scenario and return the reactor to nominal."""
        self._active = None
        self._elapsed_s = 0.0
        self._idle_remaining_s = self._next_idle_gap()

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def update(self, dt_s: float) -> Disturbance:
        """
        Advance by dt_s and return the disturbance now in effect.

        Returns NO_DISTURBANCE when the reactor is running nominally.
        """
        if dt_s <= 0.0:
            raise ValueError(f"dt_s must be positive, got {dt_s}")

        if self._active is None:
            self._advance_idle(dt_s)

            if self._active is None:
                return NO_DISTURBANCE

        self._elapsed_s += dt_s

        if self._elapsed_s >= self._active.duration_s:
            # Completed. Release the reactor and schedule the next idle gap.
            self.cancel()

            return NO_DISTURBANCE

        return self._disturbance_for(self._active, self.active_progress)

    def _advance_idle(self, dt_s: float) -> None:
        """Count down to the next randomly-scheduled scenario."""
        if self._mode is not ScenarioMode.RANDOM:
            return

        if not self._definitions:
            return

        self._idle_remaining_s -= dt_s

        if self._idle_remaining_s > 0.0:
            return

        selected = self._select_weighted()

        if selected is not None:
            self._active = selected
            self._elapsed_s = 0.0

    def _select_weighted(self) -> ScenarioDefinition | None:
        """
        Pick a scenario in proportion to its weight.

        Returns None when every definition has zero weight -- a legitimate
        configuration meaning "nothing fires automatically, but these remain
        available to trigger by hand".
        """
        candidates = [
            definition
            for definition in self._definitions.values()
            if definition.weight > 0.0
        ]

        if not candidates:
            return None

        weights = [definition.weight for definition in candidates]

        return self._rng.choices(candidates, weights=weights, k=1)[0]

    def _next_idle_gap(self) -> float:
        mean = self._timing.mean_interval_s

        if mean <= 0.0:
            return self._timing.min_gap_s

        return self._timing.min_gap_s + self._rng.expovariate(1.0 / mean)

    @staticmethod
    def _disturbance_for(
        definition: ScenarioDefinition, progress: float
    ) -> Disturbance:
        """Scale a definition by its ramp envelope at this point in its run."""
        envelope = _ramp_envelope(progress, definition.ramp_fraction)

        return Disturbance(
            pressure_bias_kpa=definition.pressure_bias_kpa * envelope,
            # A different envelope, because an impulse is a different kind of
            # thing from a bias. A bias is a sustained offset and holds at
            # full value; an impulse is a transient and must come back.
            #
            # Under the raised-cosine envelope an impulse integrated in one
            # direction for the whole scenario, so a 7.5 kPa/s "spike" lasting
            # 25 s displaced pressure by 130 kPa and tripped the EMERGENCY
            # limit -- when its entire purpose was to trip the RATE limit
            # while absolute pressure stayed in band.
            pressure_impulse_kpa_s=(
                definition.pressure_impulse_kpa_s * _transient_envelope(progress)
            ),
            # Gas scaling eases toward 1.0, not toward 0.0 -- the neutral
            # value for a multiplier is unity, and enveloping it to zero would
            # mean "all gas production stops" during every ramp.
            gas_multiplier=1.0 + (definition.gas_multiplier - 1.0) * envelope,
            # Sensor stalls are binary. A sensor is either reporting or it is
            # not; there is no half-stalled state to ramp through.
            stall_pressure_sensor=definition.stall_pressure_sensor,
            stall_gas_sensors=definition.stall_gas_sensors,
        )


def _ramp_envelope(progress: float, ramp_fraction: float) -> float:
    """
    Raised-cosine ease in and out. Returns a factor in [0, 1].

    Chosen over a linear ramp because its derivative is zero at both ends: the
    disturbance starts and stops smoothly instead of producing a corner in
    dP/dt that the rate threshold would see as a genuine transient.
    """
    progress = min(1.0, max(0.0, progress))

    if ramp_fraction <= 0.0:
        return 1.0

    ramp = min(0.5, ramp_fraction)

    if progress < ramp:
        return 0.5 * (1.0 - math.cos(math.pi * progress / ramp))

    if progress > 1.0 - ramp:
        remaining = (1.0 - progress) / ramp

        return 0.5 * (1.0 - math.cos(math.pi * remaining))

    return 1.0


def _transient_envelope(progress: float) -> float:
    """
    One full sine cycle over the scenario. Returns a factor in [-1, 1].

    Used for rate impulses. Because the positive and negative halves cancel,
    the disturbance produces a rise and an equal recovery with no net
    displacement -- pressure kicks up, then comes back to where it started.

    That is what makes a spike scenario able to breach the rate-of-change
    limit without also breaching the absolute pressure limit, which is
    precisely the condition the dashboard needs to be able to show: a reactor
    losing control while every absolute reading still looks fine.
    """
    return math.sin(2.0 * math.pi * min(1.0, max(0.0, progress)))
