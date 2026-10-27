"""Custom exceptions raised by the Ezviz Cloud API wrapper."""


class PyEzvizError(Exception):
    """Base exception for all Ezviz API related errors."""


class EzvizPushFatalError(PyEzvizError):
    """Push stopped and requires caller intervention before a new client is started."""
