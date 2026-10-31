"""
TelemetrySource -- the seam between the application and wherever bytes come from.

This is the interface that lets the reactor dashboard run for two days without
an Arduino and then switch to real hardware by changing one configuration
value, with no edit to any dashboard, analytics or backend code.

THE CONTRACT IS ONE METHOD
--------------------------
A telemetry source provides bytes. That is its entire responsibility.

It is deliberately NOT responsible for:

    starting        A source that needs setup does it in its constructor.
    stopping        See "Shutdown" below.
    threading       An implementation may use a thread, a timer, or nothing.
    buffering       An implementation may queue internally, or not.
    scheduling      The driver decides when to read.
    framing         PacketDecoder owns that.
    connection state See "Link health" below.

Every one of those is an implementation detail that differs between the
simulator and the UART, and none of them is something a caller needs in order
to obtain bytes. Forcing them into the interface would mean SimulationSource
growing empty start()/stop() methods purely to satisfy a contract written for
UARTSource -- an interface serving its implementations rather than its callers.

WHY typing.Protocol RATHER THAN AN ABC
--------------------------------------
Structural typing: an implementation satisfies this by having a read() method,
not by inheriting anything. SimulationSource and UARTSource therefore share no
base class and cannot accumulate shared behaviour in one -- composition over
inheritance, enforced by there being nothing to inherit from.

Neither implementation needs to import this module at all. The coupling
between the two sides of the seam is exactly zero; this file exists for type
checkers and for the reader.

The accepted cost: no construction-time guarantee that an implementation is
complete. A missing read() surfaces as an AttributeError when first called
rather than at instantiation. With one required method, two in-repo
implementations and tests covering both, that is a trade worth making.

The Protocol is deliberately not @runtime_checkable. isinstance() against a
runtime-checkable Protocol verifies only that an attribute named `read`
exists -- not its signature, arity or return type. That is false assurance,
and false assurance in an industrial codebase is worse than none.

PULL, NOT PUSH
--------------
The driver polls; the source does not call back. A push interface would
require every source to own a timer or thread in order to have something to
push from, which reintroduces exactly the lifecycle this interface excludes.

Polling costs nothing at the rates involved: telemetry arrives at 1 Hz, a
whole packet is 56 bytes, and a poll of an idle source returns an empty
bytes object.

SHUTDOWN
--------
There is no close(). Reconnection after a cable fault is UARTSource's own
internal concern -- it reopens its own port without anyone asking -- and on
process exit the operating system releases the file descriptor. An explicit
lifecycle method would exist to serve one implementation and be a no-op in the
other.

If a future source ever genuinely requires deterministic release before
process exit (a network socket a server is counting, say), the honest fix is a
separate optional protocol checked structurally at the call site, not a
close() forced onto every implementation from here.

LINK HEALTH
-----------
There is no is_connected() and no status signal. Connection state is derived
from frame arrival, not reported by the transport.

That is the more truthful measurement. A serial port can be open, healthy and
entirely useless -- because the Arduino behind it has crashed, lost power or
been unplugged at the far end. The port reports "connected" throughout. What
an operator actually needs to know is whether fresh telemetry is arriving,
which TelemetryStore answers from the arrival times it stamps on each record,
and PacketDecoder supplements with CRC and resync counters.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol


class TelemetrySourceKind(StrEnum):
    """
    What kind of source produced a stream.

    Lives here rather than in config/settings.py because it is a domain fact
    about received telemetry, not a configuration fact. Every stored frame
    carries it (see backend/transport_metadata.py) so that a CSV row, a replay
    file or a screenshot can always answer "was this a real reactor?" -- a
    question nobody should ever have to guess at.

    The configuration loader parses into this; it does not own it.
    """

    SIMULATION = "simulation"
    UART = "uart"

    @property
    def is_simulated(self) -> bool:
        return self is TelemetrySourceKind.SIMULATION


class TelemetrySource(Protocol):
    """A source of raw telemetry bytes. See the module docstring for rationale."""

    def read(self) -> bytes:
        """
        Return the bytes currently available, which may be none.

        Contract
        --------
        * Returns b"" when no data is available. This is the normal idle case
          and is not an error.
        * MUST NOT block indefinitely. A short internal timeout is acceptable;
          waiting for data to arrive is not. The caller may be driving a GUI.
        * Byte boundaries carry no meaning. A return value may contain a
          fragment of a packet, several whole packets, or a packet boundary
          part-way through. Reassembly is PacketDecoder's job -- which is
          precisely why this returns bytes rather than packets.
        * May raise on genuine transport failure (the port disappeared). The
          driver is expected to be resilient to that; no exception hierarchy
          is mandated here, because doing so would make this interface know
          about transports.
        """
        ...
