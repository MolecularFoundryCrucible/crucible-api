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
        assert validate_username_format("a" * 24) == "a" * 24

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
            validate_username_format("a" * 25)

    def test_invalid_contains_dot(self):
        with pytest.raises(ValueError, match="username must"):
            validate_username_format("first.last")

    def test_invalid_contains_space(self):
        with pytest.raises(ValueError, match="username must"):
            validate_username_format("first last")

    def test_invalid_contains_at(self):
        with pytest.raises(ValueError, match="username must"):
            validate_username_format("user@host")

    def test_invalid_trailing_hyphen(self):
        with pytest.raises(ValueError, match="username must"):
            validate_username_format("abc-")

    def test_invalid_trailing_underscore(self):
        with pytest.raises(ValueError, match="username must"):
            validate_username_format("abc_")

    def test_invalid_consecutive_hyphens(self):
        with pytest.raises(ValueError, match="username must"):
            validate_username_format("ab--cd")

    def test_invalid_consecutive_underscores(self):
        with pytest.raises(ValueError, match="username must"):
            validate_username_format("ab__cd")

    def test_invalid_mixed_consecutive_separators(self):
        with pytest.raises(ValueError, match="username must"):
            validate_username_format("ab-_cd")

    def test_a_service_account_mfid_can_never_pass_as_a_username(self):
        """26 chars always exceeds the 24-char cap — the collision this
        length limit exists to rule out by construction."""
        mfid_shaped = "0tkn2knjast3h0008nyq9zps2c"  # real SA unique_id, see test_resource_access_routes.py
        assert len(mfid_shaped) == 26
        with pytest.raises(ValueError, match="username must"):
            validate_username_format(mfid_shaped)


class TestSanitizeUsernameFromEmail:
    def test_simple_email(self):
        assert sanitize_username_from_email("fabrice@lbl.gov") == "fabrice"

    def test_dot_converted_to_underscore(self):
        assert sanitize_username_from_email("first.last@lbl.gov") == "first_last"

    def test_leading_digits_stripped(self):
        assert sanitize_username_from_email("123abc@example.com") == "abc"

    def test_uppercase_lowercased(self):
        assert sanitize_username_from_email("Fabrice@lbl.gov") == "fabrice"

    def test_truncates_to_24(self):
        long_prefix = "a" * 40
        result = sanitize_username_from_email(f"{long_prefix}@example.com")
        assert len(result) == 24

    def test_no_letters_after_strip_returns_empty(self):
        assert sanitize_username_from_email("123@example.com") == ""

    def test_hyphen_preserved(self):
        assert sanitize_username_from_email("first-last@example.com") == "first-last"

    def test_plus_sign_replaced(self):
        assert sanitize_username_from_email("user+tag@example.com") == "user_tag"

    def test_consecutive_bad_chars_collapse_to_one_separator(self):
        """'..' would otherwise become '__' — two separators in a row,
        which validate_username_format now rejects. The sanitizer must not
        produce something it would itself reject."""
        assert sanitize_username_from_email("first..last@example.com") == "first_last"

    def test_existing_consecutive_hyphens_also_collapse(self):
        assert sanitize_username_from_email("first--last@example.com") == "first_last"

    def test_truncation_does_not_leave_a_trailing_separator(self):
        # 23 letters + '-' lands the separator exactly at the 24-char cut.
        local = "a" * 23 + "-" + "z" * 10
        result = sanitize_username_from_email(f"{local}@example.com")
        assert len(result) <= 24
        assert not result.endswith(("-", "_"))

    def test_output_always_satisfies_validate_username_format(self):
        """The sanitizer's contract: its output must already be valid, since
        the backfill script that calls it never re-validates."""
        for email in ["first.last@x.com", "first--last@x.com", "a" * 40 + "@x.com",
                      "123.abc@x.com", "user+tag@x.com"]:
            candidate = sanitize_username_from_email(email)
            if len(candidate) >= 3:
                assert validate_username_format(candidate) == candidate
