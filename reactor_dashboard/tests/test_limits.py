"""
The firmware-mirrored threshold logic.

These tests are the guard on a cross-codebase duplication. Every constant here
also exists in the Arduino's safety_config.h, and every comparison mirrors
ThresholdInference.cpp. If the two ever drift, the Pi's reconstructed warning
detail becomes quietly wrong -- which is the failure mode this file exists to
make loud.

Boundary behaviour gets disproportionate attention on purpose. The firmware
uses strictly greater-than throughout; a >= here would make the Pi disagree
with the reactor on exactly-at-limit values and nowhere else, which is the
hardest possible bug to notice in a running plant.
"""

import pytest

from backend.protocol.limits import (
    ANOMALY_THRESHOLD,
    EMERGENCY_PRESSURE_KPA,
    EMERGENCY_TEMPERATURE_C,
    MAX_CH4_PPM,
    MAX_CO2_PPM,
    MAX_CO_PPM,
    MAX_PRESSURE_KPA,
    MAX_PRESSURE_RATE_KPA_PER_SEC,
    MAX_TEMPERATURE_C,
    MAX_TEMPERATURE_RATE_C_PER_SEC,
    MAX_TOTAL_GAS_PPM,
    evaluate_thresholds,
)
from backend.protocol.spec import ReactorState


def evaluate(**overrides):
    """Evaluate against a nominal reactor, overriding only what a test cares about."""
    values = dict(
        pressure_kpa=105.0,
        pressure_rate_kpa_s=0.0,
        temperature_c=0.0,
        temperature_rate_c_s=0.0,
        co=420.0,
        co2=3200.0,
        ch4=1850.0,
        gas_concentration=5470.0,
    )
    values.update(overrides)

    return evaluate_thresholds(**values)


# --------------------------------------------------------------------------
# Constants match the firmware
# --------------------------------------------------------------------------


def test_constants_match_safety_config_h():
    """
    Transcribed from include/config/safety_config.h.

    Written out literally rather than computed, so that changing a constant
    in limits.py without checking the firmware fails here.
    """
    assert MAX_PRESSURE_KPA == 150.0
    assert EMERGENCY_PRESSURE_KPA == 180.0

    assert MAX_TEMPERATURE_C == 550.0
    assert EMERGENCY_TEMPERATURE_C == 650.0

    assert MAX_CO_PPM == 1000.0
    assert MAX_CO2_PPM == 8000.0
    assert MAX_CH4_PPM == 5000.0
    assert MAX_TOTAL_GAS_PPM == 12000.0

    assert MAX_PRESSURE_RATE_KPA_PER_SEC == 5.0
    assert MAX_TEMPERATURE_RATE_C_PER_SEC == 10.0

    assert ANOMALY_THRESHOLD == 0.80


def test_emergency_limits_exceed_warning_limits():
    """A warning that fires after its emergency would be a config inversion."""
    assert EMERGENCY_PRESSURE_KPA > MAX_PRESSURE_KPA
    assert EMERGENCY_TEMPERATURE_C > MAX_TEMPERATURE_C


# --------------------------------------------------------------------------
# Nominal
# --------------------------------------------------------------------------


def test_nominal_reactor_raises_nothing():
    verdict = evaluate()

    assert verdict.warning is False
    assert verdict.emergency is False
    assert verdict.active_warnings() == ()


# --------------------------------------------------------------------------
# Boundaries -- strictly greater-than, matching the firmware
# --------------------------------------------------------------------------


def test_value_exactly_on_limit_is_not_a_warning():
    """ThresholdInference.cpp uses `>`, not `>=`."""
    assert evaluate(pressure_kpa=MAX_PRESSURE_KPA).pressure_warning is False
    assert evaluate(co=MAX_CO_PPM).gas_warning is False
    assert (
        evaluate(pressure_rate_kpa_s=MAX_PRESSURE_RATE_KPA_PER_SEC).pressure_rate_warning
        is False
    )


def test_value_just_over_limit_is_a_warning():
    assert evaluate(pressure_kpa=MAX_PRESSURE_KPA + 0.01).pressure_warning is True
    assert evaluate(co=MAX_CO_PPM + 0.01).gas_warning is True


def test_pressure_exactly_on_emergency_limit_is_not_an_emergency():
    assert evaluate(pressure_kpa=EMERGENCY_PRESSURE_KPA).emergency is False
    assert evaluate(pressure_kpa=EMERGENCY_PRESSURE_KPA + 0.01).emergency is True


# --------------------------------------------------------------------------
# Individual limits
# --------------------------------------------------------------------------


def test_each_gas_species_can_trip_alone():
    """
    A single species breaches well before the sum does.

    ThresholdInference.cpp:36-40 checks each species as well as the total,
    precisely so a CO excursion is caught without waiting for total gas to
    reach 12000.
    """
    assert evaluate(co=MAX_CO_PPM + 1).gas_warning is True
    assert evaluate(co2=MAX_CO2_PPM + 1).gas_warning is True
    assert evaluate(ch4=MAX_CH4_PPM + 1).gas_warning is True


def test_total_gas_can_trip_with_every_species_in_range():
    """Each species below its own limit, sum above the total limit."""
    verdict = evaluate(co=900.0, co2=7000.0, ch4=4500.0, gas_concentration=12400.0)

    assert verdict.gas_warning is True


def test_rate_warnings_use_magnitude_not_sign():
    """
    A rapid pressure DROP is as significant as a rapid rise -- both mean loss
    of control (ThresholdInference.cpp:44-47).
    """
    rise = evaluate(pressure_rate_kpa_s=MAX_PRESSURE_RATE_KPA_PER_SEC + 1)
    fall = evaluate(pressure_rate_kpa_s=-(MAX_PRESSURE_RATE_KPA_PER_SEC + 1))

    assert rise.pressure_rate_warning is True
    assert fall.pressure_rate_warning is True


def test_rate_warning_does_not_require_absolute_breach():
    """
    The case a dashboard showing only absolute values would miss entirely:
    pressure comfortably in band, changing far too fast.
    """
    verdict = evaluate(pressure_kpa=110.0, pressure_rate_kpa_s=9.0)

    assert verdict.pressure_warning is False
    assert verdict.pressure_rate_warning is True
    assert verdict.warning is True


# --------------------------------------------------------------------------
# Roll-up behaviour
# --------------------------------------------------------------------------


def test_warning_is_the_or_of_the_five_granular_bits():
    """
    This relationship is what the decoder's self-check relies on: the OR of
    the reconstructed bits must equal the transmitted `warning` bit.
    """
    cases = [
        {},
        {"pressure_kpa": 160.0},
        {"co2": 9000.0},
        {"pressure_rate_kpa_s": 8.0},
        {"pressure_kpa": 160.0, "co2": 9000.0},
        {"temperature_c": 600.0},
        {"temperature_rate_c_s": 20.0},
    ]

    for overrides in cases:
        verdict = evaluate(**overrides)

        expected = (
            verdict.pressure_warning
            or verdict.temperature_warning
            or verdict.gas_warning
            or verdict.pressure_rate_warning
            or verdict.temperature_rate_warning
        )

        assert verdict.warning == expected, overrides


def test_emergency_is_not_a_rollup_of_warnings():
    """
    Emergency has its own higher limits and covers pressure and temperature
    only (ThresholdInference.cpp:66-68). A gas warning, however severe, is
    never an emergency.
    """
    verdict = evaluate(co2=50_000.0, gas_concentration=60_000.0)

    assert verdict.gas_warning is True
    assert verdict.warning is True
    assert verdict.emergency is False


def test_emergency_implies_warning_for_pressure():
    """Any pressure past the emergency limit is also past the warning limit."""
    verdict = evaluate(pressure_kpa=EMERGENCY_PRESSURE_KPA + 10)

    assert verdict.emergency is True
    assert verdict.pressure_warning is True


# --------------------------------------------------------------------------
# Temperature -- mirrored but unreachable today
# --------------------------------------------------------------------------


def test_temperature_logic_is_present_but_unreachable_at_hard_zero():
    """
    The firmware hard-zeroes temperature because no sensor exists, so these
    can never fire in practice (docs/PROTOCOL.md section 6.1). The logic is
    mirrored anyway so that adding the sensor needs no change here.
    """
    assert evaluate(temperature_c=0.0).temperature_warning is False

    assert evaluate(temperature_c=600.0).temperature_warning is True
    assert evaluate(temperature_c=700.0).emergency is True


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def test_active_warnings_names_only_what_tripped():
    verdict = evaluate(pressure_kpa=160.0, pressure_rate_kpa_s=8.0)

    assert set(verdict.active_warnings()) == {"Pressure", "Pressure rate"}


def test_active_warnings_is_empty_not_none_when_nominal():
    """Callers treat it as a sequence; None would force a check at every use."""
    assert evaluate().active_warnings() == ()


def test_verdict_is_immutable():
    verdict = evaluate()

    with pytest.raises(Exception):
        verdict.warning = True


# --------------------------------------------------------------------------
# Agreement with the firmware's fusion policy
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({}, ReactorState.NORMAL),
        ({"pressure_kpa": 160.0}, ReactorState.WARNING),
        ({"co2": 9000.0}, ReactorState.WARNING),
        ({"pressure_rate_kpa_s": 8.0}, ReactorState.WARNING),
        ({"pressure_kpa": 190.0}, ReactorState.EMERGENCY),
    ],
)
def test_verdict_feeds_the_firmware_fusion_policy(overrides, expected):
    """
    Threshold evaluation and state fusion compose the way the firmware
    composes them: ThresholdInference -> DecisionEngine.
    """
    verdict = evaluate(**overrides)

    state = ReactorState.derive(
        warning=verdict.warning, emergency=verdict.emergency, anomaly=False
    )

    assert state is expected
