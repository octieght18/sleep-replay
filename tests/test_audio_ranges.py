"""Byte-range semantics independent of the pinned framework's parser."""

import pytest

from backend.api.audio_response import AudioFileResponse
from starlette.responses import MalformedRangeHeader, RangeNotSatisfiable


@pytest.mark.parametrize(
    "header,expected",
    [
        ("bytes=0-0", [(0, 1)]),
        ("bytes=0-999", [(0, 100)]),
        ("bytes=-150", [(0, 100)]),
        ("bytes=-5", [(95, 100)]),
        ("bytes=100-,0-2", [(0, 3)]),
        ("bytes=50-55,0-3,3-6", [(0, 7), (50, 56)]),
        ("bytes=0-49,50-99", [(0, 100)]),
        ("bytes=-0,0-2", [(0, 3)]),
    ],
)
def test_range_slices(header, expected):
    assert AudioFileResponse._parse_range_header(header, 100) == expected


@pytest.mark.parametrize(
    "header",
    [
        "",
        "bytes=",
        "bytes=-",
        "bytes=5-4",
        "bytes=0-2junk",
        "bytes=0-2,",
        "items=0-3",
        "bytes=0--2",
    ],
)
def test_malformed_ranges_reject_partial_matches(header):
    with pytest.raises(MalformedRangeHeader):
        AudioFileResponse._parse_range_header(header, 100)


@pytest.mark.parametrize("header", ["bytes=-0", "bytes=100-", "bytes=150-170,200-250"])
def test_wholly_unsatisfiable_ranges(header):
    with pytest.raises(RangeNotSatisfiable):
        AudioFileResponse._parse_range_header(header, 100)


def test_empty_file_is_unsatisfiable():
    with pytest.raises(RangeNotSatisfiable):
        AudioFileResponse._parse_range_header("bytes=0-", 0)
