"""Read-only AppFolio import: properties, units, tenants, vendors.

Run:  .venv/bin/python -m app.appfolio   (from the project root)

Needs APPFOLIO_CLIENT_ID, APPFOLIO_CLIENT_SECRET and APPFOLIO_API_BASE in the
environment. Without them it prints what to set and exits cleanly — nothing
is changed.

Endpoint paths below follow AppFolio's REST conventions; if your AppFolio API
plan uses different paths, adjust them here. Auth is HTTP Basic with the
client id / secret. The import is idempotent: properties match by name, units
by (property, label), tenants by phone, vendors by (name, trade).
"""
import sys

import requests
from requests.auth import HTTPBasicAuth

from . import config
from .db import SessionLocal, init_db
from .models import Property, Tenant, Unit, Vendor


def _session() -> requests.Session:
    s = requests.Session()
    s.auth = HTTPBasicAuth(config.APPFOLIO_CLIENT_ID, config.APPFOLIO_CLIENT_SECRET)
    s.headers["Accept"] = "application/json"
    return s


def _get(s: requests.Session, path: str):
    url = f"{config.APPFOLIO_API_BASE}{path}"
    resp = s.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    # tolerate {items: [...]} envelopes or bare lists
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data["items"]
    return data if isinstance(data, list) else []


def _upsert_property(db, item: dict) -> Property:
    name = item.get("name") or "Unnamed property"
    prop = db.query(Property).filter_by(name=name).first()
    if not prop:
        prop = Property(name=name, address=item.get("address") or "")
        db.add(prop)
        db.flush()
    return prop


def _upsert_unit(db, prop: Property, item: dict) -> Unit:
    label = str(item.get("unit_number") or item.get("label") or "?")
    unit = db.query(Unit).filter_by(property_id=prop.id, label=label).first()
    if not unit:
        unit = Unit(property_id=prop.id, label=label)
        db.add(unit)
        db.flush()
    return unit


def run_import() -> dict:
    """Returns counts per entity. Exits non-zero only on unexpected errors."""
    if not config.appfolio_configured():
        print("AppFolio import skipped: set APPFOLIO_CLIENT_ID, "
              "APPFOLIO_CLIENT_SECRET and APPFOLIO_API_BASE first.")
        return {}
    init_db()
    s = _session()
    db = SessionLocal()
    counts = {"properties": 0, "units": 0, "tenants": 0, "vendors": 0}
    try:
        for item in _get(s, "/properties"):
            prop = _upsert_property(db, item)
            counts["properties"] += 1
            for u in item.get("units", []) or _get(s, f"/properties/{item.get('id')}/units"):
                unit = _upsert_unit(db, prop, u)
                counts["units"] += 1
                for t in u.get("tenants", []):
                    phone = t.get("phone") or t.get("mobile")
                    if not phone:
                        continue
                    existing = db.query(Tenant).filter_by(phone=phone).first()
                    if not existing:
                        db.add(Tenant(unit_id=unit.id,
                                      name=t.get("name") or "Unknown tenant",
                                      phone=phone))
                        counts["tenants"] += 1
        for v in _get(s, "/vendors"):
            name, trade = v.get("name"), v.get("trade") or v.get("category") or "general"
            if not name:
                continue
            existing = db.query(Vendor).filter_by(name=name, trade=trade).first()
            if not existing:
                db.add(Vendor(trade=trade, name=name,
                              phone=v.get("phone") or "",
                              priority=int(v.get("priority") or 1)))
                counts["vendors"] += 1
        db.commit()
    finally:
        db.close()
    print("AppFolio import complete: " +
          ", ".join(f"{k}={n}" for k, n in counts.items()))
    return counts


def main() -> int:
    try:
        run_import()
        return 0
    except requests.RequestException as e:
        print(f"AppFolio import failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
