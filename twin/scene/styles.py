"""Built-in materials and design-style presets.

A scene only stores overrides; everything else resolves deterministically from the
project style: room surfaces by room type, furniture material roles by style.
Custom materials in ``scene.materials`` override built-ins with the same id.
"""

from __future__ import annotations

from twin.scene.model import Material, Room, Scene

_M = Material

BUILTIN_MATERIALS: dict[str, Material] = {m.id: m for m in [
    # floors
    _M(id="oak_planks", name="Oak planks", base_color="#B08458", roughness=0.55, pattern="wood", tile_size=2.0),
    _M(id="walnut_planks", name="Walnut planks", base_color="#6B4A33", roughness=0.5, pattern="wood", tile_size=2.0),
    _M(id="ash_planks", name="Light ash planks", base_color="#D6C2A1", roughness=0.6, pattern="wood", tile_size=2.0),
    _M(id="polished_concrete", name="Polished concrete", base_color="#9C9A96", roughness=0.35, pattern="concrete", tile_size=3.0),
    _M(id="tile_white", name="White porcelain tile", base_color="#E6E4DF", roughness=0.3, pattern="tile", tile_size=1.2),
    _M(id="tile_grey", name="Grey porcelain tile", base_color="#8D8D8B", roughness=0.35, pattern="tile", tile_size=1.2),
    _M(id="marble_floor", name="White marble", base_color="#ECE9E3", roughness=0.15, pattern="marble", tile_size=1.6),
    # walls / ceilings
    _M(id="plaster_white", name="White plaster", base_color="#F0EEE9", roughness=0.85, pattern="plaster", tile_size=2.0),
    _M(id="plaster_warm", name="Warm plaster", base_color="#E7DECF", roughness=0.85, pattern="plaster", tile_size=2.0),
    _M(id="plaster_grey", name="Grey plaster", base_color="#C8C7C3", roughness=0.85, pattern="plaster", tile_size=2.0),
    _M(id="plaster_green", name="Sage plaster", base_color="#B9C2AE", roughness=0.85, pattern="plaster", tile_size=2.0),
    _M(id="brick_red", name="Exposed brick", base_color="#8E4E3A", roughness=0.9, pattern="brick", tile_size=1.0),
    _M(id="concrete_wall", name="Board concrete", base_color="#A8A6A1", roughness=0.8, pattern="concrete", tile_size=2.5),
    _M(id="wall_tile_white", name="White wall tile", base_color="#EEEEEB", roughness=0.25, pattern="tile", tile_size=0.6),
    _M(id="render_light", name="Light exterior render", base_color="#D9D5CC", roughness=0.9, pattern="plaster", tile_size=3.0),
    _M(id="ceiling_white", name="Ceiling white", base_color="#F6F6F4", roughness=0.9),
    _M(id="slab_concrete", name="Structural slab", base_color="#7E7C78", roughness=0.9, pattern="concrete", tile_size=3.0),
    # HVAC
    _M(id="hvac_duct_supply", name="Supply duct (galvanised)", base_color="#B9C4CF", roughness=0.4, metallic=0.55,
       pattern="brushed", tile_size=1.0),
    _M(id="hvac_duct_return", name="Return duct (galvanised)", base_color="#C8BFAE", roughness=0.45, metallic=0.5,
       pattern="brushed", tile_size=1.0),
    _M(id="hvac_unit", name="Fan coil casing", base_color="#DCDDD8", roughness=0.5, metallic=0.2),
    _M(id="hvac_unit_dark", name="Fan coil panel", base_color="#4B5157", roughness=0.5, metallic=0.3),
    _M(id="hvac_terminal", name="Diffuser face", base_color="#F3F3F1", roughness=0.35),
    _M(id="hvac_terminal_core", name="Diffuser core", base_color="#8E959C", roughness=0.4, metallic=0.4),
    # openings
    _M(id="door_white", name="White door", base_color="#ECECE8", roughness=0.45),
    _M(id="door_oak", name="Oak door", base_color="#A77B4F", roughness=0.5, pattern="wood", tile_size=1.0),
    _M(id="door_black", name="Black door", base_color="#232323", roughness=0.4),
    _M(id="frame_white", name="White frame", base_color="#F2F2EF", roughness=0.4),
    _M(id="frame_black", name="Black metal frame", base_color="#1D1D1E", roughness=0.35, metallic=0.6),
    _M(id="frame_brass", name="Brass frame", base_color="#A88653", roughness=0.3, metallic=0.9),
    _M(id="window_glass", name="Window glass", base_color="#BFD6DC", roughness=0.05, opacity=0.2),
    # furniture roles
    _M(id="fabric_light", name="Light linen", base_color="#D6CFC2", roughness=0.95, pattern="fabric", tile_size=0.5),
    _M(id="fabric_grey", name="Grey wool", base_color="#7C7D80", roughness=0.95, pattern="fabric", tile_size=0.5),
    _M(id="fabric_dark", name="Charcoal fabric", base_color="#3F4044", roughness=0.95, pattern="fabric", tile_size=0.5),
    _M(id="velvet_green", name="Emerald velvet", base_color="#2D5546", roughness=0.7, pattern="fabric", tile_size=0.5),
    _M(id="leather_cognac", name="Cognac leather", base_color="#8A4F2A", roughness=0.55),
    _M(id="cushion_cream", name="Cream cushion", base_color="#EAE3D6", roughness=0.95, pattern="fabric", tile_size=0.4),
    _M(id="cushion_rust", name="Rust cushion", base_color="#B0583A", roughness=0.95, pattern="fabric", tile_size=0.4),
    _M(id="cushion_gold", name="Gold cushion", base_color="#C59A4A", roughness=0.8, pattern="fabric", tile_size=0.4),
    _M(id="wood_oak", name="Oak", base_color="#B98F62", roughness=0.55, pattern="wood", tile_size=1.0),
    _M(id="wood_ash", name="Ash", base_color="#D8C3A0", roughness=0.6, pattern="wood", tile_size=1.0),
    _M(id="wood_walnut", name="Walnut", base_color="#5C3E2A", roughness=0.5, pattern="wood", tile_size=1.0),
    _M(id="wood_black", name="Black-stained wood", base_color="#2A2624", roughness=0.5, pattern="wood", tile_size=1.0),
    _M(id="metal_black", name="Black steel", base_color="#1E1E1F", roughness=0.4, metallic=0.8),
    _M(id="metal_brass", name="Brass", base_color="#B08D57", roughness=0.25, metallic=1.0),
    _M(id="metal_chrome", name="Chrome", base_color="#CACBCE", roughness=0.15, metallic=1.0),
    _M(id="metal_raw", name="Raw steel", base_color="#5F6064", roughness=0.55, metallic=0.9),
    _M(id="ceramic_white", name="White ceramic", base_color="#F3F3F1", roughness=0.15),
    _M(id="stone_white", name="White quartz", base_color="#E5E2DC", roughness=0.2, pattern="marble", tile_size=1.0),
    _M(id="stone_black", name="Black granite", base_color="#2C2C2E", roughness=0.25, pattern="stone", tile_size=1.0),
    _M(id="stone_concrete", name="Concrete top", base_color="#9B9994", roughness=0.6, pattern="concrete", tile_size=1.0),
    _M(id="glass_clear", name="Clear glass", base_color="#D4E3E6", roughness=0.05, opacity=0.25),
    _M(id="appliance_steel", name="Brushed steel", base_color="#B5B7B9", roughness=0.35, metallic=0.9),
    _M(id="appliance_white", name="White enamel", base_color="#F1F1EF", roughness=0.3),
    _M(id="stainless_steel", name="Stainless steel", base_color="#C9CCCE", roughness=0.32, metallic=0.85,
       pattern="brushed", tile_size=1.0),
    _M(id="mirror", name="Mirror", base_color="#D9DDE0", roughness=0.02, metallic=1.0),
    _M(id="screen_black", name="Screen", base_color="#0C0C0E", roughness=0.1),
    _M(id="plant_green", name="Foliage", base_color="#3E6B35", roughness=0.8),
    _M(id="pot_terracotta", name="Terracotta", base_color="#B5653E", roughness=0.85),
    _M(id="pot_black", name="Black ceramic pot", base_color="#262626", roughness=0.4),
    _M(id="rug_wool", name="Wool rug", base_color="#CFC6B6", roughness=1.0, pattern="fabric", tile_size=0.6),
    _M(id="rug_dark", name="Dark rug", base_color="#4A4643", roughness=1.0, pattern="fabric", tile_size=0.6),
    _M(id="rug_jewel", name="Jewel rug", base_color="#5B3B52", roughness=1.0, pattern="fabric", tile_size=0.6),
    _M(id="shade_linen", name="Linen shade", base_color="#EFE8DA", roughness=0.9, emission=0.0),
    _M(id="bedding_white", name="White bedding", base_color="#F2F0EC", roughness=0.95, pattern="fabric", tile_size=0.5),
    _M(id="bedding_grey", name="Grey bedding", base_color="#9EA0A3", roughness=0.95, pattern="fabric", tile_size=0.5),
    _M(id="lacquer_white", name="White lacquer", base_color="#EDEDEA", roughness=0.35),
    _M(id="lacquer_black", name="Black lacquer", base_color="#1F1F20", roughness=0.35),
    _M(id="lacquer_sage", name="Sage lacquer", base_color="#8E9C86", roughness=0.4),
    _M(id="lacquer_navy", name="Navy lacquer", base_color="#24324A", roughness=0.3),
]}

WET_ROOMS = {"bathroom", "toilet", "laundry"}
SERVICE_ROOMS = {"kitchen", "utility", "balcony", "entry"}

_ROLES = ("upholstery", "cushion", "wood", "frame_metal", "metal_accent", "ceramic", "stone_top", "glass",
          "appliance", "screen", "plant", "pot", "rug", "lamp_shade", "bedding", "lacquer", "mirror", "stainless")

STYLES: dict[str, dict] = {
    "modern": {
        "exterior": "render_light",
        "label": "Modern",
        "floor": {"default": "oak_planks", "wet": "tile_grey", "service": "tile_white"},
        "walls": {"default": "plaster_white", "wet": "wall_tile_white"},
        "ceiling": "ceiling_white",
        "door": "door_white", "door_frame": "frame_white", "window_frame": "frame_black",
        "roles": {"upholstery": "fabric_grey", "cushion": "cushion_cream", "wood": "wood_oak",
                  "frame_metal": "metal_black", "metal_accent": "metal_black", "ceramic": "ceramic_white",
                  "stone_top": "stone_white", "glass": "glass_clear", "appliance": "appliance_steel",
                  "screen": "screen_black", "plant": "plant_green", "pot": "pot_black", "rug": "rug_wool",
                  "lamp_shade": "shade_linen", "bedding": "bedding_white", "lacquer": "lacquer_white", "mirror": "mirror",
                  "stainless": "stainless_steel"},
        "lighting": "neutral",
    },
    "minimalist": {
        "exterior": "plaster_white",
        "label": "Minimalist",
        "floor": {"default": "ash_planks", "wet": "tile_white", "service": "tile_white"},
        "walls": {"default": "plaster_white", "wet": "wall_tile_white"},
        "ceiling": "ceiling_white",
        "door": "door_white", "door_frame": "frame_white", "window_frame": "frame_white",
        "roles": {"upholstery": "fabric_light", "cushion": "cushion_cream", "wood": "wood_ash",
                  "frame_metal": "metal_chrome", "metal_accent": "metal_chrome", "ceramic": "ceramic_white",
                  "stone_top": "stone_white", "glass": "glass_clear", "appliance": "appliance_white",
                  "screen": "screen_black", "plant": "plant_green", "pot": "pot_terracotta", "rug": "rug_wool",
                  "lamp_shade": "shade_linen", "bedding": "bedding_white", "lacquer": "lacquer_white", "mirror": "mirror",
                  "stainless": "stainless_steel"},
        "lighting": "daylight",
    },
    "industrial": {
        "exterior": "brick_red",
        "label": "Industrial",
        "floor": {"default": "polished_concrete", "wet": "tile_grey", "service": "polished_concrete"},
        "walls": {"default": "brick_red", "wet": "concrete_wall"},
        "ceiling": "concrete_wall",
        "door": "door_black", "door_frame": "frame_black", "window_frame": "frame_black",
        "roles": {"upholstery": "leather_cognac", "cushion": "cushion_rust", "wood": "wood_walnut",
                  "frame_metal": "metal_raw", "metal_accent": "metal_black", "ceramic": "ceramic_white",
                  "stone_top": "stone_concrete", "glass": "glass_clear", "appliance": "appliance_steel",
                  "screen": "screen_black", "plant": "plant_green", "pot": "pot_black", "rug": "rug_dark",
                  "lamp_shade": "metal_black", "bedding": "bedding_grey", "lacquer": "lacquer_black", "mirror": "mirror",
                  "stainless": "stainless_steel"},
        "lighting": "warm",
    },
    "luxury": {
        "exterior": "render_light",
        "label": "Luxury",
        "floor": {"default": "walnut_planks", "wet": "marble_floor", "service": "marble_floor"},
        "walls": {"default": "plaster_warm", "wet": "marble_floor"},
        "ceiling": "ceiling_white",
        "door": "door_oak", "door_frame": "frame_brass", "window_frame": "frame_brass",
        "roles": {"upholstery": "velvet_green", "cushion": "cushion_gold", "wood": "wood_walnut",
                  "frame_metal": "metal_brass", "metal_accent": "metal_brass", "ceramic": "ceramic_white",
                  "stone_top": "stone_black", "glass": "glass_clear", "appliance": "appliance_steel",
                  "screen": "screen_black", "plant": "plant_green", "pot": "pot_black", "rug": "rug_jewel",
                  "lamp_shade": "shade_linen", "bedding": "bedding_white", "lacquer": "lacquer_navy", "mirror": "mirror",
                  "stainless": "stainless_steel"},
        "lighting": "warm",
    },
    "scandinavian": {
        "exterior": "plaster_white",
        "label": "Scandinavian",
        "floor": {"default": "ash_planks", "wet": "tile_white", "service": "ash_planks"},
        "walls": {"default": "plaster_white", "wet": "wall_tile_white"},
        "ceiling": "ceiling_white",
        "door": "door_white", "door_frame": "frame_white", "window_frame": "frame_white",
        "roles": {"upholstery": "fabric_light", "cushion": "cushion_rust", "wood": "wood_ash",
                  "frame_metal": "metal_black", "metal_accent": "metal_brass", "ceramic": "ceramic_white",
                  "stone_top": "stone_white", "glass": "glass_clear", "appliance": "appliance_white",
                  "screen": "screen_black", "plant": "plant_green", "pot": "pot_terracotta", "rug": "rug_wool",
                  "lamp_shade": "shade_linen", "bedding": "bedding_grey", "lacquer": "lacquer_sage", "mirror": "mirror",
                  "stainless": "stainless_steel"},
        "lighting": "daylight",
    },
}

for _name, _style in STYLES.items():  # catch typos at import time
    assert set(_style["roles"]) == set(_ROLES), f"style {_name} must map every material role"
    for _ref in [_style["exterior"], *_style["floor"].values(), *_style["walls"].values(), _style["ceiling"], _style["door"],
                 _style["door_frame"], _style["window_frame"], *_style["roles"].values()]:
        assert _ref in BUILTIN_MATERIALS, f"style {_name} references unknown material {_ref}"


def material_table(scene: Scene) -> dict[str, Material]:
    """All materials a scene can reference: built-ins overridden by scene-defined ones."""
    table = dict(BUILTIN_MATERIALS)
    table.update({m.id: m for m in scene.materials})
    return table


def _room_class(room: Room) -> str:
    if room.type in WET_ROOMS:
        return "wet"
    if room.type in SERVICE_ROOMS:
        return "service"
    return "default"


def room_floor_material(scene: Scene, room: Room) -> str:
    if room.finishes.floor:
        return room.finishes.floor
    floors = STYLES[scene.project.style]["floor"]
    return floors.get(_room_class(room), floors["default"])


def room_wall_material(scene: Scene, room: Room) -> str:
    if room.finishes.walls:
        return room.finishes.walls
    walls = STYLES[scene.project.style]["walls"]
    return walls["wet"] if room.type in WET_ROOMS else walls["default"]


def room_wall_bands(room: Room) -> list[float]:
    """Heights above the floor where this room's wall finish changes (dado top, frieze bottom)."""
    f = room.finishes
    return [h for h, m in ((f.dado_height, f.dado), (f.frieze_from, f.frieze)) if m]


def room_wall_material_at(scene: Scene, room: Room, height: float) -> str:
    """Wall finish facing ``room`` at ``height`` metres above the level's floor."""
    f = room.finishes
    if f.dado and height < f.dado_height:
        return f.dado
    if f.frieze and height > f.frieze_from:
        return f.frieze
    return room_wall_material(scene, room)


def room_ceiling_material(scene: Scene, room: Room) -> str:
    return room.finishes.ceiling or STYLES[scene.project.style]["ceiling"]


def role_material(scene: Scene, role: str, override: dict[str, str] | None = None) -> str:
    if override and role in override:
        return override[role]
    return STYLES[scene.project.style]["roles"][role]


def exterior_wall_material(scene: Scene) -> str:
    return STYLES[scene.project.style]["exterior"]


def opening_materials(scene: Scene) -> dict[str, str]:
    s = STYLES[scene.project.style]
    return {"door": s["door"], "door_frame": s["door_frame"], "window_frame": s["window_frame"],
            "glass": "window_glass"}
