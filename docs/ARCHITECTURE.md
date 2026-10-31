# Reactor Dashboard — Software Architecture

**Target:** Raspberry Pi Zero 2 W
**Stack:** Python 3.11, PySide6, PyQtGraph, PySerial
**Companion document:** [PROTOCOL.md](PROTOCOL.md) — the Arduino contract

---

## 0. Runtime and Toolchain

**Python 3.11 — pinned to the deployment target, not to the newest release.**

Raspberry Pi OS Bookworm ships Python 3.11. Developing on a newer interpreter
and deploying to 3.11 invites the one class of bug that appears only on the
hardware, which is precisely the class we cannot afford to discover on
commissioning day. The development venv therefore runs 3.11 as well.

**64-bit Raspberry Pi OS is required.** Qt publishes manylinux `aarch64`
wheels for PySide6 but no `armv7l` wheels, so a 32-bit image cannot
`pip install PySide6` and would need a source build — days of work on a Pi
Zero. Confirm `uname -m` reports `aarch64` when imaging the card.

Dependencies are pinned exactly (`requirements.txt`). An industrial HMI that
changes behaviour because a transitive dependency published a release is not
an industrial HMI; version bumps are deliberate, reviewed and tested.

---

## 1. Scope and Non-Goals

The Pi is a **read-only industrial HMI and analytics platform**. It receives
telemetry, interprets it, displays it and logs it.

**The Pi never controls the reactor. It is not safety-critical.**

That single fact shapes the whole design:

- No command path, no actuation, no interlocks.
- A crashed or frozen dashboard is an availability problem, not a safety
  problem — so the design optimises for *clarity and observability* over
  hard-real-time guarantees.
- But the operator may still *act* on what the screen says. So the HMI must
  never present absent data as good data. See PROTOCOL.md §6.1.

**Non-goals:** control, reactor configuration, firmware update, remote access,
multi-user auth.

---

## 2. Layering

Strictly one-directional. Each layer knows only the layer beneath it.

```
              ┌─────────────────────────────┐
              │        dashboard/           │   PySide6 widgets. No I/O,
              │        (presentation)       │   no parsing, no maths.
              └──────────────┬──────────────┘
                             │ Qt signals
              ┌──────────────┴──────────────┐
              │        analytics/           │   Health, stability, trends,
              │  (interpretation)           │   events, derived warnings.
              └──────────────┬──────────────┘
                             │
              ┌──────────────┴──────────────┐
              │        backend/             │   TelemetryStore (ring buffer),
              │  (domain + orchestration)   │   TelemetryFrame, session state.
              └──────────────┬──────────────┘
                             │ TelemetryFrame
       ┌─────────────────────┴─────────────────────┐
       │                                            │
┌──────┴───────┐  ┌──────────────┐          ┌───────┴───────┐
│  serial/     │  │ simulation/  │          │   storage/    │
│  UARTSource  │  │ SimSource    │          │ CSV / SQLite  │
└──────────────┘  └──────────────┘          └───────────────┘
   both implement TelemetrySource            sink, not a source
```

**Rule:** a `dashboard/` module may not `import serial`, `struct`, or `csv`. If
it needs a number, the layer below hands it one. This is what makes the
simulator-to-UART swap invisible to the UI.

---

## 3. The Seam: `TelemetrySource`

The single most important design decision, because the whole "hardware arrives
in two days" requirement rests on it.

```python
class TelemetrySource(Protocol):
    """A source of raw telemetry bytes. That is the entire contract."""

    def read(self) -> bytes: ...
```

**One method. No lifecycle.** A source provides bytes; everything else —
startup, shutdown, threading, buffering, scheduling, reconnection — is an
implementation detail that differs between the two sources and that no caller
needs in order to obtain bytes.

Forcing `start()`/`stop()` into the contract would mean `SimulationSource`
growing empty methods purely to satisfy an interface written for
`UARTSource`: an interface serving its implementations rather than its
callers.

Two implementations, identical contract:

| | `SimulationSource` | `UARTSource` |
| --- | --- | --- |
| Emits | `bytes` | `bytes` |
| Cadence | 1 Hz (matches firmware) | 1 Hz (driven by firmware) |
| Threading | none needed | private — thread or non-blocking poll |
| Failure modes | none | port missing, desync, silence |

Three consequences follow from the minimal contract, each deliberate:

**Pull, not push.** A driver polls; the source never calls back. A push
interface would require every source to own a timer or thread in order to have
something to push *from* — reintroducing exactly the lifecycle this excludes.
Polling costs nothing at 1 Hz with 56-byte packets, and an idle poll returns
`b""`.

**No `close()`.** Reconnection after a cable fault is `UARTSource`'s own
internal business — it reopens its own port without being asked — and process
exit releases the file descriptor. A lifecycle method existing to serve one
implementation and no-op in the other is not a contract, it's a courtesy.

**No connection status.** Link health is *derived from frame arrival*, not
reported by the transport. This is the more truthful measurement: a serial
port can be open, healthy and completely useless because the Arduino behind it
crashed, lost power, or was unplugged at the far end — and the port reports
"connected" throughout. What the operator needs is whether fresh telemetry is
arriving, which the store answers from `received_at`, supplemented by
`DecoderStats`.

### Why `Protocol` and not an ABC

Structural typing: an implementation conforms by having `read()`, not by
inheriting. Neither source imports this module — coupling across the seam is
exactly zero, and there is no base class in which shared behaviour could
accumulate.

Accepted cost: no construction-time completeness check; a missing `read()`
surfaces as an `AttributeError` at first call. With one required method and
two tested in-repo implementations, that is the right trade.

Deliberately **not** `@runtime_checkable` — `isinstance()` against a
runtime-checkable Protocol verifies only that an attribute named `read`
exists, not its signature or return type. False assurance is worse than none.

Selection is a **single configuration value**:

```toml
# config/app.toml
[telemetry]
source = "simulation"   # -> "uart" when hardware arrives
```

A factory reads that value and constructs the right object; `main.py` injects
the result. **No other file in the project knows which source is running** —
including every file under `dashboard/`.

Configuration is validated strictly at startup: unknown sections, unknown
keys, wrong types and unknown enum values are all hard errors before the GUI
opens. The permissive alternative — ignore what you don't recognise, fall back
to a default — means an engineer who types `sim` instead of `simulation`,
restarts, and sees no change concludes the setting doesn't work. It is also
why there is **no configuration key without a consumer**: dead config is a
lie about what the system does. Keys land alongside the module that reads them.

Note what is *not* configurable: baud rate, framing and packet geometry are
protocol constants owned by `backend/protocol/spec.py`. They are contract, not
deployment choice, and exposing them in `app.toml` would only create a way to
set them wrong.

### Why the simulator must produce real `TelemetryFrame`s

The simulator does **not** get a shortcut path into the UI. It produces exactly
the same object the UART path produces, with exactly the same units, ranges and
quirks — including `temperature = 0.0` and unscaled gas values
(PROTOCOL.md §6.1, §6.2).

If the simulator emitted "nicer" data than the reactor, the dashboard would be
tuned against fiction and every scale, colour band and threshold would be wrong
on day one of real hardware. The simulator's job is to be *representative*, not
pleasant.

**Decided: byte-accurate simulation.** `SimulationSource` emits real **bytes**
through the same `PacketDecoder` the UART path uses. The decoder — the
component most likely to harbour an off-by-one — is therefore exercised
continuously for two days before hardware arrives, rather than being tested for
the first time when the reactor is live. Cost: one extra serialiser
(`protocol/encoder.py`) plus a round-trip test.

This also makes the two sources differ in exactly one respect — where the bytes
come from — which is the strongest possible form of the seam.

Packet generation is kept **separate from** the simulator's physics:

```
TelemetryGenerator  ->  PacketEncoder  ->  SimulationSource  ->  PacketDecoder
   reactor state          serialise          read() -> bytes       parse
```

`TelemetryGenerator` produces `TelemetryFrame` objects and knows nothing about
bytes. `PacketEncoder` (already built, `backend/protocol/encoder.py`) owns
serialisation and is the same code path a test uses. `SimulationSource` is a
thin adapter: generate, encode, hand back bytes.

The separation keeps all protocol knowledge in `backend/protocol/` — the
simulator never learns a byte offset — while still driving the encoder and
decoder continuously before hardware integration.

### Link faults are a separate layer

A pressure excursion is the *reactor* misbehaving. A CRC error is the *cable*
misbehaving. Folding both into `SimulationSource` would give it two
responsibilities and make it impossible to test the dashboard's communication
handling without also perturbing the reactor.

So fault injection is a decorator that is itself a `TelemetrySource`:

```
SimulationSource  ->  FaultInjectingSource  ->  PacketDecoder
```

Two consequences fall out of this for free:

- Because the seam is structural, it wraps **any** source — including
  `UARTSource`. "Does the dashboard recover from a cable pull?" becomes a
  question you can ask on demand instead of by physically unplugging things.
- It needs **zero** protocol knowledge. Dropping a read is packet loss;
  flipping a bit is a CRC error; returning `b""` is a dead link. Nothing in
  `fault_injection.py` imports from `backend/protocol/`.

One honest consequence: a flipped bit lands wherever it lands. Roughly 1 in 28
lands in the 2-byte sync word of a 56-byte packet and the decoder
resynchronises instead of reporting a CRC error. Measured over 3000 frames: 34
corruptions injected → 30 CRC errors + 4 resyncs. Both are real line faults,
and neither is worth special-casing away.

It is composed in only when `[simulation.link] enabled = true`, so a developer
building a gauge is not fighting injected errors, and a live reactor link is
never damaged by default.

---

## 4. Module Plan

```
reactor_dashboard/
├── backend/
│   ├── telemetry_frame.py       # frozen dataclass — pure wire payload
│   ├── transport_metadata.py    # arrival, source, sequence, session + record
│   ├── device_clock.py          # restart / discontinuity detection
│   ├── telemetry_store.py       # deque window, typed views, link health
│   ├── telemetry_source.py      # the seam + TelemetrySourceKind
│   └── protocol/
│       ├── spec.py              # offsets, sizes, sync bytes, version, enums
│       ├── limits.py            # mirrored firmware constants (PROTOCOL.md §10)
│       ├── crc.py               # Modbus CRC-16
│       ├── decoder.py           # byte stream -> TelemetryFrame (stateful)
│       └── encoder.py           # TelemetryFrame -> bytes (test + simulator)
│
├── transport/
│   ├── uart_source.py           # QThread reader, feeds PacketDecoder
│   └── port_discovery.py        # locate the device, survive replug
│
├── simulation/
│   ├── reactor_physics.py       # the physics — no Qt, no I/O, pure and testable
│   ├── disturbance.py           # value object shared by physics and generator
│   ├── scenario.py              # upset lifecycle; definitions come from config
│   ├── telemetry_generator.py   # reactor state -> TelemetryFrame
│   ├── simulation_source.py     # cadence only; read() -> bytes
│   ├── fault_injection.py       # link faults, wraps ANY source
│   └── factory.py               # assembly from validated config
│
├── analytics/
│   ├── derived_warnings.py      # reconstruct the 5 threshold bits (PROTOCOL.md §11)
│   ├── health_score.py          # Reactor Health Score  (Pi-computed)
│   ├── stability_index.py       # Reactor Stability Index (Pi-computed)
│   ├── ai_confidence.py         # derived from anomalyScore (Pi-computed)
│   ├── trend.py                 # rising / falling / steady per signal
│   ├── statistics.py            # min / max / mean / σ over the window
│   └── event_log.py             # state transitions -> operator timeline
│
├── storage/
│   ├── csv_logger.py            # append-only, rotating, flushed
│   └── (sqlite_logger.py)       # later
│
├── dashboard/
│   ├── theme.py                 # the single source of colour and type
│   ├── main_window.py
│   └── widgets/                 # banner, gauges, sparklines, trends, cards…
│
├── config/
│   ├── app.toml                 # the one file an operator edits
│   └── settings.py              # typed loader with validation
│
├── tests/
├── requirements.txt
└── main.py                      # composition root: build graph, inject, run
```

### Notes on specific choices

**`TelemetryStore` is the single owner of received telemetry.** The dashboard,
analytics, the logger and future replay all read through it; none keeps its
own copy. That rule is worth stating because the alternative is easy to drift
into — a gauge caching "the last pressure it drew", an analytics class with
its own rolling window, a logger with a pending list. Each is individually
reasonable, and together they let the screen show four different answers to
the same question with no way for the operator to tell which is right.

**A frame is exactly the wire payload; arrival data lives beside it.**
`TelemetryFrame` carried a host-stamped `received_at` until step 4. Removing
it means two frames decoded from identical bytes are now *equal*, which is
what makes round-trip, replay and regression comparison mean anything.
Arrival time, source, sequence and session live in `TransportMetadata`, and
the pair is a `TelemetryRecord`. Analytics that takes a `TelemetryFrame` is
structurally unable to depend on delivery details.

**Storage is a `deque`; numpy is only a view.** `deque(maxlen=capacity)` gives
O(1) append and free eviction, and holds domain objects rather than columns. A
numpy-backed ring buffer would force a struct-of-arrays layout — stopping a
record being one object anyone can pass around — or repeated resizing. Columns
are materialised on demand for plotting and are *not* cached: measured at
0.65 ms for three columns over 3600 records, or ~0.3% of a core at a 5 Hz
repaint. If profiling on the Pi ever disagrees, the fix is a dirty flag
invalidated in `append()`; it is not worth the complexity in advance.

**Any backward step in the device clock is a restart, never a rollover.**
`millis()` can regress because the Arduino rebooted or because the counter
wrapped at 49.7 days. Both start a new session, and the asymmetry decides it:
a rollover mislabelled as a restart costs one spurious session boundary every
fifty days, while a restart mislabelled as a rollover silently stitches two
unrelated runs together and computes a seven-week elapsed time — poisoning
every rate, trend and gap that spans it. The code is deliberately wrong in the
cheap direction.

**`backend/protocol/` is a package, not a file.** The protocol is the contract
with another team's firmware; it deserves its own namespace, its own tests, and
a hard boundary. Nothing outside it may know a byte offset.

**`limits.py` is quarantined.** It holds constants duplicated from firmware
(PROTOCOL.md §10). Duplication across a process boundary is a known liability,
so it lives in exactly one place, annotated, and tested.

**`simulation/reactor_model.py` has no Qt import.** The physics is a pure
function of state and dt. That makes it unit-testable without an event loop and
lets us run 10 000 simulated seconds in a test in milliseconds.

**`analytics/` is pure computation over the store.** No widgets, no timers. Each
metric is independently testable with a synthetic frame sequence.

**`transport/`, not `serial/`.** A package named `serial` shadows PySerial's
import name inside that directory. Absolute imports make it work, but it is a
trap for the next reader, so the link layer is named for what it is rather than
for the library it wraps. This is the only deviation from the specified
structure.

---

## 5. Threading Model

The Pi Zero 2 W is a modest 4-core machine and PySide6 is strictly
single-threaded for UI. The model is deliberately boring:

| Thread | Owns | Notes |
| --- | --- | --- |
| **Main (Qt)** | All widgets, `TelemetryStore`, analytics | Never blocks |
| **Reader** (`QThread`) | `pyserial` handle + `PacketDecoder` | Emits `TelemetryFrame` via queued signal |
| **Logger** | CSV writes | Queued; a slow SD card must never stall the UI |

Cross-thread communication is **Qt queued signals only** — no shared mutable
state, no locks. Frames are immutable (`frozen=True`), so handing one across a
thread boundary is safe by construction.

**UI repaint is decoupled from arrival.** Frames arrive at 1 Hz but plots
redraw on their own ~5 Hz timer reading the store. A burst (e.g. a history
replay, PROTOCOL.md §7) therefore cannot trigger 60 repaints.

---

## 6. Signals the Firmware Does Not Send

Three headline HMI numbers do not exist in the packet and must be computed on
the Pi. This is stated explicitly so they are never mistaken for reactor
output:

| Metric | Basis | Honesty requirement |
| --- | --- | --- |
| **Reactor Health Score** | Weighted distance of each signal from its firmware limit, penalised by active warnings and by data staleness | Must degrade when inputs are missing, not stay green |
| **Reactor Stability Index** | Rolling variance of pressure / gas over the window, normalised to `[0,1]` | Firmware lists this as a blocked feature (`PhysicsEngine.h:122-127`); the Pi implements it because it has the history the Arduino lacks |
| **AI Confidence** | Distance of `anomalyScore` from `ANOMALY_THRESHOLD = 0.80` | Labelled **derived**. It is *not* a model-reported confidence, and the model is currently observe-only (PROTOCOL.md §6.3) |

Health Score must be a **function of measured coverage as well as measured
values.** With temperature absent (PROTOCOL.md §6.1) the reactor is only
partially observed, and a 100 % health score would be a lie. The score is
therefore capped by an observability factor, with the cap explained in the UI.

---

## 7. HMI Principles

Design language: Siemens WinCC / ABB 800xA / Honeywell Experion. Dark,
minimal, industrial.

**The 3-second rule.** An operator glancing at the screen must know the reactor
state in under three seconds. That drives a strict visual hierarchy:

1. **State banner** — full width, colour-coded, unmissable. Reads at 3 m.
2. **Health / Stability / Confidence** — three large figures.
3. **Gauges with trend arrows** — shape and colour before digits.
4. **Trends and sparklines** — context.
5. **Statistics, event timeline, diagnostics** — detail on demand.

**Colour is redundant, never sole.** ~8 % of men have a colour vision
deficiency, and industrial panels are viewed in bad light. Every state is
carried by colour **and** by text **and** by position/shape.

| State | Colour | Also carried by |
| --- | --- | --- |
| `NORMAL` | Green | Text "NORMAL", calm banner |
| `WARNING` | Amber | Text, banner border, warning list |
| `EMERGENCY` | Red | Text, pulsing border, top placement |
| `ANOMALY` | Cyan/violet | Text, distinct from the threshold palette |
| `NO DATA` | Grey, desaturated | Text "NO SIGNAL", struck-through values |

**Absent data must look absent.** Grey, hatched, `--`. Never `0.0` in green.
This is the single rule that most separates an industrial HMI from a pretty
dashboard, and it is unusually load-bearing here because two of the eleven
telemetered values are permanently zero (PROTOCOL.md §6.1).

**No animation for its own sake.** Motion means change in the process. A
pulsing element must correspond to something actually pulsing.

---

## 8. Reliability

| Failure | Behaviour |
| --- | --- |
| Serial port absent at start | Dashboard runs, banner reads `NO SIGNAL`, retry loop |
| Cable unplugged mid-run | `NO SIGNAL` after a 3-frame timeout; last values greyed with an age stamp; **never** frozen-but-green |
| CRC errors | Counted, shown in the diagnostics strip; frame discarded |
| Byte desync | Resync per PROTOCOL.md §9.6; counted |
| Unknown protocol version | Counted, dropped, banner warns of firmware skew |
| Arduino reboot (`timestamp` jumps back) | New session marked on the timeline; rate maths reset |
| `millis()` rollover (49.7 d) | Detected and stitched, not treated as a reboot |
| Disk full / SD failure | Logger reports degraded; **UI keeps running** |

The diagnostics strip (connection, packets, CRC errors, logger state) is
permanently visible. An operator must never have to open a menu to find out
whether the number in front of them is live.

---

## 9. Build Order

Each step leaves the project runnable. Roughly 75 % complete when hardware
lands.

| # | Step | Deliverable |
| --- | --- | --- |
| 0 | ✅ Protocol spec + architecture | *this document*, `PROTOCOL.md` |
| 1 | ✅ `backend/protocol/` + `TelemetryFrame` | `python main.py` self-check; codec + CRC tests |
| 2 | ✅ `config/` + `TelemetrySource` protocol | The seam, and the one value that flips it |
| 3 | ✅ `simulation/` — physics, scenarios, generator, faults | Realistic byte-accurate 1 Hz stream |
| 4 | ✅ `backend/` — store, transport metadata, device clock | Headless telemetry pipeline |
| 5 | `dashboard/` — theme, shell, state banner | First window: state readable in 3 s |
| 6 | Gauges, sparklines, trend arrows | Primary indication complete |
| 7 | `analytics/` — health, stability, warnings, events | Derived metrics live |
| 8 | Live trend graphs, statistics cards, timeline | Full HMI |
| 9 | `storage/` — CSV logger | Logging + logger status indicator |
| 10 | **`UARTSource`** — hardware day | Flip one config value |
| 11 | SQLite, history replay, hardening | Post-hardware |

Step 10 touches **no** dashboard, analytics, backend or simulation code. If it
does, the seam in §3 was built wrong.

---

## 10. Open Questions

Recorded here rather than guessed at. None block steps 1–9.

1. **Nominal operating envelope — OPEN, working assumption in place.** Firmware
   warns at 150 kPa and declares emergency at 180 kPa, but a BMP280 reads
   absolute pressure (~101 kPa at atmosphere). Not yet characterised.
   **Working assumption: nominal 100–110 kPa, gauge span 0–200 kPa** with bands
   at 150 / 180. Both the simulator baseline and every gauge scale are
   **configuration values, not literals**, so retuning after the reactor is
   characterised is a config edit rather than a code change.
2. **Gas scaling factor.** Until `RS485GasSensor` applies datasheet scaling
   (PROTOCOL.md §6.2), gas is in raw counts. Do the datasheets exist yet? The
   simulator generates values on the ppm scale the thresholds assume, labelled
   provisional.
3. **Temperature sensor timeline.** If it lands before the demo, health scoring
   and the gauge grid change materially. Worth knowing now.
4. ~~`serial/` package name~~ — **resolved: renamed to `transport/`.**
5. **Logging retention.** CSV rotation size/age, and whether the Pi's SD card
   or a USB stick is the target. Affects the logger design, not the UI.
