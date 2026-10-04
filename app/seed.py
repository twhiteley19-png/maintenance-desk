"""Seed the demo database: 2 properties, 12 units, 12 tenants, 6 vendors.

Run:  .venv/bin/python -m app.seed   (from the project root)
"""
from .db import SessionLocal, init_db
from .models import Property, Tenant, Unit, Vendor

VENDORS = [
    # trade, name, phone, priority, after_hours_ok
    ("plumbing", "Rapid Rooter Plumbing", "(919) 555-0101", 1, 1),
    ("electrical", "Bright Spark Electric", "(919) 555-0102", 1, 1),
    ("hvac", "AirCare Heating & Cooling", "(919) 555-0103", 1, 1),
    ("appliance", "Fix-It Appliance Repair", "(919) 555-0104", 1, 0),
    ("locksmith", "All Hours Locksmith", "(919) 555-0105", 1, 1),
    ("general", "HandyPro Services", "(919) 555-0106", 1, 0),
]

PROPERTIES = [
    {
        "name": "Maple Grove Apartments",
        "address": "1200 Maple Grove Dr, Holly Springs, NC 27540",
        "owner_spend_limit": 500.0,
        "units": ["1A", "1B", "2A", "2B", "3A", "3B"],
        "tenants": [
            ("Alicia Gomez", "(919) 555-1001"),
            ("Brian Carter", "(919) 555-1002"),
            ("Cynthia Nguyen", "(919) 555-1003"),
            ("David Okafor", "(919) 555-1004"),
            ("Emily Ross", "(919) 555-1005"),
            ("Frank Delgado", "(919) 555-1006"),
        ],
    },
    {
        "name": "Cedar Court Townhomes",
        "address": "45 Cedar Court Ln, Apex, NC 27502",
        "owner_spend_limit": 750.0,
        "units": ["101", "102", "103", "104", "105", "106"],
        "tenants": [
            ("Grace Liu", "(919) 555-2001"),
            ("Henry Patel", "(919) 555-2002"),
            ("Irene Alvarez", "(919) 555-2003"),
            ("James Kim", "(919) 555-2004"),
            ("Karen Brooks", "(919) 555-2005"),
            ("Leo Martinez", "(919) 555-2006"),
        ],
    },
]


def seed():
    init_db()
    db = SessionLocal()
    try:
        # Idempotent: wipe demo tables so re-running the seed is clean.
        for model in (Property, Vendor):
            db.query(model).delete()
        db.commit()

        for spec in PROPERTIES:
            prop = Property(
                name=spec["name"],
                address=spec["address"],
                owner_spend_limit=spec["owner_spend_limit"],
            )
            db.add(prop)
            db.flush()
            for label, (tname, tphone) in zip(spec["units"], spec["tenants"]):
                unit = Unit(property_id=prop.id, label=label)
                db.add(unit)
                db.flush()
                db.add(Tenant(unit_id=unit.id, name=tname, phone=tphone))

        for trade, name, phone, priority, after_hours in VENDORS:
            db.add(
                Vendor(
                    trade=trade,
                    name=name,
                    phone=phone,
                    priority=priority,
                    after_hours_ok=after_hours,
                )
            )
        db.commit()

        print(f"Seeded {db.query(Property).count()} properties, "
              f"{db.query(Unit).count()} units, "
              f"{db.query(Tenant).count()} tenants, "
              f"{db.query(Vendor).count()} vendors.")
    finally:
        db.close()


if __name__ == "__main__":
    seed()


def seed_if_empty():
    """Seed demo data only when the database has no tenants.

    Safe to run on every boot: Render's free tier uses an ephemeral
    filesystem, so the SQLite DB comes back empty after each deploy or
    restart. Once real tenants exist (e.g. via the AppFolio import),
    this is a no-op and never wipes anything.
    """
    init_db()
    db = SessionLocal()
    try:
        count = db.query(Tenant).count()
    finally:
        db.close()
    if count:
        print(f"Database already has {count} tenants — skipping demo seed.")
        return
    seed()
