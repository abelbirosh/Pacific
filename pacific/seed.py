"""Genesis: mint the supply, create the platform, people, agents and the service catalog."""

from __future__ import annotations

import json
import random
import shutil

from .agents.brain import SKILL_DESCRIPTIONS, SKILLS, make_brain
from .agents.personas import PERSONAS
from .config import Config
from .db import Database
from .grants import ensure_keys
from .ids import new_api_key
from .ledger import Ledger
from .money import to_micro
from .names import agent_name, person_name
from .registry import register_agent_offering, register_service
from .services import app as services_app
from .services.catalog import PLATFORM_SERVICES, request_schemas


def genesis(config: Config, seed: int | None = None, supply: float = 1_000_000, n_agents: int = 5,
            force: bool = False, brain: str = "scripted") -> dict:
    """`brain` is given to every agent ("scripted", "sim:frugal"...); plain "sim" deals the
    personas out so the market has a mix of them."""
    if brain != "sim":
        make_brain(brain)  # fail fast on a bad name
    if config.home.exists():
        if not force:
            raise RuntimeError(f"{config.home} already exists; pass --force to wipe it and start over")
        shutil.rmtree(config.home)
    config.home.mkdir(parents=True)
    rng = random.Random(seed)

    db = Database(config.ledger_path)
    ledger = Ledger(db)
    secrets = {"admin_key": new_api_key(), "people": {}, "agents": {}}
    ensure_keys(config.home)  # the exchange signs every call it forwards with this key
    schemas = request_schemas(services_app.create_app(config.data_dir, None))

    with db.tx() as conn:
        db_meta = conn.execute
        db_meta("INSERT INTO meta(key, value) VALUES ('seed', ?)", (str(seed),))

        treasury, _ = ledger.create_account(conn, "treasury", "Pacific Treasury", with_key=False)
        ledger.mint(conn, treasury, to_micro(supply), "genesis supply")

        platform, _ = ledger.create_account(conn, "platform", "Pacific Platform", with_key=False)
        for name, spec in PLATFORM_SERVICES.items():
            acct, _ = ledger.create_account(conn, "service", name, owner_id=platform, with_key=False)
            endpoints = {
                ep: (desc, f"/{name}/{ep}", plan, schemas.get(f"/{name}/{ep}", {}))
                for ep, (desc, plan) in spec["endpoints"].items()
            }
            register_service(conn, name, "platform", acct, spec["description"], config.services_url, endpoints)

        personas = rng.sample(sorted(PERSONAS), len(PERSONAS))
        used_names: set[str] = set()
        for i in range(n_agents):
            agent_brain = f"sim:{personas[i % len(personas)]}" if brain == "sim" else brain
            pname = person_name(rng)
            while pname in used_names:
                pname = person_name(rng)
            used_names.add(pname)
            person, pkey = ledger.create_account(conn, "person", pname, profile={"generated": True})
            grant = rng.randrange(2_000, 5_001, 100)
            ledger.transfer(conn, treasury, person, to_micro(grant), "genesis grant")
            secrets["people"][person] = {"name": pname, "key": pkey}

            aname = agent_name(rng)
            while aname in used_names:
                aname = agent_name(rng)
            used_names.add(aname)
            skills = sorted(rng.sample(SKILLS, rng.randint(2, 4)))
            agent, akey = ledger.create_account(conn, "agent", aname, owner_id=person,
                                                profile={"skills": skills, "brain": agent_brain})
            stake = rng.randrange(400, 1_201, 50)
            ledger.transfer(conn, person, agent, to_micro(stake), f"{pname} funds {aname}")
            offerings = []
            for skill in skills:
                fee = round(rng.uniform(0.5, 4.0), 2)
                markup = float(rng.choice([5, 10, 15, 20, 25]))
                offerings.append(register_agent_offering(conn, agent, aname, skill, SKILL_DESCRIPTIONS[skill], fee,
                                                         markup, config.agents_url))
            secrets["agents"][agent] = {"name": aname, "key": akey, "owner": person, "skills": skills,
                                        "brain": agent_brain, "offerings": offerings}

    config.secrets_path.write_text(json.dumps(secrets, indent=2))
    config.secrets_path.chmod(0o600)
    audit = ledger.audit()
    db.close()
    return {"secrets": secrets, "audit": audit}
