class Cancelled(RuntimeError):
    """The owner cancelled this candidate before production activation."""


class LeaseLost(RuntimeError):
    """Another worker now owns this operation; the old worker must stop."""


class ObservationUnavailable(RuntimeError):
    """Retry observation of the same resource, never blindly resubmit it."""
