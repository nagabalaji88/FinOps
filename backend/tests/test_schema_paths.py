"""The two ways this platform's schema gets built, and the rule that keeps them apart.

`create_all` (dev) and Alembic (everywhere else) both write DDL. Exactly one may own a given
database. When that rule was implicit, a dev database created by `create_all` carried no
migration history, so the next `alembic upgrade head` replayed from the initial revision and
died on "table agents already exists" -- which reads as a corrupt database rather than as two
tools that never spoke to each other.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from app.db.base import Base
from app.services.bootstrap import _create_or_defer_to_alembic

ALEMBIC_DIR = str(Path(__file__).resolve().parents[1] / "alembic")


def _head() -> str:
    return ScriptDirectory(ALEMBIC_DIR).get_current_head() or ""


def _revision_of(connection) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


@pytest.fixture
def sqlite(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'schema.db'}")
    yield engine
    engine.dispose()


class TestDevPath:
    def test_creating_the_schema_records_the_revision(self, sqlite):
        """Without this the database looks empty to Alembic while every table exists."""
        with sqlite.begin() as connection:
            _create_or_defer_to_alembic(connection)
        with sqlite.connect() as connection:
            assert _revision_of(connection) == _head()
            assert "agents" in inspect(connection).get_table_names()

    def test_running_it_twice_is_harmless(self, sqlite):
        for _ in range(2):
            with sqlite.begin() as connection:
                _create_or_defer_to_alembic(connection)
        with sqlite.connect() as connection:
            assert _revision_of(connection) == _head()


class TestAlembicOwnedDatabase:
    def _stamp(self, engine, revision: str) -> None:
        with engine.begin() as connection:
            MigrationContext.configure(connection).stamp(ScriptDirectory(ALEMBIC_DIR), revision)

    def test_an_existing_history_is_never_overwritten(self, sqlite):
        """Stamping head over a half-migrated database would claim migrations that never ran."""
        Base.metadata.create_all(sqlite)  # tables exist, but the history says otherwise
        self._stamp(sqlite, "937904a33105")

        with sqlite.begin() as connection:
            _create_or_defer_to_alembic(connection)

        with sqlite.connect() as connection:
            assert _revision_of(connection) == "937904a33105"

    def test_it_creates_nothing_when_alembic_is_in_charge(self, sqlite):
        """Adding a table ahead of the recorded revision breaks the *next* upgrade, not this
        run, so the damage surfaces a release later."""
        self._stamp(sqlite, "937904a33105")
        with sqlite.begin() as connection:
            _create_or_defer_to_alembic(connection)
        with sqlite.connect() as connection:
            assert inspect(connection).get_table_names() == ["alembic_version"]


class TestStampedButIncomplete:
    """`alembic stamp head` on a database built before a migration existed.

    The stamp claims every migration ran, so `upgrade head` has nothing left to do and the
    table it would have created never appears. Nothing else creates it either, and the
    failure surfaces much later as a 500 on the first query -- which reads as a broken
    feature rather than as a schema that was told it was finished.
    """

    def _stamp(self, engine, revision: str) -> None:
        with engine.begin() as connection:
            MigrationContext.configure(connection).stamp(ScriptDirectory(ALEMBIC_DIR), revision)

    def test_a_table_missing_at_head_is_created(self, sqlite):
        Base.metadata.create_all(sqlite)
        with sqlite.begin() as connection:
            connection.exec_driver_sql("DROP TABLE platform_settings")
        self._stamp(sqlite, _head())

        with sqlite.begin() as connection:
            _create_or_defer_to_alembic(connection)

        with sqlite.connect() as connection:
            assert "platform_settings" in inspect(connection).get_table_names()
            assert _revision_of(connection) == _head()

    def test_a_complete_schema_at_head_is_left_alone(self, sqlite):
        Base.metadata.create_all(sqlite)
        self._stamp(sqlite, _head())
        before = set(inspect(sqlite).get_table_names())

        with sqlite.begin() as connection:
            _create_or_defer_to_alembic(connection)

        assert set(inspect(sqlite).get_table_names()) == before

    def test_a_pending_migration_still_defers(self, sqlite):
        """Below head the migrations will build it; creating it here breaks the upgrade."""
        self._stamp(sqlite, "937904a33105")
        with sqlite.begin() as connection:
            _create_or_defer_to_alembic(connection)
        with sqlite.connect() as connection:
            assert inspect(connection).get_table_names() == ["alembic_version"]
