#
# Zabbix MCP Server
# Copyright (C) 2026 initMAX s.r.o.
# Licensed under the GNU Affero General Public License v3.
# See LICENSE for details.
#

"""Bearer token verification: the static token, its optional expiry, the caller fingerprint."""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

from zabbix_mcp.auth import BearerTokenVerifier, caller_fingerprint, parse_expiry
from zabbix_mcp.config import ConfigError, load_config

SERVER_ENV = {
    "ZABBIX_MCP_ZABBIX_SERVERS__PROD__URL": "http://z",
    "ZABBIX_MCP_ZABBIX_SERVERS__PROD__API_TOKEN": "tok",
}


def _verify(verifier: BearerTokenVerifier, token: str):
    return asyncio.run(verifier.verify_token(token))


class TestBearerTokenVerifier(unittest.TestCase):
    def test_right_token_passes_and_wrong_one_does_not(self):
        v = BearerTokenVerifier("s3cret")
        self.assertIsNotNone(_verify(v, "s3cret"))
        self.assertIsNone(_verify(v, "s3cret "))
        self.assertIsNone(_verify(v, ""))

    def test_non_ascii_token_does_not_raise(self):
        # hmac.compare_digest on str raises TypeError for non-ASCII input.
        self.assertIsNone(_verify(BearerTokenVerifier("s3cret"), "s3cré"))

    def test_expired_token_is_rejected(self):
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        v = BearerTokenVerifier("s3cret", past)
        self.assertTrue(v.expired)
        self.assertIsNone(_verify(v, "s3cret"))

    def test_unexpired_token_reports_its_expiry(self):
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        v = BearerTokenVerifier("s3cret", future)
        access = _verify(v, "s3cret")
        self.assertFalse(v.expired)
        self.assertEqual(access.expires_at, int(future.timestamp()))


class TestParseExpiry(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(parse_expiry("2030-01-01"), datetime(2030, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(parse_expiry("2030-01-01T12:30:00Z"), datetime(2030, 1, 1, 12, 30, tzinfo=timezone.utc))
        self.assertEqual(parse_expiry("2030-01-01T12:30:00+02:00").utcoffset(), timedelta(hours=2))

    def test_garbage_is_an_error_not_no_expiry(self):
        with self.assertRaises(ValueError):
            parse_expiry("next tuesday")


class TestExpiryFromEnvironment(unittest.TestCase):
    def _load(self, **extra):
        with patch.dict(os.environ, {**SERVER_ENV, **extra}, clear=True):
            return load_config()

    def test_expiry_and_token_are_loaded(self):
        cfg = self._load(
            ZABBIX_MCP_SERVER__AUTH_TOKEN="abc",
            ZABBIX_MCP_SERVER__AUTH_TOKEN_EXPIRES_AT="2030-01-01T00:00:00Z",
        )
        self.assertEqual(cfg.server.auth_token.get_secret_value(), "abc")
        self.assertEqual(cfg.server.auth_token_expires_at, datetime(2030, 1, 1, tzinfo=timezone.utc))

    def test_blank_token_means_no_auth(self):
        self.assertIsNone(self._load(ZABBIX_MCP_SERVER__AUTH_TOKEN="  ").server.auth_token)

    def test_bad_expiry_is_a_config_error(self):
        with self.assertRaises(ConfigError) as ctx:
            self._load(ZABBIX_MCP_SERVER__AUTH_TOKEN_EXPIRES_AT="soon")
        self.assertIn("auth_token_expires_at", str(ctx.exception))


class TestCallerFingerprint(unittest.TestCase):
    def test_none_without_an_authenticated_caller(self):
        self.assertIsNone(caller_fingerprint())

    def test_stable_per_token_and_distinct_between_tokens(self):
        def fingerprint_for(token: str) -> str | None:
            user = AuthenticatedUser(AccessToken(token=token, client_id="c", scopes=[]))
            reset = auth_context_var.set(user)
            try:
                return caller_fingerprint()
            finally:
                auth_context_var.reset(reset)

        a1, a2, b = fingerprint_for("token-a"), fingerprint_for("token-a"), fingerprint_for("token-b")
        self.assertEqual(a1, a2)
        self.assertNotEqual(a1, b)
        self.assertNotIn("token-a", a1)


if __name__ == "__main__":
    unittest.main()
