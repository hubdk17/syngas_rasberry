"""
Configuration loading and validation.

The tests that matter here are the rejection cases. A loader that reads a
correct file correctly is easy; the value of this module is that it refuses a
subtly wrong file instead of starting up with a plausible-looking default.
"""

import pytest

from backend.protocol.spec import TELEMETRY_PERIOD_MS
from config.settings import (
    DEFAULT_CONFIG_PATH,
    AppConfig,
    ConfigError,
    ScenarioMode,
    TelemetrySourceKind,
    load_config,
)


def write_config(tmp_path, text: str):
    path = tmp_path / "app.toml"
    path.write_text(text, encoding="utf-8")
    return path


#: A minimal complete configuration. Every required key, no scenarios.
VALID = """
[telemetry]
source = "simulation"

[uart]
port = "/dev/serial0"

[storage]
history_seconds = 3600
stale_after_frames = 3

[simulation]
frame_period_ms = 1000
scenario_mode = "random"
scenario_mean_interval_s = 240.0
scenario_min_gap_s = 90.0

[simulation.physics]
pressure_baseline_kpa = 105.0
pressure_reversion_per_s = 0.06
pressure_noise_kpa = 0.30
co_baseline = 420.0
co2_baseline = 3200.0
ch4_baseline = 1850.0
gas_reversion_per_s = 0.08
gas_noise_fraction = 0.010
gas_pressure_coupling = 0.8

[simulation.generator]
anomaly_baseline = 0.05
anomaly_sensitivity = 0.55
anomaly_noise = 0.02
anomaly_reference_alpha = 0.02
anomaly_pressure_scale_kpa = 12.0
anomaly_gas_scale_fraction = 0.12
emulate_anomaly_verdict = false

[simulation.link]
enabled = false
packet_loss_probability = 0.0
corruption_probability = 0.0
delay_probability = 0.0
delay_seconds = 2.5
disconnect_probability = 0.0
disconnect_duration_s = 20.0
"""

SCENARIO = """
[[simulation.scenario]]
name = "pressure_ramp"
duration_s = 180.0
weight = 3.0
pressure_bias_kpa = 52.0
"""


# --------------------------------------------------------------------------
# The shipped configuration
# --------------------------------------------------------------------------


def test_shipped_config_loads():
    """
    The app.toml in the repository must always be valid.

    This is the test that fails when someone edits app.toml and breaks it,
    rather than the failure showing up as an application that will not start.
    """
    config = load_config()

    assert isinstance(config, AppConfig)


def test_shipped_config_defaults_to_simulation():
    """Hardware is not connected. The default must not assume it is."""
    config = load_config()

    assert config.telemetry.source is TelemetrySourceKind.SIMULATION
    assert config.telemetry.is_simulated is True


def test_default_config_path_exists():
    assert DEFAULT_CONFIG_PATH.is_file()


# --------------------------------------------------------------------------
# Valid input
# --------------------------------------------------------------------------


def test_parses_valid_config(tmp_path):
    config = load_config(write_config(tmp_path, VALID))

    assert config.telemetry.source is TelemetrySourceKind.SIMULATION
    assert config.uart.port == "/dev/serial0"


def test_parses_uart_source(tmp_path):
    text = VALID.replace('source = "simulation"', 'source = "uart"')

    config = load_config(write_config(tmp_path, text))

    assert config.telemetry.source is TelemetrySourceKind.UART
    assert config.telemetry.is_simulated is False


def test_source_switch_is_the_only_change_needed(tmp_path):
    """
    The headline requirement: one value moves the whole application between
    simulated and live telemetry.
    """
    simulated = load_config(write_config(tmp_path, VALID))

    live = load_config(
        write_config(tmp_path, VALID.replace('"simulation"', '"uart"'))
    )

    assert simulated.telemetry.source is not live.telemetry.source
    assert simulated.uart == live.uart


def test_config_is_immutable(tmp_path):
    """Configuration is read once at startup and cannot drift afterwards."""
    config = load_config(write_config(tmp_path, VALID))

    with pytest.raises(Exception):
        config.telemetry.source = TelemetrySourceKind.UART


@pytest.mark.parametrize(
    "port", ["/dev/serial0", "/dev/ttyAMA0", "/dev/ttyUSB0", "COM3"]
)
def test_accepts_realistic_port_names(tmp_path, port):
    text = VALID.replace("/dev/serial0", port)

    config = load_config(write_config(tmp_path, text))

    assert config.uart.port == port


# --------------------------------------------------------------------------
# Rejection -- the group that earns its keep
# --------------------------------------------------------------------------


def test_unknown_source_value_is_rejected_with_valid_options(tmp_path):
    """
    A typo must not silently fall back to a default.

    The message has to name the valid options, because the whole point is
    that the engineer fixes it without reading the source.
    """
    text = VALID.replace('"simulation"', '"sim"')

    with pytest.raises(ConfigError) as error:
        load_config(write_config(tmp_path, text))

    message = str(error.value)

    assert "sim" in message
    assert "simulation" in message
    assert "uart" in message


def test_unknown_key_is_rejected(tmp_path):
    """A key nothing reads means the engineer believes something untrue."""
    text = VALID.replace(
        'port = "/dev/serial0"', 'port = "/dev/serial0"\nbaud_rate = 9600'
    )

    with pytest.raises(ConfigError) as error:
        load_config(write_config(tmp_path, text))

    assert "baud_rate" in str(error.value)


def test_unknown_section_is_rejected(tmp_path):
    text = VALID + '\n[logging]\nlevel = "debug"\n'

    with pytest.raises(ConfigError) as error:
        load_config(write_config(tmp_path, text))

    assert "logging" in str(error.value)


# --------------------------------------------------------------------------
# Simulation
# --------------------------------------------------------------------------


def test_parses_simulation_defaults(tmp_path):
    config = load_config(write_config(tmp_path, VALID))

    simulation = config.simulation

    assert simulation.frame_period_ms == 1000
    assert simulation.frame_period_s == 1.0
    assert simulation.scenario_mode is ScenarioMode.RANDOM
    assert simulation.scenarios == ()
    assert simulation.seed is None
    assert simulation.physics.pressure_baseline_kpa == 105.0
    assert simulation.generator.emulate_anomaly_verdict is False


def test_frame_period_matches_firmware_cadence():
    """
    The shipped config must emit at the rate the reactor actually transmits.

    Developing the dashboard against a faster or slower stream would tune
    every trend window and staleness timeout to a cadence that never arrives.
    """
    assert load_config().simulation.frame_period_ms == TELEMETRY_PERIOD_MS


def test_seed_is_optional(tmp_path):
    """Present means reproducible; absent means a fresh run every time."""
    assert load_config(write_config(tmp_path, VALID)).simulation.seed is None

    # Inserted into [simulation], not appended -- a trailing key would land in
    # whichever section happens to be last in the file.
    text = VALID.replace("frame_period_ms = 1000", "seed = 7\nframe_period_ms = 1000")

    assert load_config(write_config(tmp_path, text)).simulation.seed == 7


def test_parses_scenario_definitions(tmp_path):
    config = load_config(write_config(tmp_path, VALID + SCENARIO))

    (scenario,) = config.simulation.scenarios

    assert scenario.name == "pressure_ramp"
    assert scenario.duration_s == 180.0
    assert scenario.weight == 3.0
    assert scenario.pressure_bias_kpa == 52.0

    # Unstated fields take neutral defaults, so a definition mentions only
    # what it actually perturbs.
    assert scenario.gas_multiplier == 1.0
    assert scenario.pressure_impulse_kpa_s == 0.0
    assert scenario.stall_pressure_sensor is False
    assert scenario.ramp_fraction == 0.25


def test_scenarios_can_be_added_without_touching_code(tmp_path):
    """
    The headline claim of the scenario design: a new failure mode is a new
    table in app.toml, not a new class.
    """
    extra = """
[[simulation.scenario]]
name = "ch4_line_blockage"
duration_s = 240.0
gas_multiplier = 0.4
stall_gas_sensors = true
"""

    config = load_config(write_config(tmp_path, VALID + SCENARIO + extra))

    assert [s.name for s in config.simulation.scenarios] == [
        "pressure_ramp",
        "ch4_line_blockage",
    ]


def test_shipped_scenarios_are_usable():
    """Every shipped scenario must actually perturb something."""
    scenarios = load_config().simulation.scenarios

    assert len(scenarios) >= 5

    for scenario in scenarios:
        perturbs = (
            scenario.pressure_bias_kpa != 0.0
            or scenario.pressure_impulse_kpa_s != 0.0
            or scenario.gas_multiplier != 1.0
            or scenario.stall_pressure_sensor
            or scenario.stall_gas_sensors
        )

        assert perturbs, f"scenario {scenario.name!r} does nothing"


def test_duplicate_scenario_names_are_rejected(tmp_path):
    """A duplicate would silently shadow the first in the engine's lookup."""
    with pytest.raises(ConfigError) as error:
        load_config(write_config(tmp_path, VALID + SCENARIO + SCENARIO))

    assert "duplicate" in str(error.value)
    assert "pressure_ramp" in str(error.value)


def test_unknown_scenario_key_is_rejected(tmp_path):
    text = VALID + SCENARIO + "temperature_bias_c = 50.0\n"

    with pytest.raises(ConfigError) as error:
        load_config(write_config(tmp_path, text))

    assert "temperature_bias_c" in str(error.value)


def test_unknown_scenario_mode_is_rejected(tmp_path):
    text = VALID.replace('scenario_mode = "random"', 'scenario_mode = "chaos"')

    with pytest.raises(ConfigError) as error:
        load_config(write_config(tmp_path, text))

    assert "chaos" in str(error.value)
    assert "random" in str(error.value)
    assert "manual" in str(error.value)


@pytest.mark.parametrize(
    "extra_line",
    [
        "weight = -1.0",
        "ramp_fraction = 0.9",  # ramping in and out cannot exceed the duration
        "ramp_fraction = -0.1",
        "gas_multiplier = -0.5",
    ],
)
def test_out_of_range_scenario_values_are_rejected(tmp_path, extra_line):
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, VALID + SCENARIO + extra_line + "\n"))


@pytest.mark.parametrize("duration", ["0.0", "-5.0"])
def test_non_positive_scenario_duration_is_rejected(tmp_path, duration):
    text = VALID + SCENARIO.replace("duration_s = 180.0", f"duration_s = {duration}")

    with pytest.raises(ConfigError) as error:
        load_config(write_config(tmp_path, text))

    assert "duration_s" in str(error.value)


def test_zero_frame_period_is_rejected(tmp_path):
    text = VALID.replace("frame_period_ms = 1000", "frame_period_ms = 0")

    with pytest.raises(ConfigError) as error:
        load_config(write_config(tmp_path, text))

    assert "frame_period_ms" in str(error.value)


def test_missing_physics_section_is_rejected(tmp_path):
    text = VALID.replace("[simulation.physics]", "[simulation.unused]")

    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, text))


def test_boolean_is_not_accepted_as_a_number(tmp_path):
    """
    bool is an int subclass in Python, so `true` would otherwise parse as 1.

    A physical parameter is never a boolean, and silently reading `true` as
    1.0 kPa would be a genuinely confusing way to lose an afternoon.
    """
    text = VALID.replace("pressure_noise_kpa = 0.30", "pressure_noise_kpa = true")

    with pytest.raises(ConfigError) as error:
        load_config(write_config(tmp_path, text))

    assert "pressure_noise_kpa" in str(error.value)


def test_integers_are_accepted_where_floats_are_expected(tmp_path):
    """`1` and `1.0` mean the same thing for a physical parameter."""
    text = VALID.replace("pressure_baseline_kpa = 105.0", "pressure_baseline_kpa = 105")

    config = load_config(write_config(tmp_path, text))

    assert config.simulation.physics.pressure_baseline_kpa == 105.0


def test_missing_telemetry_section_is_rejected(tmp_path):
    text = '[uart]\nport = "/dev/serial0"\n'

    with pytest.raises(ConfigError) as error:
        load_config(write_config(tmp_path, text))

    assert "telemetry" in str(error.value)


def test_missing_uart_section_is_rejected(tmp_path):
    text = '[telemetry]\nsource = "simulation"\n'

    with pytest.raises(ConfigError) as error:
        load_config(write_config(tmp_path, text))

    assert "uart" in str(error.value)


def test_missing_key_is_rejected(tmp_path):
    text = '[telemetry]\n\n[uart]\nport = "/dev/serial0"\n'

    with pytest.raises(ConfigError) as error:
        load_config(write_config(tmp_path, text))

    assert "telemetry.source" in str(error.value)


def test_wrong_type_is_rejected(tmp_path):
    text = VALID.replace('port = "/dev/serial0"', "port = 115200")

    with pytest.raises(ConfigError) as error:
        load_config(write_config(tmp_path, text))

    assert "uart.port" in str(error.value)
    assert "string" in str(error.value)


def test_empty_port_is_rejected(tmp_path):
    text = VALID.replace("/dev/serial0", "   ")

    with pytest.raises(ConfigError) as error:
        load_config(write_config(tmp_path, text))

    assert "uart.port" in str(error.value)


def test_section_declared_as_scalar_is_rejected(tmp_path):
    text = 'telemetry = "simulation"\n\n[uart]\nport = "/dev/serial0"\n'

    with pytest.raises(ConfigError) as error:
        load_config(write_config(tmp_path, text))

    assert "telemetry" in str(error.value)


def test_missing_file_is_rejected(tmp_path):
    with pytest.raises(ConfigError) as error:
        load_config(tmp_path / "nonexistent.toml")

    assert "not found" in str(error.value)


def test_malformed_toml_is_rejected(tmp_path):
    with pytest.raises(ConfigError) as error:
        load_config(write_config(tmp_path, "[telemetry\nsource = broken"))

    assert "valid TOML" in str(error.value)


def test_every_source_kind_is_loadable(tmp_path):
    """
    Guards against adding a TelemetrySourceKind member that the loader or the
    config file cannot actually express.
    """
    for kind in TelemetrySourceKind:
        text = VALID.replace('"simulation"', f'"{kind.value}"')

        config = load_config(write_config(tmp_path, text))

        assert config.telemetry.source is kind
