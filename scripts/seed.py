"""Seed the store with realistic Indian kirana SKUs.

HSN codes and GST slabs are illustrative but realistic; they live in DATA so a
rate change (e.g. GST council rationalization) is an UPDATE, never a deploy.
Run:  python scripts/seed.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kirana.db.database import get_conn, init_db, transaction  # noqa: E402

# name, brand, unit, is_loose, hsn, gst%, cost, sell (incl. GST), mrp, qty, reorder, aliases
PRODUCTS = [
    ("Aashirvaad Atta 5kg", "Aashirvaad", "packet", 0, "1101", 5, 240, 265, 285, 30, 8, "atta,aata,wheat flour"),
    ("Loose Atta (per kg)", None, "kg", 1, "1101", 0, 38, 45, None, 80, 20, "loose atta,open atta"),
    ("Tata Salt 1kg", "Tata", "packet", 0, "2501", 5, 24, 28, 30, 60, 15, "salt,uppu,namak"),
    ("Loose Sugar (per kg)", None, "kg", 1, "1701", 5, 40, 44, None, 100, 25, "sugar,sakkarai,cheeni"),
    ("Loose Toor Dal (per kg)", None, "kg", 1, "0713", 0, 130, 155, None, 50, 10, "toor dal,arhar,dal,paruppu"),
    ("Loose Rice Ponni (per kg)", None, "kg", 1, "1006", 0, 52, 62, None, 200, 50, "rice,ponni,arisi,chawal"),
    ("Fortune Sunflower Oil 1L", "Fortune", "packet", 0, "1512", 5, 128, 142, 155, 40, 10, "sunflower oil,oil,ennai"),
    ("Amul Butter 100g", "Amul", "packet", 0, "0405", 12, 54, 62, 62, 25, 6, "butter,amul butter"),
    ("Amul Milk 500ml", "Amul", "packet", 0, "0401", 0, 26, 29, 29, 45, 12, "milk,paal,doodh"),
    ("Maggi Noodles 70g", "Nestle", "packet", 0, "1902", 12, 12, 14, 14, 120, 30, "maggi,noodles"),
    ("Parle-G 250g", "Parle", "packet", 0, "1905", 5, 20, 25, 25, 90, 20, "parle g,parle,biscuit,glucose biscuit"),
    ("Britannia Marie Gold 250g", "Britannia", "packet", 0, "1905", 5, 30, 35, 36, 40, 10, "marie,marie gold"),
    ("Surf Excel 1kg", "HUL", "packet", 0, "3402", 18, 118, 135, 140, 20, 5, "surf,surf excel,detergent"),
    ("Lifebuoy Soap 100g", "HUL", "piece", 0, "3401", 18, 28, 34, 36, 60, 15, "soap,lifebuoy,soppu"),
    ("Colgate Strong Teeth 100g", "Colgate", "piece", 0, "3306", 18, 48, 55, 58, 35, 8, "colgate,toothpaste,paste"),
    ("Dairy Milk 24g", "Cadbury", "piece", 0, "1806", 18, 17, 20, 20, 80, 20, "dairy milk,chocolate"),
    ("Red Label Tea 250g", "Brooke Bond", "packet", 0, "0902", 5, 128, 145, 150, 25, 6, "tea,red label,chaya,chai"),
    ("Bru Instant Coffee 50g", "Bru", "packet", 0, "2101", 18, 95, 110, 115, 15, 4, "coffee,bru,kaapi"),
    ("Eggs (per dozen)", None, "dozen", 1, "0407", 0, 66, 78, None, 20, 5, "eggs,muttai,anda"),
    ("Onion (per kg)", None, "kg", 1, "0703", 0, 22, 30, None, 60, 15, "onion,vengayam,pyaz"),
    ("Tomato (per kg)", None, "kg", 1, "0702", 0, 18, 26, None, 45, 12, "tomato,thakkali,tamatar"),
    ("Potato (per kg)", None, "kg", 1, "0701", 0, 20, 28, None, 70, 15, "potato,urulai,aloo"),
    ("Bananas (per dozen)", None, "dozen", 1, "0803", 0, 40, 55, None, 15, 5, "banana,vazhaipazham,kela"),
    ("Kissan Mixed Fruit Jam 200g", "Kissan", "piece", 0, "2007", 12, 62, 72, 75, 12, 4, "jam,kissan"),
    ("Haldiram Bhujia 200g", "Haldiram", "packet", 0, "2106", 12, 48, 55, 58, 30, 8, "bhujia,namkeen,mixture"),
]

CUSTOMERS = [("Ramesh", "9876500001"), ("Suresh", "9876500002"), ("Lakshmi", "9876500003")]

DEFAULT_PREFS = [
    ("store", "shop_name", "Sri Murugan Stores"),
    ("store", "shop_address", "12 Gandhipuram Main Road, Coimbatore 641012"),
    ("store", "shop_phone", "+91 98765 43210"),
    ("store", "gstin", "33ABCDE1234F1Z5"),
]


def seed() -> None:
    init_db()
    conn = get_conn()
    existing = conn.execute("SELECT COUNT(*) c FROM products").fetchone()["c"]
    if existing:
        print(f"DB already has {existing} products; skipping seed.")
        return
    with transaction() as tx:
        tx.executemany(
            """INSERT INTO products (name, brand, unit, is_loose, hsn, gst_rate, cost_price,
                                     sell_price, mrp, qty, reorder_level, aliases)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            PRODUCTS,
        )
        tx.executemany("INSERT INTO customers (name, phone) VALUES (?,?)", CUSTOMERS)
        tx.executemany(
            "INSERT OR IGNORE INTO preferences (scope, key, value) VALUES (?,?,?)", DEFAULT_PREFS
        )
    print(f"Seeded {len(PRODUCTS)} products, {len(CUSTOMERS)} khata customers, defaults set.")


if __name__ == "__main__":
    seed()
