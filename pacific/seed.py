"""Genesis: mint the supply, create the platform, people, agents and the service catalog."""

from __future__ import annotations

import json
import random
import secrets as pysecrets
import shutil

from .agents.brain import SKILL_DESCRIPTIONS, SKILLS, make_brain
from .agents.personas import DEFAULT_MIX, PERSONAS
from .config import Config
from .db import Database
from .ids import new_api_key
from .ledger import Ledger
from .money import to_micro
from .names import agent_name, person_name
from .promises import SKILL_MILESTONES, SKILL_PROMISES
from .registry import put_promise, register_agent_offering, register_service
from .services import app as services_app
from .services.catalog import PLATFORM_SERVICES, request_schemas


def genesis(config: Config, seed: int | None = None, supply: float = 1_000_000, n_agents: int = 5,
            force: bool = False, brain: str = "scripted") -> dict:
    """`brain` is given to every agent ("scripted", "sim:frugal"...); plain "sim" deals the
    personas out so the market has a mix of them, and "sim:a,b,c" deals out just those."""
    dealt = list(DEFAULT_MIX) if brain == "sim" else None
    if brain.startswith("sim:") and "," in brain:
        dealt = [p.strip() for p in brain[4:].split(",") if p.strip()]
        if unknown := [p for p in dealt if p not in PERSONAS]:
            raise ValueError(f"unknown personas {unknown}; available: {sorted(PERSONAS)}")
    if dealt is None:
        make_brain(brain)  # fail fast on a bad name
    if config.home.exists():
        if not force:
            raise RuntimeError(f"{config.home} already exists; pass --force to wipe it and start over")
        shutil.rmtree(config.home)
    config.home.mkdir(parents=True)
    rng = random.Random(seed)
    # Promise terms draw from their own stream, so a seed gives the same people, agents and prices
    # whatever the promises add.
    terms_rng = random.Random(None if seed is None else f"promises-{seed}")

    db = Database(config.ledger_path)
    ledger = Ledger(db)
    secrets = {"admin_key": new_api_key(), "gateway_secret": pysecrets.token_urlsafe(32), "people": {}, "agents": {}}
    schemas = request_schemas(services_app.create_app(config.data_dir, "unused"))

    with db.tx() as conn:
        db_meta = conn.execute
        db_meta("INSERT INTO meta(key, value) VALUES ('seed', ?)", (str(seed),))

        treasury, _ = ledger.create_account(conn, "treasury", "Pacific Treasury", with_key=False)
        ledger.mint(conn, treasury, to_micro(supply), "genesis supply")

        platform, _ = ledger.create_account(conn, "platform", "Pacific Platform", with_key=False)
        ledger.escrow_account(conn)
        for name, spec in PLATFORM_SERVICES.items():
            acct, _ = ledger.create_account(conn, "service", name, owner_id=platform, with_key=False)
            endpoints = {
                ep: (desc, f"/{name}/{ep}", plan, schemas.get(f"/{name}/{ep}", {}))
                for ep, (desc, plan) in spec["endpoints"].items()
            }
            register_service(conn, name, "platform", acct, spec["description"], config.services_url, endpoints)

        # an explicit list is dealt in order; the full roster is shuffled
        personas = dealt if brain != "sim" else rng.sample(dealt, len(dealt))
        used_names: set[str] = set()
        for i in range(n_agents):
            agent_brain = f"sim:{personas[i % len(personas)]}" if dealt is not None else brain
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
                offering = register_agent_offering(conn, agent, aname, skill, SKILL_DESCRIPTIONS[skill], fee,
                                                   markup, config.agents_url)
                offerings.append(offering)
                # standard is the listed price; thorough lets the buyer demand more, for a bigger premium
                premiums = {"standard": (fee, markup),
                            "thorough": (round(fee * terms_rng.uniform(1.6, 2.4), 2), markup + terms_rng.choice([5, 10, 15]))}
                # each seller decides whether its thorough tier is paid on completion or by milestone
                progress = skill in SKILL_MILESTONES and terms_rng.random() < 0.5
                for tier, (deliverables, checks, cost_cap, turnaround_s) in SKILL_PROMISES[skill].items():
                    by_milestone = tier == "thorough" and progress
                    put_promise(conn, offering, "delegate", tier, *premiums[tier], cost_cap, turnaround_s,
                                deliverables, checks, "progress" if by_milestone else "completion",
                                SKILL_MILESTONES[skill] if by_milestone else None)
            secrets["agents"][agent] = {"name": aname, "key": akey, "owner": person, "skills": skills,
                                        "brain": agent_brain, "offerings": offerings}

    config.secrets_path.write_text(json.dumps(secrets, indent=2))
    config.secrets_path.chmod(0o600)
    audit = ledger.audit()
    db.close()
    return {"secrets": secrets, "audit": audit}
