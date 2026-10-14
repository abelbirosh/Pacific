"""Random names for people and agents."""

from __future__ import annotations

import random

FIRST = ["Amara", "Bodhi", "Camila", "Dario", "Elif", "Farid", "Greta", "Hiro", "Ines", "Jonah", "Kaia", "Luca",
         "Maren", "Nikhil", "Odette", "Pavel", "Quinn", "Rosa", "Soren", "Tala", "Uma", "Viktor", "Wren", "Ximena",
         "Yusuf", "Zadie", "Ayo", "Bea", "Cyrus", "Dalia", "Emeka", "Freya", "Gael", "Hana", "Idris", "Juno"]
LAST = ["Okafor", "Lindqvist", "Moreau", "Tanaka", "Haddad", "Novak", "Castillo", "Mbeki", "Petrov", "Sato",
        "Achebe", "Ferreira", "Kowalski", "Nakamura", "Oyelaran", "Quispe", "Rahman", "Silva", "Varga", "Whitlock",
        "Yilmaz", "Zhou", "Abernathy", "Bianchi", "Delacroix", "Eriksen", "Gallagher", "Iyer", "Johansson", "Keane"]

_ONSET = ["b", "d", "f", "g", "k", "l", "m", "n", "p", "r", "s", "t", "v", "z", "br", "dr", "kr", "st", "tr", "sh"]
_VOWEL = ["a", "e", "i", "o", "u", "a", "e", "o"]
_CODA = ["", "", "", "n", "r", "s", "x", "l"]


def person_name(rng: random.Random) -> str:
    return f"{rng.choice(FIRST)} {rng.choice(LAST)}"


def agent_name(rng: random.Random) -> str:
    syllables = rng.choice([2, 2, 2, 3])
    name = "".join(rng.choice(_ONSET) + rng.choice(_VOWEL) for _ in range(syllables)) + rng.choice(_CODA)
    return name.capitalize()
