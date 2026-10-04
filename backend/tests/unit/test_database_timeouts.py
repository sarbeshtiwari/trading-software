"""Database deadlines are configurable, bounded and applied to real driver options."""

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.db.session import _engine_kwargs


@pytest.mark.parametrize("field", [
    "database_connect_timeout_seconds", "database_command_timeout_seconds",
    "database_pool_timeout_seconds", "database_session_timeout_seconds",
])
@pytest.mark.parametrize("value", [0, -1, 121, float("inf"), float("nan")])
def test_invalid_deadline_is_rejected(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_driver_deadlines_and_sqlite_compatibility():
    settings = Settings(_env_file=None, database_url="postgresql+asyncpg://localhost/test",
                        database_connect_timeout_seconds=2,
                        database_command_timeout_seconds=3, database_pool_timeout_seconds=4)
    options = _engine_kwargs(settings)
    assert options["connect_args"] == {"timeout": 2, "command_timeout": 3}
    assert options["pool_timeout"] == 4
    assert options["pool_pre_ping"] is True
    assert _engine_kwargs(settings.model_copy(update={"database_url": "sqlite+aiosqlite://"})) == {
        "echo": settings.database_echo,
    }
