# Reactor Dashboard

Industrial HMI and analytics platform for a biomass/syngas reactor.

Runs on a Raspberry Pi Zero 2 W, receiving binary telemetry from an Arduino
GIGA R1 over UART.

**The Raspberry Pi never controls the reactor. It receives, interprets,
displays and logs. It is not part of the safety loop.**

---

## Documentation

| Document | Contents |
| --- | --- |
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | The wire contract, derived from the firmware. **Read this first.** |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Layering, module plan, threading, HMI principles, build order |

The firmware at `D:\Desktop\syngas_arduino` is the single source of truth for
the protocol. `docs/PROTOCOL.md` is a derivation of it, with firmware file and
line citations throughout. If the two ever disagree, the firmware is right and
the document is stale.

---

## Requirements

- **Python 3.11** — matches Raspberry Pi OS Bookworm. See ARCHITECTURE.md §0.
- **64-bit Raspberry Pi OS** on the target (`uname -m` must report `aarch64`).

## Setup

```bash
python3.11 -m venv .venv

# Linux / macOS
source .venv/bin/activate
# Windows
.venv\Scripts\activate

cd reactor_dashboard
pip install -r requirements.txt -r requirements-dev.txt
```

## Verify the install

```bash
cd reactor_dashboard
python main.py
```

Encodes a firmware-shaped telemetry frame, pushes it through the real decoder
in undersized fragments, and reports what came back. Exits non-zero on
failure, so it is usable as a commissioning check on a freshly imaged card.

## Run the tests

```bash
cd reactor_dashboard
pytest tests/ -q
```

---

## Project status

Hardware has not arrived. Development runs against a simulator that produces
byte-identical protocol traffic, so the dashboard is fully exercisable without
an Arduino.

Switching from simulation to real hardware is a **single configuration
value**. No dashboard, analytics or backend code changes.

Build progress is tracked in [ARCHITECTURE.md §9](docs/ARCHITECTURE.md).

| Step | Module | Status |
| --- | --- | --- |
| 0 | Protocol specification + architecture | ✅ Done |
| 1 | `backend/protocol/` + `TelemetryFrame` | ✅ Done |
| 2 | `config/` + `TelemetrySource` seam | ✅ Done |
| 3 | `simulation/` — physics, scenarios, generator, link faults | ✅ Done |
| 4 | `backend/` — telemetry store, transport metadata, device clock | ✅ Done |
| 5 | `dashboard/` — theme, shell, reactor state banner | Next |
| 6–11 | Gauges, analytics, logging, UART | Planned |

To switch to live hardware, edit one value in
[reactor_dashboard/config/app.toml](reactor_dashboard/config/app.toml):

```toml
[telemetry]
source = "uart"      # was "simulation"
```

---

## Layout

```
reactor_dashboard/
├── backend/          Domain: TelemetryFrame, store, wire protocol
│   └── protocol/     The firmware contract. The ONLY place byte offsets live.
├── transport/        UART link (named for the layer, not for PySerial)
├── simulation/       Reactor simulator emitting real protocol bytes
├── analytics/        Health, stability, trends, derived warnings
├── storage/          CSV and (later) SQLite logging
├── dashboard/        PySide6 widgets. Display only, no business logic.
├── config/           app.toml and its typed loader
├── tests/
└── main.py
```
