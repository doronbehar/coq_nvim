from functools import cache
from pathlib import Path
from sqlite3.dbapi2 import Connection
from typing import Protocol, cast

from std2.pathlib import AnyPath
from std2.sqllite3 import add_functions, escape

from .parse import similarity


class _Loader(Protocol):
    def __call__(self, *paths: AnyPath) -> str:
        ...


def loader(base: Path) -> _Loader:
    def cont(*paths: AnyPath) -> str:
        path = (base / Path(*paths)).with_suffix(".sql")
        return path.read_text("UTF-8")

    return cast(_Loader, cache(cont))


def _like_esc(like: str) -> str:
    escaped = escape(nono={"%", "_", "["}, escape="!", param=like)
    return f"{escaped}%"


def init_db(conn: Connection) -> None:
    add_functions(conn)
    conn.create_function("X_LIKE_ESC", narg=1, func=_like_esc, deterministic=True)
    conn.create_function("X_SIMILARITY", narg=2, func=similarity, deterministic=True)

