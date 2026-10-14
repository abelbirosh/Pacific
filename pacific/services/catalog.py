"""What the platform sells and for how much. Seeded into the exchange's registry."""

from __future__ import annotations

from ..pricing import PricePlan as P

PLATFORM_SERVICES: dict[str, dict] = {
    "ocean_brave": {
        "description": "Budget search over Wikipedia and StackOverflow. Titles, URLs and snippets only. Fast, shallow, unranked.",
        "endpoints": {
            "search": ("Search returning up to 10 results with snippets.", P(0.5)),
        },
    },
    "ocean_tavily": {
        "description": "Mid-tier search over 4 sources (Wikipedia, StackOverflow, GitHub, HN). Reranks, reads each result, returns relevant passages and an extractive answer.",
        "endpoints": {
            "search": ("Search and read up to 5 pages; billed per page read.",
                       P(1.0, "page", 0.5, max_units=5, units_param="max_results")),
            "extract": ("Fetch and clean the text of up to 5 URLs.", P(0.2, "page", 0.4, max_units=5, units_from="count:urls")),
        },
    },
    "ocean_exa": {
        "description": "Premium research search over 8 sources (adds MDN, npm, arXiv, Bing). Quality priors, relevance filtering, source-aware page reading and content reranking with highlights.",
        "endpoints": {
            "search": ("Multi-source search with page contents and highlights; billed per page read.",
                       P(3.0, "page", 0.8, max_units=10, units_param="num_results")),
            "contents": ("Full cleaned text of up to 10 URLs, with optional query highlights.",
                         P(0.2, "page", 0.5, max_units=10, units_from="count:urls")),
        },
    },
    "ocean_db": {
        "description": "Managed SQLite databases, isolated per workspace.",
        "endpoints": {
            "create": ("Create a new empty database.", P(5.0)),
            "execute": ("Run DDL/DML (one or more statements) in a transaction; billed per row changed.",
                        P(0.2, "row", 0.0005, max_units=2_000)),
            "execute_many": ("Run one parameterised statement for many rows (bulk insert).",
                             P(0.2, "row", 0.0005, max_units=10_000, units_from="count:rows")),
            "query": ("Read-only SELECT; billed per row returned.", P(0.1, "row", 0.001, max_units=1000, units_param="limit")),
            "schema": ("Tables, columns and row counts.", P(0.05)),
            "list": ("List databases in this workspace.", P(0.01)),
        },
    },
    "ocean_shield": {
        "description": "Software security: static analysis (bandit + secret and pattern rules) and dependency audits against OSV.",
        "endpoints": {
            "scan_code": ("Scan source files for vulnerabilities and leaked secrets; billed per 1,000 lines.",
                          P(2.0, "kloc", 1.5, max_units=50, units_from="kloc:files")),
            "audit_deps": ("Audit pinned requirements.txt entries for known CVEs; billed per package.",
                           P(1.0, "package", 0.25, max_units=200, units_from="lines:requirements")),
        },
    },
    "ocean_sandbox": {
        "description": "Run Python code with time/CPU limits and get stdout, stderr and created files back.",
        "endpoints": {
            "run": ("Execute a Python program; billed per second of runtime.",
                    P(0.25, "second", 0.5, max_units=30, units_param="timeout_s")),
        },
    },
    "ocean_storage": {
        "description": "Object storage for documents, reports and build artifacts, isolated per workspace.",
        "endpoints": {
            "put": ("Store a text object at a key; billed per KB.",
                    P(0.05, "kb", 0.01, max_units=2048, units_from="kb:content")),
            "get": ("Read an object.", P(0.05)),
            "list": ("List object keys under a prefix.", P(0.01)),
            "delete": ("Delete an object.", P(0.01)),
        },
    },
}


def request_schemas(app) -> dict[str, dict]:
    """Map '/service/endpoint' to its JSON request schema, read from the app's OpenAPI spec."""
    spec = app.openapi()
    components = spec.get("components", {}).get("schemas", {})

    def resolve(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(components[node["$ref"].rsplit("/", 1)[-1]])
            return {k: resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    out = {}
    for path, ops in spec["paths"].items():
        body = ops.get("post", {}).get("requestBody", {}).get("content", {}).get("application/json", {})
        out[path] = resolve(body.get("schema", {"type": "object", "properties": {}}))
    return out
