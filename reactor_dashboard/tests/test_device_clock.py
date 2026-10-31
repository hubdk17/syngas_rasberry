"""
Device clock discontinuity detection.

The Arduino reports uptime since boot. When that counter goes backwards the
receiver has to decide what happened, and the decision is deliberately
lopsided: every regression is treated as a restart, never as a 49.7-day
counter rollover.

These tests pin that choice down, because the temptation to "improve" it by
detecting rollover is real and the improvement would be a regression. A
rollover mislabelled as a restart costs one spurious session boundary every
fifty days. A restart mislabelled as a rollover silently stitches two
unrelated runs together and computes a seven-week elapsed time, poisoning
every rate and gap that spans it.
"""

from backend.device_clock import DeviceClock


def test_first_observation_starts_session_zero_without_a_restart():
    """
    Nothing restarted -- this is simply the first thing we ever saw. Reporting
    a restart here would put a spurious marker on the event timeline every
    time the dashboard starts.
    """
    reading = DeviceClock().observe(1000)

    assert reading.session == 0
    assert reading.restarted is False
    assert reading.gap_ms is None


def test_normal_progression_reports_the_gap():
    clock = DeviceClock()

    clock.observe(1000)
    reading = clock.observe(2000)

    assert reading.session == 0
    assert reading.restarted is False
    assert reading.gap_ms == 1000


def test_gap_reveals_lost_frames():
    """
    The only way packet loss is visible from telemetry alone.

    A single frame cannot report what failed to arrive, and the reactor does
    not know either. Four seconds of device time between consecutive received
    frames means three went missing.
    """
    clock = DeviceClock()

    clock.observe(1000)
    reading = clock.observe(5000)

    assert reading.gap_ms == 4000
    assert reading.restarted is False


def test_regression_starts_a_new_session():
    clock = DeviceClock()

    clock.observe(500_000)
    reading = clock.observe(1000)

    assert reading.restarted is True
    assert reading.session == 1
    assert clock.restarts == 1

    # No gap across a session boundary -- there is nothing meaningful to
    # measure from, and differentiating across the break would emit a large
    # false transient.
    assert reading.gap_ms is None


def test_counter_rollover_is_reported_as_a_restart():
    """
    The deliberate mislabel.

    A value near 2**32 followed by a small one is what a genuine rollover
    looks like. It is still reported as a restart, because being wrong this
    way costs one spurious session boundary every fifty days, while the
    alternative would compute a seven-week gap for every real reboot.
    """
    clock = DeviceClock()

    clock.observe(2**32 - 500)
    reading = clock.observe(500)

    assert reading.restarted is True
    assert reading.session == 1


def test_sessions_accumulate_across_repeated_restarts():
    """
    An Arduino resetting in a loop is a real fault, and an invisible one --
    every individual frame after a reset looks perfectly healthy.
    """
    clock = DeviceClock()

    for cycle in range(5):
        clock.observe(1000)
        clock.observe(2000)
        clock.observe(3000)

        assert clock.session == cycle

        clock.observe(100)  # reboot

    assert clock.restarts == 5
    assert clock.session == 5


def test_equal_timestamps_are_not_a_regression():
    """
    The firmware can emit two frames inside the same millisecond, and its own
    physics layer treats that as "no elapsed time" rather than as an error
    (PhysicsEngine.cpp:105-107).
    """
    clock = DeviceClock()

    clock.observe(1000)
    reading = clock.observe(1000)

    assert reading.restarted is False
    assert reading.gap_ms == 0
    assert clock.restarts == 0


def test_reset_forgets_the_previous_reading_without_renumbering_sessions():
    """
    Called when the link reopens. The device may have rebooted during the
    outage, so differentiating against what was seen before it is meaningless
    -- but the session count is a fact about the device, not about the link,
    and must not be rewritten by a cable being replugged.
    """
    clock = DeviceClock()

    clock.observe(1000)
    clock.observe(9000)  # reboot to a lower value later

    clock.reset()

    reading = clock.observe(50)

    assert reading.restarted is False
    assert reading.gap_ms is None
    assert clock.session == 0
    assert clock.restarts == 0


def test_reading_is_immutable():
    reading = DeviceClock().observe(1000)

    try:
        reading.session = 99
    except Exception:
        return

    raise AssertionError("ClockReading should be frozen")
