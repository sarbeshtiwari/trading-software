"""Isolated report-shaped fixtures; no symbol below is a claim about a live ban."""

from datetime import date
from hashlib import sha256

import pytest

from app.fno.restrictions import parse_report


def test_report_uses_embedded_trade_date_and_retains_exact_document_hash():
    document = "Securities in Ban For Trade Date 05-OCT-2026:\r\n1,TEST\r\n2,M&M\r\n"
    report = parse_report(document)
    assert report.trade_date == date(2026, 10, 5)
    assert report.underlyings == ("TEST", "M&M")
    assert report.sha256 == sha256(document.encode()).hexdigest()
    assert parse_report("Securities in Ban For Trade Date 05-OCT-2026:\n").underlyings == ()


@pytest.mark.parametrize(
    "document",
    [
        "",
        "\ufeff",
        "<html>blocked</html>",
        "1,TEST",
        "Securities in Ban For Trade Date 31-FEB-2026:",
        "Securities in Ban For Trade Date 05-OCT-2026:\n2,TEST",
        "Securities in Ban For Trade Date 05-OCT-2026:\n1,TEST\n2,TEST",
        "Securities in Ban For Trade Date 05-OCT-2026:\n1,Test",
        'Securities in Ban For Trade Date 05-OCT-2026:\n1,"TEST',
        "Securities in Ban For Trade Date 05-OCT-2026:\n1,TEST,extra",
    ],
)
def test_malformed_report_never_becomes_empty_clear_list(document):
    with pytest.raises(ValueError):
        parse_report(document)
