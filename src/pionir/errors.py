"""Typed failures that callers can handle without parsing error strings."""


class PionirError(Exception):
    """Base class for expected orchestration failures."""


class CapabilityNotFound(PionirError):
    pass


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
