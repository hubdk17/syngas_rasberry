"""
Reactor Dashboard -- application entry point.

CURRENT SCOPE
-------------
The protocol, configuration and simulation layers are implemented (build steps
1 to 3 of docs/ARCHITECTURE.md section 9). There is no GUI yet, so this entry
point does what it can honestly do:

    1. Load and report the configuration.
    2. Run a protocol self-check -- encode a firmware-shaped frame, push it
       through the real decoder in fragments, report what came back.
    3. Run the configured telemetry source for a few minutes of simulated
       time and summarise the stream.

That is not a throwaway. It is the command an engineer runs on a freshly
imaged Raspberry Pi to confirm the install is sound before touching the
reactor, and it stays useful once the GUI exists.

As later modules land this file becomes the composition root: it will wire the
telemetry source to the store, analytics and dashboard, and start the Qt event
loop. The self-check moves to tools/ at that point.

Usage:
    python main.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The application root must be importable before any `backend.*` import, so
# that `python main.py` works from any working directory.
APP_ROOT = Path(__file__).resolve().parent

if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from backend.protocol.decoder import PacketDecoder  # noqa: E402
from backend.protocol.encoder import encode_frame  # noqa: E402
from backend.protocol.spec import (  # noqa: E402
    BAUD_RATE,
    HEADER_LENGTH,
    MAX_PACKET_LENGTH,
    PROTOCOL_VERSION,
    SAMPLE_PAYLOAD_LENGTH,
    TELEMETRY_PERIOD_MS,
    ReactorState,
)
import numpy as np  # noqa: E402

from backend.telemetry_frame import TelemetryFrame  # noqa: E402
from backend.telemetry_store import TelemetryStore  # noqa: E402
from config.settings import AppConfig, ConfigError, load_config  # noqa: E402
from telemetry_factory import create_telemetry_source  # noqa: E402

#: Fragment size used by the self-check. Deliberately not a packet multiple,
#: so packets straddle read boundaries exactly as they will over a real UART.
_FRAGMENT_SIZE = 7

#: Simulated seconds summarised by the preview. Ten minutes is long enough for
#: the random scenario scheduler to fire two or three upsets, so the summary
#: shows the reactor doing something rather than sitting at baseline.
_PREVIEW_SECONDS = 600


def _reference_frame() -> TelemetryFrame:
    """
    A frame shaped like what the current firmware actually emits.

    Temperature is zero because no temperature sensor exists
    (docs/PROTOCOL.md section 6.1) and the anomaly verdict is suppressed
    (section 6.3). Using realistic values here means the self-check output
    doubles as documentation of what "normal" looks like on this reactor.
    """
    return TelemetryFrame(
        device_uptime_ms=1_234_567,
        pressure_kpa=101.4,
        pressure_rate_kpa_s=0.12,
        temperature_c=0.0,
        temperature_rate_c_s=0.0,
        co=418.0,
        co2=3180.0,
        ch4=1860.0,
        gas_ratio=0.5849,
        gas_concentration=5458.0,
        anomaly_score=0.07,
        warning=False,
        emergency=False,
        anomaly=False,
        state=ReactorState.NORMAL,
    )


def _print_configuration(config: AppConfig) -> None:
    print("Configuration")
    print(f"  telemetry source    {config.telemetry.source.value}")

    if config.telemetry.is_simulated:
        print("                      (SIMULATED -- not a live reactor)")
    else:
        print(f"  serial port         {config.uart.port}")


def _print_contract() -> None:
    print("Protocol contract (derived from firmware, see docs/PROTOCOL.md)")
    print(f"  protocol version    {PROTOCOL_VERSION}")
    print(f"  link                {BAUD_RATE} baud, 8N1")
    print(f"  frame cadence       {TELEMETRY_PERIOD_MS} ms")
    print(f"  header / payload    {HEADER_LENGTH} + {SAMPLE_PAYLOAD_LENGTH} bytes")
    print(f"  full FRAME packet   {MAX_PACKET_LENGTH} bytes")


def _print_packet(packet: bytes) -> None:
    print(f"Encoded FRAME ({len(packet)} bytes)")

    for offset in range(0, len(packet), 16):
        chunk = packet[offset : offset + 16]
        print(f"  {offset:04x}  {chunk.hex(' ')}")


def _print_decoded(frame: TelemetryFrame) -> None:
    print("Decoded frame")
    print(f"  device uptime       {frame.device_uptime_s:.3f} s")
    print(f"  pressure            {frame.pressure_kpa:.2f} kPa")
    print(f"  pressure rate       {frame.pressure_rate_kpa_s:+.3f} kPa/s")
    print(f"  temperature         {frame.temperature_c:.1f} C  (NO SENSOR)")
    print(f"  CO / CO2 / CH4      {frame.co:.0f} / {frame.co2:.0f} / {frame.ch4:.0f} raw")
    print(f"  gas ratio           {frame.gas_ratio:.4f}")
    print(f"  gas concentration   {frame.gas_concentration:.0f} raw")
    print(f"  anomaly score       {frame.anomaly_score:.3f}  (OBSERVE-ONLY)")
    print(f"  state               {frame.state.label}")


def _run_preview(config: AppConfig) -> int:
    """
    Drive the configured telemetry source through the real decoder.

    Uses a synthetic clock rather than sleeping, so ten minutes of reactor
    behaviour is summarised in a fraction of a second. Nothing here is
    simulation-specific: the same loop drives UARTSource unchanged, which is
    the seam doing its job.
    """
    elapsed = [0.0]
    wall = [datetime(2026, 1, 1, tzinfo=timezone.utc)]

    def monotonic() -> float:
        return elapsed[0]

    def wall_clock() -> datetime:
        return wall[0]

    source = create_telemetry_source(config, clock=monotonic)
    decoder = PacketDecoder()

    # The store is the single owner of received telemetry. Nothing below keeps
    # its own copy of a frame -- every figure printed is read back out of it.
    store = TelemetryStore(
        capacity=config.storage.history_frames,
        source=config.telemetry.source,
        clock=wall_clock,
    )

    step_s = TELEMETRY_PERIOD_MS / 1000.0

    for _ in range(_PREVIEW_SECONDS):
        elapsed[0] += step_s
        wall[0] += timedelta(seconds=step_s)

        for packet in decoder.feed(source.read()):
            if packet.frame is not None:
                store.append(packet.frame)

    print(f"Telemetry preview ({_PREVIEW_SECONDS} s simulated)")

    if store.is_empty:
        print("  no frames received")
        return 1

    pressures = store.pressures()
    rates = np.abs(store.pressure_rates())
    scores = store.anomaly_scores()
    states = store.states()

    counts = {
        ReactorState(value).label: int((states == value).sum())
        for value in np.unique(states)
    }

    print(f"  frames received     {store.total_received}")
    print(f"  window retained     {len(store)} / {store.capacity}")
    print(f"  pressure            {pressures.min():.1f} .. {pressures.max():.1f} kPa")
    print(f"  |pressure rate| max {rates.max():.2f} kPa/s")
    print(f"  anomaly score       {scores.min():.3f} .. {scores.max():.3f}")
    print(f"  warning frames      {int(store.warnings().sum())}")
    print(f"  emergency frames    {int(store.emergencies().sum())}")
    print("  states              " + ", ".join(
        f"{name} {count}" for name, count in sorted(counts.items())
    ))
    print(f"  device restarts     {store.restarts}")
    print(f"  max frame gap       {int(store.device_gaps_ms().max())} ms")
    print(f"  link stale          {store.is_stale(config.storage.stale_after_seconds)}")
    print(f"  decode errors       {decoder.stats.error_count}")
    print()

    if decoder.stats.error_count:
        print(f"PREVIEW FAILED: {decoder.stats.error_count} decode error(s)")
        return 1

    return 0


def run_self_check() -> int:
    """Load config, then encode/fragment/decode/verify. Returns an exit code."""
    # Configuration is validated before anything else runs. A misconfiguration
    # must never be able to present as a working system showing the wrong
    # thing, so it fails here rather than surfacing later.
    try:
        config = load_config()
    except ConfigError as error:
        print(f"CONFIGURATION ERROR\n  {error}")
        return 2

    _print_configuration(config)
    print()

    _print_contract()
    print()

    original = _reference_frame()
    packet = encode_frame(original)

    _print_packet(packet)
    print()

    # Feed the packet in undersized fragments to exercise the reassembly path
    # rather than the easy case of one whole packet per read.
    decoder = PacketDecoder()
    decoded = []

    for offset in range(0, len(packet), _FRAGMENT_SIZE):
        decoded.extend(decoder.feed(packet[offset : offset + _FRAGMENT_SIZE]))

    print(
        f"Fed {len(packet)} bytes in {_FRAGMENT_SIZE}-byte fragments -> "
        f"{len(decoded)} packet(s)"
    )
    print()

    if len(decoded) != 1 or decoded[0].frame is None:
        print("SELF-CHECK FAILED: decoder did not return exactly one frame")
        return 1

    _print_decoded(decoded[0].frame)
    print()

    stats = decoder.stats
    print("Decoder statistics")
    print(f"  bytes received      {stats.bytes_received}")
    print(f"  packets decoded     {stats.packets_decoded}")
    print(f"  CRC errors          {stats.crc_errors}")
    print(f"  version rejects     {stats.version_rejects}")
    print(f"  resyncs             {stats.resyncs}")
    print()

    if stats.error_count:
        print(f"SELF-CHECK FAILED: {stats.error_count} decode error(s)")
        return 1

    # The source is only exercised when one exists. Asking for UART before
    # step 10 raises rather than silently simulating.
    try:
        preview_status = _run_preview(config)
    except NotImplementedError as error:
        print(f"Telemetry preview skipped\n  {error}\n")
        preview_status = 0

    if preview_status:
        return preview_status

    print("SELF-CHECK PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_self_check())
