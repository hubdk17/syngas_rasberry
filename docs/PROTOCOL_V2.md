# Reactor Telemetry Protocol — Version 2

**Status:** CANONICAL SPECIFICATION. This document is the contract.
**Supersedes:** Protocol V1, which is **DEPRECATED** (§12).
**Governs:** `syngas_arduino` (encoder) ↔ `syngas_rasberry` (decoder).

---

## 0. Standing of this document

Neither repository owns the protocol. **This document owns it.**

| Party | Owns | Does not own |
|---|---|---|
| `syngas_arduino` | The **encoder**. All reactor knowledge and every decision. | The wire format |
| `syngas_rasberry` | The **decoder**. All presentation. | The wire format, any reactor decision |
| This document | The **wire format** and every field's meaning | Neither implementation |

**Both implementations conform to this document. Neither negotiates it.**

An identical copy lives at `docs/PROTOCOL_V2.md` in both repositories. The two copies
must be **byte-identical**; verify with `sha256sum` before any protocol change is
considered complete. The operative conformance check, however, is §11: both
implementations must reproduce the golden vectors published here, which is a stronger
guarantee than file equality because it tests behaviour rather than text.

### 0.1 Single source of truth

> Reactor decisions originate **only** on the Arduino.
> Visualization originates **only** on the Raspberry Pi.
> No business logic may exist in both repositories.

Consequences that are binding on the decoder:

- The Pi **must not** recompute thresholds, fuse states, run inference or re-derive
  `reactorState`. Every such value arrives on the wire and is authoritative.
- The Pi **must not** infer firmware state that this protocol transmits — staleness,
  sensor health, reboots or packet loss are all carried explicitly.
- The Pi **may** perform sanity checks and cross-checks, and **must** report a
  disagreement rather than silently correcting it. A cross-check is a diagnostic, never
  a substitute value.

---

## 1. Frozen invariants

These hold for **every** protocol version, past and future. Nothing may change them.

| Offset | Size | Field | Rule |
|---|---|---|---|
| 0 | 1 | `SYNC_0` | Always `0xAA` |
| 1 | 1 | `SYNC_1` | Always `0x55` |
| 2 | 1 | `protocolVersion` | Always at this offset, always one byte |

**Why this matters more than anything else in the document.** A receiver must be able to
identify the version of a packet it does not understand, using only knowledge frozen
across all versions. Because `protocolVersion` sits at offset 2 in both V1 and V2, a V1
decoder meets a V2 packet, reads `2`, and rejects it **loudly and specifically**. It never
misparses. Move this field and that guarantee is gone permanently.

Two further invariants:

- **All multi-byte fields are little endian.** No exceptions.
- **The encoder writes byte by byte.** Layout never depends on compiler padding or target
  alignment, so a Python `struct` with the `<` prefix maps 1:1 onto the wire.

---

## 2. Physical link

| Property | Value | Note |
|---|---|---|
| Arduino port | `Serial3` (`PLC_SERIAL`) | |
| Baud / framing | 115200 8N1 | Fixed by the protocol, not a deployment setting |
| Flow control | None | |
| Direction | Arduino → Pi | Pi → Arduino remains unimplemented (§10) |
| Live cadence | 1 Hz (`TELEMETRY_PERIOD_MS = 1000`) | |

Debug output goes to USB CDC `Serial` only, never to `PLC_SERIAL`. The binary stream is
clean; the Pi will not see interleaved ASCII.

A 95-byte V2 `FRAME` occupies **8.25 ms** at 115200 8N1 — link utilisation ~0.83 % at 1 Hz.

---

## 3. Packet structure

```
 Offset  Size  Field             Notes
 ------  ----  ----------------  --------------------------------------------
 0       1     SYNC_0            0xAA                          [frozen]
 1       1     SYNC_1            0x55                          [frozen]
 2       1     protocolVersion   0x02                          [frozen offset]
 3       1     packetType        §4
 4       2     payloadLength     uint16, byte count of payload
 6       2     bootCount         uint16, §5.1
 8       4     sequence          uint32, §5.2
 12      N     payload           packet-type dependent
 12+N    2     crc16             uint16, §9
```

**Header length: 12 bytes** (V1: 6). `bootCount` and `sequence` are header fields, not
payload fields, because they describe **the packet**, not the sample it carries. A
`HISTORY_RECORD` replayed minutes after the fact must not claim the sequence number of
the original live frame.

### CRC coverage

The CRC covers offsets **2 through 12+N−1 inclusive** — version, type, length,
bootCount, sequence and payload. **The sync bytes are excluded.**

> **Why:** a receiver resynchronising mid-stream can validate a candidate packet without
> rescanning the sync bytes it has already consumed.

This is the single easiest thing to get wrong. A CRC over the whole packet, or over the
payload alone, will both fail against a conforming encoder.

---

## 4. Packet types

Wire values are unchanged from V1. **Do not renumber.**

| Name | Value | Payload | Total | Meaning |
|---|---|---|---|---|
| `FRAME` | `0x01` | 81 B sample | 95 B | One live sample, every 1000 ms |
| `HISTORY_BEGIN` | `0x02` | 2 B `uint16` count | 16 B | Opens a history burst |
| `HISTORY_RECORD` | `0x03` | 81 B sample | 95 B | One archived sample, oldest first |
| `HISTORY_END` | `0x04` | none | 14 B | Closes the burst |

`FRAME` and `HISTORY_RECORD` carry an **identical** payload layout, so the decoder needs
exactly one payload parser. This is a deliberate constraint on future versions.

---

## 5. Header fields

### 5.1 `bootCount`

| | |
|---|---|
| **Type** | `uint16`, little endian |
| **Units** | boots |
| **Range** | `0` … `65535`, wrapping |
| **Meaning** | Which continuous run of the device this packet belongs to |

Increments once per firmware start. A change in `bootCount` between consecutive packets
means the device restarted — **the Pi must not infer this from the device clock**.

`0` is reserved: it means *the firmware could not persist a boot counter*. When the
firmware emits `bootCount = 0` it **must** also set `SYS_BOOT_COUNT_UNRELIABLE`
(§8.2 bit 9). A receiver seeing `0` must treat every packet as belonging to an unknown
run and fall back to clock-based reboot detection, reporting reduced confidence.

> Persistence mechanism is a firmware implementation detail and is deliberately not
> specified here. What is specified is the **failure disclosure**: unreliable is stated,
> never guessed at.

### 5.2 `sequence`

| | |
|---|---|
| **Type** | `uint32`, little endian |
| **Units** | packets |
| **Range** | `0` … `4294967295`, wrapping |
| **Meaning** | Count of packets transmitted since this boot, starting at `0` |

Increments for **every** transmitted packet of every type, including history packets.
Resets to `0` on boot.

**This is how packet loss becomes observable.** A gap between consecutive received
`sequence` values is exactly the number of packets that did not arrive. The receiver
must not infer loss from timing when `sequence` is available.

Wrap occurs after ~136 years at 1 Hz and is specified only for completeness. A backward
step in `sequence` **without** a change in `bootCount` is a protocol fault, not a wrap.

---

## 6. Sample payload — 81 bytes

Carried by `FRAME` and `HISTORY_RECORD`.

**Python format string:** `"<I9fH9B12BBBHHfBIBH"` → `struct.calcsize` = **81** ✔

| Off | Size | Type | Field | Units | Range / Meaning |
|---|---|---|---|---|---|
| 0 | 4 | `uint32` | `timestamp` | ms | `millis()` at feature computation. Uptime, **not** wall clock. Wraps at 2³² ms ≈ 49.7 d |
| 4 | 4 | `float32` | `pressure` | kPa abs | ~101 at atmosphere. **May be NaN** (§7) |
| 8 | 4 | `float32` | `pressureRate` | kPa/s | Signed. **May be NaN** |
| 12 | 4 | `float32` | `temperature` | °C | **Always NaN today** — no sensor fitted (§6.5) |
| 16 | 4 | `float32` | `temperatureRate` | °C/s | **Always NaN today** |
| 20 | 4 | `float32` | `co` | raw counts | Scaling not yet applied (§6.6). **May be NaN** |
| 24 | 4 | `float32` | `co2` | raw counts | **May be NaN** |
| 28 | 4 | `float32` | `ch4` | raw counts | **May be NaN** |
| 32 | 4 | `float32` | `gasRatio` | dimensionless | `ch4 / co2`. **May be NaN** |
| 36 | 4 | `float32` | `gasConcentration` | raw counts | `co + co2 + ch4`, all-or-nothing. **May be NaN** |
| 40 | 2 | `uint16` | `validityMask` | bitmask | Bit *n* = feature *n* is `VALID`. §6.1 |
| 42 | 9 | `uint8[9]` | `featureValidity` | enum | One reason code per feature. §6.2 |
| 51 | 12 | `uint8[12]` | `sensors` | struct | 4 sensors × (status, fault, consecutiveFailures). §6.3 |
| 63 | 1 | `uint8` | `reactorState` | enum | §6.4 |
| 64 | 1 | `uint8` | `decisionFlags` | bitmask | §6.7 |
| 65 | 2 | `uint16` | `warningMask` | bitmask | Which warning limits tripped. §6.8 |
| 67 | 2 | `uint16` | `emergencyMask` | bitmask | Which emergency limits tripped. §6.8 |
| 69 | 4 | `float32` | `anomalyScore` | probability | `[0.0, 1.0]`, or **NaN when the model was withheld** |
| 73 | 1 | `uint8` | `systemHealth` | enum | §8.1 |
| 74 | 4 | `uint32` | `systemFaults` | bitmask | Latched firmware faults. §8.2 |
| 78 | 1 | `uint8` | `resetCause` | enum | §8.3 |
| 79 | 2 | `uint16` | `watchdogTimeoutMs` | ms | IWDG timeout **read back from hardware**. `0` = not running |

Floats are IEEE-754 single precision, little endian.

### 6.1 Feature identity — frozen ordering

Feature order is frozen across the float array, `validityMask` bit positions and the
`featureValidity` array. All three use the same index.

| Index | Feature |
|---|---|
| 0 | `pressure` |
| 1 | `pressureRate` |
| 2 | `temperature` |
| 3 | `temperatureRate` |
| 4 | `co` |
| 5 | `co2` |
| 6 | `ch4` |
| 7 | `gasRatio` |
| 8 | `gasConcentration` |

`validityMask` bits 9–15 are reserved and **must be 0**.

`validityMask` is redundant with `featureValidity` and is transmitted anyway: it gives
the decoder a single-comparison "is everything valid" test and a compact form for
storage. **If the two ever disagree, `featureValidity` wins** and the receiver must
report a protocol fault.

### 6.2 `FeatureValidity` — reason codes

| Value | Name | Meaning | Operator reading |
|---|---|---|---|
| 0 | `VALID` | Computed from sufficient, current, in-range inputs | Usable |
| 1 | `INCOMPLETE` | One or more source inputs unusable | **Go fix a sensor** |
| 2 | `NOT_COMPUTABLE` | Inputs present, result mathematically undefined | **Nothing is broken** — e.g. `gasRatio` with `co2 ≈ 0` |
| 3 | `STALE` | Inputs valid but older than the freshness bound | Currently unreachable (§6.9) |
| 4 | `NO_SOURCE` | No instrument fitted for this channel | **Absent by design**, not a fault |
| 5–255 | — | Reserved | Receiver must treat as not-valid and report unknown code |

`INCOMPLETE` versus `NOT_COMPUTABLE` is the distinction that justifies the reason code:
the first sends a technician to the plant, the second does not.

`NO_SOURCE` must never render like a fault. A channel absent by design and a channel
broken today are different facts.

### 6.3 Sensor block — 12 bytes

Four sensors, three bytes each, in frozen index order:

| Index | Sensor | Bus |
|---|---|---|
| 0 | CO | RS485 / Modbus RTU |
| 1 | CO₂ | RS485 / Modbus RTU |
| 2 | CH₄ | RS485 / Modbus RTU |
| 3 | Pressure (BMP280) | I²C |

Per sensor, at offset `51 + 3n`:

| Rel | Size | Field | Meaning |
|---|---|---|---|
| +0 | 1 | `status` | `SensorStatus`, §6.3.1 |
| +1 | 1 | `fault` | `SensorFault`, §6.3.2 |
| +2 | 1 | `consecutiveFailures` | Consecutive failed acquisitions, **saturating at 255** |

#### 6.3.1 `SensorStatus`

Small, bus-agnostic, and the **only** sensor field any logic may branch on.

| Value | Name | Meaning | First action |
|---|---|---|---|
| 0 | `INITIALIZING` | Not yet attempted. Default state. | Wait; escalate if persistent |
| 1 | `OK` | Plausible measurement this cycle. **The only usable status.** | — |
| 2 | `TIMEOUT` | Was reachable; not answering now | Wiring, power, addressing |
| 3 | `INVALID` | Answered; answer cannot be trusted | Signal integrity |
| 4 | `OFFLINE` | Attempted and failed; never reached | Device absent |
| 5–255 | — | Reserved | Treat as not-`OK` |

#### 6.3.2 `SensorFault`

Diagnostic detail for the operator. **No logic may branch on this field.** Values are
range-partitioned by bus so a decoder can label them without knowing the sensor.

| Value | Name | Applies to |
|---|---|---|
| `0x00` | `NONE` | all |
| `0x10` | `MODBUS_TIMEOUT` | RS485 |
| `0x11` | `MODBUS_CRC_ERROR` | RS485 |
| `0x12` | `MODBUS_SLAVE_EXCEPTION` | RS485 |
| `0x13` | `MODBUS_WRONG_SLAVE` | RS485 |
| `0x14` | `MODBUS_BAD_FUNCTION` | RS485 |
| `0x15` | `MODBUS_BAD_BYTE_COUNT` | RS485 |
| `0x16` | `MODBUS_SHORT_FRAME` | RS485 |
| `0x17` | `MODBUS_TRANSMIT_FAILED` | RS485 |
| `0x18` | `MODBUS_INVALID_ARGUMENT` | RS485 |
| `0x20` | `I2C_INIT_FAILED` | I²C |
| `0x21` | `I2C_CONVERSION_NAN` | I²C |
| `0x22` | `I2C_CONVERSION_ZERO` | I²C |
| `0x23` | `I2C_OUT_OF_RANGE` | I²C |

`0x01`–`0x0F`, `0x19`–`0x1F`, `0x24`–`0xFF` reserved. `0x10`–`0x1F` is the Modbus range;
`0x20`–`0x2F` the I²C range.

### 6.4 `ReactorState`

**Renumbered from V1.** Describes the physical process and nothing else.

| Value | Name | Severity | Meaning |
|---|---|---|---|
| 0 | `UNKNOWN` | 2 | **The process is not observable.** Not a fault claim |
| 1 | `NORMAL` | 0 | Within all limits, on the evidence available |
| 2 | `WARNING` | 2 | A warning limit breached |
| 3 | `ANOMALY` | 3 | Model flagged a pattern; no fixed limit breached |
| 4 | `EMERGENCY` | 4 | An emergency limit breached |
| 5–255 | — | — | Reserved; receiver rejects the packet |

> **Wire values are identifiers, not severities.** Use the severity column; never compare
> the enum numerically. V1 got this wrong — `EMERGENCY=2` sorted below `ANOMALY=3` — and
> a numeric comparison silently downgraded emergencies.

`UNKNOWN` is emitted when no safety-relevant feature is valid. It is **epistemic**: the
process may be perfectly healthy, and the firmware is declining to guess. It is *not*
`FAULT`; firmware faults live in `systemHealth` (§8), which is an independent axis.

`UNKNOWN` carries `WARNING`-level severity because being blind demands attention without
itself being evidence of an excursion. Escalating on **duration** is a Pi presentation
policy and is deliberately not encoded here.

### 6.5 Temperature is unmeasured

No temperature sensor is fitted. `temperature` and `temperatureRate` are **always NaN**
with `featureValidity = NO_SOURCE`.

Consequences: the temperature warning and emergency limits are permanently unreachable,
and `EMERGENCY` can currently be raised by pressure only.

**Display rule:** render as **`NO SENSOR`**. Never as `0 °C`, never as a fault, never
green.

### 6.6 Gas values are unscaled

Gas readings are raw Modbus register counts; the datasheet scaling factor is not yet
applied in firmware. The ppm limits are therefore compared against unscaled counts.

**Display rule:** label the unit `raw`, provisionally. `gasRatio` is dimensionless and
unaffected by a common scale factor, so it is the more trustworthy gas indicator today.
When firmware applies scaling, these fields become ppm — a **semantic** change with no
layout change, and it requires a version bump regardless (§12.1).

### 6.7 `decisionFlags`

| Bit | Name | Meaning |
|---|---|---|
| 0 | `WARNING` | A warning limit is breached |
| 1 | `EMERGENCY` | An emergency limit is breached |
| 2 | `ANOMALY` | Model verdict asserted |
| 3 | `DATA_COMPLETE` | Every feature backing this decision was valid |
| 4 | `DATA_UNAVAILABLE` | **Nothing** was observable |
| 5 | `MODEL_RAN` | `anomalyScore` is a real number, not NaN |
| 6–7 | — | Reserved, must be 0 |

`MODEL_RAN = 0` and `anomalyScore = NaN` always occur together. The flag exists so a
receiver never has to test a float for NaN to learn a control fact.

### 6.8 `warningMask` and `emergencyMask`

These replace the Pi's mirrored threshold logic. **The firmware states which limit
tripped; the Pi never recomputes it.**

`warningMask`:

| Bit | Limit |
|---|---|
| 0 | Pressure |
| 1 | Temperature |
| 2 | Gas |
| 3 | Pressure rate |
| 4 | Temperature rate |
| 5–15 | Reserved, must be 0 |

`emergencyMask`:

| Bit | Limit |
|---|---|
| 0 | Pressure |
| 1 | Temperature |
| 2–15 | Reserved, must be 0 |

A bit is set **only** when the check actually ran against a valid input. An invalid input
never raises a warning — a limit cannot be judged breached on a measurement that does not
exist. This is why `DATA_COMPLETE` must be read alongside: all-zero masks with
`DATA_COMPLETE = 0` means *"these results do not cover the whole reactor"*, never
*"the reactor is fine"*.

Rate checks compare **magnitude**: a rapid pressure drop is as significant as a rise.

### 6.9 `STALE` is currently unreachable

Every feature is recomputed each cycle and set NaN on failure, so nothing can go stale.
`STALE` becomes reachable when per-sensor scheduling lands and features may combine
inputs of differing ages. It is specified now so that change needs no new version.

---

## 7. NaN — the absent-measurement marker

**NaN is a permanent part of this protocol.** It is not a defect and not a placeholder.

| | |
|---|---|
| **Any `float32` field may be NaN.** | It means *no measurement exists*. |
| `featureValidity` explains **why**. | NaN prevents accidental numerical use. |

The two are complementary, and both are transmitted deliberately:

- Remove NaN and an invalid feature carries a plausible number that a careless
  comparison will consume.
- Remove `featureValidity` and the receiver knows a value is missing but not whether a
  technician should be dispatched.

### 7.1 Encoder rule — strict

The encoder emits the canonical quiet NaN **`0x7FC00000`** (positive sign, no payload)
for every absent value. It emits no other NaN pattern and no infinity.

### 7.2 Decoder rules — liberal on receipt, strict thereafter

1. **Accept any NaN bit pattern.** Signalling NaNs and NaNs with payloads are valid
   input; normalise on receipt.
2. **Detect NaN explicitly.** Never rely on a comparison to filter it: `nan > limit` is
   `False`, which reads as *"within limits"* — the precise fail-silent behaviour this
   protocol exists to prevent.
3. **NaN must never silently pass into** charts, statistics, aggregates, CSV columns or
   equality comparisons. Every one of those needs a defined NaN behaviour chosen on
   purpose.
4. **Equality:** `NaN != NaN`. Any frame-equality or round-trip comparison must compare
   NaN fields by bit pattern or by an explicit both-are-NaN test. A naive `==` makes
   round-trip tests silently unable to fail.
5. **Rendering:** NaN renders as a stated absence — `NO DATA`, `NO SENSOR`, a gap in a
   trend — never as `0`, never as the last known value, never as a blank that reads as
   nominal.

---

## 8. Firmware health

An axis **independent** of the reactor process. A firmware fault is not a process alarm,
and a process excursion is not a firmware fault.

### 8.1 `systemHealth`

| Value | Name | Meaning | Consequence for the operator |
|---|---|---|---|
| 0 | `HEALTHY` | No faults recorded this power cycle | — |
| 1 | `DEGRADED` | Functioning; a capability impaired or a fault recorded | Data still trustworthy; investigate |
| 2 | `FAILED` | Firmware integrity cannot be assured | **The reactor verdict must not be trusted** |
| 3–255 | — | Reserved | Treat as `FAILED` |

### 8.2 `systemFaults` — latched bitmask

Each bit **latches on first occurrence** and clears only on acknowledgement or power
cycle. `systemHealth` is the maximum severity currently latched.

| Bit | Name | Class | Severity |
|---|---|---|---|
| 0 | `SYS_WATCHDOG_RESET` | Firmware | `FAILED` |
| 1 | `SYS_WATCHDOG_NOT_RUNNING` | Firmware | `FAILED` |
| 2 | `SYS_WATCHDOG_TIMEOUT_MISMATCH` | Firmware | `DEGRADED` |
| 3 | `SYS_CONFIG_INVALID` | Config | `FAILED` |
| 4 | `SYS_RS485_BABBLE` | Communication | `DEGRADED` |
| 5 | `SYS_RS485_BUS_DEAD` | Communication | `DEGRADED` |
| 6 | `SYS_TELEMETRY_TX_FAILED` | Communication | `DEGRADED` |
| 7 | `SYS_SENSOR_RACK_INCOMPLETE` | Hardware | `DEGRADED` |
| 8 | `SYS_CYCLE_OVERRUN` | Resource | `DEGRADED` |
| 9 | `SYS_BOOT_COUNT_UNRELIABLE` | Firmware | `DEGRADED` |
| 10–31 | — | Reserved, must be 0 | |

`SYS_WATCHDOG_RESET` is a fact about the **previous** boot. No instantaneous process
state can express it, and it is the single most important thing the firmware can report.

**Sensor failure versus infrastructure failure** is distinguished by cardinality: one
Modbus sensor failing sets `SYS_SENSOR_RACK_INCOMPLETE`; **all** of them failing
simultaneously additionally sets `SYS_RS485_BUS_DEAD`. Three coincident device failures
and one dead bus are different faults with different first actions.

### 8.3 `resetCause`

| Value | Name |
|---|---|
| 0 | `UNKNOWN` |
| 1 | `POWER_ON` |
| 2 | `PIN_RESET` |
| 3 | `SOFTWARE` |
| 4 | `WATCHDOG` |
| 5 | `BROWN_OUT` |
| 6 | `LOCKUP` |
| 7–255 | Reserved |

### 8.4 `watchdogTimeoutMs`

The IWDG timeout **read back from the peripheral**, not the value requested. `0` means
the watchdog is not running — which, with `SYS_WATCHDOG_NOT_RUNNING`, means the firmware
is executing unsupervised.

Expected value is 4000 ms, derived from measured worst-case execution time. A different
non-zero value means the hardware rounded the request, and the firmware sets
`SYS_WATCHDOG_TIMEOUT_MISMATCH`.

---

## 9. CRC-16

Modbus CRC-16, shared with the RS485 sensor bus rather than carrying a second
implementation.

| Parameter | Value |
|---|---|
| Width | 16 bits |
| Polynomial | `0xA001` (reflected `0x8005`) |
| Initial value | `0xFFFF` |
| Reflect in/out | Yes (inherent to the reflected form) |
| Final XOR | `0x0000` |
| Wire byte order | **Little endian**, low byte first |

```python
def modbus_crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 0x0001 else crc >> 1
    return crc
```

**Known answer:** `modbus_crc16(b"123456789") == 0x4B37`. Both implementations must
assert this.

---

## 10. Receiver requirements

A conforming decoder must:

1. Treat the input as a **byte stream**, not messages. A packet may arrive across many
   reads, or several packets in one read.
2. Hunt for `AA 55`, then read the remaining 10 header bytes.
3. **Reject `protocolVersion != 2`** — counted and dropped, never parsed. This is the
   mechanism that makes a version skew loud instead of silent.
4. **Bounds-check `payloadLength` before waiting.** A corrupt length field is the classic
   denial-of-progress bug. Reject anything above the largest defined payload (81) and
   resynchronise.
5. Verify the CRC over `[2, 12+N)`, low byte first.
6. **On any failure, discard exactly one byte** and hunt again — never skip a claimed
   length. A false `AA 55` inside float payload data must not consume the real packet
   behind it.
7. Validate every enum against this document; an out-of-range value is a protocol fault,
   not a value to pass upward.
8. Count everything: bytes, packets, CRC errors, version rejects, length errors, type
   errors, enum errors, resyncs, discarded bytes, sequence gaps.
9. Never block the UI thread.

### 10.1 Inbound commands — not implemented

Pi → Arduino remains unimplemented; `pollCommands()` is a stub, so **history bursts are
never emitted in practice**. The decoder implements the history packet types because the
firmware can already send them and only the trigger is missing, but **no Pi feature may
depend on history replay**. The Pi builds its own window from the live 1 Hz stream.

---

## 11. Conformance — golden vectors

**These bytes are the contract.** Both implementations must reproduce them exactly:
the Arduino encoder must emit them given the stated inputs; the Pi decoder must parse
them to the stated values and re-encode them byte-identically.

Common to all three: `SYNC=AA 55`, `version=02`, `type=01` (`FRAME`), `payloadLength=0x0051` (81).

### GV-1 — nominal frame

`bootCount=7`, `sequence=1234`, `timestamp=3600000`, pressure `105.25` kPa,
pressureRate `0.5`, temperature/temperatureRate NaN (`NO_SOURCE`), co `420.0`,
co2 `3200.0`, ch4 `1850.0`, gasRatio `0.578125`, gasConcentration `5470.0`,
`validityMask=0x01F3`, all sensors `OK`, `reactorState=NORMAL`, `decisionFlags=0x00`,
`anomalyScore=NaN`, `systemHealth=HEALTHY`, `systemFaults=0`, `resetCause=POWER_ON`,
`watchdogTimeoutMs=4000`.

```
0000  AA 55 02 01 51 00 07 00 D2 04 00 00 80 EE 36 00
0016  00 80 D2 42 00 00 00 3F 00 00 C0 7F 00 00 C0 7F
0032  00 00 D2 43 00 00 48 45 00 40 E7 44 00 00 14 3F
0048  00 F0 AA 45 F3 01 00 00 04 04 00 00 00 00 00 01
0064  00 00 01 00 00 01 00 00 01 00 00 01 00 00 00 00
0080  00 00 00 C0 7F 00 00 00 00 00 01 A0 0F B2 79
```
CRC-16 = `0x79B2`

### GV-2 — blind reactor

**The case this protocol exists for.** Every gas sensor timing out, pressure sensor
offline, nothing observable. `bootCount=7`, `sequence=1235`, `timestamp=812500`, all
nine features NaN, `validityMask=0x0000`, `featureValidity=[INCOMPLETE ×2, NO_SOURCE ×2,
INCOMPLETE ×5]`, gas sensors `TIMEOUT`/`MODBUS_TIMEOUT`/3 failures, pressure sensor
`OFFLINE`/`I2C_INIT_FAILED`/255, **`reactorState=UNKNOWN`**,
`decisionFlags=0x10` (`DATA_UNAVAILABLE`), `anomalyScore=NaN`,
`systemHealth=DEGRADED`, `systemFaults=0x000000B0`
(`RS485_BABBLE | RS485_BUS_DEAD | SENSOR_RACK_INCOMPLETE`), `resetCause=POWER_ON`.

```
0000  AA 55 02 01 51 00 07 00 D3 04 00 00 D4 65 0C 00
0016  00 00 C0 7F 00 00 C0 7F 00 00 C0 7F 00 00 C0 7F
0032  00 00 C0 7F 00 00 C0 7F 00 00 C0 7F 00 00 C0 7F
0048  00 00 C0 7F 00 00 01 01 04 04 01 01 01 01 01 02
0064  10 03 02 10 03 02 10 03 04 20 FF 00 10 00 00 00
0080  00 00 00 C0 7F 01 B0 00 00 00 01 A0 0F 08 12
```
CRC-16 = `0x1208`

Under V1 this same condition transmitted `state = NORMAL` with no field contradicting
it. That is the defect Protocol V2 closes.

### GV-3 — emergency after a watchdog reset

Demonstrates that process state and firmware health are **independent and simultaneous**.
`bootCount=8`, `sequence=42`, `timestamp=7200000`, pressure `186.0` kPa,
pressureRate `6.25`, `reactorState=EMERGENCY`, `decisionFlags=0x03`,
`warningMask=0x0009` (pressure + pressure rate), `emergencyMask=0x0001` (pressure),
`systemHealth=FAILED`, `systemFaults=0x00000001` (`WATCHDOG_RESET`),
`resetCause=WATCHDOG`.

```
0000  AA 55 02 01 51 00 08 00 2A 00 00 00 00 DD 6D 00
0016  00 00 3A 43 00 00 C8 40 00 00 C0 7F 00 00 C0 7F
0032  00 00 D2 43 00 00 48 45 00 40 E7 44 00 00 14 3F
0048  00 F0 AA 45 F3 01 00 00 04 04 00 00 00 00 00 01
0064  00 00 01 00 00 01 00 00 01 00 00 04 03 09 00 01
0080  00 00 00 C0 7F 02 01 00 00 00 04 A0 0F 4D 28
```
CRC-16 = `0x284D`

The reactor is in a genuine `EMERGENCY` **and** the firmware reset itself. A receiver
must show both, and must not let either mask the other.

---

## 12. Version history

### 12.0 Protocol evolution table

| Version | Status | Wire size | Major change | Reason it exists |
|---|---|---|---|---|
| **V1** | **Deprecated** | 56 B frame | Basic telemetry: features + rolled-up decision | First working link. Carried measurements only |
| **V2** | **Current** | 95 B frame | Health architecture: `bootCount`, `sequence`, `SensorStatus`, `SensorFault`, `FeatureValidity`, `SystemHealth`, `warningMask`, NaN contract | V1 could report `NORMAL` from a blind reactor, and forced the receiver to mirror firmware safety logic to learn which limit tripped |
| **V3** | Reserved | — | — | — |

The *"reason it exists"* column is the one that matters in two years. A version number
records that something changed; only the reason explains why the previous design was
insufficient, and that is what stops a later engineer reverting it by accident.

**Every future version must add a row here before any code is written** (§12.1).

### V2 — current

Added: `bootCount`, `sequence`, `SensorStatus` ×4, `SensorFault` ×4,
`consecutiveFailures` ×4, `featureValidity` ×9, `validityMask`, `warningMask`,
`emergencyMask`, `decisionFlags`, `systemHealth`, `systemFaults`, `resetCause`,
`watchdogTimeoutMs`.

Changed:
- Header 6 → 12 bytes
- Sample payload 48 → 81 bytes
- `ReactorState` **renumbered**, `UNKNOWN` added
- NaN specified as a permitted value (§7) — it was already being transmitted with no
  contract term, from F-01 onward

Removed: nothing.

### V1 — DEPRECATED

Superseded by V2. **Not** maintained; not repaired. Known defects, recorded so the
history is honest:

1. Transmitted `state = NORMAL` from a completely blind reactor, with no field capable
   of contradicting it.
2. `ReactorState` numbering not severity-ordered — `EMERGENCY=2` below `ANOMALY=3`.
3. Emitted NaN from F-01 onward with no contract term permitting it, invalidating the
   documented ranges for `temperature`, `gasRatio` and `anomalyScore`.
4. Carried no sensor health, forcing the receiver to infer staleness heuristically.
5. Transmitted a rolled-up `warning` boolean only, forcing the receiver to mirror
   firmware safety constants to learn which limit tripped.

Conforming V2 implementations **reject** V1 packets by version, and vice versa. The two
must never interoperate.

### 12.1 Rules for a future version

A version bump is required for **any** of:

- adding, removing, reordering or resizing a field;
- changing the meaning or unit of an existing field — including gas counts becoming
  ppm (§6.6);
- renumbering any enum, or assigning a previously reserved value;
- changing CRC coverage or byte order.

A bump is **not** required for a firmware change that alters only which values occur —
e.g. a temperature sensor being fitted, making `NO_SOURCE` stop appearing.

Reserved bits and reserved enum values must be **written as zero** and **ignored on
receipt**, so a receiver built against this version tolerates a later one up to the
version gate.
