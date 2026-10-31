"""
The reactor simulator: physics, scenarios, generator and source.

The tests are grouped by component, matching the four responsibilities. What
they collectively pin down is the claim the whole simulation-first plan rests
on:

    The simulator produces telemetry the real firmware could have produced.

Anywhere the simulator is more convenient than the reactor -- a temperature
that is not always zero, an anomaly flag the firmware cannot raise, a warning
asserted rather than computed -- the dashboard would be tuned against fiction.
Several tests below exist only to stop that happening.
"""

import random

import pytest

from backend.protocol.decoder import PacketDecoder
from backend.protocol.limits import (
    EMERGENCY_PRESSURE_KPA,
    MAX_PRESSURE_KPA,
    MAX_PRESSURE_RATE_KPA_PER_SEC,
    MAX_TOTAL_GAS_PPM,
    evaluate_thresholds,
)
from backend.protocol.spec import PacketType, ReactorState
from config.settings import (
    GeneratorConfig,
    PhysicsConfig,
    ScenarioDefinition,
    ScenarioMode,
    ScenarioTiming,
    load_config,
)
from simulation.disturbance import NO_DISTURBANCE, Disturbance
from simulation.factory import create_simulation_source
from simulation.reactor_physics import ReactorPhysics
from simulation.scenario import ScenarioEngine
from simulation.simulation_source import SimulationSource
from simulation.telemetry_generator import TelemetryGenerator

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

PHYSICS = PhysicsConfig(
    pressure_baseline_kpa=105.0,
    pressure_reversion_per_s=0.06,
    pressure_noise_kpa=0.30,
    co_baseline=420.0,
    co2_baseline=3200.0,
    ch4_baseline=1850.0,
    gas_reversion_per_s=0.08,
    gas_noise_fraction=0.010,
    gas_pressure_coupling=0.8,
)

GENERATOR = GeneratorConfig(
    anomaly_baseline=0.05,
    anomaly_sensitivity=0.55,
    anomaly_noise=0.02,
    anomaly_reference_alpha=0.02,
    anomaly_pressure_scale_kpa=12.0,
    anomaly_gas_scale_fraction=0.12,
    emulate_anomaly_verdict=False,
)

TIMING = ScenarioTiming(mean_interval_s=240.0, min_gap_s=90.0)


def quiet_physics() -> PhysicsConfig:
    """Physics with no noise, so a test can assert exact trajectories."""
    return PhysicsConfig(
        pressure_baseline_kpa=105.0,
        pressure_reversion_per_s=0.06,
        pressure_noise_kpa=0.0,
        co_baseline=420.0,
        co2_baseline=3200.0,
        ch4_baseline=1850.0,
        gas_reversion_per_s=0.08,
        gas_noise_fraction=0.0,
        gas_pressure_coupling=0.8,
    )


def quiet_generator() -> GeneratorConfig:
    """Generator with no anomaly jitter."""
    return GeneratorConfig(
        anomaly_baseline=0.05,
        anomaly_sensitivity=0.55,
        anomaly_noise=0.0,
        anomaly_reference_alpha=0.02,
        anomaly_pressure_scale_kpa=12.0,
        anomaly_gas_scale_fraction=0.12,
        emulate_anomaly_verdict=False,
    )


class FakeClock:
    """A manually advanced monotonic clock, so tests never sleep."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def build_source(
    *,
    scenarios: tuple[ScenarioDefinition, ...] = (),
    mode: ScenarioMode = ScenarioMode.MANUAL,
    physics_config: PhysicsConfig | None = None,
    generator_config: GeneratorConfig | None = None,
    seed: int = 1234,
    clock: FakeClock | None = None,
) -> tuple[SimulationSource, FakeClock]:
    clock = clock or FakeClock()
    rng = random.Random(seed)

    source = SimulationSource(
        physics=ReactorPhysics(physics_config or PHYSICS, rng),
        generator=TelemetryGenerator(generator_config or GENERATOR, rng),
        scenarios=ScenarioEngine(scenarios, mode, TIMING, rng),
        period_s=1.0,
        clock=clock,
    )

    return source, clock


def collect_frames(source, clock, seconds: int, decoder=None) -> list:
    """Run the source for `seconds` and return every decoded frame."""
    decoder = decoder or PacketDecoder()
    frames = []

    for _ in range(seconds):
        clock.advance(1.0)
        frames.extend(packet.frame for packet in decoder.feed(source.read()))

    return frames


# ==========================================================================
# ReactorPhysics
# ==========================================================================


def test_physics_starts_at_baseline():
    physics = ReactorPhysics(PHYSICS, random.Random(1))

    assert physics.state.pressure_kpa == PHYSICS.pressure_baseline_kpa
    assert physics.state.co == PHYSICS.co_baseline


def test_physics_reverts_toward_baseline_after_a_push():
    """
    Mean reversion is what makes this a controlled process rather than a
    random walk. Without it pressure drifts off scale within the hour and
    breaches limits for reasons that have nothing to do with the reactor.
    """
    physics = ReactorPhysics(quiet_physics(), random.Random(1))

    # Push it well above baseline with a sustained bias, then release.
    for _ in range(200):
        physics.step(1.0, Disturbance(pressure_bias_kpa=40.0))

    lifted = physics.state.pressure_kpa
    assert lifted > 130.0

    for _ in range(200):
        physics.step(1.0, NO_DISTURBANCE)

    assert physics.state.pressure_kpa == pytest.approx(105.0, abs=1.0)


def test_physics_is_stochastic_without_a_disturbance():
    """Undisturbed does not mean static -- a flat line would be unrealistic."""
    physics = ReactorPhysics(PHYSICS, random.Random(7))

    readings = [physics.step(1.0, NO_DISTURBANCE).pressure_kpa for _ in range(100)]

    assert len(set(readings)) > 90


def test_physics_stays_bounded_over_a_long_run():
    """
    Twelve simulated hours. Mean reversion must keep the process in a
    plausible band -- an endurance run that wanders to 10,000 kPa is not
    testing the dashboard, it is testing float formatting.
    """
    physics = ReactorPhysics(PHYSICS, random.Random(99))

    readings = [physics.step(1.0, NO_DISTURBANCE).pressure_kpa for _ in range(43_200)]

    assert min(readings) > 80.0
    assert max(readings) < 140.0


def test_physics_is_reproducible_from_a_seed():
    """A seeded run replays exactly -- what makes a bug report replayable."""
    def run():
        physics = ReactorPhysics(PHYSICS, random.Random(42))
        return [physics.step(1.0, NO_DISTURBANCE).pressure_kpa for _ in range(50)]

    assert run() == run()


def test_physics_pressure_never_goes_negative():
    """Absolute pressure has a hard floor at zero."""
    physics = ReactorPhysics(PHYSICS, random.Random(3))

    for _ in range(500):
        state = physics.step(1.0, Disturbance(pressure_bias_kpa=-10_000.0))

        assert state.pressure_kpa >= 0.0


def test_physics_gas_follows_pressure():
    """
    In a gasifier the two are physically linked. Without the coupling the
    signals wander independently and the dashboard's correlation displays are
    showing nothing but noise.
    """
    physics = ReactorPhysics(quiet_physics(), random.Random(1))

    for _ in range(300):
        physics.step(1.0, Disturbance(pressure_bias_kpa=40.0))

    assert physics.state.gas_concentration > (
        PHYSICS.co_baseline + PHYSICS.co2_baseline + PHYSICS.ch4_baseline
    )


def test_physics_gas_multiplier_scales_output():
    physics = ReactorPhysics(quiet_physics(), random.Random(1))

    for _ in range(300):
        physics.step(1.0, Disturbance(gas_multiplier=2.0))

    baseline_total = PHYSICS.co_baseline + PHYSICS.co2_baseline + PHYSICS.ch4_baseline

    assert physics.state.gas_concentration == pytest.approx(
        2.0 * baseline_total, rel=0.05
    )


def test_physics_rejects_non_positive_dt():
    """
    A zero or negative step is always a scheduling bug in the caller.
    Accepting it silently produces a frozen or time-reversed reactor that
    looks like a physics problem.
    """
    physics = ReactorPhysics(PHYSICS, random.Random(1))

    with pytest.raises(ValueError):
        physics.step(0.0, NO_DISTURBANCE)

    with pytest.raises(ValueError):
        physics.step(-1.0, NO_DISTURBANCE)


def test_physics_statistics_are_independent_of_step_size():
    """
    The sqrt(dt) on the noise term is not decoration: it is what makes the
    process behave the same whether stepped once per second or ten times.
    """
    def spread(dt: float, steps: int) -> float:
        physics = ReactorPhysics(PHYSICS, random.Random(5))
        readings = [
            physics.step(dt, NO_DISTURBANCE).pressure_kpa for _ in range(steps)
        ]
        mean = sum(readings) / len(readings)
        return (sum((r - mean) ** 2 for r in readings) / len(readings)) ** 0.5

    coarse = spread(1.0, 4000)
    fine = spread(0.1, 40_000)

    assert fine == pytest.approx(coarse, rel=0.35)


# ==========================================================================
# ScenarioEngine
# ==========================================================================

RAMP = ScenarioDefinition(
    name="ramp", duration_s=100.0, pressure_bias_kpa=50.0, ramp_fraction=0.25
)

SPIKE = ScenarioDefinition(
    name="spike", duration_s=12.0, pressure_impulse_kpa_s=8.0
)

STALL = ScenarioDefinition(
    name="stall", duration_s=60.0, ramp_fraction=0.0, stall_pressure_sensor=True
)


def make_engine(*definitions, mode=ScenarioMode.MANUAL, seed=1):
    return ScenarioEngine(definitions, mode, TIMING, random.Random(seed))


def test_manual_mode_stays_idle_until_triggered():
    """
    The whole point of manual mode: a demo or a UI test is not interrupted by
    an unrelated upset firing halfway through.
    """
    engine = make_engine(RAMP)

    for _ in range(10_000):
        assert engine.update(1.0) is NO_DISTURBANCE

    assert engine.active_name is None


def test_random_mode_eventually_fires_a_scenario():
    engine = make_engine(RAMP, mode=ScenarioMode.RANDOM)

    fired = any(not engine.update(1.0).is_quiescent for _ in range(5_000))

    assert fired


def test_random_mode_respects_the_minimum_gap():
    """
    Without the floor, the exponential distribution occasionally fires two
    upsets back to back, which reads as a broken simulator rather than a
    plausible reactor.
    """
    engine = make_engine(SPIKE, mode=ScenarioMode.RANDOM, seed=11)

    idle_runs = []
    idle = 0

    for _ in range(20_000):
        if engine.update(1.0).is_quiescent and engine.active_name is None:
            idle += 1
        elif idle:
            idle_runs.append(idle)
            idle = 0

    assert idle_runs
    assert min(idle_runs) >= TIMING.min_gap_s


def test_trigger_starts_immediately():
    engine = make_engine(RAMP)

    engine.trigger("ramp")

    assert engine.active_name == "ramp"
    assert not engine.update(1.0).is_quiescent


def test_trigger_works_in_random_mode_too():
    """
    Manual mode only stops the engine STARTING scenarios on its own. Operator
    intent outranks the schedule in either mode.
    """
    engine = make_engine(RAMP, mode=ScenarioMode.RANDOM)

    engine.trigger("ramp")

    assert engine.active_name == "ramp"


def test_trigger_replaces_a_running_scenario():
    """Someone demonstrating a spike should not wait for a ramp to finish."""
    engine = make_engine(RAMP, SPIKE)

    engine.trigger("ramp")
    engine.update(10.0)

    engine.trigger("spike")

    assert engine.active_name == "spike"
    assert engine.active_progress == 0.0


def test_trigger_unknown_name_lists_what_is_available():
    """The caller is usually a person typing a name."""
    engine = make_engine(RAMP, SPIKE)

    with pytest.raises(ValueError) as error:
        engine.trigger("nope")

    assert "nope" in str(error.value)
    assert "ramp" in str(error.value)
    assert "spike" in str(error.value)


def test_scenario_completes_and_releases_the_reactor():
    engine = make_engine(RAMP)

    engine.trigger("ramp")

    for _ in range(int(RAMP.duration_s) + 5):
        engine.update(1.0)

    assert engine.active_name is None
    assert engine.update(1.0) is NO_DISTURBANCE


def test_cancel_stops_the_active_scenario():
    engine = make_engine(RAMP)

    engine.trigger("ramp")
    engine.update(5.0)
    engine.cancel()

    assert engine.active_name is None


def test_bias_eases_in_and_out_rather_than_stepping():
    """
    A step change in bias produces an instantaneous dP/dt far beyond anything
    physical, tripping the rate warning as an artefact of the simulation
    rather than as a property of the scenario.
    """
    engine = make_engine(RAMP)
    engine.trigger("ramp")

    biases = [engine.update(1.0).pressure_bias_kpa for _ in range(int(RAMP.duration_s))]

    assert biases[0] < 5.0
    assert max(biases) == pytest.approx(RAMP.pressure_bias_kpa, rel=0.02)
    assert biases[-1] < 5.0

    # Monotonic rise to the plateau, monotonic fall after it.
    peak = biases.index(max(biases))
    assert biases[:peak] == sorted(biases[:peak])


def test_impulse_is_a_transient_with_no_net_displacement():
    """
    An impulse must come back. Under a raised-cosine envelope it integrated in
    one direction for the whole scenario, so a "spike" displaced pressure by
    over 100 kPa and tripped EMERGENCY -- when its purpose was to trip the
    RATE limit while absolute pressure stayed in band.
    """
    engine = make_engine(SPIKE)
    engine.trigger("spike")

    impulses = [
        engine.update(0.1).pressure_impulse_kpa_s
        for _ in range(int(SPIKE.duration_s * 10))
    ]

    assert max(impulses) > 0.0
    assert min(impulses) < 0.0

    # Positive and negative halves cancel: no sustained offset.
    assert sum(impulses) == pytest.approx(0.0, abs=abs(max(impulses)) * 2)


def test_gas_multiplier_eases_toward_one_not_zero():
    """
    The neutral value for a multiplier is unity. Enveloping it toward zero
    would mean "all gas production stops" during every ramp in and out.
    """
    definition = ScenarioDefinition(
        name="gas", duration_s=100.0, gas_multiplier=2.5, ramp_fraction=0.25
    )

    engine = make_engine(definition)
    engine.trigger("gas")

    multipliers = [engine.update(1.0).gas_multiplier for _ in range(100)]

    assert min(multipliers) >= 1.0
    assert max(multipliers) == pytest.approx(2.5, rel=0.02)


def test_sensor_stall_is_binary_not_ramped():
    """A sensor is either reporting or it is not; there is no half-stall."""
    engine = make_engine(STALL)
    engine.trigger("stall")

    for _ in range(int(STALL.duration_s) - 1):
        assert engine.update(1.0).stall_pressure_sensor is True


def test_zero_weight_scenarios_never_fire_on_their_own():
    """
    Weight 0 means "available to trigger, but not in an overnight run" --
    the runaway scenario uses it.
    """
    never = ScenarioDefinition(name="never", duration_s=10.0, weight=0.0,
                               pressure_bias_kpa=50.0)

    engine = make_engine(never, mode=ScenarioMode.RANDOM)

    for _ in range(20_000):
        engine.update(1.0)
        assert engine.active_name is None

    # But it can still be triggered by hand.
    engine.trigger("never")
    assert engine.active_name == "never"


def test_engine_with_no_definitions_is_inert():
    """A legitimate configuration: observe pure steady-state behaviour."""
    engine = make_engine(mode=ScenarioMode.RANDOM)

    for _ in range(1000):
        assert engine.update(1.0) is NO_DISTURBANCE

    assert engine.available == ()


def test_weighted_selection_favours_heavier_scenarios():
    """
    Weight controls relative likelihood in random mode.

    Durations are several ticks long so a scenario is actually observable
    while running -- with duration equal to the tick, activation and
    completion happen inside one update() and active_name is never non-None.
    """
    common = ScenarioDefinition(name="common", duration_s=5.0, weight=9.0,
                                pressure_bias_kpa=1.0)
    rare = ScenarioDefinition(name="rare", duration_s=5.0, weight=1.0,
                              pressure_bias_kpa=1.0)

    engine = ScenarioEngine(
        (common, rare),
        ScenarioMode.RANDOM,
        ScenarioTiming(mean_interval_s=2.0, min_gap_s=1.0),
        random.Random(5),
    )

    counts = {"common": 0, "rare": 0}
    previous = None

    for _ in range(20_000):
        engine.update(1.0)
        current = engine.active_name

        if current is not None and current != previous:
            counts[current] += 1

        previous = current

    assert counts["common"] + counts["rare"] > 100, "no scenarios fired"
    assert counts["common"] > counts["rare"] * 3


# ==========================================================================
# TelemetryGenerator
# ==========================================================================


def test_first_frame_reports_zero_rate():
    """
    Nothing to differentiate against. The firmware returns zero rather than
    differentiating from a zero baseline, which would emit a large false spike
    straight into the safety thresholds (PhysicsEngine.cpp:79-90).
    """
    source, clock = build_source()

    frames = collect_frames(source, clock, 1)

    assert frames[0].pressure_rate_kpa_s == 0.0


def test_temperature_is_always_exactly_zero():
    """
    The firmware hard-zeroes temperature because no sensor exists. Simulating
    one would build a dashboard around data the real system cannot produce
    (docs/PROTOCOL.md section 6.1).
    """
    source, clock = build_source()

    for frame in collect_frames(source, clock, 300):
        assert frame.temperature_c == 0.0
        assert frame.temperature_rate_c_s == 0.0


def test_anomaly_flag_is_never_raised_by_default():
    """
    Faithful to firmware: ENABLE_XGBOOST_INFERENCE is undefined, so
    DecisionEngine forces the flag false while the forest is a placeholder
    (DecisionEngine.cpp:50-56). ReactorState.ANOMALY is therefore unreachable,
    exactly as on the reactor.
    """
    source, clock = build_source(
        scenarios=(RAMP, SPIKE), mode=ScenarioMode.RANDOM
    )

    frames = collect_frames(source, clock, 2000)

    assert not any(frame.anomaly for frame in frames)
    assert not any(frame.state is ReactorState.ANOMALY for frame in frames)


def test_anomaly_score_is_still_transmitted_while_the_verdict_is_suppressed():
    """The score is always computed and always sent, by firmware design."""
    source, clock = build_source()

    scores = [frame.anomaly_score for frame in collect_frames(source, clock, 200)]

    assert all(0.0 <= score <= 1.0 for score in scores)
    assert len(set(scores)) > 1


def test_anomaly_score_rises_with_process_deviation():
    """
    A constant or purely random score would let the dashboard's AI panel be
    built without ever being exercised.
    """
    source, clock = build_source(scenarios=(RAMP,))

    quiet = collect_frames(source, clock, 120)

    source.scenarios.trigger("ramp")
    disturbed = collect_frames(source, clock, 100)

    quiet_peak = max(frame.anomaly_score for frame in quiet[-60:])
    disturbed_peak = max(frame.anomaly_score for frame in disturbed)

    assert disturbed_peak > quiet_peak * 2


def test_emulated_anomaly_verdict_can_be_enabled_for_ui_testing():
    """
    Opt-in only. Off by default because a simulator producing states the
    reactor cannot reach would let the HMI be tuned around a condition that
    never arrives.
    """
    emulating = GeneratorConfig(
        anomaly_baseline=0.95,
        anomaly_sensitivity=0.55,
        anomaly_noise=0.0,
        anomaly_reference_alpha=0.02,
        anomaly_pressure_scale_kpa=12.0,
        anomaly_gas_scale_fraction=0.12,
        emulate_anomaly_verdict=True,
    )

    source, clock = build_source(generator_config=emulating)

    frames = collect_frames(source, clock, 20)

    assert any(frame.anomaly for frame in frames)
    assert any(frame.state is ReactorState.ANOMALY for frame in frames)


def test_warning_flags_are_computed_not_asserted():
    """
    The central claim of the generator: flags come out of the same logic the
    reactor uses, so re-evaluating a received frame reproduces them exactly.

    Asserting the flags instead would validate the dashboard against our
    expectation of the firmware rather than against the firmware's behaviour.
    """
    source, clock = build_source(
        scenarios=(RAMP, SPIKE), mode=ScenarioMode.RANDOM, seed=8
    )

    frames = collect_frames(source, clock, 3000)

    assert any(frame.warning for frame in frames), "no warning ever occurred"

    for frame in frames:
        verdict = evaluate_thresholds(
            pressure_kpa=frame.pressure_kpa,
            pressure_rate_kpa_s=frame.pressure_rate_kpa_s,
            temperature_c=frame.temperature_c,
            temperature_rate_c_s=frame.temperature_rate_c_s,
            co=frame.co,
            co2=frame.co2,
            ch4=frame.ch4,
            gas_concentration=frame.gas_concentration,
        )

        assert frame.warning == verdict.warning
        assert frame.emergency == verdict.emergency

        assert frame.state is ReactorState.derive(
            warning=frame.warning, emergency=frame.emergency, anomaly=frame.anomaly
        )


def test_gas_ratio_matches_the_firmware_derivation():
    """ch4 / co2, guarded against a zero divisor. PhysicsEngine.cpp:65-68"""
    source, clock = build_source()

    for frame in collect_frames(source, clock, 100):
        assert frame.gas_ratio == pytest.approx(frame.ch4 / frame.co2, rel=1e-5)


def test_gas_concentration_is_the_sum_of_species():
    source, clock = build_source()

    for frame in collect_frames(source, clock, 100):
        assert frame.gas_concentration == pytest.approx(
            frame.co + frame.co2 + frame.ch4, rel=1e-5
        )


# --------------------------------------------------------------------------
# The stale-sensor failure -- docs/PROTOCOL.md section 6.4
# --------------------------------------------------------------------------


def test_stalled_pressure_sensor_freezes_the_reported_value():
    """
    The firmware keeps its last good reading when a conversion fails and
    forwards it without checking the validity flag. The reactor keeps
    changing; the Arduino stops seeing it.
    """
    source, clock = build_source(scenarios=(STALL,))

    collect_frames(source, clock, 60)

    source.scenarios.trigger("stall")
    stalled = collect_frames(source, clock, 40)

    pressures = {frame.pressure_kpa for frame in stalled}

    assert len(pressures) == 1, "reported pressure should be frozen"


def test_stalled_pressure_sensor_produces_exactly_zero_rate():
    """
    The dangerous part. Because rates are differentiated from the REPORTED
    value, a stalled sensor looks like a perfectly stable reactor: frozen
    pressure, zero rate, no warning. Detecting it is analytics' job, and this
    is the condition that has to exist for that to be testable.
    """
    source, clock = build_source(scenarios=(STALL,))

    collect_frames(source, clock, 60)

    source.scenarios.trigger("stall")
    stalled = collect_frames(source, clock, 40)

    # Skip the first stalled frame: it still differentiates against the last
    # genuine reading before the stall began.
    for frame in stalled[1:]:
        assert frame.pressure_rate_kpa_s == 0.0
        assert frame.warning is False


def test_reactor_keeps_evolving_while_the_sensor_is_stalled():
    """
    Confirms the stall is a sensing fault, not a physics freeze. If the
    reactor stopped too, the scenario would be modelling the wrong thing.
    """
    source, clock = build_source(scenarios=(STALL,))

    collect_frames(source, clock, 60)

    source.scenarios.trigger("stall")

    before = source._physics.state.pressure_kpa  # noqa: SLF001 - white box on purpose
    collect_frames(source, clock, 40)
    after = source._physics.state.pressure_kpa  # noqa: SLF001

    assert before != after


def test_stalled_gas_sensors_freeze_gas_but_not_pressure():
    definition = ScenarioDefinition(
        name="gas_stall", duration_s=60.0, ramp_fraction=0.0, stall_gas_sensors=True
    )

    source, clock = build_source(scenarios=(definition,))

    collect_frames(source, clock, 60)

    source.scenarios.trigger("gas_stall")
    stalled = collect_frames(source, clock, 40)

    assert len({frame.co2 for frame in stalled}) == 1
    assert len({frame.pressure_kpa for frame in stalled}) > 1


# ==========================================================================
# SimulationSource
# ==========================================================================


def test_source_emits_decodable_protocol_bytes():
    """
    The reason the simulator encodes at all: every frame the dashboard shows
    during development has been through the same serialise/deserialise path
    the reactor will use.
    """
    source, clock = build_source()
    decoder = PacketDecoder()

    frames = collect_frames(source, clock, 100, decoder)

    assert len(frames) == 100
    assert decoder.stats.error_count == 0
    assert decoder.stats.frames_decoded == 100


def test_source_emits_frame_packets():
    source, clock = build_source()

    clock.advance(1.0)
    packets = PacketDecoder().feed(source.read())

    assert [packet.type for packet in packets] == [PacketType.FRAME]


def test_source_returns_nothing_before_a_frame_is_due():
    """A caller polling faster than the frame rate sees an idle link."""
    source, clock = build_source()

    for _ in range(9):
        clock.advance(0.1)
        assert source.read() == b""

    clock.advance(0.15)
    assert source.read() != b""


def test_elapsed_seconds_produce_exactly_that_many_frames():
    """
    No off-by-one in the schedule.

    An earlier design made the first frame due immediately, so a read one
    period later found frames due at both t=0 and t=period and emitted two --
    every caller then saw one more frame than seconds elapsed.
    """
    source, clock = build_source()

    assert source.read() == b""  # nothing due yet

    assert len(collect_frames(source, clock, 10)) == 10


def test_uptime_advances_with_the_frame_period():
    source, clock = build_source()

    frames = collect_frames(source, clock, 5)

    assert [frame.device_uptime_ms for frame in frames] == [
        1000, 2000, 3000, 4000, 5000
    ]


def test_source_catches_up_after_a_stall_then_resynchronises():
    """
    A long caller stall -- a debugger breakpoint, a suspended laptop -- must
    not produce a burst of thousands of frames. Beyond the catch-up cap the
    schedule resynchronises, which is what a real link does when the kernel
    buffer overruns.
    """
    source, clock = build_source()

    source.read()

    clock.advance(3600.0)

    packets = PacketDecoder().feed(source.read())

    assert 0 < len(packets) <= 8

    # And the schedule is sane afterwards rather than still thousands behind.
    clock.advance(1.0)
    assert len(PacketDecoder().feed(source.read())) <= 2


def test_source_rejects_a_non_positive_period():
    rng = random.Random(1)

    with pytest.raises(ValueError):
        SimulationSource(
            physics=ReactorPhysics(PHYSICS, rng),
            generator=TelemetryGenerator(GENERATOR, rng),
            scenarios=ScenarioEngine((), ScenarioMode.MANUAL, TIMING, rng),
            period_s=0.0,
        )


def test_seeded_runs_are_byte_identical():
    """
    Same seed, same bytes. This is what makes a UI bug reproducible and a
    screenshot comparison meaningful.
    """
    def run() -> bytes:
        source, clock = build_source(scenarios=(RAMP, SPIKE),
                                     mode=ScenarioMode.RANDOM, seed=2024)
        chunks = []

        for _ in range(500):
            clock.advance(1.0)
            chunks.append(source.read())

        return b"".join(chunks)

    assert run() == run()


def test_different_seeds_produce_different_runs():
    def run(seed: int) -> bytes:
        source, clock = build_source(seed=seed)
        return b"".join(
            (clock.advance(1.0), source.read())[1] for _ in range(100)
        )

    assert run(1) != run(2)


# ==========================================================================
# End to end, through the shipped configuration
# ==========================================================================


def test_shipped_config_produces_a_clean_decodable_stream():
    """
    The configuration an engineer actually runs. An hour of simulated time
    through the real decoder, with no errors and no resyncs.
    """
    clock = FakeClock()
    source = create_simulation_source(load_config().simulation, clock=clock)
    decoder = PacketDecoder()

    frames = collect_frames(source, clock, 3600, decoder)

    assert len(frames) == 3600
    assert decoder.stats.error_count == 0
    assert decoder.stats.resyncs == 0
    assert decoder.stats.state_mismatches == 0


def test_shipped_config_exercises_warnings_over_an_endurance_run():
    """
    Random mode has to actually produce interesting conditions. A simulator
    that runs for an hour without ever tripping a limit would leave the
    warning and emergency treatments of the HMI completely untested.
    """
    clock = FakeClock()
    source = create_simulation_source(load_config().simulation, clock=clock)

    frames = collect_frames(source, clock, 7200)

    assert any(frame.warning for frame in frames)
    assert any(frame.state is ReactorState.WARNING for frame in frames)


def test_shipped_config_stays_within_plausible_bounds():
    """
    Values must stay in a range a real reactor could produce. Gauge scales get
    tuned against these, so a stray excursion to 10,000 kPa would silently
    ruin every chart axis.
    """
    clock = FakeClock()
    source = create_simulation_source(load_config().simulation, clock=clock)

    frames = collect_frames(source, clock, 7200)

    for frame in frames:
        assert 0.0 <= frame.pressure_kpa < 250.0
        assert 0.0 <= frame.gas_concentration < 60_000.0
        assert 0.0 <= frame.anomaly_score <= 1.0


@pytest.mark.parametrize(
    "name,expect",
    [
        ("pressure_ramp", "pressure"),
        ("pressure_spike", "rate"),
        ("gas_excursion", "gas"),
        ("runaway", "emergency"),
    ],
)
def test_each_shipped_scenario_produces_its_intended_condition(name, expect):
    """
    Every shipped scenario must actually reach what it claims to test. A
    scenario that never trips its limit is dead config that reads as coverage.

    Run in MANUAL mode so no randomly-fired upset contaminates the result --
    without that isolation, a spike appears to trip the pressure limit because
    an unrelated ramp fired during the same window.
    """
    import dataclasses

    simulation = dataclasses.replace(
        load_config().simulation, scenario_mode=ScenarioMode.MANUAL
    )

    clock = FakeClock()
    source = create_simulation_source(simulation, clock=clock)

    collect_frames(source, clock, 60)

    source.scenarios.trigger(name)
    frames = collect_frames(source, clock, 300)

    if expect == "pressure":
        assert max(f.pressure_kpa for f in frames) > MAX_PRESSURE_KPA
        assert max(abs(f.pressure_rate_kpa_s) for f in frames) < (
            MAX_PRESSURE_RATE_KPA_PER_SEC
        )
    elif expect == "rate":
        # The point of the spike: breaches the RATE limit while absolute
        # pressure stays comfortably in band.
        assert max(abs(f.pressure_rate_kpa_s) for f in frames) > (
            MAX_PRESSURE_RATE_KPA_PER_SEC
        )
        assert max(f.pressure_kpa for f in frames) < MAX_PRESSURE_KPA
    elif expect == "gas":
        assert max(f.gas_concentration for f in frames) > MAX_TOTAL_GAS_PPM
    elif expect == "emergency":
        assert max(f.pressure_kpa for f in frames) > EMERGENCY_PRESSURE_KPA
        assert any(f.state is ReactorState.EMERGENCY for f in frames)


def test_every_shipped_scenario_can_be_triggered_by_name():
    """Guards against a config typo making a scenario unreachable."""
    clock = FakeClock()
    config = load_config().simulation
    source = create_simulation_source(config, clock=clock)

    for definition in config.scenarios:
        source.scenarios.trigger(definition.name)

        assert source.scenarios.active_name == definition.name
