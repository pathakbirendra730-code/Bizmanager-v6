"""
utils/template_products.py  — Template Shop System
====================================================
Logic:
  1. On DB init, seed three hidden "template" shops:
       grocery_template, medical_template, electronics_template
     Each has realistic categories, HSN codes, products with correct GST slabs.

  2. copy_products_to_shop(target_shop_id, business_type)
     Copies all categories + products from the matching template shop
     into target_shop_id. Safe to call multiple times (uses UPSERT / conflict skip).

  3. reset_shop_products(target_shop_id)
     Deletes all existing products/categories for the shop, then re-copies from template.
     Used by the "Reset / Import Default Products" button.

Rules:
  - Template shops have is_template=1 and is_active=0 (invisible to normal users)
  - shop_id is never copied — always reassigned to target
  - Duplicate calls are safe (INSERT OR IGNORE)
  - No master_products table used anywhere
"""

from models.database import get_db
from datetime import datetime


# ═══════════════════════════════ TEMPLATE DEFINITIONS ═════════════════════════

# Each product tuple:
# (name, sku, hsn_code, gst_rate, cost_price, selling_price, stock_qty, threshold, description)

TEMPLATES = {

    # ──────────────────────────────────────────────────────────────────────────
    "grocery": {
        "shop_name": "_TEMPLATE_GROCERY_",
        "categories": {
            "Grains & Pulses": [
                ("Basmati Rice 5kg",      "GR-001", "10063000",  5,  200,  280, 100, 20, "Premium basmati"),
                ("Basmati Rice 1kg",      "GR-002", "10063000",  5,   45,   65,  80, 20, ""),
                ("Toor Dal 1kg",          "GR-003", "07134000",  5,   90,  130,  80, 15, "Split pigeon peas"),
                ("Chana Dal 1kg",         "GR-004", "07132000",  5,   75,  110,  60, 15, "Split chickpeas"),
                ("Moong Dal 500g",        "GR-005", "07133300",  5,   50,   75,  50, 10, "Yellow lentils"),
                ("Urad Dal 1kg",          "GR-006", "07133900",  5,   95,  140,  40, 10, "Split black gram"),
                ("Wheat Flour (Atta) 5kg","GR-007", "11010000",  5,  170,  240, 120, 25, "Whole wheat flour"),
                ("Wheat Flour 1kg",       "GR-008", "11010000",  5,   36,   52,  80, 20, ""),
                ("Semolina (Rava) 500g",  "GR-009", "11010000",  5,   25,   38,  60, 15, ""),
                ("Poha 500g",             "GR-010", "19041000",  5,   28,   42,  50, 10, "Flattened rice"),
            ],
            "Edible Oils": [
                ("Sunflower Oil 1L",      "OL-001", "15121100",  5,  110,  155,  60, 12, "Refined"),
                ("Sunflower Oil 5L",      "OL-002", "15121100",  5,  520,  720,  30,  8, ""),
                ("Mustard Oil 1L",        "OL-003", "15141100",  5,   95,  135,  45, 10, ""),
                ("Soybean Oil 1L",        "OL-004", "15079011",  5,  100,  145,  40, 10, ""),
                ("Groundnut Oil 1L",      "OL-005", "15111000",  5,  150,  210,  30,  8, ""),
                ("Coconut Oil 500ml",     "OL-006", "15131100",  5,  100,  145,  25,  8, ""),
                ("Ghee 500ml",            "OL-007", "04059000", 12,  280,  380,  30,  8, "Pure cow ghee"),
            ],
            "Sugar & Salt": [
                ("Sugar 1kg",             "SG-001", "17019100",  5,   42,   58,  80, 20, "Refined"),
                ("Sugar 5kg",             "SG-002", "17019100",  5,  200,  270,  40, 10, ""),
                ("Jaggery 1kg",           "SG-003", "17011200",  5,   55,   80,  40, 10, "Organic jaggery"),
                ("Salt 1kg",              "SG-004", "25010010",  0,   10,   18,  80, 20, "Iodised"),
                ("Rock Salt 500g",        "SG-005", "25010030",  0,   20,   35,  30, 10, ""),
            ],
            "Dairy": [
                ("Milk 1L Packet",        "DY-001", "04011000",  0,   50,   60,  30, 10, "Full cream"),
                ("Milk 500ml",            "DY-002", "04011000",  0,   26,   32,  20, 10, ""),
                ("Butter 100g",           "DY-003", "04051000", 12,   48,   65,  25,  8, "Salted"),
                ("Butter 500g",           "DY-004", "04051000", 12,  220,  295,  15,  5, ""),
                ("Paneer 200g",           "DY-005", "04061000",  5,   65,   90,  15,  5, "Fresh cottage cheese"),
                ("Curd 400g",             "DY-006", "04039010",  5,   32,   45,  20,  8, ""),
                ("Cheese Slice 200g",     "DY-007", "04069000", 12,   65,   90,  15,  5, ""),
            ],
            "Spices": [
                ("Turmeric Powder 100g",  "SP-001", "09103000",  5,   25,   38,  60, 15, ""),
                ("Red Chilli Powder 100g","SP-002", "09042210",  5,   30,   45,  50, 12, ""),
                ("Coriander Powder 100g", "SP-003", "09092910",  5,   22,   35,  50, 12, ""),
                ("Cumin Seeds 100g",      "SP-004", "09093110",  5,   35,   55,  40, 10, ""),
                ("Mustard Seeds 100g",    "SP-005", "12074010",  5,   20,   32,  40, 10, ""),
                ("Cardamom 50g",          "SP-006", "09042210",  5,   80,  120,  20,  5, "Green cardamom"),
                ("Black Pepper 100g",     "SP-007", "09042110",  5,   55,   80,  30,  8, ""),
                ("Garam Masala 100g",     "SP-008", "09109100",  5,   45,   68,  40, 10, ""),
                ("Cloves 50g",            "SP-009", "09071000",  5,   60,   90,  20,  5, ""),
            ],
            "Beverages": [
                ("Tea Leaves 500g",       "BV-001", "09024090",  5,  120,  185,  40, 10, ""),
                ("Tea Bags 100pcs",       "BV-002", "09024010",  5,   90,  130,  30,  8, ""),
                ("Instant Coffee 100g",   "BV-003", "09011110", 12,  180,  255,  20,  5, ""),
                ("Bournvita 500g",        "BV-004", "21069099", 18,  200,  275,  15,  5, ""),
                ("Horlicks 500g",         "BV-005", "21069099", 18,  190,  265,  15,  5, ""),
                ("Packaged Water 1L",     "BV-006", "22021090", 18,   12,   20,  50, 15, ""),
                ("Soft Drink 2L",         "BV-007", "22021090", 28,   50,   75,  30,  8, ""),
            ],
            "Bakery & Snacks": [
                ("Bread 400g",            "BK-001", "19059090",  5,   28,   40,  20,  8, "Wheat bread"),
                ("Biscuits 100g",         "BK-002", "19053100", 18,   12,   18,  60, 15, ""),
                ("Namkeen 200g",          "BK-003", "19049000", 12,   30,   45,  40, 10, ""),
                ("Chips 80g",             "BK-004", "20052000", 12,   18,   25,  50, 12, ""),
                ("Pasta 500g",            "BK-005", "19021900", 12,   35,   55,  30,  8, ""),
                ("Noodles 70g",           "BK-006", "19023010", 18,   12,   18,  50, 12, ""),
            ],
            "Cleaning & Household": [
                ("Detergent Powder 1kg",  "CL-001", "34022090", 18,   65,   95,  40, 10, ""),
                ("Washing Bar 200g",      "CL-002", "34011100", 18,   20,   30,  50, 12, ""),
                ("Dish Wash Liquid 500ml","CL-003", "34022090", 18,   55,   80,  30,  8, ""),
                ("Floor Cleaner 500ml",   "CL-004", "34029019", 18,   60,   90,  25,  8, ""),
                ("Toilet Cleaner 500ml",  "CL-005", "34029019", 18,   50,   75,  25,  8, ""),
                ("Mosquito Coil 10pcs",   "CL-006", "38089100", 18,   25,   38,  30,  8, ""),
            ],
        }
    },

    # ──────────────────────────────────────────────────────────────────────────
    "medical": {
        "shop_name": "_TEMPLATE_MEDICAL_",
        "categories": {
            "OTC Medicines": [
                ("Paracetamol 500mg 10s",    "MD-001", "30049099", 12,   10,   15, 100, 20, "Fever & pain"),
                ("Paracetamol 650mg 10s",    "MD-002", "30049099", 12,   12,   18, 100, 20, ""),
                ("Aspirin 75mg 14s",         "MD-003", "30049011", 12,   18,   26,  60, 15, ""),
                ("Ibuprofen 400mg 10s",      "MD-004", "30049099", 12,   22,   32,  60, 15, ""),
                ("Antacid Syrup 200ml",      "MD-005", "30049099", 12,   55,   80,  40, 10, ""),
                ("Antacid Tablets 15s",      "MD-006", "30049099", 12,   25,   38,  60, 15, ""),
                ("Cough Syrup 100ml",        "MD-007", "30049099", 12,   60,   90,  40, 10, ""),
                ("ORS Sachet 10s",           "MD-008", "30049099", 12,   30,   48,  50, 12, "Electrolyte"),
                ("Antifungal Cream 30g",     "MD-009", "30049099", 12,   65,   95,  30,  8, ""),
                ("Pain Relief Spray 60ml",   "MD-010", "30049099", 12,   95,  140,  20,  5, ""),
            ],
            "Vitamins & Supplements": [
                ("Vitamin C 500mg 10s",      "VT-001", "30049099", 12,   28,   42,  60, 15, ""),
                ("Vitamin D3 60K IU",        "VT-002", "30049099", 12,   45,   68,  40, 10, ""),
                ("Vitamin B12 10s",          "VT-003", "30049099", 12,   35,   52,  40, 10, ""),
                ("Multivitamin Tablets 30s", "VT-004", "30049099", 12,   80,  120,  30,  8, ""),
                ("Calcium 500mg 30s",        "VT-005", "30049099", 12,   55,   82,  30,  8, ""),
                ("Iron Folic 30s",           "VT-006", "30049099", 12,   40,   60,  40, 10, ""),
                ("Omega-3 Fish Oil 30s",     "VT-007", "30049099", 12,   95,  145,  20,  5, ""),
                ("Protein Powder 200g",      "VT-008", "21069099", 18,  180,  260,  15,  5, ""),
            ],
            "First Aid": [
                ("Bandage 5cm x 4m",         "FA-001", "30059090", 12,   18,   28,  50, 12, ""),
                ("Bandage 10cm x 4m",        "FA-002", "30059090", 12,   25,   38,  40, 10, ""),
                ("Cotton 100g",              "FA-003", "30059090", 12,   22,   34,  50, 12, "Absorbent"),
                ("Surgical Gloves M 50s",    "FA-004", "40151110", 12,   95,  140,  20,  5, ""),
                ("Antiseptic Solution 100ml","FA-005", "30049099", 12,   55,   80,  40, 10, "Dettol type"),
                ("Wound Dressing 10cm",      "FA-006", "30051090", 12,   35,   52,  30,  8, ""),
                ("Micropore Tape 1.25cm",    "FA-007", "30051090", 12,   28,   42,  40, 10, ""),
                ("Thermometer Digital",      "FA-008", "90251100", 12,  120,  180,  15,  5, ""),
                ("BP Monitor",               "FA-009", "90181900", 12, 800, 1200,   5,  2, "Manual"),
            ],
            "Surgical Supplies": [
                ("Syringe 2ml 10s",          "SU-001", "90183100", 12,   22,   35,  30,  8, ""),
                ("Syringe 5ml 10s",          "SU-002", "90183100", 12,   28,   42,  30,  8, ""),
                ("IV Cannula 24G",           "SU-003", "90183900", 12,   18,   28,  30,  8, ""),
                ("Nebulizer Mask Adult",     "SU-004", "90183900", 12,   45,   68,  20,  5, ""),
                ("Urine Bag 2L",             "SU-005", "90189099", 12,   45,   68,  20,  5, ""),
                ("Urine Pregnancy Test",     "SU-006", "38220000", 12,   20,   30,  50, 12, ""),
                ("Blood Glucose Test 50s",   "SU-007", "38220000", 12,  180,  260,  15,  5, ""),
            ],
            "Baby Care": [
                ("Baby Powder 100g",         "BB-001", "33049900", 18,   55,   80,  20,  5, ""),
                ("Baby Oil 100ml",           "BB-002", "33049900", 18,   65,   95,  20,  5, ""),
                ("Diapers S 12s",            "BB-003", "96190010", 12,   95,  140,  15,  5, ""),
                ("Baby Lotion 200ml",        "BB-004", "33041000", 18,   75,  110,  15,  5, ""),
            ],
            "Personal Care": [
                ("Soap 75g",                 "PC-001", "34011100", 18,   18,   28,  60, 15, ""),
                ("Sanitizer 100ml",          "PC-002", "38089400", 18,   45,   68,  40, 10, ""),
                ("Sanitizer 500ml",          "PC-003", "38089400", 18,  160,  230,  20,  5, ""),
                ("Surgical Mask 50s",        "PC-004", "63079090", 12,   80,  120,  30,  8, "3-ply"),
                ("N95 Mask",                 "PC-005", "63079090", 12,   22,   35,  50, 12, ""),
            ],
        }
    },

    # ──────────────────────────────────────────────────────────────────────────
    "electronics": {
        "shop_name": "_TEMPLATE_ELECTRONICS_",
        "categories": {
            "Laptops & Computers": [
                ("Laptop 15.6 i5 8GB",      "LP-001", "84713010", 18, 40000, 52000,  5,  2, "Intel i5, 512GB SSD"),
                ("Laptop 15.6 i7 16GB",     "LP-002", "84713010", 18, 60000, 78000,  3,  1, "Intel i7, 1TB SSD"),
                ("Laptop 14 Ryzen 5",       "LP-003", "84713010", 18, 38000, 50000,  5,  2, ""),
                ("Desktop CPU Set",         "LP-004", "84714100", 18, 22000, 30000,  3,  1, "i3, 8GB, 256SSD"),
                ("Monitor 22 inch FHD",     "LP-005", "85285100", 18,  7500, 10500,  8,  2, ""),
                ("Monitor 27 inch 4K",      "LP-006", "85285100", 18, 15000, 20000,  3,  1, ""),
                ("Mechanical Keyboard",     "LP-007", "84716041", 18,  1200,  2200, 12,  3, "RGB backlit"),
                ("Wireless Keyboard+Mouse", "LP-008", "84716041", 18,   800,  1400, 15,  5, "Combo set"),
            ],
            "Mobile Phones": [
                ("Smartphone Budget 64GB",  "MB-001", "85171290", 18,  7000,  9500, 10,  3, "4GB RAM"),
                ("Smartphone Mid 128GB",    "MB-002", "85171290", 18, 14000, 18500,  8,  2, "6GB RAM"),
                ("Smartphone Pro 256GB",    "MB-003", "85171290", 18, 22000, 29000,  5,  2, "8GB RAM"),
                ("Smartphone Flagship",     "MB-004", "85171290", 18, 45000, 58000,  3,  1, "12GB RAM"),
                ("Feature Phone Basic",     "MB-005", "85171290", 18,  1200,  1800, 20,  5, "Keypad"),
                ("Tablet 10 inch WiFi",     "MB-006", "84717090", 18, 12000, 16000,  5,  2, ""),
                ("Smartwatch",              "MB-007", "91021900", 18,  3500,  5500,  8,  3, "Fitness band"),
            ],
            "Audio & Video": [
                ("Bluetooth Speaker",       "AV-001", "85183000", 18,   900,  1800, 15,  5, "Portable"),
                ("Wireless Earbuds",        "AV-002", "85183000", 18,  1200,  2200, 12,  3, "TWS"),
                ("Wired Headphones",        "AV-003", "85183000", 18,   400,   750, 20,  5, ""),
                ("Bluetooth Headphones",    "AV-004", "85183000", 18,  1500,  2600, 10,  3, "Over-ear"),
                ("Smart TV 32 inch",        "AV-005", "85284912", 18, 12000, 16500,  3,  1, "HD"),
                ("Smart TV 43 inch",        "AV-006", "85284912", 18, 20000, 27000,  3,  1, "FHD"),
                ("Webcam HD 1080p",         "AV-007", "85258090", 18,  1500,  2600,  8,  3, ""),
                ("CCTV Camera 2MP",         "AV-008", "85258090", 18,  1200,  2000,  5,  2, ""),
            ],
            "Accessories": [
                ("USB-C Cable 1m",          "AC-001", "85444210", 18,    60,   120, 60, 15, "Fast charge"),
                ("USB-C Cable 2m",          "AC-002", "85444210", 18,    80,   160, 50, 12, ""),
                ("Lightning Cable 1m",      "AC-003", "85444210", 18,    90,   180, 40, 10, ""),
                ("HDMI Cable 1.5m",         "AC-004", "85444210", 18,   120,   220, 30,  8, "4K"),
                ("Power Bank 10000mAh",     "AC-005", "85044090", 18,   600,  1100, 15,  5, ""),
                ("Power Bank 20000mAh",     "AC-006", "85044090", 18,   900,  1600, 10,  3, ""),
                ("Wall Charger 65W USB-C",  "AC-007", "85044090", 18,   450,   850, 20,  5, ""),
                ("Multi-Port USB Hub",      "AC-008", "85444210", 18,   350,   650, 15,  5, "4-port"),
                ("Screen Guard 6.5 inch",   "AC-009", "70071900", 18,    35,    75, 50, 12, ""),
                ("Mobile Cover Universal",  "AC-010", "39269099", 18,    50,   100, 50, 12, ""),
                ("Laptop Bag 15.6",         "AC-011", "42029900", 18,   600,  1100, 10,  3, ""),
                ("Mouse Pad Large",         "AC-012", "39269099", 18,   120,   220, 20,  5, ""),
            ],
            "Networking": [
                ("WiFi Router Dual Band",   "NW-001", "85176990", 18,  1500,  2600,  8,  2, ""),
                ("WiFi Extender",           "NW-002", "85176990", 18,   800,  1400,  8,  3, ""),
                ("Network Switch 8 Port",   "NW-003", "85176990", 18,   900,  1600,  5,  2, ""),
                ("CAT6 Cable 10m",          "NW-004", "85444210", 18,   180,   320,  10,  3, ""),
                ("Ethernet Adapter USB",    "NW-005", "85176990", 18,   350,   650,  10,  3, ""),
                ("Pendrive 32GB",           "NW-006", "84717020", 18,   250,   450, 20,  5, "USB 3.0"),
                ("Pendrive 64GB",           "NW-007", "84717020", 18,   380,   680, 15,  5, ""),
                ("External HDD 1TB",        "NW-008", "84717020", 18,  2800,  4200,  5,  2, ""),
            ],
            "Power & UPS": [
                ("UPS 600VA",               "PW-001", "85044010", 12,  2000,  3000,  5,  2, ""),
                ("UPS 1000VA",              "PW-002", "85044010", 12,  3500,  5200,  3,  1, ""),
                ("Inverter 1000W",          "PW-003", "85044010", 12,  5500,  7500,  2,  1, ""),
                ("Extension Board 4m",      "PW-004", "85363010", 18,   250,   450, 20,  5, "6 socket"),
                ("Smart Plug WiFi",         "PW-005", "85363010", 18,   450,   800,  8,  3, ""),
                ("Laptop Cooling Pad",      "PW-006", "84145900", 18,   600,  1100,  8,  3, ""),
            ],
            "Printers & Scanners": [
                ("Inkjet Printer",          "PR-001", "84433100", 18, 5000,  7500,  3,  1, ""),
                ("Laser Printer B&W",       "PR-002", "84433210", 18, 9000, 13000,  2,  1, ""),
                ("Ink Cartridge Black",     "PR-003", "84439900", 18,  350,   600, 10,  3, ""),
                ("Ink Cartridge Color",     "PR-004", "84439900", 18,  550,   900,  8,  3, ""),
                ("A4 Paper 500 sheets",     "PR-005", "48025590", 12,  230,   350, 20,  5, "75 GSM"),
            ],
        }
    },

    # ──────────────────────────────────────────────────────────────────────────
    "electrical": {
        "shop_name": "_TEMPLATE_ELECTRICAL_",
        "categories": {
            "Wiring & Cables": [
                ("Copper Wire 1mm 90m",       "WR-001", "85444910", 18, 550,   750,  20,  5, "1mm single core"),
                ("Copper Wire 1.5mm 90m",     "WR-002", "85444910", 18, 750,  1050,  15,  5, "1.5mm single core"),
                ("Copper Wire 2.5mm 90m",     "WR-003", "85444910", 18,1100,  1500,  12,  3, "2.5mm single core"),
                ("Copper Wire 4mm 90m",       "WR-004", "85444910", 18,1600,  2200,   8,  2, "4mm single core"),
                ("Aluminium Wire 4mm 90m",    "WR-005", "85444910", 18, 650,   900,  10,  3, ""),
                ("Aluminium Wire 6mm 90m",    "WR-006", "85444910", 18, 900,  1250,   8,  2, ""),
                ("3-Core Flexible Cable 1mm", "WR-007", "85444920", 18, 480,   680,  15,  4, "per 10m roll"),
                ("3-Core Flexible Cable 1.5mm","WR-008","85444920", 18, 680,   950,  12,  3, "per 10m roll"),
                ("Armoured Cable 4-Core 1.5mm","WR-009","85444920", 18,1200,  1700,   6,  2, ""),
                ("Cable Roll 2-Core 0.75mm",  "WR-010", "85444920", 18, 320,   480,  20,  5, "100m roll"),
            ],
            "Switches & Boards": [
                ("Modular Switch 6A 1-Way",   "SW-001", "85365010", 18,  18,    32,  80, 20, ""),
                ("Modular Switch 6A 2-Way",   "SW-002", "85365010", 18,  25,    42,  60, 15, ""),
                ("Modular Switch 16A",         "SW-003", "85365010", 18,  35,    58,  50, 12, "Heavy duty"),
                ("5A 3-Pin Socket",            "SW-004", "85365090", 18,  22,    38,  60, 15, ""),
                ("15A 3-Pin Socket",           "SW-005", "85365090", 18,  45,    75,  40, 10, ""),
                ("Modular Switch Board 3M",    "SW-006", "85365090", 18,  85,   140,  30,  8, "Concealed"),
                ("Modular Switch Board 6M",    "SW-007", "85365090", 18, 150,   240,  20,  6, ""),
                ("Extension Board 4 Socket",   "SW-008", "85363010", 18, 180,   280,  25,  8, "With surge"),
                ("Extension Board 6 Socket",   "SW-009", "85363010", 18, 250,   380,  20,  6, "With surge"),
                ("MCB 6A Single Pole",         "SW-010", "85362000", 18, 120,   180,  20,  5, ""),
                ("MCB 16A Single Pole",        "SW-011", "85362000", 18, 135,   200,  20,  5, ""),
                ("MCB 32A Single Pole",        "SW-012", "85362000", 18, 150,   225,  15,  4, ""),
                ("MCB 63A Double Pole",        "SW-013", "85362000", 18, 380,   560,  10,  3, ""),
                ("RCCB 25A 30mA",             "SW-014", "85362010", 18, 550,   820,   8,  2, ""),
                ("Distribution Box 4-Way",    "SW-015", "85369090", 18, 220,   350,  12,  3, ""),
            ],
            "Lighting": [
                ("LED Bulb 7W",               "LT-001", "94054090", 12,  38,    65,  80, 20, "Cool white"),
                ("LED Bulb 9W",               "LT-002", "94054090", 12,  42,    72,  80, 20, ""),
                ("LED Bulb 12W",              "LT-003", "94054090", 12,  52,    88,  60, 15, ""),
                ("LED Bulb 15W",              "LT-004", "94054090", 12,  62,   105,  50, 12, ""),
                ("LED Tube Light 20W 4ft",    "LT-005", "94054090", 12,  95,   160,  40, 10, ""),
                ("LED Tube Light 40W 8ft",    "LT-006", "94054090", 12, 165,   280,  25,  8, ""),
                ("LED Panel Light 12W",       "LT-007", "94054090", 12, 180,   300,  20,  5, "Square"),
                ("LED Panel Light 18W",       "LT-008", "94054090", 12, 240,   400,  15,  4, "Round"),
                ("LED Panel Light 24W",       "LT-009", "94054090", 12, 320,   520,  12,  3, ""),
                ("LED Strip Light 5m RGB",    "LT-010", "94054090", 12, 280,   480,  15,  4, "With remote"),
                ("LED Strip Light 5m WW",     "LT-011", "94054090", 12, 220,   380,  15,  4, "Warm white"),
                ("LED Spotlight 5W",          "LT-012", "94054090", 12,  85,   145,  30,  8, ""),
                ("LED Spotlight 10W",         "LT-013", "94054090", 12, 120,   200,  25,  6, ""),
                ("CFL 15W",                   "LT-014", "85393140", 12,  55,    90,  30,  8, ""),
                ("Emergency Light LED",       "LT-015", "94054090", 12, 280,   480,  15,  5, "8hr backup"),
                ("Solar LED Street Light 20W","LT-016", "94054090", 12,1200,  1900,   5,  2, ""),
            ],
            "Fans & Cooling": [
                ("Ceiling Fan 48 inch",        "FN-001","84145100", 18, 800,  1250,  10,  3, ""),
                ("Ceiling Fan 52 inch",        "FN-002","84145100", 18, 950,  1500,   8,  2, ""),
                ("BLDC Ceiling Fan 48 inch",   "FN-003","84145100", 18,1800,  2800,   5,  2, "Energy saving"),
                ("Wall Fan 16 inch",           "FN-004","84145100", 18, 600,   950,   8,  3, ""),
                ("Table Fan 16 inch",          "FN-005","84145100", 18, 650,  1050,   6,  2, ""),
                ("Exhaust Fan 6 inch",         "FN-006","84145100", 18, 280,   450,  12,  3, ""),
                ("Exhaust Fan 8 inch",         "FN-007","84145100", 18, 380,   600,  10,  3, ""),
                ("Pedestal Fan 18 inch",       "FN-008","84145100", 18,1200,  1900,   4,  2, ""),
                ("Air Cooler 20L",             "FN-009","84792000", 18,3500,  5500,   3,  1, ""),
                ("Capacitor 2.5 MFD",         "FN-010","85322100", 18,  25,    45,  30,  8, "Fan capacitor"),
                ("Capacitor 3.5 MFD",         "FN-011","85322100", 18,  30,    52,  25,  8, ""),
            ],
            "Heating Appliances": [
                ("Room Heater 1000W",          "HT-001","85161000", 18,1200,  1900,   6,  2, ""),
                ("Room Heater 2000W",          "HT-002","85161000", 18,1800,  2800,   4,  2, ""),
                ("Oil-Filled Radiator 9-Fin",  "HT-003","85161000", 18,4500,  7000,   2,  1, ""),
                ("Electric Iron 1000W",        "HT-004","85161000", 18, 450,   750,  10,  3, "Steam"),
                ("Electric Iron 1200W",        "HT-005","85161000", 18, 580,   950,   8,  2, "Dry"),
                ("Water Heater 3L",            "HT-006","85161000", 18,1500,  2400,   5,  2, "Instant"),
                ("Water Heater 15L",           "HT-007","85161000", 18,3800,  6000,   3,  1, "Storage"),
                ("Water Heater 25L",           "HT-008","85161000", 18,5500,  8500,   2,  1, "Storage"),
                ("Immersion Rod 1500W",        "HT-009","85161000", 18, 150,   250,  20,  5, ""),
                ("Immersion Rod 2000W",        "HT-010","85161000", 18, 180,   300,  15,  4, ""),
            ],
            "Kitchen Appliances": [
                ("Mixer Grinder 500W 3-Jar",   "KA-001","85094000", 18,1200,  1900,   6,  2, ""),
                ("Mixer Grinder 750W 3-Jar",   "KA-002","85094000", 18,1800,  2800,   4,  2, ""),
                ("Electric Cooktop 1-Burner",  "KA-003","85166000", 18, 850,  1350,   5,  2, "1500W"),
                ("Electric Cooktop 2-Burner",  "KA-004","85166000", 18,1500,  2400,   4,  2, ""),
                ("Induction Cooktop 1800W",    "KA-005","85166000", 18,1200,  1900,   5,  2, ""),
                ("Electric Kettle 1.5L",       "KA-006","85161000", 18, 480,   780,   8,  3, ""),
                ("Electric Kettle 1.8L",       "KA-007","85161000", 18, 580,   950,   6,  2, ""),
                ("Sandwich Maker",             "KA-008","85161000", 18, 680,  1100,   5,  2, ""),
                ("Toaster 2-Slice",            "KA-009","85161000", 18, 550,   900,   5,  2, ""),
                ("Hand Blender 250W",          "KA-010","85094000", 18, 480,   780,   6,  2, ""),
            ],
            "Small Devices & Accessories": [
                ("Bluetooth Speaker 5W",       "SD-001","85182200", 18, 350,   600,  12,  3, ""),
                ("Bluetooth Speaker 10W",      "SD-002","85182200", 18, 650,  1100,   8,  3, ""),
                ("LED Torch Small",            "SD-003","94054090", 12,  80,   140,  25,  8, ""),
                ("LED Torch Heavy Duty",       "SD-004","94054090", 12, 180,   300,  15,  5, ""),
                ("Emergency Light 12LED",      "SD-005","94054090", 12, 280,   480,  12,  4, "4hr backup"),
                ("Emergency Light 22LED",      "SD-006","94054090", 12, 380,   630,  10,  3, "8hr backup"),
                ("UPS 600VA",                  "SD-007","85044010", 12,1800,  2800,   4,  2, ""),
                ("UPS 1000VA",                 "SD-008","85044010", 12,2800,  4200,   3,  1, ""),
                ("Voltage Stabilizer 5KVA",    "SD-009","85044090", 18,2500,  4000,   3,  1, ""),
                ("Power Strip 6-Socket",       "SD-010","85363010", 18, 220,   380,  15,  4, "3m cord"),
                ("Soldering Iron 25W",         "SD-011","85154000", 18, 120,   200,  10,  3, ""),
                ("Multimeter Digital",         "SD-012","90302000", 18, 350,   600,   8,  2, ""),
                ("Electric Bell",             "SD-013","85311010", 18,  95,   160,  15,  4, ""),
                ("Door Bell Wireless",         "SD-014","85311010", 18, 280,   480,  10,  3, ""),
                ("Landline Phone",             "SD-015","85171810", 12, 450,   750,   6,  2, "Corded"),
            ],
        }
    },
}

# business_type aliases → template key
TYPE_MAP = {
    "grocery":     "grocery",
    "kirana":      "grocery",
    "supermarket": "grocery",
    "food":        "grocery",
    "medical":     "medical",
    "pharmacy":    "medical",
    "chemist":     "medical",
    "health":      "medical",
    "electronics": "electronics",
    "mobile":      "electronics",
    "computer":    "electronics",
    "gadgets":     "electronics",
    "electrical":  "electrical",
    "hardware":    "electrical",
    "wiring":      "electrical",
    "lighting":    "electrical",
}


# ═══════════════════════════════ TEMPLATE SHOP IDs ════════════════════════════

def get_template_shop_id(business_type: str) -> int | None:
    """Return the template shop id for the given business_type, or None."""
    key = TYPE_MAP.get((business_type or "").lower().strip())
    if not key:
        return {"categories": 0, "products": 0, "skipped": 0, "error": "No template for this business type"}

    # Guard: never write products into a template shop
    _conn = get_db()
    _row  = _conn.execute("SELECT is_template FROM shops WHERE id=?", (target_shop_id,)).fetchone()
    _conn.close()
    if _row and _row["is_template"]:
        return {"categories": 0, "products": 0, "skipped": 0,
                "error": "Cannot copy into a template shop"}

    conn = get_db()
    row  = conn.execute(
        "SELECT id FROM shops WHERE name=? AND is_template=1",
        (template_name,)
    ).fetchone()
    conn.close()
    return row["id"] if row else None


# ═══════════════════════════════ SEED TEMPLATE SHOPS ═════════════════════════

def seed_template_shops():
    """
    Create the three template shops and fill them with products.
    Idempotent — skips if already seeded.
    """
    conn = get_db()
    for btype, tdata in TEMPLATES.items():
        shop_name = tdata["shop_name"]
        # Skip if already seeded
        exists = conn.execute(
            "SELECT id FROM shops WHERE name=? AND is_template=1",
            (shop_name,)
        ).fetchone()
        if exists:
            continue

        # Create template shop (is_template=1, is_active=0 — never visible in UI)
        conn.execute("""INSERT INTO shops
            (name, is_template, is_active, business_type, state_code)
            VALUES (?, 1, 0, ?, '27')""",
            (shop_name, btype))
        shop_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        _insert_template_products(conn, shop_id, tdata["categories"])
        conn.commit()
        print(f"[Templates] Seeded {btype} template (shop_id={shop_id})")

    conn.close()


def _insert_template_products(conn, shop_id: int, categories: dict):
    """Insert categories + products for a template shop."""
    for cat_name, products in categories.items():
        conn.execute(
            "INSERT OR IGNORE INTO categories (name, shop_id) VALUES (?, ?)",
            (cat_name, shop_id)
        )
        cat_row = conn.execute(
            "SELECT id FROM categories WHERE name=? AND shop_id=?",
            (cat_name, shop_id)
        ).fetchone()
        cat_id = cat_row["id"]

        for (name, sku, hsn, gst_rate,
             cost, sell, stock, threshold, desc) in products:
            conn.execute("""
                INSERT OR IGNORE INTO products
                  (shop_id, name, sku, category_id, hsn_code, gst_rate,
                   cost_price, selling_price, stock_quantity,
                   low_stock_threshold, description, is_active)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,1)
            """, (shop_id, name, sku, cat_id, hsn, gst_rate,
                  cost, sell, stock, threshold, desc))


# ═══════════════════════════════ COPY TO NEW SHOP ════════════════════════════

def copy_products_to_shop(target_shop_id: int, business_type: str) -> dict:
    """
    Copy all categories and products from the matching template shop
    into target_shop_id.

    Returns {"categories": n, "products": n, "skipped": n}
    Safe to call multiple times — SKUs already present are skipped (not duplicated).
    """
    # Never write into a template shop itself
    _chk = get_db()
    _row = _chk.execute("SELECT is_template FROM shops WHERE id=?", (target_shop_id,)).fetchone()
    _chk.close()
    if _row and _row["is_template"]:
        return {"categories": 0, "products": 0, "skipped": 0,
                "error": "Cannot copy into a template shop"}

    key = TYPE_MAP.get((business_type or "").lower().strip())
    if not key:
        return {"categories": 0, "products": 0, "skipped": 0,
                "error": "No template for this business type"}

    conn = get_db()
    template_name = TEMPLATES[key]["shop_name"]
    tmpl = conn.execute(
        "SELECT id FROM shops WHERE name=? AND is_template=1",
        (template_name,)
    ).fetchone()

    if not tmpl:
        conn.close()
        return {"categories": 0, "products": 0, "skipped": 0, "error": "Template shop not seeded yet"}

    tmpl_id    = tmpl["id"]
    cat_copied = 0
    prd_copied = 0
    prd_skipped= 0

    # ── Copy categories ────────────────────────────────────────────────────────
    tmpl_cats = conn.execute(
        "SELECT * FROM categories WHERE shop_id=?", (tmpl_id,)
    ).fetchall()

    cat_id_map = {}   # old_cat_id → new_cat_id

    for tc in tmpl_cats:
        # Check if category already exists
        existing = conn.execute(
            "SELECT id FROM categories WHERE name=? AND shop_id=?",
            (tc["name"], target_shop_id)
        ).fetchone()

        if existing:
            cat_id_map[tc["id"]] = existing["id"]
        else:
            conn.execute(
                "INSERT INTO categories (name, shop_id) VALUES (?, ?)",
                (tc["name"], target_shop_id)
            )
            new_cat_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            cat_id_map[tc["id"]] = new_cat_id
            cat_copied += 1

    # ── Copy products ──────────────────────────────────────────────────────────
    tmpl_prods = conn.execute(
        "SELECT * FROM products WHERE shop_id=? AND is_active=1",
        (tmpl_id,)
    ).fetchall()

    for tp in tmpl_prods:
        # Skip if SKU already exists for this shop
        existing = conn.execute(
            "SELECT id FROM products WHERE sku=? AND shop_id=?",
            (tp["sku"], target_shop_id)
        ).fetchone()

        if existing:
            prd_skipped += 1
            continue

        new_cat_id = cat_id_map.get(tp["category_id"])
        conn.execute("""
            INSERT INTO products
              (shop_id, name, sku, category_id, hsn_code, gst_rate,
               cost_price, selling_price, stock_quantity,
               low_stock_threshold, description, barcode, is_active)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)
        """, (target_shop_id,
              tp["name"], tp["sku"], new_cat_id,
              tp["hsn_code"], tp["gst_rate"],
              tp["cost_price"], tp["selling_price"],
              tp["stock_quantity"], tp["low_stock_threshold"],
              tp["description"] or "", tp["barcode"] or ""))
        prd_copied += 1

    conn.commit()
    conn.close()
    return {"categories": cat_copied, "products": prd_copied, "skipped": prd_skipped}


# ═══════════════════════════════ RESET / RE-IMPORT ════════════════════════════

def reset_shop_products(target_shop_id: int, business_type: str) -> dict:
    """
    Soft-delete all existing products + categories for the shop, then
    re-import from the matching template. Used by the Reset button.

    Steps:
      1. Soft-delete all products (is_active=0) — keeps FK refs from invoice_items intact
      2. Set category_id=NULL on those products so categories can be deleted
      3. Delete all categories for this shop
      4. Re-copy from template
    """
    conn = get_db()
    # Step 1: soft-delete products
    conn.execute(
        "UPDATE products SET is_active=0, category_id=NULL WHERE shop_id=?",
        (target_shop_id,)
    )
    # Step 2: now safe to delete categories (no FK pointing to them)
    conn.execute("DELETE FROM categories WHERE shop_id=?", (target_shop_id,))
    conn.commit()
    conn.close()

    return copy_products_to_shop(target_shop_id, business_type)


# ═══════════════════════════════ HELPERS ════════════════════════════════════

def available_business_types() -> list:
    """Return list of (value, label) for business type dropdown."""
    return [
        ("general",     "General / Other"),
        ("grocery",     "Grocery / Kirana Store"),
        ("supermarket", "Supermarket"),
        ("medical",     "Medical / Pharmacy"),
        ("pharmacy",    "Pharmacy / Chemist"),
        ("electronics", "Electronics Shop"),
        ("mobile",      "Mobile / Phone Shop"),
        ("computer",    "Computer / IT Shop"),
        ("electrical",  "Electrical / Hardware Shop"),
        ("hardware",    "Hardware Store"),
        ("lighting",    "Lighting / Electrical"),
    ]
