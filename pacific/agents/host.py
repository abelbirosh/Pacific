"""Hosts the agents. The exchange calls in here to run a task or to perform a delegated subtask."""


import secrets as pysecrets
import traceback
from dataclasses import asdict

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from ..config import Config
from .brain import AgentContext, Task, describe_result, make_brain
from .client import ExchangeClient
from .tools import Toolbox


def create_app(config: Config) -> FastAPI:
    secrets = config.secrets()
    agents: dict[str, dict] = secrets["agents"]
    app = FastAPI(title="Pacific agent host")

    def require_gateway(x_pacific_gateway_secret: str = Header(default="")):
        if not pysecrets.compare_digest(x_pacific_gateway_secret, secrets["gateway_secret"]):
            raise HTTPException(401, "only the exchange may dispatch work to agents")

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
        client = ExchangeClient(config.exchange_url, agent["key"], job_id=work.job_id)
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

    @app.post("/agents/{agent_id}/run", dependencies=[Depends(require_gateway)])
    def run(agent_id: str, work: Work):
        return execute(agent_id, work, delegated=False)

    @app.post("/agents/{agent_id}/delegate", dependencies=[Depends(require_gateway)])
    def delegate(agent_id: str, work: Work):
        return execute(agent_id, work, delegated=True)

    return app
