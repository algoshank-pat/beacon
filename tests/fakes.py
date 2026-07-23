"""Test doubles for HTTP calls — no real network in the test suite."""
from __future__ import annotations

import re

import requests


class FakeResponse:
    def __init__(self, status_code: int = 200, json_data=None, text: str = ""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")


class FakeSession:
    """Returns each item in `responses` in order; an Exception instance is raised."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeAnthropicTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeAnthropicUsage:
    def __init__(self, input_tokens, output_tokens):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeAnthropicResponse:
    def __init__(self, text, input_tokens=100, output_tokens=50):
        self.content = [FakeAnthropicTextBlock(text)]
        self.usage = FakeAnthropicUsage(input_tokens, output_tokens)


class FakeAnthropicMessages:
    """`responses` is either a single string (reused for every call) or a list
    of strings consumed in order."""

    def __init__(self, responses):
        self._fixed = responses if isinstance(responses, str) else None
        self._responses = list(responses) if isinstance(responses, list) else None
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        text = self._fixed if self._fixed is not None else self._responses.pop(0)
        return FakeAnthropicResponse(text)


class FakeAnthropicClient:
    def __init__(self, responses):
        self.messages = FakeAnthropicMessages(responses)


def _col_to_index(letters: str) -> int:
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx


def _parse_a1_range(range_name: str):
    """Single-row-oriented A1 range parser -- covers what this codebase
    actually sends ("A1", "F3", "A5:E5", "G5:H5"), not the full A1 grammar."""
    m = re.match(r"^([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?$", range_name)
    if not m:
        raise NotImplementedError(range_name)
    start_col, start_row, end_col, end_row = m.groups()
    start_col_idx = _col_to_index(start_col)
    start_row = int(start_row)
    if end_col is None:
        return start_col_idx, start_row, start_col_idx, start_row
    return start_col_idx, start_row, _col_to_index(end_col), int(end_row)


class _FakeSpreadsheet:
    def __init__(self, sheet_id=0):
        self.batch_update_calls = []
        self.sheet_id = sheet_id
        self.conditional_formats = []

    def batch_update(self, body):
        self.batch_update_calls.append(body)
        for request in body.get("requests", []):
            if "addConditionalFormatRule" in request:
                rule_request = request["addConditionalFormatRule"]
                index = rule_request.get("index", len(self.conditional_formats))
                self.conditional_formats.insert(index, rule_request["rule"])
            elif "deleteConditionalFormatRule" in request:
                index = request["deleteConditionalFormatRule"]["index"]
                del self.conditional_formats[index]

    def fetch_sheet_metadata(self):
        return {"sheets": [{"properties": {"sheetId": self.sheet_id}, "conditionalFormats": self.conditional_formats}]}


class FakeWorksheet:
    """Minimal stand-in for gspread.Worksheet covering the calls sheets.py/job_log.py make."""

    def __init__(self, rows=None, row_count=None):
        self.rows = rows or []
        self.formats = []
        self.validations = []
        self.appended = []
        self.id = 0
        self.spreadsheet = _FakeSpreadsheet(sheet_id=self.id)
        self.row_count = row_count if row_count is not None else len(self.rows)

    def resize(self, rows=None, cols=None):
        if rows is not None:
            self.row_count = rows

    def row_values(self, row_number):
        idx = row_number - 1
        return self.rows[idx] if idx < len(self.rows) else []

    def col_values(self, col_number):
        idx = col_number - 1
        return [row[idx] if idx < len(row) else "" for row in self.rows]

    def update(self, values, range_name=None, **kwargs):
        start_col, start_row, _end_col, _end_row = _parse_a1_range(range_name)
        for r_offset, row_values in enumerate(values):
            row_idx = start_row - 1 + r_offset
            while len(self.rows) <= row_idx:
                self.rows.append([])
            row = self.rows[row_idx]
            for c_offset, val in enumerate(row_values):
                col_idx = start_col - 1 + c_offset
                while len(row) <= col_idx:
                    row.append("")
                row[col_idx] = val

    def format(self, range_name, format_dict):
        self.formats.append((range_name, format_dict))

    def add_validation(self, range_name, condition_type, values, **kwargs):
        self.validations.append((range_name, condition_type, list(values), kwargs))

    def append_row(self, row_values, value_input_option=None, table_range=None):
        self.rows.append(row_values)
        self.appended.append(row_values)

    def delete_rows(self, start_index, end_index=None):
        end_index = end_index or start_index
        del self.rows[start_index - 1:end_index]

    def sort(self, *specs, range=None):
        if range is not None:
            _, start_row, _, end_row = _parse_a1_range(range)
        else:
            start_row, end_row = 1, len(self.rows)
        start_idx, end_idx = start_row - 1, end_row  # end_row is inclusive, 1-based

        segment = self.rows[start_idx:end_idx]
        for col_index, order in reversed(specs):  # stable multi-key sort: last spec first
            idx = col_index - 1
            segment.sort(key=lambda row: (row[idx] if idx < len(row) else ""), reverse=(order == "des"))
        self.rows[start_idx:end_idx] = segment
