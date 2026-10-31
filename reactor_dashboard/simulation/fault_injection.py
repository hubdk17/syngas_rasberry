"""
FaultInjectingSource -- a telemetry source that damages another one.

Responsibility: model the LINK misbehaving, as opposed to the reactor
misbehaving.

WHY THIS IS NOT PART OF SimulationSource
----------------------------------------
A pressure excursion and a CRC error are different kinds of event. One is the
reactor doing something; the other is the cable doing something. Folding fault
injection into SimulationSource would give that class two responsibilities --
produce telemetry, and corrupt it -- and would make it impossible to test the
dashboard's communication handling without also perturbing the reactor.

Keeping them separate means the two axes can be varied independently: a
perfectly behaved reactor on a terrible link, or a runaway reactor on a
flawless one.

IT WRAPS *ANY* SOURCE
---------------------
Because TelemetrySource is structural, this wraps anything with read() --
including UARTSource. The same fault scenarios can therefore be run against
real hardware later, which is genuinely useful: "does the dashboard recover
from a real cable pull" is a question worth being able to ask on demand rather
than by physically unplugging things.

It is only wired into the simulated path today (see simulation/factory.py),
because injecting faults into a live reactor link by default would be a
surprising thing for a monitoring tool to do.

IT KNOWS NOTHING ABOUT THE PROTOCOL
-----------------------------------
No imports from backend.protocol. No byte offsets, no sync words, no CRC.
Faults are expressed purely on the byte stream:

    dropping a read       -> a lost packet
    flipping a bit        -> a CRC error (or a resync, see below)
    holding a read back   -> a late packet
    returning nothing     -> a dead link

That is all a real fault is. The moment this module needed to know where the
CRC lives in order to break it, protocol knowledge would have leaked out of
backend/protocol/ -- and it does not need to.

One consequence worth stating: a flipped bit lands wherever it lands. Most of
the time that is in the payload and the decoder reports a CRC error; roughly
one time in twenty-eight for a 56-byte packet it lands in the header and the
decoder resynchronises instead. Both are real line faults, both should be
survivable, and neither is worth special-casing to avoid.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable

from config.settings import LinkFaultConfig


class LinkFault(StrEnum):
    """A fault that can be injected on demand."""

    PACKET_LOSS = "packet_loss"
    CORRUPTION = "corruption"
    DELAY = "delay"
    DISCONNECT = "disconnect"


@dataclass(slots=True)
class FaultStats:
    """
    What the injector has done, so a test or debug panel can confirm it.

    Deliberately separate from DecoderStats. These count faults *caused*;
    DecoderStats counts faults *observed*. Comparing the two is how you check
    the dashboard noticed what happened to it.
    """

    reads_passed: int = 0
    packets_dropped: int = 0
    packets_corrupted: int = 0
    packets_delayed: int = 0
    disconnects: int = 0
    bytes_suppressed: int = 0


class FaultInjectingSource:
    """Wraps a TelemetrySource and applies link faults to its byte stream."""

    def __init__(
        self,
        source,
        config: LinkFaultConfig,
        rng: random.Random,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        Args:
            source: Any object with read() -> bytes. Not type-annotated as
                TelemetrySource because doing so would make this module import
                the seam it is a member of; structural typing needs no such
                declaration.
            config: Fault rates.
            rng: Random source, injected so a fault run is reproducible.
            clock: Monotonic time source, for delays and disconnect durations.
        """
        self._source = source
        self._config = config
        self._rng = rng
        self._clock = clock

        self._stats = FaultStats()

        #: Chunks held back by a delay fault, as (release_at, payload).
        self._delayed: list[tuple[float, bytes]] = []

        #: When the link comes back, or None while connected.
        self._reconnect_at: float | None = None

        #: Faults requested by an operator, applied to the next read that has
        #: data. One-shot: triggering is an action, not a mode.
        self._pending: list[LinkFault] = []

    # ------------------------------------------------------------------
    # TelemetrySource
    # ------------------------------------------------------------------

    def read(self) -> bytes:
        now = self._clock()

        upstream = self._source.read()

        if self._is_disconnected(now):
            # Bytes produced during a disconnect are gone. A real unplugged
            # cable does not buffer them for later.
            self._stats.bytes_suppressed += len(upstream)

            return b""

        released = self._release_due(now)

        if not upstream:
            # Nothing new, but a previously delayed chunk may now be due.
            return released

        outcome = self._apply_faults(upstream, now)

        return released + outcome

    # ------------------------------------------------------------------
    # Operator control
    # ------------------------------------------------------------------

    @property
    def stats(self) -> FaultStats:
        return self._stats

    @property
    def source(self):
        """
        The wrapped source.

        Exposed explicitly rather than by forwarding unknown attributes. A
        debug panel reaching the simulator writes `injector.source.scenarios`,
        which says plainly that it is reaching past the injector -- whereas
        attribute forwarding would hide a whole layer.
        """
        return self._source

    @property
    def is_disconnected(self) -> bool:
        return self._is_disconnected(self._clock())

    def trigger(self, fault: LinkFault) -> None:
        """
        Inject a fault on the next read that carries data.

        One-shot rather than a mode: an operator demonstrating CRC handling
        wants one error, not a permanently broken link.
        """
        if fault is LinkFault.DISCONNECT:
            self.disconnect()

            return

        self._pending.append(fault)

    def disconnect(self, duration_s: float | None = None) -> None:
        """Cut the link for `duration_s`, or the configured default."""
        seconds = (
            duration_s if duration_s is not None else self._config.disconnect_duration_s
        )

        self._reconnect_at = self._clock() + seconds
        self._stats.disconnects += 1

    def reconnect(self) -> None:
        """Restore the link immediately, ending any disconnect early."""
        self._reconnect_at = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _is_disconnected(self, now: float) -> bool:
        if self._reconnect_at is None:
            return False

        if now >= self._reconnect_at:
            self._reconnect_at = None

            return False

        return True

    def _release_due(self, now: float) -> bytes:
        """Return delayed chunks whose release time has arrived."""
        if not self._delayed:
            return b""

        due = [payload for release_at, payload in self._delayed if now >= release_at]

        if not due:
            return b""

        self._delayed = [
            entry for entry in self._delayed if now < entry[0]
        ]

        return b"".join(due)

    def _apply_faults(self, payload: bytes, now: float) -> bytes:
        """
        Apply at most one fault to this chunk.

        At most one, because two faults on the same packet -- corrupted AND
        delayed -- would make the resulting decoder behaviour ambiguous to
        interpret, and the point of the injector is to produce conditions
        whose expected outcome is clear.
        """
        fault = self._next_fault()

        if fault is None:
            self._stats.reads_passed += 1

            return payload

        if fault is LinkFault.DISCONNECT:
            self.disconnect()
            self._stats.bytes_suppressed += len(payload)

            return b""

        if fault is LinkFault.PACKET_LOSS:
            self._stats.packets_dropped += 1
            self._stats.bytes_suppressed += len(payload)

            return b""

        if fault is LinkFault.CORRUPTION:
            self._stats.packets_corrupted += 1

            return _flip_one_bit(payload, self._rng)

        # DELAY
        self._delayed.append((now + self._config.delay_seconds, payload))
        self._stats.packets_delayed += 1

        return b""

    def _next_fault(self) -> LinkFault | None:
        """An operator-triggered fault if one is pending, else a random one."""
        if self._pending:
            return self._pending.pop(0)

        config = self._config

        # Checked in a fixed order, each against its own draw. Sampling once
        # and partitioning the range would make the rates interact, so raising
        # the loss rate would silently lower the corruption rate.
        candidates = (
            (LinkFault.DISCONNECT, config.disconnect_probability),
            (LinkFault.PACKET_LOSS, config.packet_loss_probability),
            (LinkFault.CORRUPTION, config.corruption_probability),
            (LinkFault.DELAY, config.delay_probability),
        )

        for fault, probability in candidates:
            if probability > 0.0 and self._rng.random() < probability:
                return fault

        return None


def _flip_one_bit(payload: bytes, rng: random.Random) -> bytes:
    """
    Corrupt exactly one bit at a uniformly random position.

    One bit rather than a whole byte: a single-bit flip is what marginal
    signalling actually produces, and it is the hardest case for a checksum,
    so it is the right thing to hold the CRC to.
    """
    if not payload:
        return payload

    corrupted = bytearray(payload)

    index = rng.randrange(len(corrupted))

    corrupted[index] ^= 1 << rng.randrange(8)

    return bytes(corrupted)
