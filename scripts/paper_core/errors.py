"""Structured errors; the CLI maps each class to one exit code."""
from __future__ import annotations


class CoreError(Exception):
    exit_code = 2
    code = "ERROR"

    def __init__(self, message, *, code=None, records=None, retry=None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.records = list(records or [])
        self.retry = retry

    def to_json(self):
        payload = {"error": {"code": self.code, "message": self.message, "records": self.records}}
        if self.retry is not None:
            payload["error"]["retry"] = self.retry
        return payload


class InvalidRequest(CoreError):
    exit_code = 2
    code = "INVALID_REQUEST"


class ConflictError(CoreError):
    exit_code = 3
    code = "CONFLICT"


class IncompatibleError(CoreError):
    exit_code = 4
    code = "INCOMPATIBLE"


class SourceUnavailable(CoreError):
    exit_code = 5
    code = "SOURCE_UNAVAILABLE"


class PublicationError(CoreError):
    exit_code = 6
    code = "PUBLICATION_FAILED"
