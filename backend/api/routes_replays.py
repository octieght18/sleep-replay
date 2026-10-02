"""Single-flight generation and private, seekable stored replay downloads."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.responses import MalformedRangeHeader, RangeNotSatisfiable
from backend.api.audio_response import AudioFileResponse

from backend.domain.errors import User_Error
from backend.persistence.metadata_store import validate_record_id
from backend.processing.fingerprint import input_fingerprint

router = APIRouter()


def replay_response(record, cached=False):
    return dict(
        id=record.replay_id,
        cached=cached,
        manifest_url=f"/api/replays/{record.replay_id}/manifest",
        audio_url=f"/api/replays/{record.replay_id}/audio",
    )


@router.post("/api/replays")
def generate(request: Request):
    state = request.app.state
    if not state.generation_lock.acquire(blocking=False):
        raise User_Error(
            "GENERATION_IN_PROGRESS",
            "A replay is already being generated.",
            "Wait for the current generation to finish and try again.",
        )
    try:
        with state.operation_lock:
            config = state.settings.get().to_mapping_config()
            session = state.pipeline.selected_session
            if session is None:
                raise User_Error(
                    "NO_SLEEP_SESSION",
                    "No sleep session is selected.",
                    "Import sleep logs or define a manual range first.",
                )
            hit = state.cache.find(
                input_fingerprint(session), config, state.pipeline.display_tz.key
            )
            if hit:
                return replay_response(hit, True)
            result = state.pipeline.generate(config)
            return replay_response(
                state.pipeline.metadata_store.get_replay(result.replay_id)
            )
    finally:
        state.generation_lock.release()


def stored_paths(request, replay_id):
    try:
        validate_record_id(replay_id, "replay_id")
        record = request.app.state.pipeline.metadata_store.get_replay(replay_id)
    except ValueError:
        record = None
    paths = request.app.state.cache.paths(record) if record else None
    if paths is None:
        raise HTTPException(404, "The replay was not found. Generate it again.")
    return paths


@router.get("/api/replays/{replay_id}/manifest")
def manifest(request: Request, replay_id: str):
    _, path = stored_paths(request, replay_id)
    return FileResponse(
        path, media_type="application/json", headers={"Cache-Control": "no-store"}
    )


@router.get("/api/replays/{replay_id}/audio")
@router.head("/api/replays/{replay_id}/audio")
def audio(request: Request, replay_id: str):
    path, _ = stored_paths(request, replay_id)
    stat = path.stat()
    response = AudioFileResponse(
        path,
        stat_result=stat,
        media_type="audio/wav",
        filename=path.name,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, no-cache"},
    )
    # Pinned Starlette returns an empty 416 directly; validate ahead of its
    # streaming response so rejected ranges also have the API error contract.
    if request.headers.get("range") is not None and (
        request.headers.get("if-range") is None
        or response._should_use_range(request.headers["if-range"])
    ):
        try:
            response._parse_range_header(request.headers["range"], stat.st_size)
        except MalformedRangeHeader:
            raise HTTPException(400, "The audio byte range is malformed.") from None
        except RangeNotSatisfiable:
            raise HTTPException(
                416,
                "The audio byte range is outside this replay.",
                headers={"Content-Range": f"bytes */{stat.st_size}"},
            ) from None
    return response
