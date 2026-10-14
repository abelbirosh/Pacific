"""ocean_shield: real security scanning.

scan_code   bandit static analysis on Python files, plus secret detection and a handful of
            dangerous-pattern rules for JS/TS/shell on every file.
audit_deps  checks pinned Python requirements against the OSV vulnerability database.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import sys
import tempfile
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .common import ok

SECRET_RULES = [
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "HIGH"),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), "HIGH"),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "HIGH"),
    ("stripe-secret-key", re.compile(r"\bsk_live_[A-Za-z0-9]{16,}\b"), "HIGH"),
    ("anthropic-api-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"), "HIGH"),
    ("openai-api-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b"), "HIGH"),
    ("private-key-block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"), "HIGH"),
    (
        "hardcoded-credential",
        re.compile(r"(?i)\b(password|passwd|secret|api_?key|token)\s*[:=]\s*['\"][^'\"\s]{8,}['\"]"),
        "MEDIUM",
    ),
]

PATTERN_RULES = [
    ("js-eval", re.compile(r"\beval\s*\("), "MEDIUM", (".js", ".ts", ".jsx", ".tsx")),
    ("js-innerhtml", re.compile(r"\.innerHTML\s*="), "MEDIUM", (".js", ".ts", ".jsx", ".tsx")),
    ("js-child-process-exec", re.compile(r"child_process.*\bexec\(|\bexecSync\("), "HIGH", (".js", ".ts")),
    ("react-dangerous-html", re.compile(r"dangerouslySetInnerHTML"), "MEDIUM", (".jsx", ".tsx")),
    ("sql-string-concat", re.compile(r"(?i)\b(select|insert|update|delete)\b[^;\n]*['\"]\s*\+\s*\w"), "MEDIUM", None),
    ("shell-curl-pipe-sh", re.compile(r"curl[^|\n]*\|\s*(sudo\s+)?(ba)?sh"), "HIGH", (".sh", ".bash", "Dockerfile")),
]

SEVERITIES = ("HIGH", "MEDIUM", "LOW")


def _rule_findings(path: str, content: str) -> list[dict]:
    out = []
    for i, line in enumerate(content.splitlines(), 1):
        for name, rx, sev in SECRET_RULES:
            if rx.search(line):
                out.append({"file": path, "line": i, "rule": name, "severity": sev, "tool": "pacific-secrets",
                            "message": f"possible {name.replace('-', ' ')} committed in source"})
        for name, rx, sev, exts in PATTERN_RULES:
            if (exts is None or path.endswith(exts)) and rx.search(line):
                out.append({"file": path, "line": i, "rule": name, "severity": sev, "tool": "pacific-patterns",
                            "message": name.replace("-", " ")})
    return out


async def _bandit(root: Path) -> tuple[list[dict], str | None]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "bandit", "-r", str(root), "-f", "json", "-q",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
    try:
        report = json.loads(out or b"{}")
    except json.JSONDecodeError:
        return [], (err or out).decode()[:300]
    findings = [
        {
            "file": str(Path(r["filename"]).relative_to(root)),
            "line": r["line_number"],
            "rule": f"{r['test_id']}:{r['test_name']}",
            "severity": r["issue_severity"],
            "confidence": r["issue_confidence"],
            "tool": "bandit",
            "message": r["issue_text"],
            "cwe": (r.get("issue_cwe") or {}).get("id"),
        }
        for r in report.get("results", [])
    ]
    return findings, None


def _summary(findings: list[dict]) -> dict:
    counts = {s: sum(f["severity"] == s for f in findings) for s in SEVERITIES}
    return {"counts": counts, "total": len(findings), "passed": counts["HIGH"] == 0}


router = APIRouter(prefix="/ocean_shield")


class ScanCode(BaseModel):
    files: dict[str, str] = Field(min_length=1, max_length=500, description="path -> file content")


@router.post("/scan_code")
async def scan_code(req: ScanCode):
    lines = 0
    findings: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="shield_") as tmp:
        root = Path(tmp)
        for rel, content in req.files.items():
            clean = Path(rel.lstrip("/"))
            if ".." in clean.parts:
                raise HTTPException(400, f"bad path {rel!r}")
            dest = root / clean
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)
            lines += content.count("\n") + 1
            findings += _rule_findings(str(clean), content)
        bandit_error = None
        if any(p.endswith(".py") for p in req.files):
            bandit_findings, bandit_error = await _bandit(root)
            findings += bandit_findings
    findings.sort(key=lambda f: (SEVERITIES.index(f["severity"]) if f["severity"] in SEVERITIES else 9, f["file"], f["line"]))
    kloc = max(0.1, math.ceil(lines / 100) / 10)
    return ok({"files": len(req.files), "lines": lines, **_summary(findings), "findings": findings,
               "bandit_error": bandit_error}, units=kloc)


class AuditDeps(BaseModel):
    requirements: str = Field(min_length=1, max_length=100_000, description="requirements.txt contents")


_REQ = re.compile(r"^\s*([A-Za-z0-9_.\-\[\]]+)\s*==\s*([A-Za-z0-9_.\-+!]+)")


@router.post("/audit_deps")
async def audit_deps(req: AuditDeps):
    pinned, findings = [], []
    for raw in req.requirements.splitlines():
        line = raw.split("#")[0].strip()
        if not line or line.startswith("-"):
            continue
        m = _REQ.match(line)
        if m:
            pinned.append((re.sub(r"\[.*\]", "", m.group(1)).lower(), m.group(2)))
        else:
            findings.append({"package": line, "severity": "LOW", "rule": "unpinned-dependency",
                             "message": "not pinned to an exact version; cannot be audited reproducibly"})
    if len(pinned) > 200:
        raise HTTPException(400, "at most 200 pinned packages per audit")
    async with httpx.AsyncClient(timeout=15) as http:
        if pinned:
            resp = await http.post("https://api.osv.dev/v1/querybatch", json={"queries": [
                {"package": {"name": n, "ecosystem": "PyPI"}, "version": v} for n, v in pinned]})
            if resp.status_code != 200:
                raise HTTPException(502, f"OSV returned {resp.status_code}")
            vuln_ids = [[v["id"] for v in r.get("vulns", [])] for r in resp.json()["results"]]
            details = {}
            unique = sorted({i for ids in vuln_ids for i in ids})[:40]
            for vid, d in zip(unique, await asyncio.gather(*(http.get(f"https://api.osv.dev/v1/vulns/{i}") for i in unique))):
                details[vid] = d.json() if d.status_code == 200 else {}
            for (name, ver), ids in zip(pinned, vuln_ids):
                for vid in ids:
                    d = details.get(vid, {})
                    sev = (d.get("database_specific") or {}).get("severity", "").upper()
                    fixed = sorted({e["fixed"] for a in d.get("affected", []) for r in a.get("ranges", [])
                                    for e in r.get("events", []) if "fixed" in e})
                    findings.append({
                        "package": f"{name}=={ver}", "id": vid, "aliases": d.get("aliases", []),
                        "severity": {"CRITICAL": "HIGH", "HIGH": "HIGH", "MODERATE": "MEDIUM", "LOW": "LOW"}.get(sev, "MEDIUM"),
                        "rule": "known-vulnerability", "message": d.get("summary", "known vulnerability"),
                        "fixed_in": fixed[:3],
                    })
    order = {s: i for i, s in enumerate(SEVERITIES)}
    findings.sort(key=lambda f: order.get(f["severity"], 9))
    return ok({"packages_checked": len(pinned), **_summary(findings), "findings": findings}, units=len(pinned))
