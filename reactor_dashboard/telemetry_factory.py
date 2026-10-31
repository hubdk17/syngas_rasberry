"""
Construction of the configured telemetry source.

Responsibility: read `telemetry.source` and return the matching
TelemetrySource. This is the single place in the project where that choice is
made.

WHY THIS MODULE IS THE WHOLE "SWITCH TO HARDWARE" STORY
-------------------------------------------------------
Everything above the seam -- store, analytics, dashboard, logging -- receives
a TelemetrySource and calls read(). None of them can tell which implementation
they were given, because the interface exposes nothing that would let them.

So swapping simulation for hardware is: one value in app.toml, and one branch
here. When UARTSource lands at build step 10, this function grows one arm and
nothing else in the project changes. If that turns out not to be true, the
seam was built wrong.

It sits at the application root rather than inside backend/ because it is
composition, not domain: it is the one module allowed to know that both
simulation/ and transport/ exist.
"""

from __future__ import annotations

import time
from typing import Callable

from backend.telemetry_source import TelemetrySource
from config.settings import AppConfig, TelemetrySourceKind
from simulation.factory import create_simulation_source


def create_telemetry_source(
    config: AppConfig,
    clock: Callable[[], float] = time.monotonic,
) -> TelemetrySource:
    """
    Build the telemetry source named by configuration.

    Raises:
        NotImplementedError: The configured source is valid but not yet built.
    """
    kind = config.telemetry.source

    if kind is TelemetrySourceKind.SIMULATION:
        return create_simulation_source(config.simulation, clock=clock)

    if kind is TelemetrySourceKind.UART:
        # Deliberately explicit rather than silently falling back to the
        # simulator. Quietly simulating when an engineer asked for hardware
        # would put fabricated reactor data on an operator's screen, which is
        # the single worst failure this application could have.
        raise NotImplementedError(
            "UART telemetry is not implemented yet (build step 10). "
            'Set telemetry.source = "simulation" in app.toml until hardware '
            "support lands."
        )

    # Unreachable: the config loader rejects unknown values. Present so that
    # adding a TelemetrySourceKind member without wiring it here fails loudly
    # instead of returning None.
    raise NotImplementedError(f"No telemetry source registered for {kind!r}")
