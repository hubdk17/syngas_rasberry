"""
SimulationSource -- a TelemetrySource backed by a simulated reactor.

Responsibility: decide *when* a frame is due, and hand back its bytes.

It owns cadence and nothing else. It does not model the reactor, does not
decide what a warning is, and does not know a byte offset -- it delegates to
the four collaborators it is given:

    ScenarioEngine      what is being done to the reactor
    ReactorPhysics      how the reactor responds
    TelemetryGenerator  what the Arduino would report about it
    PacketEncoder       how that becomes bytes

Every one of those is injected. This class is therefore about sixty lines of
scheduling, and each collaborator is testable without it.

WHY IT RETURNS BYTES RATHER THAN FRAMES
---------------------------------------
Returning TelemetryFrame directly would be less code and would skip the
encoder entirely. It would also mean that on the day hardware arrives, the
encoder and decoder meet real traffic for the first time -- after two days of
development that exercised neither.

Emitting real protocol bytes means every frame the dashboard displays during
those two days has been through the same serialise/deserialise path the
reactor will use. The decoder, the component most likely to hide an
off-by-one, is under continuous test from day one.

It also makes SimulationSource and UARTSource differ in exactly one respect --
where the bytes originate -- which is the strongest form the seam can take.
"""

from __future__ import annotations

import time
from typing import Callable

from backend.protocol.encoder import encode_frame
from backend.telemetry_frame import TelemetryFrame
from simulation.reactor_physics import ReactorPhysics
from simulation.scenario import ScenarioEngine
from simulation.telemetry_generator import TelemetryGenerator

_MS_PER_SECOND = 1000.0

#: Most frames emitted from a single read() when the caller has fallen
#: behind. A brief UI stall should be caught up smoothly, but a long one --
#: a debugger breakpoint, a suspended laptop -- must not produce a burst of
#: thousands. Beyond this the schedule resynchronises to now, which is what a
#: real link does when the kernel buffer overruns.
_MAX_CATCHUP_FRAMES = 8


class SimulationSource:
    """
    Byte-accurate simulated telemetry. Satisfies TelemetrySource.

    Note there is no start() or stop(): the simulator has nothing to start.
    The first frame falls due one period after construction, so N seconds of
    elapsed time produce exactly N frames.

    Making the first frame due *immediately* was tried and rejected: a read at
    t = period then found frames due at both t = 0 and t = period and emitted
    two, so every caller saw one more frame than seconds elapsed. Waiting one
    period costs a second of NO SIGNAL at startup, which is what a real link
    does anyway while the Arduino's next transmission comes round.
    """

    def __init__(
        self,
        physics: ReactorPhysics,
        generator: TelemetryGenerator,
        scenarios: ScenarioEngine,
        period_s: float,
        clock: Callable[[], float] = time.monotonic,
        encode: Callable[[TelemetryFrame], bytes] = encode_frame,
    ) -> None:
        """
        Args:
            period_s: Frame cadence. Should match the firmware's
                TELEMETRY_PERIOD_MS so the dashboard sees the real rate.
            clock: Monotonic time source. Injected so tests can drive the
                schedule deterministically instead of sleeping.
            encode: Frame serialiser. Injected to keep packet generation a
                separate collaborator rather than something this class knows
                how to do.
        """
        if period_s <= 0.0:
            raise ValueError(f"period_s must be positive, got {period_s}")

        self._physics = physics
        self._generator = generator
        self._scenarios = scenarios
        self._period_s = period_s
        self._clock = clock
        self._encode = encode

        self._uptime_ms = 0
        self._frames_emitted = 0

        # First frame falls due one period from now -- see the class docstring.
        self._next_emit_at = clock() + period_s

    # ------------------------------------------------------------------
    # TelemetrySource
    # ------------------------------------------------------------------

    def read(self) -> bytes:
        """
        Return bytes for every frame now due, or b"" if none is.

        Emits whole packets. A caller polling faster than the frame rate --
        which is the normal case -- gets b"" most of the time, exactly as it
        would from an idle UART.
        """
        now = self._clock()

        if now < self._next_emit_at:
            return b""

        chunks: list[bytes] = []

        while now >= self._next_emit_at and len(chunks) < _MAX_CATCHUP_FRAMES:
            chunks.append(self._encode(self._advance()))

            self._next_emit_at += self._period_s

        if now >= self._next_emit_at:
            # Still behind after the catch-up cap: the caller was stalled for
            # a long time. Drop the backlog rather than accumulating it.
            self._next_emit_at = now + self._period_s

        return b"".join(chunks)

    # ------------------------------------------------------------------
    # Simulator control -- not part of TelemetrySource
    #
    # These exist for the scenario debug panel and for tests. UARTSource has
    # no equivalent, which is precisely why they are not in the seam: a real
    # reactor cannot be told to have a pressure excursion.
    # ------------------------------------------------------------------

    @property
    def scenarios(self) -> ScenarioEngine:
        """The scenario engine, for triggering upsets on demand."""
        return self._scenarios

    @property
    def frames_emitted(self) -> int:
        return self._frames_emitted

    @property
    def uptime_ms(self) -> int:
        """Simulated millis() since reactor boot."""
        return self._uptime_ms

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _advance(self) -> TelemetryFrame:
        """Run one simulation tick and produce the frame for it."""
        self._uptime_ms += int(self._period_s * _MS_PER_SECOND)
        self._frames_emitted += 1

        disturbance = self._scenarios.update(self._period_s)

        state = self._physics.step(self._period_s, disturbance)

        return self._generator.generate(state, disturbance, self._uptime_ms)
