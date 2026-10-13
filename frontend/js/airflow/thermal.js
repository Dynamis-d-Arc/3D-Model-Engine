// @ts-check
/**
 * What drives the airflow illustration: per unit a temperature (cloud colour) and a supply "drop" (how much
 * colder the jet is), and whether the unit is drawn at all, from a MATLAB day or the BMS feed.
 * Pure functions: browser and Node (tests).
 *
 * model  = GET /airflow-zones: { floors: [{ floor, splitX, unitCount, airflowLs }], zones: [{ key, tag, floor,
 *          units: [[x, y, z]], airflowLs }] }
 * thermal state = { zones: [{ floor, west, east, powerKW }], options: { coolingKW, inputKW }, building:
 *          { floors: [{ floor, splitX, unitCount, airflowLs }] } }
 */

export const COOLING_KW = 7, INPUT_KW = 2, FAN_KW = 0.06;   // the study's defaults (thermal-inputs)
export const FULL_DAY = 480;                                // minute at which the jets are fully developed
const AIR = 1.2 * 1005;                                     // rho * cp, J/(m3 K)

/** The results floor that holds this model's units (most shared tags), else 1. */
export function matchFloor(results, model) {
  const tags = new Set(model.zones.map((z) => z.tag));
  let best = { floor: 1, n: -1 };
  for (const f of results.building?.floors || []) {
    const n = (f.tags || []).filter((t) => tags.has(t)).length;
    if (n > best.n) best = { floor: f.floor, n };
  }
  return best.floor;
}

/** The split line and unit counts of the model, with the cooling inputs of the results floor when known. */
export function buildingFor(model, results = null, resultsFloor = 1) {
  return {
    floors: model.floors.map((f) => {
      const r = results?.building?.floors?.find((x) => x.floor === resultsFloor);
      return { floor: f.floor, splitX: f.splitX, unitCount: r?.unitCount ?? f.unitCount, airflowLs: r?.airflowLs ?? f.airflowLs };
    }),
  };
}

/** MATLAB mode: the zones of one setpoint scenario at one minute (series rows: west, east, AC kW, ...). */
export function matlabState(results, setpoint, minute, model, resultsFloor) {
  const s = results.scenarios.find((x) => x.setpoint === setpoint) || results.scenarios[results.scenarios.length - 1];
  const f = s.floors.find((x) => x.floor === resultsFloor) || s.floors[0];
  const row = f.series[Math.max(0, Math.min(f.series.length - 1, minute))];
  return {
    zones: model.floors.map((mf) => ({ floor: mf.floor, west: row[0], east: row[1], powerKW: row[2] })),
    options: { coolingKW: results.options.coolingKW, inputKW: results.options.inputKW },
    building: buildingFor(model, results, resultsFloor),
    scenario: s, floorSeries: f, minute,
  };
}

/** MATLAB mode: every unit takes its floor's west or east temperature (x < splitX) and the floor's cooling drop. */
export function zoneValues(model, d) {
  const values = {}, drops = {};
  for (const z of model.zones) {
    const zone = d.zones.find((x) => x.floor === z.floor), f = d.building.floors.find((x) => x.floor === z.floor);
    if (!zone || !f) continue;
    const t = z.units[0][0] < f.splitX ? zone.west : zone.east;
    values[z.key] = [t, t];
    const totalCooling = Math.max(0, zone.powerKW - f.unitCount * FAN_KW) * (d.options.coolingKW / d.options.inputKW) * 1000;
    drops[z.key] = Math.min(Math.max(0, t - 12), totalCooling / ((f.airflowLs / 1000) * AIR));
  }
  return { values, drops };
}

/** A unit's air temperature from the feed (room, else return air), null when it reports nothing usable. */
export const reading = (u) => {
  const t = u ? (u.room_temp_c ?? u.return_air_c ?? u.roomTemp) : null;
  return t != null && t > 0 ? t : null;
};
/** Blowing: switched on and not stale. */
export const flowing = (u) => !!(u && u.on === true && u.state !== "stale");

/** Live mode: per-floor west/east means of the units' readings, then every running unit gets its
 * own temperature and a drop from its drawing airflow and 7 kW of cooling; units off or silent are hidden. */
export function liveState(model, readings) {
  const units = readings || {};
  const building = buildingFor(model);
  const zones = building.floors.map((f) => {
    const mine = model.zones.filter((z) => z.floor === f.floor);
    const temps = (west) => mine.filter((z) => (z.units[0][0] < f.splitX) === west).map((z) => reading(units[z.key])).filter((t) => t != null);
    const mean = (a) => (a.length ? a.reduce((x, y) => x + y, 0) / a.length : null);
    const fallback = mean(mine.map((z) => reading(units[z.key])).filter((t) => t != null)) ?? 27;
    const running = mine.filter((z) => flowing(units[z.key])).length;
    return { floor: f.floor, west: mean(temps(true)) ?? fallback, east: mean(temps(false)) ?? fallback,
      powerKW: running * INPUT_KW + f.unitCount * FAN_KW };
  });
  const d = { zones, options: { coolingKW: COOLING_KW, inputKW: INPUT_KW }, building, minute: FULL_DAY };
  const { values, drops } = zoneValues(model, d);
  const hidden = new Set();
  let running = 0;
  for (const z of model.zones) {
    const u = units[z.key];
    if (!flowing(u)) { hidden.add(z.key); continue; }
    running++;
    const t = reading(u);
    if (t != null) {
      values[z.key] = [t, t];
      const ls = z.airflowLs || 240;
      drops[z.key] = Math.min(Math.max(0, t - 12), (COOLING_KW * 1000) / ((ls / 1000) * AIR));
    }
  }
  return { ...d, values, drops, hidden, running };
}
