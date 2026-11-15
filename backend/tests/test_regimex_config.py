"""Optional integration configuration fails before sending any credentials."""

import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from backend.app.core.config import Settings

TOKEN = "test-backend-credential-value"


def configured(**values):
    with patch.dict(os.environ, {}, clear=True):
        return Settings(
            _env_file=None,
            database_url="sqlite://",
            jwt_secret="test-jwt-key",
            password_pepper="test-password-pepper",
            **values,
        )


class RegimexConfigurationTests(unittest.TestCase):
    def test_unconfigured_simulator_does_not_need_regimex(self):
        settings = configured()
        self.assertIsNone(settings.regimex_connection())
        self.assertEqual(settings.regimex_source_id, "trade")

    def test_https_connection_keeps_secret_out_of_representations(self):
        settings = configured(regimex_url="https://analysis.example.test", regimex_token=TOKEN)
        connection = settings.regimex_connection()
        self.assertEqual(connection.url, "https://analysis.example.test")
        self.assertNotIn(TOKEN, repr(connection))
        self.assertNotIn(TOKEN, repr(settings))

    def test_incomplete_configuration_and_ineffective_flags_are_rejected(self):
        for values in (
            {"regimex_url": "https://analysis.example.test"},
            {"regimex_token": TOKEN},
            {"regimex_allow_loopback_http": True},
            {
                "regimex_url": "https://analysis.example.test",
                "regimex_token": TOKEN,
                "regimex_allow_loopback_http": True,
            },
        ):
            with self.subTest(values=tuple(values)), self.assertRaises(ValidationError):
                configured(**values)

    def test_only_explicit_numeric_loopback_can_use_plain_http(self):
        for origin in ("http://127.0.0.1:8000", "http://[::1]:8000"):
            settings = configured(
                regimex_url=origin,
                regimex_token=TOKEN,
                regimex_allow_loopback_http=True,
            )
            self.assertEqual(settings.regimex_connection().url, origin)
        for origin in (
            "http://127.0.0.1",
            "http://localhost:8000",
            "http://10.0.0.1",
            "http://127.0.0.1.example.test",
            "http://2130706433",
            "ftp://127.0.0.1",
        ):
            with self.subTest(origin=origin), self.assertRaises(ValidationError):
                configured(
                    regimex_url=origin,
                    regimex_token=TOKEN,
                    regimex_allow_loopback_http=origin != "http://127.0.0.1",
                )

    def test_url_injection_paths_and_invalid_identifiers_are_rejected(self):
        for origin in (
            "https://user:secret@analysis.example.test",
            "https://analysis.example.test/api",
            "https://analysis.example.test?secret=value",
            "https://analysis.example.test#fragment",
            "https://analysis.example.test\\evil",
            "https://analysis.example.test\n",
            "https://analysis.example.test:0",
            "https://analysis.example.test:99999",
            "",
        ):
            with self.subTest(origin=origin), self.assertRaises(ValidationError) as error:
                configured(regimex_url=origin, regimex_token=TOKEN)
            self.assertNotIn(TOKEN, str(error.exception))
        for source in ("", "source with spaces", "source/other", "a" * 129):
            with self.subTest(source=source), self.assertRaises(ValidationError):
                configured(regimex_source_id=source)
        for token in ("short", TOKEN + "\n", TOKEN + " header"):
            with self.subTest(token_length=len(token)), self.assertRaises(ValidationError):
                configured(regimex_url="https://analysis.example.test", regimex_token=token)
