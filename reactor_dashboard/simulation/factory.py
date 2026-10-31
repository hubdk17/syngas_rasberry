"""
Assembly of the simulated telemetry source.

Responsibility: wire the four simulation components together from validated
configuration. It is the only place that knows the assembly order, which keeps
that knowledge out of both the components and the composition root.

A module-level function rather than a builder class: this is one construction
step with no intermediate state and no options to accumulate.
"""

from __future__ import annotations

import random
import time
from typing import Callable

from config.settings import SimulationConfig
from simulation.fault_injection import FaultInjectingSource
from simulation.reactor_physics import ReactorPhysics
from simulation.scenario import ScenarioEngine
from simulation.simulation_source import SimulationSource
from simulation.telemetry_generator import TelemetryGenerator


def create_simulation_source(
    config: SimulationConfig,
    clock: Callable[[], float] = time.monotonic,
) -> SimulationSource:
    """
    Build a fully wired SimulationSource.

    Args:
        config: Validated simulation settings.
        clock: Monotonic time source, injected so tests can drive the frame
            schedule without sleeping.
    """
    # One shared random stream, not one per component.
    #
    # A single seed then reproduces the entire run -- the same reactor noise,
    # the same scenarios at the same moments, the same anomaly jitter. Giving
    # each component its own generator would require three seeds to reproduce
    # a bug, and getting two of them right would produce a run that looks
    # correct and is not the one being investigated.
    rng = random.Random(config.seed)

    physics = ReactorPhysics(config.physics, rng)

    generator = TelemetryGenerator(config.generator, rng)

    scenarios = ScenarioEngine(
        definitions=config.scenarios,
        mode=config.scenario_mode,
        timing=config.scenario_timing,
        rng=rng,
    )

    source = SimulationSource(
        physics=physics,
        generator=generator,
        scenarios=scenarios,
        period_s=config.frame_period_s,
        clock=clock,
    )

    if not config.link.enabled:
        return source

    # Link faults are a decorator, not a feature of the simulator. Composed in
    # only when asked for, so a developer building a gauge is not fighting
    # injected CRC errors, and so the reactor and the cable can be made to
    # misbehave independently.
    return FaultInjectingSource(
        source=source,
        config=config.link,
        rng=rng,
        clock=clock,
    )
