class AdapterError(Exception):
    """Base class for all adapter-layer failures."""

class UnsupportedParameter(AdapterError):
    pass

class AuthExpired(AdapterError):
    pass

class MalformedResponse(AdapterError):
    pass

class NotFound(AdapterError):
    pass

class TransportError(AdapterError):
    pass

class RateLimited(AdapterError):
    def __init__(self, retry_after_seconds=None):
        super().__init__("rate limited")
        self.retry_after_seconds = retry_after_seconds

class Rejected(AdapterError):
    def __init__(self, status=None):
        super().__init__(f"rejected with status {status}")
        self.status = status
