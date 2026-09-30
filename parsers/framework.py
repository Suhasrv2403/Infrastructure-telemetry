"""Parser framework: registry + dispatch for turning landed Stage 0 message envelopes into
Stage 1 rows.

Ticket: P1-03 ("Parser framework with versioned per-firmware parsers").

Scope note: this module is the FRAMEWORK only - a registry for firmware-versioned parser
functions, dispatch by (device_class, firmware_version), and quarantine handling for anything
that can't be dispatched or fails while parsing. It ships with exactly one demonstration
parser (parsers/supercharger_stall/firmware_2_1_4.py) to prove the framework works end to
end. Real per-firmware parser coverage for every firmware version of both Supercharger device
classes (and eventually Powerwall/Megapack/Powerpack) is P1-04 and later tickets, not this one.

Per parsers/README.md, a message whose (device_class, firmware_version) has no registered
parser is never dropped and never force-parsed by a fallback/guess - it is quarantined with
the original message preserved verbatim, so it can be reprocessed once a parser exists. A
parser that raises while parsing one message degrades that single message to quarantine
rather than crashing the batch or silently losing data.

What this is NOT: a Dagster asset, an S3/Iceberg writer, or the Stage 1 MERGE-on-
(device_id, device_ts, payload_hash) implementation (CLAUDE.md invariant 2, that's P1-06).
This module is pure Python operating on plain dicts - the same S3/Dagster-free "pure core"
separation pipeline/stage0_landing/capture.py keeps from its own orchestration wiring.

What a registered parser returns is also intentionally NOT canonicalized: fields keep their
raw, per-firmware names and units exactly as the device sent them. Mapping raw field ->
canonical name/unit/type is Stage 2's job (P1-08) via catalog/signals.yaml (itself still an
unreviewed skeleton as of P0-10) - this framework only reshapes "one envelope with N
readings" into "N flat per-reading rows tagged with their parent envelope's identity fields".
"""
from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable
from typing import Any

# A parser takes one landed Stage 0 message envelope and returns zero or more flat Stage 1
# rows (still in raw field-name form - see module docstring).
ParserFunc = Callable[[dict[str, Any]], list[dict[str, Any]]]

# (device_class, firmware_version) -> parser function.
Registry = dict[tuple[str, str], ParserFunc]


class ParserFrameworkError(Exception):
    """Base class for parser-framework errors."""


class DuplicateParserError(ParserFrameworkError):
    """Raised when a (device_class, firmware_version) pair is registered more than once.

    Two parsers silently racing for the same messages (the second registration shadowing the
    first) is almost certainly a bug, so registration fails loudly instead.
    """


class ReconciliationError(ParserFrameworkError):
    """Raised when messages_seen doesn't equal (messages_parsed + messages_quarantined).

    Mirrors pipeline/stage0_landing/capture.py's ReconciliationError: every message must be
    accounted for exactly once, never silently vanish.
    """


# Module-level default registry, populated by @register_parser at import time (the same
# "decorate to register" shape as Dagster's own @asset). Tests that need an isolated registry
# (e.g. to register a throwaway parser that deliberately raises) can pass their own `registry`
# dict to register_parser()/parse_messages() instead of relying on this global one.
_REGISTRY: Registry = {}


def register_parser(
    device_class: str, firmware_version: str, *, registry: Registry | None = None
) -> Callable[[ParserFunc], ParserFunc]:
    """Decorator registering `func` as the parser for one (device_class, firmware_version) pair.

    `func` takes one landed Stage 0 message envelope (a dict - see
    tests/fixtures/generators/supercharger.py for the shape a synthetic one takes, or
    pipeline/stage0_landing/capture.py for what actually lands in Stage 0) and returns a list
    of zero or more flat Stage 1 rows.

    `registry` defaults to this module's global registry; pass an explicit dict to register
    into an isolated registry instead (useful in tests, so a throwaway parser registered for a
    fake firmware version doesn't leak into the shared global registry other code relies on).
    """
    target = _REGISTRY if registry is None else registry

    def decorator(func: ParserFunc) -> ParserFunc:
        key = (device_class, firmware_version)
        if key in target:
            raise DuplicateParserError(
                f"a parser is already registered for device_class={device_class!r}, "
                f"firmware_version={firmware_version!r}"
            )
        target[key] = func
        return func

    return decorator


def registered_parsers(*, registry: Registry | None = None) -> tuple[tuple[str, str], ...]:
    """The (device_class, firmware_version) pairs currently registered, for introspection/tests."""
    target = _REGISTRY if registry is None else registry
    return tuple(target.keys())


def _no_parser_reason(device_class: Any, firmware_version: Any) -> str:
    return (
        "no_parser_registered_for_(device_class="
        f"{device_class!r}, firmware_version={firmware_version!r})"
    )


def _parser_error_reason(exc: Exception) -> str:
    return f"parser_error: {exc}"


@dataclasses.dataclass(frozen=True)
class QuarantinedMessage:
    """One message that could not be turned into Stage 1 rows.

    `message` is the original Stage 0 envelope, preserved verbatim (not copied/mutated), so a
    quarantined message can be reprocessed later once a parser exists or a parser bug is
    fixed.
    """

    message: dict[str, Any]
    reason: str


@dataclasses.dataclass(frozen=True)
class ParseResult:
    """Summary of one dispatch run, mirroring CaptureResult's style (see
    pipeline/stage0_landing/capture.py): counters to log, plus a reconciles()-style check that
    every message seen was accounted for exactly once."""

    messages_seen: int
    messages_parsed: int
    rows: tuple[dict[str, Any], ...]
    quarantined: tuple[QuarantinedMessage, ...]

    @property
    def rows_parsed(self) -> int:
        return len(self.rows)

    @property
    def messages_quarantined(self) -> int:
        return len(self.quarantined)

    def reconciles(self) -> bool:
        """True iff every message seen either produced rows via a successful parser dispatch
        or was quarantined - never both, never neither."""
        return self.messages_seen == self.messages_parsed + len(self.quarantined)


def parse_messages(
    messages: Iterable[dict[str, Any]], *, registry: Registry | None = None
) -> ParseResult:
    """Dispatch each landed Stage 0 message to its registered (device_class, firmware_version)
    parser, producing Stage 1 rows.

    A message whose (device_class, firmware_version) has no registered parser, or whose parser
    raises while parsing it, is quarantined (with a reason and the original message preserved
    verbatim) rather than dropped or crashing the batch - see module docstring.

    `registry` defaults to this module's global registry (populated by @register_parser at
    import time); pass an explicit dict to dispatch against an isolated registry instead.
    """
    target = _REGISTRY if registry is None else registry

    messages_seen = 0
    messages_parsed = 0
    rows: list[dict[str, Any]] = []
    quarantined: list[QuarantinedMessage] = []

    for message in messages:
        messages_seen += 1
        device_class = message.get("device_class")
        firmware_version = message.get("firmware_version")
        parser = target.get((device_class, firmware_version))

        if parser is None:
            quarantined.append(
                QuarantinedMessage(
                    message=message,
                    reason=_no_parser_reason(device_class, firmware_version),
                )
            )
            continue

        try:
            parsed_rows = parser(message)
        except Exception as exc:  # noqa: BLE001 - a parser bug must degrade to quarantine, not
            # crash the batch or lose the message (see module docstring).
            quarantined.append(
                QuarantinedMessage(message=message, reason=_parser_error_reason(exc))
            )
            continue

        messages_parsed += 1
        rows.extend(parsed_rows)

    return ParseResult(
        messages_seen=messages_seen,
        messages_parsed=messages_parsed,
        rows=tuple(rows),
        quarantined=tuple(quarantined),
    )


def reconcile(result: ParseResult) -> None:
    """Raise ReconciliationError unless every message seen was accounted for exactly once."""
    if not result.reconciles():
        raise ReconciliationError(
            f"{result.messages_seen} messages seen but {result.messages_parsed} parsed + "
            f"{len(result.quarantined)} quarantined = "
            f"{result.messages_parsed + len(result.quarantined)} accounted for - every message "
            "must be either parsed or quarantined, never both, never neither"
        )
