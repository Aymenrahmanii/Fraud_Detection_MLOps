class ScannerError(Exception):
    """Base class for scanner failures."""


class ScannerUnavailableError(ScannerError):
    """Raised when the underlying CLI tool is not installed / not on PATH."""


class ScannerExecutionError(ScannerError):
    """Raised when the tool ran but its output could not be parsed."""
