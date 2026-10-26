from adapters.errors import AdapterError

class UnsupportedCapability(AdapterError):
    """Raised when a capability the adapter does not declare is invoked."""

__all__ = ["UnsupportedCapability"]
