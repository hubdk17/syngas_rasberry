"""
Typed, validated application configuration.

Responsibility: turn app.toml into frozen dataclasses, or fail loudly.

That is the whole job. This module does not decide what to do with the values
and does not construct any runtime object from them -- it produces typed data
that the composition root injects into the modules that need it.

It therefore owns every configuration type, including ScenarioDefinition and
its companions. Those are not simulation domain concepts that happen to be
configurable; they are the typed representation of an app.toml table and
nothing else reads them from anywhere else. Keeping them here gives one clean
dependency direction -- simulation imports config, never the reverse.

VALIDATION IS STRICT, AND DELIBERATELY SO
-----------------------------------------
Unknown sections, unknown keys, wrong types and unknown enum values are all
hard errors at startup.

The permissive alternative -- ignore what you do not recognise, fall back to a
default -- is the wrong trade for industrial software. An engineer who fixes a
setting, restarts, and sees no change will conclude the setting does not work,
when in fact they typed `sim` instead of `simulation` and the system quietly
kept its old behaviour. Failing at startup with a message naming the bad key
and listing the valid options turns a debugging session into a five-second fix.

Errors are raised before the GUI opens, so a misconfiguration can never
present as a working dashboard showing the wrong thing.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from backend.protocol.spec import TELEMETRY_PERIOD_MS
from backend.telemetry_source import TelemetrySourceKind

#: Shipped configuration, alongside this module.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "app.toml"


class ConfigError(Exception):
    """
    Configuration is missing, malformed, or contradicts itself.

    A single exception type rather than a hierarchy: every case has the same
    remedy -- an engineer edits app.toml and restarts -- so distinguishing
    them in code would serve nobody. The message carries the detail.
    """


# ===========================================================================
# Telemetry source selection
# ===========================================================================


@dataclass(frozen=True, slots=True)
class TelemetryConfig:
    """Which source provides telemetry bytes."""

    source: TelemetrySourceKind

    @property
    def is_simulated(self) -> bool:
        """
        True when running without hardware.

        Exists so the dashboard can display a prominent SIMULATION marker.
        An operator must never be able to mistake simulated telemetry for a
        live reactor, and the check for that belongs here rather than being
        an enum comparison scattered through the UI.
        """
        return self.source.is_simulated


@dataclass(frozen=True, slots=True)
class UartConfig:
    """
    Serial link settings.

    Only the port. Baud rate, framing and packet geometry are protocol
    constants owned by backend/protocol/spec.py, not deployment settings.
    """

    port: str


# ===========================================================================
# Simulation
# ===========================================================================


@dataclass(frozen=True, slots=True)
class PhysicsConfig:
    """
    The reactor's operating envelope and its stochastic character.

    These are the numbers to revisit once the real reactor is characterised
    (docs/ARCHITECTURE.md open question 1). They are configuration precisely
    so that retuning is an app.toml edit rather than a code change.
    """

    pressure_baseline_kpa: float
    pressure_reversion_per_s: float
    pressure_noise_kpa: float

    co_baseline: float
    co2_baseline: float
    ch4_baseline: float

    gas_reversion_per_s: float
    gas_noise_fraction: float
    gas_pressure_coupling: float


@dataclass(frozen=True, slots=True)
class GeneratorConfig:
    """Parameters of the synthetic anomaly model."""

    anomaly_baseline: float
    anomaly_sensitivity: float
    anomaly_noise: float
    anomaly_reference_alpha: float
    anomaly_pressure_scale_kpa: float
    anomaly_gas_scale_fraction: float

    #: Let the score drive the anomaly flag. False matches the firmware, which
    #: forces the flag off while the XGBoost forest is a placeholder. Enable
    #: only to exercise the ANOMALY banner.
    emulate_anomaly_verdict: bool


@dataclass(frozen=True, slots=True)
class LinkFaultConfig:
    """
    Telemetry-link fault injection.

    Separate from the reactor scenarios because it models a different thing.
    A pressure excursion is the reactor misbehaving; a CRC error is the cable
    misbehaving. The dashboard has to handle both, and conflating them would
    mean never being able to test one without the other.

    Every probability is per read that returned data, so at the 1 Hz frame
    rate a probability of 0.01 is roughly one event per hundred frames.
    """

    #: Whether to wrap the telemetry source at all. With this false there is
    #: no injector in the chain and the stream is pristine.
    enabled: bool

    #: Drop an entire read. On the simulator, where one read is one packet,
    #: that is a lost packet; the dashboard should show a gap, not a stall.
    packet_loss_probability: float

    #: Flip one bit of one byte. Lands in the payload most of the time and
    #: produces a CRC error; occasionally lands in the header and produces a
    #: resync instead. Both are real line faults and both are worth showing.
    corruption_probability: float

    #: Hold a read back and release it later, out of step with the cadence.
    delay_probability: float
    delay_seconds: float

    #: Go silent for a while, as if the cable were pulled.
    disconnect_probability: float
    disconnect_duration_s: float


class ScenarioMode(StrEnum):
    """Who decides when a scenario starts."""

    RANDOM = "random"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class ScenarioTiming:
    """When RANDOM mode fires the next scenario."""

    #: Mean idle time between upsets. Inter-arrival times are exponentially
    #: distributed around this, so upsets cluster and space out the way real
    #: process disturbances do rather than arriving on a metronome.
    mean_interval_s: float

    #: Floor on the idle gap. Without it the exponential distribution
    #: occasionally fires two upsets back to back, which reads as a broken
    #: simulator rather than as a plausible reactor.
    min_gap_s: float


@dataclass(frozen=True, slots=True)
class ScenarioDefinition:
    """
    One reactor upset, as declared in app.toml.

    Every field except name and duration has a neutral default, so a
    definition states only what it actually perturbs -- a pressure ramp does
    not mention gas.
    """

    name: str
    duration_s: float

    #: Relative likelihood in RANDOM mode. Ignored in MANUAL mode. Weight 0
    #: means "never fires on its own, but remains available to trigger by
    #: hand" -- useful for a scenario you want on the menu but not in an
    #: overnight endurance run.
    weight: float = 1.0

    #: Fraction of the duration spent easing in, and again easing out. 0.25
    #: ramps up for a quarter, holds for a half, ramps down for a quarter.
    ramp_fraction: float = 0.25

    pressure_bias_kpa: float = 0.0
    pressure_impulse_kpa_s: float = 0.0
    gas_multiplier: float = 1.0

    stall_pressure_sensor: bool = False
    stall_gas_sensors: bool = False


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    """Everything the simulated telemetry source needs."""

    #: Seed for every random draw. None means nondeterministic. A seeded run
    #: is reproducible frame for frame, which is what makes a bug report
    #: replayable and a UI test deterministic.
    seed: int | None

    frame_period_ms: int

    scenario_mode: ScenarioMode
    scenario_timing: ScenarioTiming
    scenarios: tuple[ScenarioDefinition, ...]

    physics: PhysicsConfig
    generator: GeneratorConfig
    link: LinkFaultConfig

    @property
    def frame_period_s(self) -> float:
        return self.frame_period_ms / 1000.0


@dataclass(frozen=True, slots=True)
class StorageConfig:
    """How much telemetry the store retains in memory."""

    #: Retained window, in seconds of reactor time.
    #:
    #: Expressed as a duration rather than a frame count because that is the
    #: question an engineer actually has: "how far back can I scroll?" The
    #: frame count follows from the protocol's fixed cadence.
    history_seconds: int

    #: Frames with no arrival before the link is declared dead.
    #:
    #: Three missed frames at 1 Hz. Long enough that one dropped packet does
    #: not flicker the banner, short enough that an operator is not looking at
    #: stale numbers believing them live.
    stale_after_frames: int

    @property
    def history_frames(self) -> int:
        """
        Store capacity in records.

        Derived from the firmware's transmission cadence rather than from the
        simulator's, so the retained window is the same duration whether the
        telemetry is simulated or real.
        """
        return max(1, round(self.history_seconds * 1000 / TELEMETRY_PERIOD_MS))

    @property
    def stale_after_seconds(self) -> float:
        return self.stale_after_frames * TELEMETRY_PERIOD_MS / 1000.0


@dataclass(frozen=True, slots=True)
class AppConfig:
    """The complete validated configuration."""

    telemetry: TelemetryConfig
    uart: UartConfig
    storage: StorageConfig
    simulation: SimulationConfig


# ---------------------------------------------------------------------------
# Loading
#
# Module-level functions rather than a ConfigLoader class. Parsing is one
# operation with no state to carry between calls, so a class would add a
# lifecycle without adding meaning.
# ---------------------------------------------------------------------------

_ROOT_SECTIONS = {"telemetry", "uart", "storage", "simulation"}


def load_config(path: Path | None = None) -> AppConfig:
    """
    Read and validate configuration.

    Args:
        path: Configuration file. Defaults to the shipped app.toml.

    Raises:
        ConfigError: The file is missing, is not valid TOML, or its contents
            fail validation. The message names the offending section or key.
    """
    config_path = path if path is not None else DEFAULT_CONFIG_PATH

    document = _read_toml(config_path)

    _reject_unknown_keys("<root>", document, _ROOT_SECTIONS, config_path)

    return AppConfig(
        telemetry=_parse_telemetry(document, config_path),
        uart=_parse_uart(document, config_path),
        storage=_parse_storage(document, config_path),
        simulation=_parse_simulation(document, config_path),
    )


def _parse_storage(document: dict[str, Any], path: Path) -> StorageConfig:
    section = _require_section(document, "storage", path)

    _reject_unknown_keys(
        "storage", section, {"history_seconds", "stale_after_frames"}, path
    )

    history_seconds = _require_int(section, "storage", "history_seconds", path)

    if history_seconds <= 0:
        raise ConfigError(f"{path}: storage.history_seconds must be positive")

    stale_after_frames = _require_int(section, "storage", "stale_after_frames", path)

    if stale_after_frames <= 0:
        raise ConfigError(f"{path}: storage.stale_after_frames must be positive")

    return StorageConfig(
        history_seconds=history_seconds,
        stale_after_frames=stale_after_frames,
    )


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        raise ConfigError(f"Configuration file not found: {path}") from None
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path} is not valid TOML: {error}") from error
    except OSError as error:
        raise ConfigError(f"Could not read {path}: {error}") from error


def _parse_telemetry(document: dict[str, Any], path: Path) -> TelemetryConfig:
    section = _require_section(document, "telemetry", path)

    _reject_unknown_keys("telemetry", section, {"source"}, path)

    raw_source = _require_str(section, "telemetry", "source", path)

    try:
        source = TelemetrySourceKind(raw_source)
    except ValueError:
        valid = ", ".join(f'"{kind.value}"' for kind in TelemetrySourceKind)

        raise ConfigError(
            f'{path}: telemetry.source is "{raw_source}", '
            f"which is not a known source. Valid options: {valid}"
        ) from None

    return TelemetryConfig(source=source)


def _parse_uart(document: dict[str, Any], path: Path) -> UartConfig:
    section = _require_section(document, "uart", path)

    _reject_unknown_keys("uart", section, {"port"}, path)

    port = _require_str(section, "uart", "port", path)

    if not port.strip():
        raise ConfigError(f"{path}: uart.port must not be empty")

    return UartConfig(port=port)


# ---------------------------------------------------------------------------
# Simulation parsing
# ---------------------------------------------------------------------------


def _parse_simulation(document: dict[str, Any], path: Path) -> SimulationConfig:
    section = _require_section(document, "simulation", path)

    _reject_unknown_keys(
        "simulation",
        section,
        {
            "seed",
            "frame_period_ms",
            "scenario_mode",
            "scenario_mean_interval_s",
            "scenario_min_gap_s",
            "physics",
            "generator",
            "link",
            "scenario",
        },
        path,
    )

    raw_mode = _require_str(section, "simulation", "scenario_mode", path)

    try:
        mode = ScenarioMode(raw_mode)
    except ValueError:
        valid = ", ".join(f'"{value.value}"' for value in ScenarioMode)

        raise ConfigError(
            f'{path}: simulation.scenario_mode is "{raw_mode}", which is not '
            f"a known mode. Valid options: {valid}"
        ) from None

    frame_period_ms = _require_int(section, "simulation", "frame_period_ms", path)

    if frame_period_ms <= 0:
        raise ConfigError(f"{path}: simulation.frame_period_ms must be positive")

    seed = _optional_int(section, "simulation", "seed", path)

    timing = ScenarioTiming(
        mean_interval_s=_require_positive_float(
            section, "simulation", "scenario_mean_interval_s", path
        ),
        min_gap_s=_require_non_negative_float(
            section, "simulation", "scenario_min_gap_s", path
        ),
    )

    return SimulationConfig(
        seed=seed,
        frame_period_ms=frame_period_ms,
        scenario_mode=mode,
        scenario_timing=timing,
        scenarios=_parse_scenarios(section, path),
        physics=_parse_physics(section, path),
        generator=_parse_generator(section, path),
        link=_parse_link(section, path),
    )


_LINK_KEYS = {
    "enabled",
    "packet_loss_probability",
    "corruption_probability",
    "delay_probability",
    "delay_seconds",
    "disconnect_probability",
    "disconnect_duration_s",
}


def _parse_link(simulation: dict[str, Any], path: Path) -> LinkFaultConfig:
    section = _require_section(simulation, "link", path, prefix="simulation")

    _reject_unknown_keys("simulation.link", section, _LINK_KEYS, path)

    name = "simulation.link"

    return LinkFaultConfig(
        enabled=_require_bool(section, name, "enabled", path),
        packet_loss_probability=_require_probability(
            section, name, "packet_loss_probability", path
        ),
        corruption_probability=_require_probability(
            section, name, "corruption_probability", path
        ),
        delay_probability=_require_probability(
            section, name, "delay_probability", path
        ),
        delay_seconds=_require_non_negative_float(section, name, "delay_seconds", path),
        disconnect_probability=_require_probability(
            section, name, "disconnect_probability", path
        ),
        disconnect_duration_s=_require_non_negative_float(
            section, name, "disconnect_duration_s", path
        ),
    )


_PHYSICS_KEYS = {
    "pressure_baseline_kpa",
    "pressure_reversion_per_s",
    "pressure_noise_kpa",
    "co_baseline",
    "co2_baseline",
    "ch4_baseline",
    "gas_reversion_per_s",
    "gas_noise_fraction",
    "gas_pressure_coupling",
}


def _parse_physics(simulation: dict[str, Any], path: Path) -> PhysicsConfig:
    section = _require_section(simulation, "physics", path, prefix="simulation")

    _reject_unknown_keys("simulation.physics", section, _PHYSICS_KEYS, path)

    name = "simulation.physics"

    baseline = _require_positive_float(section, name, "pressure_baseline_kpa", path)

    return PhysicsConfig(
        pressure_baseline_kpa=baseline,
        pressure_reversion_per_s=_require_positive_float(
            section, name, "pressure_reversion_per_s", path
        ),
        pressure_noise_kpa=_require_non_negative_float(
            section, name, "pressure_noise_kpa", path
        ),
        co_baseline=_require_non_negative_float(section, name, "co_baseline", path),
        co2_baseline=_require_non_negative_float(section, name, "co2_baseline", path),
        ch4_baseline=_require_non_negative_float(section, name, "ch4_baseline", path),
        gas_reversion_per_s=_require_positive_float(
            section, name, "gas_reversion_per_s", path
        ),
        gas_noise_fraction=_require_non_negative_float(
            section, name, "gas_noise_fraction", path
        ),
        gas_pressure_coupling=_require_float(
            section, name, "gas_pressure_coupling", path
        ),
    )


_GENERATOR_KEYS = {
    "anomaly_baseline",
    "anomaly_sensitivity",
    "anomaly_noise",
    "anomaly_reference_alpha",
    "anomaly_pressure_scale_kpa",
    "anomaly_gas_scale_fraction",
    "emulate_anomaly_verdict",
}


def _parse_generator(simulation: dict[str, Any], path: Path) -> GeneratorConfig:
    section = _require_section(simulation, "generator", path, prefix="simulation")

    _reject_unknown_keys("simulation.generator", section, _GENERATOR_KEYS, path)

    name = "simulation.generator"

    alpha = _require_float(section, name, "anomaly_reference_alpha", path)

    if not 0.0 < alpha <= 1.0:
        raise ConfigError(
            f"{path}: {name}.anomaly_reference_alpha must be in (0, 1], got {alpha}"
        )

    return GeneratorConfig(
        anomaly_baseline=_require_non_negative_float(
            section, name, "anomaly_baseline", path
        ),
        anomaly_sensitivity=_require_non_negative_float(
            section, name, "anomaly_sensitivity", path
        ),
        anomaly_noise=_require_non_negative_float(section, name, "anomaly_noise", path),
        anomaly_reference_alpha=alpha,
        anomaly_pressure_scale_kpa=_require_positive_float(
            section, name, "anomaly_pressure_scale_kpa", path
        ),
        anomaly_gas_scale_fraction=_require_positive_float(
            section, name, "anomaly_gas_scale_fraction", path
        ),
        emulate_anomaly_verdict=_require_bool(
            section, name, "emulate_anomaly_verdict", path
        ),
    )


_SCENARIO_KEYS = {
    "name",
    "duration_s",
    "weight",
    "ramp_fraction",
    "pressure_bias_kpa",
    "pressure_impulse_kpa_s",
    "gas_multiplier",
    "stall_pressure_sensor",
    "stall_gas_sensors",
}


def _parse_scenarios(
    simulation: dict[str, Any], path: Path
) -> tuple[ScenarioDefinition, ...]:
    """
    Parse the [[simulation.scenario]] array.

    An empty or absent array is legal: it means the reactor simply runs
    nominally, which is a valid way to observe steady-state behaviour.
    """
    raw = simulation.get("scenario", [])

    if not isinstance(raw, list):
        raise ConfigError(
            f"{path}: simulation.scenario must be an array of tables "
            f"([[simulation.scenario]])"
        )

    definitions: list[ScenarioDefinition] = []
    seen: set[str] = set()

    for index, entry in enumerate(raw):
        label = f"simulation.scenario[{index}]"

        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: {label} must be a table")

        _reject_unknown_keys(label, entry, _SCENARIO_KEYS, path)

        name = _require_str(entry, label, "name", path)

        if not name.strip():
            raise ConfigError(f"{path}: {label}.name must not be empty")

        if name in seen:
            # Duplicates would silently shadow one another in the engine's
            # name lookup, so the second definition would never run and
            # nobody would be told why.
            raise ConfigError(f"{path}: duplicate scenario name {name!r}")

        seen.add(name)

        duration = _require_positive_float(entry, label, "duration_s", path)

        ramp = _optional_float(entry, label, "ramp_fraction", 0.25, path)

        if not 0.0 <= ramp <= 0.5:
            raise ConfigError(
                f"{path}: {label}.ramp_fraction must be in [0, 0.5], got {ramp}. "
                f"Ramping in and out cannot together exceed the full duration."
            )

        definitions.append(
            ScenarioDefinition(
                name=name,
                duration_s=duration,
                weight=_optional_non_negative_float(entry, label, "weight", 1.0, path),
                ramp_fraction=ramp,
                pressure_bias_kpa=_optional_float(
                    entry, label, "pressure_bias_kpa", 0.0, path
                ),
                pressure_impulse_kpa_s=_optional_float(
                    entry, label, "pressure_impulse_kpa_s", 0.0, path
                ),
                gas_multiplier=_optional_non_negative_float(
                    entry, label, "gas_multiplier", 1.0, path
                ),
                stall_pressure_sensor=_optional_bool(
                    entry, label, "stall_pressure_sensor", False, path
                ),
                stall_gas_sensors=_optional_bool(
                    entry, label, "stall_gas_sensors", False, path
                ),
            )
        )

    return tuple(definitions)


# ---------------------------------------------------------------------------
# Validation primitives
#
# Each raises ConfigError naming the exact location, so the message is
# actionable without opening the source.
# ---------------------------------------------------------------------------


def _require_section(
    document: dict[str, Any], name: str, path: Path, prefix: str = ""
) -> dict[str, Any]:
    label = f"{prefix}.{name}" if prefix else name

    if name not in document:
        raise ConfigError(f"{path}: missing required section [{label}]")

    section = document[name]

    if not isinstance(section, dict):
        raise ConfigError(f"{path}: [{label}] must be a section, not a value")

    return section


def _require_str(
    section: dict[str, Any], section_name: str, key: str, path: Path
) -> str:
    value = _require_key(section, section_name, key, path)

    if not isinstance(value, str):
        raise ConfigError(
            f"{path}: {section_name}.{key} must be a string, "
            f"got {type(value).__name__}"
        )

    return value


def _require_bool(
    section: dict[str, Any], section_name: str, key: str, path: Path
) -> bool:
    value = _require_key(section, section_name, key, path)

    if not isinstance(value, bool):
        raise ConfigError(
            f"{path}: {section_name}.{key} must be true or false, "
            f"got {type(value).__name__}"
        )

    return value


def _require_int(
    section: dict[str, Any], section_name: str, key: str, path: Path
) -> int:
    value = _require_key(section, section_name, key, path)

    # bool is a subclass of int; accepting it here would let `true` parse as 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"{path}: {section_name}.{key} must be an integer, "
            f"got {type(value).__name__}"
        )

    return value


def _require_float(
    section: dict[str, Any], section_name: str, key: str, path: Path
) -> float:
    value = _require_key(section, section_name, key, path)

    return _coerce_float(value, section_name, key, path)


def _require_positive_float(
    section: dict[str, Any], section_name: str, key: str, path: Path
) -> float:
    value = _require_float(section, section_name, key, path)

    if value <= 0.0:
        raise ConfigError(f"{path}: {section_name}.{key} must be positive, got {value}")

    return value


def _require_non_negative_float(
    section: dict[str, Any], section_name: str, key: str, path: Path
) -> float:
    value = _require_float(section, section_name, key, path)

    if value < 0.0:
        raise ConfigError(
            f"{path}: {section_name}.{key} must not be negative, got {value}"
        )

    return value


def _require_probability(
    section: dict[str, Any], section_name: str, key: str, path: Path
) -> float:
    value = _require_float(section, section_name, key, path)

    if not 0.0 <= value <= 1.0:
        raise ConfigError(
            f"{path}: {section_name}.{key} is a probability and must be in "
            f"[0, 1], got {value}"
        )

    return value


def _optional_float(
    section: dict[str, Any],
    section_name: str,
    key: str,
    default: float,
    path: Path,
) -> float:
    if key not in section:
        return default

    return _coerce_float(section[key], section_name, key, path)


def _optional_non_negative_float(
    section: dict[str, Any],
    section_name: str,
    key: str,
    default: float,
    path: Path,
) -> float:
    value = _optional_float(section, section_name, key, default, path)

    if value < 0.0:
        raise ConfigError(
            f"{path}: {section_name}.{key} must not be negative, got {value}"
        )

    return value


def _optional_bool(
    section: dict[str, Any],
    section_name: str,
    key: str,
    default: bool,
    path: Path,
) -> bool:
    if key not in section:
        return default

    return _require_bool(section, section_name, key, path)


def _optional_int(
    section: dict[str, Any], section_name: str, key: str, path: Path
) -> int | None:
    if key not in section:
        return None

    return _require_int(section, section_name, key, path)


def _require_key(
    section: dict[str, Any], section_name: str, key: str, path: Path
) -> Any:
    if key not in section:
        raise ConfigError(f"{path}: missing required key {section_name}.{key}")

    return section[key]


def _coerce_float(value: Any, section_name: str, key: str, path: Path) -> float:
    # TOML distinguishes integers from floats, but `1` and `1.0` mean the same
    # thing for a physical parameter, so integers are accepted. bool is
    # excluded because it is an int subclass and `true` is never a quantity.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(
            f"{path}: {section_name}.{key} must be a number, "
            f"got {type(value).__name__}"
        )

    return float(value)


def _reject_unknown_keys(
    section_name: str, section: dict[str, Any], allowed: set[str], path: Path
) -> None:
    """
    Refuse anything not explicitly supported.

    Catches typos, settings copied from a newer version, and keys left behind
    by a removed feature -- all of which otherwise fail silently.
    """
    unknown = sorted(set(section) - allowed)

    if not unknown:
        return

    expected = ", ".join(sorted(allowed))

    raise ConfigError(
        f"{path}: unknown key(s) in [{section_name}]: {', '.join(unknown)}. "
        f"Supported: {expected}"
    )
