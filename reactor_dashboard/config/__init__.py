"""
Application configuration.

Owns app.toml and its typed loader. Nothing else in the project reads a TOML
file or knows a configuration key name.
"""

from config.settings import (
    DEFAULT_CONFIG_PATH,
    AppConfig,
    ConfigError,
    StorageConfig,
    TelemetryConfig,
    TelemetrySourceKind,
    UartConfig,
    load_config,
)

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "AppConfig",
    "ConfigError",
    "StorageConfig",
    "TelemetryConfig",
    "TelemetrySourceKind",
    "UartConfig",
    "load_config",
]
