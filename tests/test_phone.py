import pytest

from app.services.phone import InvalidPhoneNumber, display_phone, normalize_phone, whatsapp_link


@pytest.mark.parametrize(
    "raw",
    [
        "0771234567",
        "771234567",
        "94771234567",
        "+94771234567",
        "+94 77 123 4567",
        "0094771234567",
        "077-123-4567",
        " 077 123 4567 ",
    ],
)
def test_sri_lankan_formats_normalise_to_same_number(raw):
    assert normalize_phone(raw) == "+94771234567"


def test_landline_and_foreign_numbers():
    assert normalize_phone("0112345678") == "+94112345678"
    assert normalize_phone("+447911123456") == "+447911123456"


@pytest.mark.parametrize("raw", ["", None, "12345", "abc", "+94 77"])
def test_invalid_numbers_rejected(raw):
    with pytest.raises(InvalidPhoneNumber):
        normalize_phone(raw)


def test_display_formats():
    assert display_phone("+94771234567") == "077 123 4567"
    assert display_phone("+447911123456").startswith("+44")
    assert display_phone(None) == ""


def test_whatsapp_link_encodes_message():
    assert whatsapp_link("+94771234567") == "https://wa.me/94771234567"
    link = whatsapp_link("+94771234567", "Hi Nimal & team?")
    assert link == "https://wa.me/94771234567?text=Hi%20Nimal%20%26%20team%3F"
