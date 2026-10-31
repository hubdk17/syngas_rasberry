"""
TelemetryIngest -- moves bytes from a source into the store.

Responsibility: poll, decode, append, and report what discrete events
happened while doing so.

    TelemetrySource -> PacketDecoder -> TelemetryStore

That is the whole receive path, and this is the only place it is assembled.

NO Qt HERE
----------
This is deliberately a plain class with a plain `poll()` method, not a QObject
with signals. Qt lives one layer up, in dashboard/telemetry_service.py, which
owns the timer and translates an IngestResult into signals.

The separation is what makes the receive path testable without an event loop:
every test in tests/test_telemetry_ingest.py runs in microseconds with no
QApplication, no offscreen platform and no pytest-qt. It also keeps backend/
free of the GUI toolkit, which is the same rule that keeps analytics free of
it.

WHY poll() RETURNS A RESULT RATHER THAN EMITTING
------------------------------------------------
The caller needs to know about *discrete* events -- a device restart, a burst
of CRC errors, the port disappearing -- because those are what deserve a
signal, a timeline entry or a banner change. It does not need to be told about
every frame, because the store already holds those and the UI reads them on
its own schedule.

Returning a value rather than emitting also means the caller decides what is
worth reacting to, and a test can assert on the return instead of wiring up a
signal spy.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.protocol.decoder import DecoderStats, PacketDecoder
from backend.telemetry_store import TelemetryStore


@dataclass(frozen=True, slots=True)
class IngestResult:
    """What happened during one poll. Almost always nothing."""

    #: Frames decoded and stored. Zero on most polls, because the UI polls
    #: faster than the reactor transmits.
    frames_added: int = 0

    #: A stored frame began a new device session -- the Arduino restarted or
    #: its counter wrapped. Worth a timeline entry and a banner note; never
    #: worth silently absorbing.
    restarted: bool = False

    #: Malformed packets seen during this poll: CRC failures, bad versions,
    #: impossible lengths. A steady trickle means a marginal cable.
    decode_errors: int = 0

    #: The source itself failed -- the port vanished, the device was
    #: unplugged. None when the read succeeded, even if it returned no data.
    source_error: str | None = None

    @property
    def has_events(self) -> bool:
        """True when something happened that a caller might want to announce."""
        return (
            self.restarted
            or self.decode_errors > 0
            or self.source_error is not None
        )


class TelemetryIngest:
    """Drives one telemetry source into one store."""

    def __init__(
        self,
        source,
        store: TelemetryStore,
        decoder: PacketDecoder | None = None,
    ) -> None:
        """
        Args:
            source: Anything with read() -> bytes. Not annotated as
                TelemetrySource because structural typing needs no
                declaration, and annotating it would make this module import
                the seam for no benefit.
            store: Where decoded frames go. The single owner of telemetry.
            decoder: Injected for tests that want to inspect its state;
                a fresh one is created otherwise.
        """
        self._source = source
        self._store = store
        self._decoder = decoder if decoder is not None else PacketDecoder()

    @property
    def stats(self) -> DecoderStats:
        """
        Decoder counters, for the diagnostics strip.

        Exposed rather than copied, because a copy would be one more thing
        that could disagree with the decoder about how many CRC errors have
        occurred.
        """
        return self._decoder.stats

    @property
    def pending_bytes(self) -> int:
        """Bytes buffered mid-packet. Non-zero briefly; useful when a link stalls."""
        return self._decoder.pending_bytes

    def poll(self) -> IngestResult:
        """
        Read whatever is available and store any complete frames.

        Never raises. A transport failure is reported in the result, because
        an exception escaping here would reach a Qt timer callback, where the
        usual outcome is a traceback on stderr and a dashboard that quietly
        stops updating while still showing its last values -- the exact
        failure an industrial HMI must not have.
        """
        try:
            data = self._source.read()
        except Exception as error:  # noqa: BLE001 - see docstring
            # Broad by intent. A transport can raise SerialException, OSError,
            # or anything else its driver decides on, and none of them may be
            # allowed to stop the refresh loop.
            return IngestResult(source_error=f"{type(error).__name__}: {error}")

        if not data:
            return IngestResult()

        errors_before = self._decoder.stats.error_count
        session_before = self._store.session

        packets = self._decoder.feed(data)

        frames_added = 0

        for packet in packets:
            if packet.frame is not None:
                self._store.append(packet.frame)
                frames_added += 1

        return IngestResult(
            frames_added=frames_added,
            restarted=self._store.session != session_before,
            decode_errors=self._decoder.stats.error_count - errors_before,
        )
