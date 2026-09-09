"""Regression test for a real bug found running SPEC-M4's Managed Identity
Postgres path against a live database: an earlier version of
_conninfo_with_password built the token-bearing connection string with
plain string concatenation ("{conninfo} password={token}"), which produces
an invalid combined conninfo whenever the base is a postgresql:// URI
(psycopg accepts either URI or keyword/value conninfo syntax, but the two
do not combine by pasting). No live Postgres connection is opened here --
this only checks the merged conninfo string psycopg would actually receive.
"""

from __future__ import annotations

from app.persistence.postgres_store import _conninfo_with_password


def test_managed_identity_token_merges_into_a_uri_conninfo() -> None:
    conninfo = _conninfo_with_password(
        "postgresql://armie-identity@armiem3-pg.postgres.database.azure.com:5432/armie?sslmode=require",
        "fake-aad-token",
    )

    assert "password=fake-aad-token" in conninfo
    assert "user=armie-identity" in conninfo
    assert "dbname=armie" in conninfo
    assert "host=armiem3-pg.postgres.database.azure.com" in conninfo


def test_managed_identity_token_merges_into_a_keyword_value_conninfo() -> None:
    conninfo = _conninfo_with_password("host=localhost dbname=armie user=armie", "fake-aad-token")

    assert "password=fake-aad-token" in conninfo
    assert "host=localhost" in conninfo
