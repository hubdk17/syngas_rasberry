# Reactor Telemetry Protocol — v1  ⚠ DEPRECATED

> ## ⚠ THIS DOCUMENT IS DEPRECATED AND PARTLY INCORRECT
>
> **Superseded by [`PROTOCOL_V2.md`](PROTOCOL_V2.md), which is the canonical
> specification and the contract between the two repositories.**
>
> V1 is retained for historical reference only. It is **not maintained and will not be
> repaired**. Conforming V2 implementations reject V1 packets by version, and vice versa.
>
> **Known-false statements in this document.** It was derived on 2026-08-04, before
> firmware findings F-01 and F-02 landed on 2026-08-05:
>
> | Section | Claim | Reality since F-01 |
> |---|---|---|
> | §4, §6.1 | `temperature` / `temperatureRate` are always exactly `0.0` | **NaN**, with `NO_SOURCE` validity |
> | §4 | `gasRatio` is `0.0` when `co2 ≤ 1e-6` | **NaN** |
> | §4 | `anomalyScore` is always in `[0.0, 1.0]` | **NaN** whenever the model is withheld |
> | §6.4 | "a failed sensor forwards its last good value" | **F-01 fixed this.** Failed reads now yield NaN |
> | §6.4 | Pi-side stale-signal detection is required | **Withdrawn.** V2 transmits `SensorStatus` directly |
> | §11 | The Pi reconstructs the five warning bits from mirrored limits | **Withdrawn.** V2 transmits `warningMask` |
>
> The V1 protocol also had two defects of its own: it reported `state = NORMAL` from a
> completely blind reactor with no field able to contradict it, and its `ReactorState`
> numbering was not severity-ordered (`EMERGENCY=2` sorted below `ANOMALY=3`). Both are
> fixed in V2. See `PROTOCOL_V2.md` §12.
>
> Nothing below this banner should be used to write new code.

**Status:** DEPRECATED — superseded by `PROTOCOL_V2.md`.
**Was:** Derived from firmware, not designed here.
**Source of truth at the time:** `D:\Desktop\syngas_arduino`
**Derived on:** 2026-08-04
**Firmware version at derivation:** `0.1.0` (`include/config/firmware_config.h`)

This document is the contract between the Arduino GIGA R1 and the Raspberry Pi
Zero 2 W. It was reverse-engineered from the firmware sources listed under
"Provenance". **The Pi implements this document; it does not negotiate it.**

If the firmware changes, this document is regenerated and
`TELEMETRY_PROTOCOL_VERSION` is expected to be bumped.

---

## 1. Physical Link

| Property | Value | Source |
| --- | --- | --- |
| Arduino port | `Serial3` (`PLC_SERIAL`) | `config/hardware_config.h:29` |
| Baud rate | `115200` (`PLC_BAUD`) | `config/communication_config.h:23` |
| Framing | 8N1 (Arduino `begin(baud)` default) | `UARTTelemetry.cpp:37` |
| Flow control | None | — |
| Direction | Arduino → Pi (Pi → Arduino is stubbed, see §7) | `UARTTelemetry.cpp:234` |

Debug logging is emitted on the **USB CDC `Serial`** port only, never on
`PLC_SERIAL` (`config/debug_config.h:11-13`). The binary stream is therefore
clean — the Pi will not see interleaved ASCII.

**Live frame cadence: 1 Hz** (`TELEMETRY_PERIOD_MS = 1000`,
`config/timing_config.h:63`).

A 56-byte FRAME takes ~4.9 ms on the wire at 115200 8N1, so the link is
~0.5 % utilised at 1 Hz.

---

## 2. Packet Framing

All multi-byte fields are **little endian**. The firmware writes fields byte by
byte rather than memcpy-ing a struct, so the layout does **not** depend on
compiler padding or target alignment (`UARTTelemetry.h:26-28`). Python
`struct` with the `<` prefix maps 1:1 onto the wire.

```
 Offset  Size  Field              Value / Notes
 ------  ----  -----------------  ----------------------------------------
 0       1     SYNC_0             0xAA          (constant)
 1       1     SYNC_1             0x55          (constant)
 2       1     protocolVersion    0x01          (TELEMETRY_PROTOCOL_VERSION)
 3       1     packetType         see §3
 4       2     payloadLength      uint16 LE, byte count of payload
 6       N     payload            packet-type dependent
 6+N     2     crc16              uint16 LE
```

Constants: `config/communication_config.h:54-57`.
Header/CRC sizes: `UARTTelemetry.h:84-91`.

### CRC coverage

The CRC covers offsets **`2` through `6+N-1` inclusive** — i.e. `4 + N` bytes:
version, type, length, and payload. **The sync bytes are excluded**
(`UARTTelemetry.cpp:20`, `164-168`).

> **Why:** a receiver re-synchronising mid-stream can validate a candidate
> packet without having to rescan the sync bytes it just consumed
> (`UARTTelemetry.h:41-42`).

This is the single easiest place to get the Pi decoder wrong. CRC over the
whole packet, or over the payload alone, will both fail against real firmware.

---

## 3. Packet Types

Wire values — **do not renumber** (`UARTTelemetry.h:72-78`).

| Name | Value | Payload | Total size | Meaning |
| --- | --- | --- | --- | --- |
| `FRAME` | `0x01` | 48 B sample | 56 B | One live sample, sent every 1000 ms |
| `HISTORY_BEGIN` | `0x02` | 2 B `uint16` record count | 10 B | Opens a history burst |
| `HISTORY_RECORD` | `0x03` | 48 B sample | 56 B | One archived sample, **oldest first** |
| `HISTORY_END` | `0x04` | none | 8 B | Closes the burst |

`FRAME` and `HISTORY_RECORD` share an **identical** payload layout, so the Pi
needs exactly one payload parser (`UARTTelemetry.h:51-52`,
`UARTTelemetry.cpp:87-89`).

History depth is 60 records at 1 Hz — a rolling 60-second window
(`CircularBuffer.h:69-71`, `config/timing_config.h:65-66`). A full burst is
`10 + 60×56 + 8 = 3378` bytes ≈ 293 ms of wire time.

---

## 4. Sample Payload — 48 bytes

Used by `FRAME` and `HISTORY_RECORD`. Serialisation order is fixed by
`UARTTelemetry::writeSamplePayload()` (`UARTTelemetry.cpp:91-122`).

| Off | Size | Type | Field | Unit | Range / Notes |
| --- | --- | --- | --- | --- | --- |
| 0 | 4 | `uint32` | `timestamp` | ms | `millis()` since boot. **Not wall clock.** Wraps at 2³² ms ≈ 49.7 days |
| 4 | 4 | `float32` | `pressure` | kPa absolute | ~101 kPa at atmosphere (BMP280) |
| 8 | 4 | `float32` | `pressureRate` | kPa/s | Signed. First frame after boot is `0.0` |
| 12 | 4 | `float32` | `temperature` | °C | ⚠ **Always `0.0`** — no sensor. See §6.1 |
| 16 | 4 | `float32` | `temperatureRate` | °C/s | ⚠ **Always `0.0`** — see §6.1 |
| 20 | 4 | `float32` | `co` | ppm (nominal) | ⚠ Raw Modbus counts today. See §6.2 |
| 24 | 4 | `float32` | `co2` | ppm (nominal) | ⚠ Raw Modbus counts today |
| 28 | 4 | `float32` | `ch4` | ppm (nominal) | ⚠ Raw Modbus counts today |
| 32 | 4 | `float32` | `gasRatio` | — | `ch4 / co2`, or `0.0` when `co2 ≤ 1e-6` |
| 36 | 4 | `float32` | `gasConcentration` | ppm (nominal) | `co + co2 + ch4` |
| 40 | 4 | `float32` | `anomalyScore` | probability | `[0.0, 1.0]` |
| 44 | 1 | `uint8` | `warning` | bool | `0` or `1` |
| 45 | 1 | `uint8` | `emergency` | bool | `0` or `1` |
| 46 | 1 | `uint8` | `anomaly` | bool | ⚠ **Always `0`** today. See §6.3 |
| 47 | 1 | `uint8` | `state` | enum | `ReactorState`, see §5 |

Floats are IEEE-754 single precision, little endian
(`UARTTelemetry.cpp:73-82`).

**Python format string:** `"<I10f4B"` → `struct.calcsize` = 48. ✔

Field origin: offsets 0–39 come from `ReactorFeatures`
(`PhysicsEngine.h:37-53`); offsets 40–47 from `DecisionResult`
(`DecisionEngine.h:50-63`).

---

## 5. Enums

### `ReactorState` (`DecisionEngine.h:42-48`)

| Value | Name | Meaning |
| --- | --- | --- |
| `0` | `NORMAL` | No limit breached, no anomaly |
| `1` | `WARNING` | At least one warning threshold breached |
| `2` | `EMERGENCY` | Pressure or temperature past its emergency limit |
| `3` | `ANOMALY` | Model flagged an anomaly, no fixed limit breached. **Unreachable today** (§6.3) |

### Fusion policy (`DecisionEngine.cpp:60-87`)

Deterministic limits **outrank** the model — a hard limit breach is auditable
and always actionable, whereas the model is probabilistic.

```
if emergency -> EMERGENCY
elif warning -> WARNING
elif anomaly -> ANOMALY
else         -> NORMAL
```

`state` is therefore fully derivable from the three flags. The Pi treats
`state` as authoritative and uses the derivation as a **decoder self-check**:
a mismatch means a corrupt-but-CRC-valid frame or a firmware/protocol skew,
and is logged as a protocol fault.

---

## 6. Known Firmware Limitations (as of v0.1.0)

These are properties of the current firmware, not protocol defects. The Pi
must render them honestly rather than presenting placeholder data as
measurement.

### 6.1 Temperature is hard-zeroed

`PhysicsEngine::update()` assigns `features_.temperature = 0.0f`
unconditionally (`PhysicsEngine.cpp:56`) — no temperature sensor exists in
`SensorManager` yet (`PhysicsEngine.h:129-133`).

**Consequences:**
- `temperature` and `temperatureRate` are always exactly `0.0`.
- `ThresholdInference` can never raise a temperature warning or a temperature
  emergency.
- `ReactorState::EMERGENCY` can currently only be triggered by pressure.

**HMI rule:** the temperature gauge must render as **`NO SENSOR`**, not as a
comfortable green `0 °C`. A green temperature tile on a reactor with no
temperature sensor is exactly the kind of false reassurance an industrial HMI
exists to prevent.

### 6.2 Gas values are unscaled

Gas readings are still raw Modbus register counts; the datasheet scaling
factor is not yet applied in `RS485GasSensor::requestMeasurement()`
(`PhysicsEngine.h:110-113`). The ppm thresholds in `safety_config.h` are
therefore being compared against unscaled counts.

**HMI rule:** gas units are labelled `raw` (or `ppm*`) with a provisional
marker until firmware applies scaling. `gasRatio` is dimensionless and
unaffected by a common scale factor, so it is the more trustworthy indicator
today.

### 6.3 The anomaly verdict is suppressed

`ENABLE_XGBOOST_INFERENCE` is commented out (`config/build_config.h:41`)
because `XGBoostModel` still carries a hand-written placeholder forest.
`DecisionEngine` therefore forces `result_.anomaly = false`
(`DecisionEngine.cpp:50-56`).

**`anomalyScore` is still computed and still transmitted on every frame**, by
design, so the Pi can observe model behaviour while the verdict is not acted
on.

**HMI rule:** display `anomalyScore` as a live trend, clearly marked
**`MODEL: OBSERVE-ONLY`**. Do not raise alarms from it. The Pi must still
handle `anomaly = 1` and `state = ANOMALY` correctly, because the flag flips
the day the toggle is enabled — with no protocol change.

### 6.4 Stale sensor values are forwarded silently

`PressureSensor::update()` marks a failed read `valid = false` but leaves
`reading_.value` at its last good figure (`PressureSensor.cpp:91-100`), and
`PhysicsEngine` copies `.value` without consulting `.valid`
(`PhysicsEngine.cpp:52`). Sensor health is **not** in the packet.

**Consequence:** a dead pressure sensor presents as a *frozen* pressure with
`pressureRate == 0.0`, not as an obvious fault.

**Pi mitigation:** stale-signal detection in the analytics layer — flag a
signal whose value is bit-identical across N consecutive frames. This is a Pi
responsibility precisely because the firmware cannot express it in v1.

### 6.5 `timestamp` is uptime, not time of day

`millis()` since boot (`PhysicsEngine.cpp:109`). The Pi must:
- keep it as the authoritative **device** clock (frame ordering, rate maths),
- pair every frame with a host wall-clock arrival time for CSV/SQLite,
- detect the 49.7-day wrap (`t_new < t_prev` by a large margin) and treat it
  as a rollover, not as reordering,
- treat a *small* backward step as a device reboot instead.

### 6.6 Pipeline / telemetry cadence skew

The pipeline runs at 500 ms (`SENSOR_POLL_PERIOD_MS`) while telemetry runs at
1000 ms (`main.cpp:180-210`). Consecutive frames may therefore carry a
`timestamp` up to ~500 ms older than their transmission instant. Rate maths on
the Pi must use the packet `timestamp` delta, never the arrival-time delta.

---

## 7. Inbound Commands (Pi → Arduino) — NOT IMPLEMENTED

`UARTTelemetry::pollCommands()` is a stub (`UARTTelemetry.cpp:234-247`). The
planned `REQUEST_HISTORY` command does not exist, so **history bursts are
never emitted in practice today**.

The Pi decoder still implements `HISTORY_BEGIN` / `HISTORY_RECORD` /
`HISTORY_END` because the firmware can already *send* them
(`sendHistory()` is complete) and only the trigger is missing. But **no Pi
feature may depend on history replay.** The Pi builds its own history from the
live 1 Hz stream, which it must do regardless — the Arduino only retains 60 s.

---

## 8. CRC-16 Reference

Modbus CRC-16, reused from the RS485 sensor bus rather than carrying a second
implementation (`ModbusCRC.cpp:12-35`, `UARTTelemetry.cpp:160-161`).

| Parameter | Value |
| --- | --- |
| Width | 16 bits |
| Polynomial | `0xA001` (reflected form of `0x8005`) |
| Initial value | `0xFFFF` |
| Reflect in / out | Yes (inherent to the reflected algorithm) |
| Final XOR | `0x0000` |
| Wire byte order | **Little endian** (low byte first) |

```python
def modbus_crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 0x0001 else crc >> 1
    return crc
```

Known-answer check: `modbus_crc16(b"123456789") == 0x4B37`.

---

## 9. Receiver Requirements

A conforming Pi decoder must:

1. **Byte-stream, not message-oriented.** UART delivers arbitrary fragments; a
   packet may arrive split across many reads or several packets in one read.
2. **Hunt for `AA 55`**, then read the 4-byte remainder of the header.
3. **Bounds-check `payloadLength` before allocating or waiting.** A corrupt
   length field is the classic denial-of-progress bug. Reject anything above
   `MAX_PACKET_LENGTH - HEADER - CRC` (48) and resynchronise.
4. **Validate `protocolVersion == 1`.** An unknown version is counted and
   dropped, never parsed.
5. **Verify CRC over `[2, 6+N)`**, low byte first.
6. **On any failure, resynchronise from the byte after the failed `SYNC_0`** —
   not from the end of the discarded packet. A false `AA 55` inside payload
   data must not consume a real packet that follows it.
7. **Count everything**: bytes, packets, CRC errors, version rejects, framing
   resyncs. These feed the HMI's diagnostics strip.
8. Never block the UI thread.

---

## 10. Mirrored Firmware Constants

These are **not** transmitted. They are compile-time constants in the firmware
that the Pi must mirror in order to reproduce the reactor's own reasoning
(see §11 and `ARCHITECTURE.md` §4).

Mirrored constants are a versioning hazard: if firmware changes a limit and the
Pi does not, the Pi's derived signals silently disagree with the reactor. They
are therefore confined to **one** Python module (`backend/protocol/limits.py`),
each annotated with its firmware source line, and covered by a test that fails
loudly if the two drift.

### Safety limits (`include/config/safety_config.h`)

| Constant | Value | Unit |
| --- | --- | --- |
| `MAX_PRESSURE_KPA` | `150.0` | kPa |
| `EMERGENCY_PRESSURE_KPA` | `180.0` | kPa |
| `MAX_TEMPERATURE_C` | `550.0` | °C |
| `EMERGENCY_TEMPERATURE_C` | `650.0` | °C |
| `MAX_CO_PPM` | `1000.0` | ppm |
| `MAX_CO2_PPM` | `8000.0` | ppm |
| `MAX_CH4_PPM` | `5000.0` | ppm |
| `MAX_TOTAL_GAS_PPM` | `12000.0` | ppm |
| `MAX_PRESSURE_RATE_KPA_PER_SEC` | `5.0` | kPa/s |
| `MAX_TEMPERATURE_RATE_C_PER_SEC` | `10.0` | °C/s |
| `ANOMALY_THRESHOLD` | `0.80` | probability |

---

## 11. Derived Warning Detail (Pi-side reconstruction)

The firmware computes **five granular warning bits**
(`ThresholdInference.h:32-43`) but transmits only the rolled-up `warning`
boolean. The wire format cannot tell the operator *which* limit tripped.

Every one of the five checks depends solely on fields that **are** in the
packet, so the Pi can reconstruct all five exactly
(`ThresholdInference.cpp:21-69`):

| Derived flag | Reconstruction |
| --- | --- |
| `pressureWarning` | `pressure > MAX_PRESSURE_KPA` |
| `temperatureWarning` | `temperature > MAX_TEMPERATURE_C` (always false, §6.1) |
| `gasWarning` | `gasConcentration > MAX_TOTAL_GAS_PPM or co > MAX_CO_PPM or co2 > MAX_CO2_PPM or ch4 > MAX_CH4_PPM` |
| `pressureRateWarning` | `abs(pressureRate) > MAX_PRESSURE_RATE_KPA_PER_SEC` |
| `temperatureRateWarning` | `abs(temperatureRate) > MAX_TEMPERATURE_RATE_C_PER_SEC` (always false, §6.1) |

Note the firmware uses **magnitude** for rate checks — a rapid pressure *drop*
is as significant as a rapid rise, since both indicate loss of control
(`ThresholdInference.cpp:44-47`).

**Self-check:** the OR of the five reconstructed flags must equal the
transmitted `warning` bit. A mismatch means the Pi's mirrored limits have
drifted from firmware, and is surfaced as a diagnostic — not hidden.

> **Recommended firmware change (v2):** replace the three status booleans at
> offsets 44–46 with a single `uint16` warning bitmask plus a `uint8` state,
> and bump `TELEMETRY_PROTOCOL_VERSION` to `2`. That removes the mirrored
> constants entirely and makes the reactor's reasoning self-describing. This is
> a firmware decision and is **not** made unilaterally by the Pi.

---

## 12. Provenance

| Fact | File |
| --- | --- |
| Frame layout, packet types, sizes | `lib/Communication/UARTTelemetry/UARTTelemetry.h` |
| Serialisation order, CRC span, endianness | `lib/Communication/UARTTelemetry/UARTTelemetry.cpp` |
| Sync bytes, version, baud | `include/config/communication_config.h` |
| `ReactorFeatures`, derivations, TODOs | `lib/Physics/PhysicsEngine/PhysicsEngine.{h,cpp}` |
| `ReactorState`, `DecisionResult`, fusion | `lib/AI/DecisionEngine/DecisionEngine.{h,cpp}` |
| Threshold logic and sub-flags | `lib/AI/ThresholdInference/ThresholdInference.{h,cpp}` |
| Anomaly score and threshold | `lib/AI/XGBoost/XGBoostInference.cpp` |
| Safety limits | `include/config/safety_config.h` |
| Cadences, history depth | `include/config/timing_config.h` |
| Feature toggle state | `include/config/build_config.h` |
| UART assignment, debug isolation | `include/config/hardware_config.h`, `config/debug_config.h` |
| Loop schedule | `src/main.cpp` |
