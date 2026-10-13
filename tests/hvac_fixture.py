"""TEST DATA: a hand-made HVAC layer for the sample apartment (not extracted from any drawing).

One ducted fan coil above the living room ceiling feeds two diffusers (living room, kitchen) through a
duct and takes its air back through one return grille; a wall split unit cools the bedroom.
"""

from __future__ import annotations

from twin.fixtures import load_sample_apartment
from twin.scene.model import Certainty, Duct, Hvac, HvacTerminal, HvacUnit, Scene, Size3

C = Certainty(status="confirmed", source="user")


def sample_hvac() -> Hvac:
    return Hvac(
        units=[HvacUnit(id="U_FCU-1", tag="FCU-1", kind="fcu_ducted", level_id="L0", room_id="R1", position=(3.0, 6.5),
                        rotation_deg=0, size=Size3(width=1.2, depth=0.7, height=0.3), airflow_lps=400, certainty=C),
               HvacUnit(id="U_SPLIT-1", tag="SPLIT-1", kind="split_wall", level_id="L0", room_id="R3", position=(2.0, 3.35),
                        rotation_deg=0, size=Size3(width=0.9, depth=0.25, height=0.3), airflow_lps=150, certainty=C)],
        terminals=[HvacTerminal(id="SD1", kind="supply_diffuser", level_id="L0", room_id="R1", position=(2.0, 5.0), width=0.6,
                                depth=0.6, airflow_lps=250, unit_id="U_FCU-1", certainty=C),
                   HvacTerminal(id="SD2", kind="supply_diffuser", level_id="L0", room_id="R2", position=(8.2, 5.8), width=0.6,
                                depth=0.6, airflow_lps=150, unit_id="U_FCU-1", certainty=C),
                   HvacTerminal(id="RG1", kind="return_grille", level_id="L0", room_id="R1", position=(4.6, 6.5), width=1.0,
                                depth=0.4, unit_id="U_FCU-1", certainty=C)],
        ducts=[Duct(id="D1", system="supply", level_id="L0", path=[(3.6, 6.5), (6.0, 6.5), (8.2, 6.5), (8.2, 5.8)],
                    width=0.4, height=0.25, unit_id="U_FCU-1", certainty=C)],
    )


def sample_scene_with_hvac() -> Scene:
    s = load_sample_apartment()
    return Scene.model_validate({**s.model_dump(mode="json"), "hvac": sample_hvac().model_dump(mode="json")})
