"""Every API failure has an actionable shape; internal details stay in safe logs."""

import uuid

from fastapi.responses import JSONResponse
from backend.domain.errors import User_Error
from backend.persistence.safe_logging import get_logger

INTERNAL_CODES = {
    "SAVE_FAILED",
    "PERSISTED_DATA_UNREADABLE",
    "DATA_DIR_UNWRITABLE",
    "RENDERING_FAILED",
    "REPLAY_WRITE_FAILED",
}
STATE_CODES = {
    "NO_SLEEP_SESSION",
    "NO_USABLE_DATA",
    "INSUFFICIENT_DATA",
    "GENERATION_IN_PROGRESS",
}


def error_response(error, status=None):
    payload = {
        key: getattr(error, key)
        for key in ("code", "description", "action", "file_name")
    }
    if "reference_id" in error.details:
        payload["reference_id"] = error.details["reference_id"]
    return JSONResponse(
        payload,
        status_code=status
        or (
            500
            if error.code in INTERNAL_CODES
            else 409
            if error.code in STATE_CODES
            else 400
        ),
    )


def internal_error(error):
    reference = uuid.uuid4().hex
    get_logger("api").exception("request.failed", error, reference_id=reference)
    return error_response(
        User_Error(
            "RENDERING_FAILED",
            "An internal error occurred while handling the request.",
            f"Retry the operation; if it recurs, report reference {reference}.",
            details={"reference_id": reference},
        ),
        500,
    )


def request_error(
    description, action="Check the request and try again.", file_name=None
):
    return User_Error("SOURCE_LOAD_FAILED", description, action, file_name=file_name)
