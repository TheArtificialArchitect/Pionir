"""Typed failures that callers can handle without parsing error strings."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for type checkers only
    from .router import RoutingDecision


class PionirError(Exception):
    """Base class for expected orchestration failures."""


class CapabilityNotFound(PionirError):
    pass


class RoutingAmbiguous(PionirError):
    """The classifier was not confident enough to choose, so it is asking.

    This is a deliberate outcome rather than a fault: the alternative is a
    silent guess that spends a model load and answers as the wrong specialist.
    The decision it carries holds the candidate routes to put to the user.
    """

    def __init__(self, message: str, *, decision: "RoutingDecision | None" = None) -> None:
        super().__init__(message)
        self.decision = decision


class PermissionDenied(PionirError):
    pass


class ResourceUnavailable(PionirError):
    pass


class NamespaceAccessDenied(PionirError):
    pass


class AdapterError(PionirError):
    """Base class for failures at a specialist process boundary."""


class AdapterUnavailable(AdapterError):
    pass


class AdapterAuthenticationError(AdapterError):
    pass


class AdapterProtocolError(AdapterError):
    pass


class AuditIntegrityError(PionirError):
    pass


class CircuitOpen(PionirError):
    pass
