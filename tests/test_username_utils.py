import pytest
from app.utils.user_utils import validate_username_format, sanitize_username_from_email


class TestValidateUsernameFormat:
    def test_valid_simple(self):
        assert validate_username_format("fabrice") == "fabrice"

    def test_valid_with_digits_and_separators(self):
        assert validate_username_format("fab_rice-2") == "fab_rice-2"

    def test_valid_minimum_length(self):
        assert validate_username_format("abc") == "abc"

    def test_valid_maximum_length(self):
        assert validate_username_format("a" * 32) == "a" * 32

    def test_invalid_starts_with_digit(self):
        with pytest.raises(ValueError, match="username must"):
            validate_username_format("1abc")

    def test_invalid_starts_with_underscore(self):
        with pytest.raises(ValueError, match="username must"):
            validate_username_format("_abc")

    def test_uppercase_is_normalised_not_rejected(self):
        assert validate_username_format("Fabrice") == "fabrice"

    def test_invalid_too_short(self):
        with pytest.raises(ValueError, match="username must"):
            validate_username_format("ab")

    def test_invalid_too_long(self):
        with pytest.raises(ValueError, match="username must"):
            validate_username_format("a" * 33)

    def test_invalid_contains_dot(self):
        with pytest.raises(ValueError, match="username must"):
            validate_username_format("first.last")

    def test_invalid_contains_space(self):
        with pytest.raises(ValueError, match="username must"):
            validate_username_format("first last")

    def test_invalid_contains_at(self):
        with pytest.raises(ValueError, match="username must"):
            validate_username_format("user@host")


class TestSanitizeUsernameFromEmail:
    def test_simple_email(self):
        assert sanitize_username_from_email("fabrice@lbl.gov") == "fabrice"

    def test_dot_converted_to_underscore(self):
        assert sanitize_username_from_email("first.last@lbl.gov") == "first_last"

    def test_leading_digits_stripped(self):
        assert sanitize_username_from_email("123abc@example.com") == "abc"

    def test_uppercase_lowercased(self):
        assert sanitize_username_from_email("Fabrice@lbl.gov") == "fabrice"

    def test_truncates_to_32(self):
        long_prefix = "a" * 40
        result = sanitize_username_from_email(f"{long_prefix}@example.com")
        assert len(result) == 32

    def test_no_letters_after_strip_returns_empty(self):
        assert sanitize_username_from_email("123@example.com") == ""

    def test_hyphen_preserved(self):
        assert sanitize_username_from_email("first-last@example.com") == "first-last"

    def test_plus_sign_replaced(self):
        assert sanitize_username_from_email("user+tag@example.com") == "user_tag"
