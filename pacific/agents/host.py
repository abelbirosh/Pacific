"""Hosts the agents. The exchange calls in here to run a task or to perform a delegated subtask."""


import os
import traceback
from dataclasses import asdict

from anyio import CapacityLimiter, to_thread

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from .. import faults
from ..auth import Credentials, DPoPAuth
from ..config import Config
from ..grants import Verifier, install as gate, require_grant
from ..idempotency import ReplayCache
from .brain import AgentContext, Task, describe_result, make_brain
from .client import ExchangeClient
from .tools import Toolbox


def create_app(config: Config) -> FastAPI:
    secrets = config.secrets()
    agents: dict[str, dict] = secrets["agents"]
    app = FastAPI(title="Pacific agent host")
    gate(app, Verifier.load(config.home), ReplayCache())  # only the exchange may dispatch work to agents
    faults.install(app, faults.faults_path(config.home))  # after the gate, so it wraps it

    class Work(BaseModel):
        task: dict
        job_id: str
        buyer_id: str | None = None
        skill: str | None = None

    def execute(agent_id: str, work: Work, delegated: bool) -> dict:
        agent = agents.get(agent_id)
        if agent is None:
            raise HTTPException(404, f"no agent {agent_id}")
        task = Task.from_dict(work.task)
        if delegated:
            task.skill = task.skill or work.skill
            task.title = task.title or f"subtask for {work.buyer_id}"
        # The brain only ever holds a short-lived token, bound to the agent's key and limited to
        # this job; a hired agent can't touch its wallet (no transfer scope).
        scope = "pacific:read pacific:spend pacific:hire" + ("" if delegated else " pacific:transfer")
        auth = DPoPAuth(Credentials.load(config.home, agent_id), scope=scope, resource=f"job:{work.job_id}")
        client = ExchangeClient(config.exchange_url, job_id=work.job_id, auth=auth)
        tools = Toolbox(client, config.home / "agents" / agent_id / "notes.json", agent_id,
                        chain=list(task.inputs.get("_chain", [])))
        ctx = AgentContext(agent_id, agent["name"], agent["skills"], tools)
        try:
            result = describe_result(make_brain(agent["brain"]).run(task, ctx))
        except Exception as e:
            result = {"status": "failed", "output": f"agent crashed: {type(e).__name__}: {e}",
                      "detail": traceback.format_exc()[-1500:]}
        finally:
            client.close()
        return {**result, "agent": agent["name"], "trace": [asdict(t) for t in tools.trace]}

    @app.get("/agents")
    def list_agents():
        return {"agents": [{"id": i, "name": a["name"], "skills": a["skills"], "brain": a["brain"]}
                           for i, a in agents.items()]}

    # Brains are synchronous, so each runs on a worker thread. A brain at delegation depth d
    # only ever waits on brains at depth d+1, so each depth gets its own threads: a busy level
    # can never starve the level it is waiting on. (One shared pool deadlocks once every thread
    # holds a task that is waiting for a hire.)
    threads_per_level = int(os.environ.get("PACIFIC_AGENT_THREADS", 64))
    levels: dict[int, CapacityLimiter] = {}

    async def in_thread(level: int, *args):
        if level not in levels:
            levels[level] = CapacityLimiter(threads_per_level)
        return await to_thread.run_sync(execute, *args, limiter=levels[level])

    @app.post("/agents/{agent_id}/run", dependencies=[Depends(require_grant)])
    async def run(agent_id: str, work: Work):
        return await in_thread(0, agent_id, work, False)

    @app.post("/agents/{agent_id}/delegate", dependencies=[Depends(require_grant)])
    async def delegate(agent_id: str, work: Work):
        depth = len((work.task.get("inputs") or {}).get("_chain", [])) or 1
        return await in_thread(depth, agent_id, work, True)

    return app
