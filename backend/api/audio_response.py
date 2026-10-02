"""Correct byte-range parsing on the pinned Starlette streaming response."""

import re

from fastapi.responses import FileResponse
from starlette.responses import MalformedRangeHeader, RangeNotSatisfiable


class AudioFileResponse(FileResponse):
    @staticmethod
    def _parse_range_header(http_range, file_size):
        units, separator, specification = http_range.partition("=")
        if not separator or units.strip().lower() != "bytes":
            raise MalformedRangeHeader()
        ranges = []
        for part in specification.split(","):
            match = re.fullmatch(r"([0-9]*)-([0-9]*)", part.strip())
            if match is None or not any(match.groups()):
                raise MalformedRangeHeader()
            first, last = match.groups()
            try:
                if first:
                    start = int(first)
                    end = int(last) + 1 if last else file_size
                    if last and end <= start:
                        raise MalformedRangeHeader()
                    end = min(end, file_size)
                else:
                    length = int(last)
                    start, end = max(0, file_size - length), file_size
            except ValueError:
                raise MalformedRangeHeader() from None
            # Unsatisfiable members do not invalidate other usable ranges.
            if start < end:
                ranges.append((start, end))
        if not ranges:
            raise RangeNotSatisfiable(file_size)
        merged = []
        for start, end in sorted(ranges):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged
