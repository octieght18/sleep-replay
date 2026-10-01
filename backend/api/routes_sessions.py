"""Session listing and persisted selection without exposing raw samples."""

from fastapi import APIRouter, Request
from backend.api.errors import request_error
from backend.domain.errors import User_Error
from backend.processing.session_detector import list_sessions, session_key

router = APIRouter()


def session_state(pipeline):
    rows = [
        r.to_dict() for r in list_sessions(pipeline.candidates, pipeline.display_tz)
    ]
    selected = pipeline.selected_session
    selected_row = (
        None
        if selected is None
        else list_sessions([selected], pipeline.display_tz)[0].to_dict()
    )
    return dict(
        sessions=rows,
        selected=selected_row,
        selected_id=None if selected is None else session_key(selected),
        display_timezone=pipeline.display_tz.key,
        has_imports=bool(pipeline.metadata_store.list_import_ids()),
    )


@router.get("/api/sessions")
def get_sessions(request: Request):
    with request.app.state.operation_lock:
        state = session_state(request.app.state.pipeline)
        if state["selected"] is None:
            raise User_Error(
                "NO_SLEEP_SESSION",
                "No sleep session is selected.",
                "Import sleep logs or define a manual range after importing telemetry.",
            )
        return state


@router.put("/api/sessions/selected")
def select(request: Request, body: dict):
    if set(body) != {"id"} or not isinstance(body["id"], str):
        raise request_error(
            "The session selection requires a string id.",
            "Choose a session from the list.",
        )
    with request.app.state.operation_lock:
        try:
            request.app.state.pipeline.select_session(body["id"])
        except LookupError:
            raise request_error(
                "The selected session is no longer available.",
                "Refresh the session list and choose an available session.",
            ) from None
        return session_state(request.app.state.pipeline)


@router.post("/api/sessions/manual")
def manual(request: Request, body: dict):
    if set(body) != {"start", "end"} or any(
        not isinstance(body[k], str) for k in ("start", "end")
    ):
        raise User_Error(
            "INVALID_MANUAL_RANGE",
            "Both manual start and end are required.",
            "Provide ISO 8601 start and end times in the display timezone.",
        )
    with request.app.state.operation_lock:
        request.app.state.pipeline.define_manual_range(body["start"], body["end"])
        return session_state(request.app.state.pipeline)
