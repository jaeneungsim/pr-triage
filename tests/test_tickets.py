import pytest

from pr_triage import tickets
from tests.conftest import TICKET_TEXT


def test_key_comes_from_branch_before_title():
    assert tickets.extract_key(["INV-123-stock", "INV-9 other"]) == "INV-123"
    assert tickets.extract_key(["feature/no-key", "INV-9 Add stock"]) == "INV-9"
    assert tickets.extract_key(["feature/INV-77-stock", None]) == "INV-77"
    assert tickets.extract_key(["feature/INV-77_stock", None]) is None  # `_` is a word character


def test_key_is_case_sensitive_and_needs_a_boundary():
    assert tickets.extract_key(["inv-123-stock", "Add stock"]) is None
    assert tickets.extract_key(["XINV-123", "INV-1234x"]) is None


def test_parse_ticket():
    ticket = tickets.parse_ticket(TICKET_TEXT)
    assert ticket.key == "INV-123"
    assert ticket.repos == ("demo-inventory-api", "demo-inventory-web")
    assert list(ticket.requirements) == ["R1", "R2"]
    assert ticket.requirements["R2"] == "The item detail page shows the stock quantity."


def test_parse_ticket_with_windows_line_endings():
    assert tickets.parse_ticket(TICKET_TEXT.replace("\n", "\r\n")).key == "INV-123"


@pytest.mark.parametrize("text", ["no front matter", "---\nkey: INV-1\n", "---\n- a\n---\n", "---\nrepos: x\n---\n"])
def test_parse_ticket_rejects_bad_files(text):
    with pytest.raises(tickets.TicketFormatError):
        tickets.parse_ticket(text)


def test_stage():
    assert tickets.stage_for("INV-123") == "inv-123"
