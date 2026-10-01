"""Sanity tests for backend.ingestion.sensorpush.csv_reader (full coverage in task 8.5)."""

from __future__ import annotations

import pytest

from backend.ingestion.sensorpush.csv_reader import (
    ColumnType,
    classify_header,
    match_headers,
    parse_csv,
    read_csv_file,
)

HEADER = "SensorId,Observed,Temperature (°F),Relative Humidity (%),Barometric Pressure (inHg),Dew Point (°F),Vapor Pressure Deficit (kPa)"
ROW = "A1,2024-03-01 23:00,68.2,41.0,29.92,43.1,1.2"


@pytest.mark.parametrize("delimiter", [",", ";"])
@pytest.mark.parametrize("bom", [False, True])
def test_delimiters_bom_and_blank_lines(delimiter: str, bom: bool) -> None:
    text = "\n".join([HEADER, "", ROW, "   ", delimiter * 6, ROW, ""]).replace(",", delimiter)
    data = text.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    result = parse_csv(data, "sp.csv")

    assert result.delimiter == delimiter
    assert result.headers[0] == "SensorId"  # BOM excluded from the first header
    assert [r.line_number for r in result.rows] == [3, 6]  # blank lines dropped
    cols = result.match.columns
    assert cols[ColumnType.SENSOR_ID].index == 0
    assert cols[ColumnType.TIMESTAMP].index == 1
    assert cols[ColumnType.TEMPERATURE].header == "Temperature (°F)"
    assert cols[ColumnType.HUMIDITY].index == 3
    assert cols[ColumnType.PRESSURE].header == "Barometric Pressure (inHg)"
    assert result.warnings == ()  # dew point and VPD ignored silently
    assert result.rows[0].cell(2) == "68.2"
    assert result.rows[0].cell(99) == ""


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("timestamp", ColumnType.TIMESTAMP),
        ("Date", ColumnType.TIMESTAMP),
        ("DateTime", ColumnType.TIMESTAMP),
        ("TIME", ColumnType.TIMESTAMP),
        ("Temp", ColumnType.TEMPERATURE),
        ("Temperature Celsius", ColumnType.TEMPERATURE),
        ("Dew Point Temperature", None),
        ("RH (%)", ColumnType.HUMIDITY),
        ("%RH", ColumnType.HUMIDITY),
        ("Humidity", ColumnType.HUMIDITY),
        ("Absolute Humidity (g/m3)", None),
        ("north", None),
        ("Baro (hPa)", ColumnType.PRESSURE),
        ("Pressure", ColumnType.PRESSURE),
        ("VPD", None),
        ("Sensor ID", ColumnType.SENSOR_ID),
        ("sensorid", ColumnType.SENSOR_ID),
        ("Device", ColumnType.SENSOR_ID),
        ("Battery", None),
        ("", None),
    ],
)
def test_classify_header(header: str, expected: ColumnType | None) -> None:
    assert classify_header(header) == expected


def test_duplicates_leftmost_wins_with_warning() -> None:
    match = match_headers(["Time", "Temp A", "Date", "Temp B", "Temperature C"], "sp.csv")
    assert match.columns[ColumnType.TEMPERATURE].index == 1
    assert match.columns[ColumnType.TIMESTAMP].index == 0
    assert match.ignored_duplicates[ColumnType.TEMPERATURE] == ("Temp B", "Temperature C")
    assert len(match.warnings) == 2
    temp_warning = next(w for w in match.warnings if "temperature" in w.description)
    assert '"Temp B"' in temp_warning.description and '"Temperature C"' in temp_warning.description
    assert temp_warning.subject == "sp.csv"
    assert match.missing_measurements == (ColumnType.HUMIDITY, ColumnType.PRESSURE)


def test_empty_file_and_read_from_disk(tmp_path) -> None:
    empty = parse_csv(b"\n  \n")
    assert empty.headers == () and empty.rows == () and empty.match.timestamp is None

    path = tmp_path / "export.csv"
    path.write_bytes(("Observed;Temp\n2024-03-01 23:00;21,5\n").encode("utf-8-sig"))
    result = read_csv_file(path)
    assert result.file_name == "export.csv"
    assert result.delimiter == ";"
    assert result.rows[0].cells == ("2024-03-01 23:00", "21,5")
