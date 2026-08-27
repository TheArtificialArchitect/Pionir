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
