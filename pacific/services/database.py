"""ocean_db: real SQLite databases, provisioned per workspace."""


import sqlite3
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .common import ok, safe_name, workspace, workspace_dir

MAX_ROWS = 1000
STATEMENT_SECONDS = 5.0


def router(data_dir: Path) -> APIRouter:
    r = APIRouter(prefix="/ocean_db")

    def db_path(ws: str, name: str) -> Path:
        return workspace_dir(data_dir, "db", ws) / f"{safe_name(name, 'database name')}.sqlite"

    def connect(path: Path, readonly: bool = False) -> sqlite3.Connection:
        if not path.exists():
            raise HTTPException(404, f"database {path.stem!r} does not exist; call create first")
        uri = f"file:{path}?mode=ro" if readonly else f"file:{path}"
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
        conn.row_factory = sqlite3.Row

        # A database must never reach outside its own file.
        def authorizer(action, *_):
            return sqlite3.SQLITE_DENY if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH) else sqlite3.SQLITE_OK

        conn.set_authorizer(authorizer)
        deadline = time.monotonic() + STATEMENT_SECONDS
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        return conn

    def statements(sql: str) -> list[str]:
        out, buf = [], ""
        for piece in sql.split(";"):
            buf += piece + ";"
            if sqlite3.complete_statement(buf):
                if buf.strip(" \n\t;"):
                    out.append(buf.strip())
                buf = ""
        if buf.strip(" \n\t;"):
            raise HTTPException(400, "incomplete SQL statement")
        return out

    class Create(BaseModel):
        name: str

    @r.post("/create")
    def create(req: Create, ws: str = Depends(workspace)):
        path = db_path(ws, req.name)
        if path.exists():
            raise HTTPException(409, f"database {req.name!r} already exists")
        sqlite3.connect(path).close()
        return ok({"database": req.name, "created": True})

    class Execute(BaseModel):
        db: str
        sql: str = Field(min_length=1, max_length=200_000)
        params: list | dict | None = None

    @r.post("/execute")
    def execute(req: Execute, ws: str = Depends(workspace)):
        conn = connect(db_path(ws, req.db))
        try:
            before = conn.total_changes
            stmts = [req.sql] if req.params is not None else statements(req.sql)
            conn.execute("BEGIN")
            for s in stmts:
                conn.execute(s, req.params or ())
            conn.execute("COMMIT")
            changed = conn.total_changes - before
        except sqlite3.Error as e:
            conn.rollback()
            raise HTTPException(400, f"sql error: {e}")
        finally:
            conn.close()
        return ok({"statements": len(stmts), "rows_changed": changed}, units=changed)

    class ExecuteMany(BaseModel):
        db: str
        sql: str
        rows: list[list | dict] = Field(min_length=1, max_length=10_000)

    @r.post("/execute_many")
    def execute_many(req: ExecuteMany, ws: str = Depends(workspace)):
        conn = connect(db_path(ws, req.db))
        try:
            conn.execute("BEGIN")
            conn.executemany(req.sql, req.rows)
            conn.execute("COMMIT")
        except sqlite3.Error as e:
            conn.rollback()
            raise HTTPException(400, f"sql error: {e}")
        finally:
            conn.close()
        return ok({"rows_written": len(req.rows)}, units=len(req.rows))

    class Query(BaseModel):
        db: str
        sql: str = Field(min_length=1, max_length=50_000)
        params: list | dict | None = None
        limit: int = MAX_ROWS

    @r.post("/query")
    def query(req: Query, ws: str = Depends(workspace)):
        conn = connect(db_path(ws, req.db), readonly=True)
        try:
            cur = conn.execute(req.sql, req.params or ())
            rows = cur.fetchmany(max(1, min(req.limit, MAX_ROWS)))
            cols = [d[0] for d in cur.description or []]
        except sqlite3.Error as e:
            raise HTTPException(400, f"sql error: {e}")
        finally:
            conn.close()
        return ok({"columns": cols, "rows": [list(row) for row in rows]}, units=len(rows))

    class Schema(BaseModel):
        db: str

    @r.post("/schema")
    def schema(req: Schema, ws: str = Depends(workspace)):
        conn = connect(db_path(ws, req.db), readonly=True)
        try:
            tables = {}
            for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                cols = [
                    {"name": c["name"], "type": c["type"], "pk": bool(c["pk"]), "notnull": bool(c["notnull"])}
                    for c in conn.execute(f'PRAGMA table_info("{name}")')
                ]
                count = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                tables[name] = {"columns": cols, "rows": count}
        finally:
            conn.close()
        return ok({"database": req.db, "tables": tables})

    @r.post("/list")
    def list_dbs(ws: str = Depends(workspace)):
        d = workspace_dir(data_dir, "db", ws)
        return ok({"databases": sorted(p.stem for p in d.glob("*.sqlite"))})

    return r
