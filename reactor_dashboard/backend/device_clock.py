"""
DeviceClock -- watches the Arduino's uptime counter for discontinuities.

Responsibility: given the sequence of `device_uptime_ms` values as they
arrive, say which continuous run of the device each frame belongs to, and how
much device time passed since the last one.

That is all. It holds no frames, knows nothing about pressure, and can be
exercised with a list of integers.

WHY A REGRESSION IS TREATED AS A RESTART, NOT A ROLLOVER
--------------------------------------------------------
`device_uptime_ms` is `millis()` since boot (PhysicsEngine.cpp:109). It can go
backwards for two reasons:

    the Arduino restarted        common -- power cycle, reset, watchdog,
                                 someone reflashing during commissioning
    the counter wrapped          once every 2**32 ms, about 49.7 days

Both are handled identically: any backward step starts a new session.

Trying to tell them apart is possible in principle -- a rollover leaves the
previous value near 2**32 -- but the asymmetry of being wrong decides it:

    A rollover reported as a restart costs a spurious session boundary once
    every fifty days. The plot breaks its line and the timeline notes a
    restart. Nothing is misreported.

    A restart mistaken for a rollover silently stitches two unrelated runs
    together and computes an elapsed time of roughly seven weeks. Every rate,
    every trend and every gap measurement spanning that point becomes
    nonsense, and nothing on screen says so.

So the code is deliberately wrong in the cheap direction. A device that is
power-cycled during commissioning far more often than once every fifty days
also makes this the *likelier* reading of any given regression.

WHY SESSIONS MATTER TO CONSUMERS
--------------------------------
Rates are differentiated across consecutive frames. Doing that across a
restart would differentiate against a value from before the reboot and emit a
large false transient into a display that is meant to show reactor behaviour.
Plots must break their line at a session boundary rather than drawing a
straight segment across a gap that never happened.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ClockReading:
    """What the device clock made of one observation."""

    #: Which continuous run of the device this frame belongs to, from zero.
    session: int

    #: True when this frame began a new session -- either the device restarted
    #: or its counter wrapped. Never true for the very first observation,
    #: which starts session 0 without anything having restarted.
    restarted: bool

    #: Device-time since the previous observation, or None when there is no
    #: previous observation in this session to measure from.
    #:
    #: At the nominal 1 Hz this is normally 1000. A larger value means frames
    #: went missing between the two, which is the only way packet loss can be
    #: seen from the telemetry itself.
    gap_ms: int | None


class DeviceClock:
    """Stateful observer of one device's uptime counter."""

    def __init__(self) -> None:
        self._session = 0
        self._restarts = 0
        self._previous_uptime_ms: int | None = None

    @property
    def session(self) -> int:
        """The current session index."""
        return self._session

    @property
    def restarts(self) -> int:
        """
        How many restarts have been observed.

        Shown on the HMI: an Arduino resetting repeatedly is a real fault, and
        one that is otherwise invisible because each individual frame after a
        reset looks perfectly healthy.
        """
        return self._restarts

    def observe(self, uptime_ms: int) -> ClockReading:
        """Record one uptime value and report what it means."""
        previous = self._previous_uptime_ms

        self._previous_uptime_ms = uptime_ms

        if previous is None:
            return ClockReading(session=self._session, restarted=False, gap_ms=None)

        if uptime_ms < previous:
            self._session += 1
            self._restarts += 1

            return ClockReading(session=self._session, restarted=True, gap_ms=None)

        # Equal timestamps are not a regression. The firmware can emit two
        # frames inside the same millisecond, and its own physics layer treats
        # that as "no elapsed time" rather than as an error
        # (PhysicsEngine.cpp:105-107).
        return ClockReading(
            session=self._session,
            restarted=False,
            gap_ms=uptime_ms - previous,
        )

    def reset(self) -> None:
        """
        Forget the previous observation without disturbing session numbering.

        Called when the link is reopened: the next frame may come from a
        device that rebooted while disconnected, so differentiating against
        what was seen before the outage would be meaningless.
        """
        self._previous_uptime_ms = None
