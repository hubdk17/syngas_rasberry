"""
Reactor simulation.

Produces byte-identical protocol traffic without an Arduino, so the dashboard
can be built and endurance-tested before hardware exists.

    ScenarioEngine       what is being done to the reactor
           |
    ReactorPhysics       how the reactor responds  (true state)
           |
    TelemetryGenerator   what the Arduino would report  (observed state)
           |
    PacketEncoder        backend/protocol/encoder.py
           |
    SimulationSource     read() -> bytes

Each stage has one responsibility and is testable alone. Warning flags and
reactor states are computed by the same firmware-mirrored logic the reactor
uses (backend/protocol/limits.py), never asserted.

Link faults are a separate, optional layer on top:

    SimulationSource -> FaultInjectingSource -> read() -> bytes

modelling the cable misbehaving rather than the reactor, so the two can be
varied independently.
"""

from simulation.disturbance import NO_DISTURBANCE, Disturbance
from simulation.fault_injection import FaultInjectingSource, FaultStats, LinkFault
from simulation.reactor_physics import PhysicsState, ReactorPhysics
from simulation.scenario import ScenarioEngine
from simulation.simulation_source import SimulationSource
from simulation.telemetry_generator import TelemetryGenerator

__all__ = [
    "NO_DISTURBANCE",
    "Disturbance",
    "FaultInjectingSource",
    "FaultStats",
    "LinkFault",
    "PhysicsState",
    "ReactorPhysics",
    "ScenarioEngine",
    "SimulationSource",
    "TelemetryGenerator",
]
