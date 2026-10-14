"""ocean_storage: a real file store, namespaced per workspace."""


import math
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .common import ok, safe_name, workspace, workspace_dir

MAX_BYTES = 2 * 1024 * 1024


def kb(n_bytes: int) -> float:
    return max(1, math.ceil(n_bytes / 1024))


def router(data_dir: Path) -> APIRouter:
    r = APIRouter(prefix="/ocean_storage")

    def key_path(ws: str, key: str) -> Path:
        parts = key.strip("/").split("/")
        if not key or len(parts) > 8:
            raise HTTPException(400, "key must be 1-8 path segments")
        return workspace_dir(data_dir, "storage", ws).joinpath(*(safe_name(p, "key segment") for p in parts))

    class Put(BaseModel):
        key: str
        content: str = Field(max_length=MAX_BYTES)

    @r.post("/put")
    def put(req: Put, ws: str = Depends(workspace)):
        path = key_path(ws, req.key)
        data = req.content.encode()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return ok({"key": req.key, "bytes": len(data)}, units=kb(len(data)))

    class Get(BaseModel):
        key: str

    @r.post("/get")
    def get(req: Get, ws: str = Depends(workspace)):
        path = key_path(ws, req.key)
        if not path.is_file():
            raise HTTPException(404, f"no object at {req.key!r}")
        data = path.read_bytes()
        return ok({"key": req.key, "bytes": len(data), "content": data.decode(errors="replace")}, units=kb(len(data)))

    class List(BaseModel):
        prefix: str = ""

    @r.post("/list")
    def list_keys(req: List, ws: str = Depends(workspace)):
        root = workspace_dir(data_dir, "storage", ws)
        keys = [
            {"key": str(p.relative_to(root)), "bytes": p.stat().st_size}
            for p in sorted(root.rglob("*"))
            if p.is_file() and str(p.relative_to(root)).startswith(req.prefix)
        ]
        return ok({"objects": keys})

    @r.post("/delete")
    def delete(req: Get, ws: str = Depends(workspace)):
        path = key_path(ws, req.key)
        existed = path.is_file()
        if existed:
            path.unlink()
        return ok({"key": req.key, "deleted": existed})

    return r
