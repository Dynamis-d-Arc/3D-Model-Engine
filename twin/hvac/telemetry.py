"""HVAC live data: ingestion of real BMS readings and a simulated BMS for demonstrations.

Reading (per unit):  on, mode, setpoint_c, supply_air_c, return_air_c, fan_speed (0..1), airflow_lps,
                     room_temp_c (optional sensor), alarm (optional text), ts (unix seconds)
Site:                outdoor_c, occupancy (0..1), ts

Real data is pushed to POST /api/projects/{pid}/hvac/telemetry (units by tag or id). When no real reading
arrived for a project within LIVE_TIMEOUT_S, /hvac/live serves the simulated BMS instead and says so.

The simulated BMS is stateless and deterministic in time: Bangkok outdoor temperature and occupancy follow
a daily curve; every unit holds its setpoint at quasi-steady state, so its supply air temperature follows
from the zone heat balance  load = airflow * rho*cp * (return - supply)  with the zone loads of the
airflow domain (same numbers the browser simulation uses). A unit whose coil cannot reach the setpoint
(supply limited to 12 C) runs warm, and one unit at a time "trips" for a few minutes so the twin has
something to react to.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field

LIVE_TIMEOUT_S = 300
BANGKOK = timezone(timedelta(hours=7))
RHO_CP = 1206.0
COIL_MIN_SUPPLY_C = 12.0
FAULT_PERIOD_S = 20 * 60
FAULT_LENGTH_S = 4 * 60


class UnitReading(BaseModel):
    unit: str = Field(min_length=1, max_length=64, description="unit tag (e.g. 1FC-23) or id")
    ts: float | None = None
    on: bool = True
    mode: str = Field(default="cool", max_length=16)
    setpoint_c: float | None = Field(default=None, ge=5, le=40)
    supply_air_c: float | None = Field(default=None, ge=-5, le=60)
    return_air_c: float | None = Field(default=None, ge=-5, le=60)
    room_temp_c: float | None = Field(default=None, ge=-30, le=60)
    fan_speed: float | None = Field(default=None, ge=0, le=1)
    airflow_lps: float | None = Field(default=None, ge=0, le=20000)
    alarm: str | None = Field(default=None, max_length=120)


class SiteReading(BaseModel):
    ts: float | None = None
    outdoor_c: float | None = Field(default=None, ge=-30, le=60)
    occupancy: float | None = Field(default=None, ge=0, le=1)


class TelemetryBody(BaseModel):
    readings: list[UnitReading] = Field(default_factory=list, max_length=2000)
    site: SiteReading | None = None


class TelemetryStore:
    """Latest reading per unit plus a short history per project (in memory; a deployment would put this in
    a time-series database)."""

    def __init__(self, history: int = 720) -> None:
        self._latest: dict[str, dict[str, dict]] = {}
        self._site: dict[str, dict] = {}
        self._history: dict[str, deque] = {}
        self._n = history
        self._lock = threading.Lock()

    def ingest(self, pid: str, body: TelemetryBody, resolve) -> dict:
        now = time.time()
        accepted, unknown = 0, []
        with self._lock:
            latest = self._latest.setdefault(pid, {})
            for r in body.readings:
                uid = resolve(r.unit)
                if uid is None:
                    unknown.append(r.unit)
                    continue
                d = r.model_dump()
                d["ts"] = d["ts"] or now
                d["unit_id"] = uid
                latest[uid] = d
                accepted += 1
            if body.site:
                s = body.site.model_dump()
                s["ts"] = s["ts"] or now
                self._site[pid] = s
            self._history.setdefault(pid, deque(maxlen=self._n)).append(
                {"ts": now, "units": {k: dict(v) for k, v in latest.items()}, "site": self._site.get(pid)})
        return {"accepted": accepted, "unknown_units": unknown[:50]}

    def live(self, pid: str) -> tuple[dict, dict | None, float] | None:
        with self._lock:
            latest = self._latest.get(pid)
            if not latest:
                return None
            newest = max(v["ts"] for v in latest.values())
            if time.time() - newest > LIVE_TIMEOUT_S:
                return None
            return {k: dict(v) for k, v in latest.items()}, self._site.get(pid), newest

    def history(self, pid: str, limit: int = 120) -> list[dict]:
        with self._lock:
            return list(self._history.get(pid, []))[-limit:]


def bangkok_hour(t: float) -> float:
    d = datetime.fromtimestamp(t, BANGKOK)
    return d.hour + d.minute / 60 + d.second / 3600


def outdoor_c(t: float) -> float:
    """Bangkok dry-bulb: ~28 C at dawn, ~34 C mid-afternoon."""
    h = bangkok_hour(t)
    return round(30.8 + 3.2 * math.sin(2 * math.pi * (h - 9.0) / 24.0), 2)


def occupancy(t: float) -> float:
    """Teaching-kitchen schedule: breakfast, a lunch peak, classes in the afternoon, dinner service."""
    h = bangkok_hour(t)
    pts = [(0, 0.15), (7, 0.2), (9, 0.6), (11.5, 1.0), (13.5, 1.0), (15, 0.7), (17.5, 0.85), (20, 0.8), (22, 0.3), (24, 0.15)]
    for (h0, v0), (h1, v1) in zip(pts, pts[1:]):
        if h0 <= h <= h1:
            f = (h - h0) / (h1 - h0)
            return round(v0 + (v1 - v0) * (0.5 - 0.5 * math.cos(math.pi * f)), 3)
    return 0.2


def simulate(domain: dict, t: float | None = None) -> tuple[dict[str, dict], dict]:
    """Simulated BMS readings for every unit of an airflow domain at time t."""
    t = time.time() if t is None else t
    out_c = outdoor_c(t)
    occ = occupancy(t)
    units = domain["units"]
    fault_idx = int(t // FAULT_PERIOD_S) % max(1, len(units))
    in_fault = (t % FAULT_PERIOD_S) < FAULT_LENGTH_S
    readings: dict[str, dict] = {}
    for n, u in enumerate(units):
        sp = u["setpoint_c"]
        q = u["airflow_lps"] / 1000.0
        wobble = math.sin(t / 900.0 + n * 1.7)               # slow, unit-specific drift (+-1)
        fan = round(min(1.0, max(0.55, 0.8 + 0.15 * wobble + 0.1 * (occ - 0.5))), 2)
        load = u["internal_w"] * (0.35 + 0.65 * occ) + u["envelope_ua"] * (out_c - sp)
        tripped = in_fault and n == fault_idx
        if tripped:
            readings[u["id"]] = {"unit_id": u["id"], "tag": u["tag"], "ts": t, "on": False, "mode": "off",
                                 "setpoint_c": sp, "supply_air_c": None, "return_air_c": round(sp + 2.5, 1),
                                 "fan_speed": 0.0, "airflow_lps": 0.0, "room_temp_c": None,
                                 "alarm": "tripped (simulated fault)"}
            continue
        zone = sp + 0.35 * wobble
        supply = zone - max(0.0, load) / (q * fan * RHO_CP)
        alarm = None
        if supply < COIL_MIN_SUPPLY_C:        # coil at full capacity: the zone runs above setpoint
            supply = COIL_MIN_SUPPLY_C
            zone = supply + max(0.0, load) / (q * fan * RHO_CP)
            alarm = "at full cooling capacity" if zone > sp + 1.5 else None
        readings[u["id"]] = {"unit_id": u["id"], "tag": u["tag"], "ts": t, "on": True, "mode": "cool",
                             "setpoint_c": sp, "supply_air_c": round(supply, 2), "return_air_c": round(zone, 2),
                             "fan_speed": fan, "airflow_lps": round(u["airflow_lps"] * fan, 1),
                             "room_temp_c": None, "alarm": alarm}
    return readings, {"ts": t, "outdoor_c": out_c, "occupancy": occ}
