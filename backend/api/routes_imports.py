"""Bounded multipart imports with every temporary file in the Data_Directory."""

import json
import tempfile
from pathlib import Path, PureWindowsPath

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.datastructures import UploadFile
from python_multipart.exceptions import MultipartParseError

from backend.api.errors import request_error
from backend.api.routes_sessions import session_state
from backend.domain.timezones import validate_overrides

MAX_UPLOAD_BYTES = 2 * 1024**3
router = APIRouter()


class LocalMultipartParser(MultiPartParser):
    def __init__(self, headers, stream, directory, limit):
        super().__init__(
            headers, stream, max_files=1000, max_fields=20, max_part_size=65536
        )
        self.directory, self.limit = directory, limit
        self.received = 0
        self.completed = False

    def on_end(self):
        super().on_end()
        self.completed = True

    def on_part_begin(self):
        super().on_part_begin()
        self.received = 0

    def on_headers_finished(self):
        super().on_headers_finished()
        if self._current_part.file:
            original = self._current_part.file.file
            original.close()
            self._files_to_close_on_error.remove(original)
            local = tempfile.TemporaryFile(dir=self.directory)
            self._files_to_close_on_error.append(local)
            self._current_part.file.file = local

    def on_part_data(self, data, start, end):
        self.received += end - start
        if self._current_part.file and self.received > self.limit:
            raise request_error(
                "The file exceeds the 2 GB upload limit.",
                "Import through python -m backend.api.cli generate, or select only supported Fitbit files.",
                self._current_part.file.filename,
            )
        super().on_part_data(data, start, end)


def report_state(pipeline, report):
    data = report.to_dict()
    for coverage in data["per_metric_coverage"].values():
        if coverage:
            from datetime import datetime

            for key in ("start", "end"):
                coverage[key] = (
                    datetime.fromisoformat(coverage[key])
                    .astimezone(pipeline.display_tz)
                    .isoformat()
                )
    return dict(report=data, **session_state(pipeline))


@router.post("/api/imports/sample")
def sample(request: Request):
    with request.app.state.operation_lock:
        report, _ = request.app.state.pipeline.use_sample_data()
        return report_state(request.app.state.pipeline, report)


@router.post("/api/imports")
async def upload(request: Request):
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "multipart/form-data"
    ):
        raise request_error(
            "The upload must use multipart/form-data.",
            "Select the files again and retry the import.",
        )
    state = request.app.state
    parent = state.pipeline.data_dir / "uploads"
    parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="import-", dir=parent) as directory:
        parser = LocalMultipartParser(
            request.headers, request.stream(), directory, state.max_upload_bytes
        )
        form = None
        try:
            form = await parser.parse()
            if not parser.completed:
                raise MultiPartException(
                    "The upload ended before its closing boundary."
                )
            raw_overrides = form.get("timezone_overrides", "{}")
            try:
                overrides = json.loads(raw_overrides)
                if not isinstance(overrides, dict):
                    raise ValueError
            except (TypeError, ValueError):
                raise request_error(
                    "Timezone overrides must be a JSON object.",
                    "Provide file-type keys and IANA timezone names.",
                ) from None
            validate_overrides(overrides)
            grouped = {"fitbit": [], "sensorpush": []}
            for index, (field, value) in enumerate(form.multi_items()):
                if not isinstance(value, UploadFile):
                    if field != "timezone_overrides":
                        raise request_error("An unknown import field was provided.")
                    continue
                name = value.filename or ""
                if (
                    not name
                    or Path(name).name != name
                    or PureWindowsPath(name).name != name
                    or name in (".", "..")
                    or PureWindowsPath(name).is_reserved()
                    or name.endswith((" ", "."))
                    or any(ord(char) < 32 or char in '<>:"|?*' for char in name)
                ):
                    raise request_error(
                        "The upload file name is invalid.",
                        "Choose a file with a plain file name.",
                    )
                source = {"fitbit": "fitbit", "sensorpush": "sensorpush"}.get(field)
                suffix = Path(name).suffix.lower()
                if source is None or suffix not in (
                    (".csv",) if source == "sensorpush" else (".json", ".csv", ".zip")
                ):
                    raise request_error(
                        "The selected file type is unsupported.",
                        "Choose Fitbit JSON/CSV files or one ZIP, and SensorPush CSV files.",
                        name,
                    )
                folder = Path(directory) / str(index)
                folder.mkdir()
                path = folder / name
                with path.open("wb") as output:
                    while chunk := await value.read(1024 * 1024):
                        output.write(chunk)
                grouped[source].append(path)
            if not any(grouped.values()):
                raise request_error(
                    "No import files were selected.",
                    "Choose at least one Fitbit or SensorPush file.",
                )
            zips = [p for p in grouped["fitbit"] if p.suffix.lower() == ".zip"]
            if zips and (len(zips) != 1 or len(grouped["fitbit"]) != 1):
                raise request_error(
                    "A Fitbit ZIP cannot be combined with other Fitbit files.",
                    "Choose one Fitbit ZIP or individual JSON/CSV files.",
                    ", ".join(p.name for p in grouped["fitbit"]),
                )
            requests = [
                (source, files, overrides) for source, files in grouped.items() if files
            ]

            def run():
                with state.operation_lock:
                    report, _ = state.pipeline.import_sources(requests)
                    return report_state(state.pipeline, report)

            return await run_in_threadpool(run)
        except (MultiPartException, MultipartParseError):
            raise request_error(
                "The multipart upload is malformed.",
                "Select the files again and retry the import.",
            ) from None
        finally:
            for stream in parser._files_to_close_on_error:
                stream.close()
