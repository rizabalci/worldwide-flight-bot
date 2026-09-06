"""
Worldwide Flight Bot — daily deal scanner from VIE/BTS to two tiers:

  SHORT-HAUL (Europe + UK):  round-trip, direct only, 4 months ahead
  LONG-HAUL  (Worldwide):    round-trip, stops allowed, 6 months ahead

Each city has its own round-trip target price. Per route, an alert fires if:
  1. Below target  -- the fare is at or under the target
  2. Big drop      -- the fare is >= ROLLING_DROP_PCT below the route's
                      rolling average across its recent history

Stack: Travelpayouts (Aviasales v3) + Telegram + GitHub Actions. Free.

Two separate Telegram messages per day, one per tier. Either is silent
when its tier has no deals.

Environment variables:
    TRAVELPAYOUTS_TOKEN   (secret)
    TELEGRAM_BOT_TOKEN    (secret)
    TELEGRAM_CHAT_ID      (secret)
    ROLLING_WINDOW_DAYS   (var)   default 14
    ROLLING_DROP_PCT      (var)   default 0.20
    SHORT_HAUL_MONTHS     (var)   default 4
    LONG_HAUL_MONTHS      (var)   default 6
    ORIGINS               (var)   default "VIE,BTS"
"""

import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from statistics import mean

import requests

# -------------------- Secrets / tuning --------------------

TRAVELPAYOUTS_TOKEN = os.environ["TRAVELPAYOUTS_TOKEN"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


def env_int(name: str, default: int) -> int:
    """Read an int env var. Empty string or unset -> default.
    (GitHub Actions passes unset repo variables as '', which int() rejects.)"""
    v = os.environ.get(name, "").strip()
    try:
        return int(v) if v else default
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    """Read a float env var. Empty string or unset -> default."""
    v = os.environ.get(name, "").strip()
    try:
        return float(v) if v else default
    except ValueError:
        return default


ROLLING_WINDOW_DAYS = env_int("ROLLING_WINDOW_DAYS", 14)
ROLLING_DROP_PCT = env_float("ROLLING_DROP_PCT", 0.20)
# A fare must be at least this far UNDER target to count as a "below target" hit.
# Kills barely-under noise like "Athens €64 vs target €65". 0.10 = must be >=10%
# under target. Set 0.0 to alert on anything at or below target (old behavior).
TARGET_MARGIN_PCT = env_float("TARGET_MARGIN_PCT", 0.10)
ORIGINS = [o.strip().upper() for o in os.environ.get("ORIGINS", "").split(",") if o.strip()] or ["VIE", "BTS"]

# Watchlist: routes you want to SEE on every run, even when not a deal.
# Comma-separated IATA codes. The bot shows each one's current cheapest fare
# in a "👀 Watching" section, and flags it if it also clears its deal target.
# Set/extend via the GitHub variable WATCHLIST, e.g. "DPS,NRT,JFK".
WATCHLIST = [c.strip().upper() for c in os.environ.get("WATCHLIST", "").split(",") if c.strip()] or ["DPS"]
FOCUS = {c.strip().upper() for c in os.environ.get("FOCUS", "").split(",") if c.strip()}
# FOCUS: when set, scan ONLY these destination codes. Empty = scan everything.
# How the watchlist measures "cheapest": respects the same trip-length caps as
# normal scanning so it won't show a watchlist price for a 23-night trip.

# Weekend / short-break mode. When WEEKEND_ONLY=true, only keep round-trips
# matching the trip length (MIN_NIGHTS..MAX_NIGHTS) and departure days (DEP_DAYS).
# Flip the GitHub variable WEEKEND_ONLY between "true" and "false" anytime.
#
#   MIN_NIGHTS / MAX_NIGHTS : trip length in NIGHTS (a Fri->Mon trip = 3 nights).
#       3 days  = 2 nights      |  4 days = 3 nights  |  1 week = 7 nights
#   DEP_DAYS : comma list of allowed departure weekdays, or "any".
#       e.g. "thu,fri"  |  "fri"  |  "any"
WEEKEND_ONLY = os.environ.get("WEEKEND_ONLY", "false").lower() == "true"
MIN_NIGHTS = env_int("MIN_NIGHTS", 2)
MAX_NIGHTS = env_int("MAX_NIGHTS", 4)

_DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_dep_raw = os.environ.get("DEP_DAYS", "thu,fri").strip().lower()
if _dep_raw in ("any", "all", ""):
    DEP_DAYS = set(range(7))  # any day allowed
else:
    DEP_DAYS = {_DAY_NAMES[d.strip()] for d in _dep_raw.split(",") if d.strip() in _DAY_NAMES}
    if not DEP_DAYS:  # fallback if the value was garbage
        DEP_DAYS = {3, 4}

# -------------------- Tier rules --------------------

SHORT_HAUL = {
    "name": "short",
    "label": "Europe",
    "arrow": "↔",
    "trip_word": "round-trip",
    "trip_type": "rt",
    "one_way": "false",
    "direct": "true",
    "months_ahead": env_int("SHORT_HAUL_MONTHS", 4),
    # Never surface trips longer than this, even in normal mode. Short European
    # hops longer than a week are rarely what you want.
    "max_nights": env_int("SHORT_HAUL_MAX_NIGHTS", 7),
}

LONG_HAUL = {
    "name": "long",
    "label": "Worldwide",
    "arrow": "↔",
    "trip_word": "round-trip",
    "trip_type": "rt",
    "one_way": "false",
    "direct": "false",
    "months_ahead": env_int("LONG_HAUL_MONTHS", 6),
    # Long-haul justifies a longer stay (the flight eats a day each way).
    "max_nights": env_int("LONG_HAUL_MAX_NIGHTS", 15),
}

# -------------------- Short-haul destinations (round-trip EUR, direct) --------------------
#
# UK is INCLUDED. Turkish passport + Slovak EU residence = UK Standard
# Visitor Visa needed (~3 weeks lead). Ireland is EU-but-non-Schengen, no
# visa needed. Cities labelled "(visa)" need real visa effort.

SHORT_HAUL_DESTINATIONS = {
    # --- United Kingdom & Crown Dependencies (visa needed) ---
    "LHR": ("London Heathrow (visa)", 210),
    "LGW": ("London Gatwick (visa)", 180),
    "STN": ("London Stansted (visa)", 130),
    "LTN": ("London Luton (visa)", 120),
    "LCY": ("London City (visa)", 90),
    "MAN": ("Manchester (visa)", 120),
    "EDI": ("Edinburgh (visa)", 60),
    "GLA": ("Glasgow (visa)", 70),
    "ABZ": ("Aberdeen (visa)", 100),
    "INV": ("Inverness (visa)", 110),
    "BHX": ("Birmingham (visa)", 60),
    "BRS": ("Bristol (visa)", 70),
    "EMA": ("East Midlands (visa)", 70),
    "LBA": ("Leeds Bradford (visa)", 75),
    "LPL": ("Liverpool (visa)", 70),
    "NCL": ("Newcastle (visa)", 75),
    "SOU": ("Southampton (visa)", 95),
    "BFS": ("Belfast Intl (visa)", 80),
    "BHD": ("Belfast City (visa)", 90),
    "LDY": ("Derry (visa)", 100),
    # --- Ireland ---
    "DUB": ("Dublin", 50),
    "ORK": ("Cork", 70),
    "SNN": ("Shannon", 80),
    "NOC": ("Knock", 90),
    "KIR": ("Kerry", 100),
    # --- Italy ---
    "FCO": ("Rome Fiumicino", 30),
    "CIA": ("Rome Ciampino", 30),
    "MXP": ("Milan Malpensa", 30),
    "BGY": ("Milan Bergamo", 25),
    "LIN": ("Milan Linate", 50),
    "TRN": ("Turin", 40),
    "RMI": ("Rimini", 55),
    "BLQ": ("Bologna", 28),
    "FLR": ("Florence", 50),
    "PSA": ("Pisa", 35),
    "VCE": ("Venice Marco Polo", 28),
    "TSF": ("Venice Treviso", 28),
    "VRN": ("Verona", 40),
    "TRS": ("Trieste", 45),
    "GOA": ("Genoa", 50),
    "AOI": ("Ancona", 50),
    "PSR": ("Pescara", 50),
    "NAP": ("Naples", 40),
    "BRI": ("Bari", 40),
    "SUF": ("Lamezia Terme (Calabria)", 50),
    "CTA": ("Catania (Sicily)", 48),
    "PMO": ("Palermo (Sicily)", 48),
    "TPS": ("Trapani (Sicily)", 48),
    "OLB": ("Olbia (Sardinia)", 60),
    "CAG": ("Cagliari (Sardinia)", 55),
    "AHO": ("Alghero (Sardinia)", 55),
    # --- Iberia ---
    "BCN": ("Barcelona", 40),
    "GRO": ("Girona", 48),
    "REU": ("Reus", 50),
    "ZAZ": ("Zaragoza", 60),
    "VLL": ("Valladolid", 90),
    "RGS": ("Burgos", 110),
    "MAD": ("Madrid", 50),
    "VLC": ("Valencia", 48),
    "ALC": ("Alicante", 50),
    "MJV": ("Murcia", 60),
    "AGP": ("Malaga", 55),
    "GRX": ("Granada", 75),
    "LEI": ("Almeria", 70),
    "SVQ": ("Seville", 60),
    "BJZ": ("Badajoz", 110),
    "BIO": ("Bilbao", 70),
    "EAS": ("San Sebastian", 90),
    "SDR": ("Santander", 75),
    "OVD": ("Asturias", 80),
    "LEN": ("Leon", 110),
    "SCQ": ("Santiago de Compostela", 70),
    "VGO": ("Vigo", 75),
    "PMI": ("Palma de Mallorca", 48),
    "IBZ": ("Ibiza", 60),
    "MAH": ("Menorca", 65),
    "TFS": ("Tenerife South", 90),
    "TFN": ("Tenerife North", 90),
    "LPA": ("Gran Canaria", 90),
    "ACE": ("Lanzarote", 90),
    "FUE": ("Fuerteventura", 90),
    "LIS": ("Lisbon", 55),
    "OPO": ("Porto", 55),
    "FAO": ("Faro (Algarve)", 65),
    "FNC": ("Funchal (Madeira)", 100),
    "PDL": ("Ponta Delgada (Azores)", 130),
    # --- France ---
    "CDG": ("Paris CDG", 48),
    "ORY": ("Paris Orly", 48),
    "BVA": ("Paris Beauvais", 42),
    "NCE": ("Nice", 55),
    "MRS": ("Marseille", 60),
    "TLN": ("Toulon-Hyeres", 75),
    "LYS": ("Lyon", 65),
    "TLS": ("Toulouse", 65),
    "BOD": ("Bordeaux", 65),
    "BIQ": ("Biarritz", 75),
    "FNI": ("Nimes", 75),
    "MPL": ("Montpellier", 70),
    "NTE": ("Nantes", 65),
    "RNS": ("Rennes", 75),
    "LIL": ("Lille", 60),
    "SXB": ("Strasbourg", 70),
    "AJA": ("Ajaccio (Corsica)", 100),
    "BIA": ("Bastia (Corsica)", 100),
    # --- Benelux ---
    "AMS": ("Amsterdam", 48),
    "EIN": ("Eindhoven", 48),
    "RTM": ("Rotterdam", 60),
    "GRQ": ("Groningen", 75),
    "MST": ("Maastricht", 75),
    "BRU": ("Brussels", 42),
    "CRL": ("Brussels Charleroi", 35),
    "ANR": ("Antwerp", 65),
    "OST": ("Ostend", 75),
    "LGG": ("Liege", 75),
    "LUX": ("Luxembourg", 75),
    # --- Germany ---
    "BER": ("Berlin", 35),
    "HAM": ("Hamburg", 42),
    "MUC": ("Munich", 48),
    "NUE": ("Nuremberg", 48),
    "FMM": ("Memmingen", 48),
    "FRA": ("Frankfurt", 48),
    "HHN": ("Frankfurt-Hahn", 48),
    "NRN": ("Weeze (Ryanair hub)", 42),
    "DUS": ("Dusseldorf", 48),
    "CGN": ("Cologne", 42),
    "DTM": ("Dortmund", 60),
    "PAD": ("Paderborn", 75),
    "STR": ("Stuttgart", 48),
    "FKB": ("Karlsruhe/Baden-Baden", 60),
    "FDH": ("Friedrichshafen", 65),
    "LEJ": ("Leipzig", 48),
    "DRS": ("Dresden", 48),
    "ERF": ("Erfurt", 75),
    "BRE": ("Bremen", 60),
    "HAJ": ("Hannover", 60),
    "RLG": ("Rostock-Laage", 110),
    # --- Switzerland ---
    "ZRH": ("Zurich", 80),
    "GVA": ("Geneva", 80),
    "BSL": ("Basel", 65),
    "BRN": ("Bern", 100),
    "LUG": ("Lugano", 85),
    # --- Czechia / Slovakia ---
    "PRG": ("Prague", 35),
    "BRQ": ("Brno", 48),
    "OSR": ("Ostrava", 48),
    "PED": ("Pardubice", 60),
    "KSC": ("Kosice", 42),
    # --- Poland ---
    "WAW": ("Warsaw", 35),
    "KRK": ("Krakow", 35),
    "GDN": ("Gdansk", 42),
    "POZ": ("Poznan", 42),
    "WRO": ("Wroclaw", 35),
    "KTW": ("Katowice", 35),
    "LCJ": ("Lodz", 50),
    "LUZ": ("Lublin", 55),
    "BZG": ("Bydgoszcz", 55),
    "RZE": ("Rzeszow", 50),
    "SZZ": ("Szczecin", 50),
    # --- Romania ---
    "OTP": ("Bucharest", 30),
    "CLJ": ("Cluj-Napoca", 42),
    "IAS": ("Iasi", 50),
    "TSR": ("Timisoara", 42),
    "SBZ": ("Sibiu", 55),
    "CND": ("Constanta", 55),
    # --- Bulgaria ---
    "SOF": ("Sofia", 30),
    "VAR": ("Varna", 50),
    "BOJ": ("Burgas", 50),
    "PDV": ("Plovdiv", 65),
    # --- Moldova ---
    "KIV": ("Chisinau", 75),
    # --- Croatia ---
    "ZAG": ("Zagreb", 48),
    "SPU": ("Split", 55),
    "DBV": ("Dubrovnik", 60),
    "ZAD": ("Zadar", 48),
    "PUY": ("Pula", 48),
    "RJK": ("Rijeka", 60),
    "OSI": ("Osijek", 70),
    # --- Balkans ---
    "BEG": ("Belgrade", 42),
    "INI": ("Nis", 65),
    "SJJ": ("Sarajevo", 55),
    "TZL": ("Tuzla", 42),
    "BNX": ("Banja Luka", 75),
    "TGD": ("Podgorica", 60),
    "TIV": ("Tivat", 65),
    "TIA": ("Tirana", 55),
    "SKP": ("Skopje", 48),
    "OHD": ("Ohrid", 65),
    "PRN": ("Pristina", 60),
    # --- Greece ---
    "ATH": ("Athens", 42),
    "SKG": ("Thessaloniki", 42),
    "KVA": ("Kavala", 65),
    "JTY": ("Astypalaia", 130),
    "JIK": ("Ikaria", 110),
    "JKH": ("Chios", 90),
    "LXS": ("Limnos", 110),
    "MJT": ("Mytilene (Lesvos)", 90),
    "SMI": ("Samos", 90),
    "JSI": ("Skiathos", 90),
    "SKU": ("Skyros", 120),
    "VOL": ("Volos", 75),
    "JNX": ("Naxos", 110),
    "PAS": ("Paros", 110),
    "MLO": ("Milos", 120),
    "JSH": ("Sitia (Crete)", 120),
    "HER": ("Heraklion (Crete)", 65),
    "CHQ": ("Chania (Crete)", 65),
    "RHO": ("Rhodes", 65),
    "AOK": ("Karpathos", 120),
    "JTR": ("Santorini", 75),
    "JMK": ("Mykonos", 90),
    "CFU": ("Corfu", 60),
    "PVK": ("Preveza/Lefkada", 90),
    "KGS": ("Kos", 70),
    "ZTH": ("Zakynthos", 70),
    "JKL": ("Kalamata", 75),
    "EFL": ("Kefalonia", 75),
    "SMI": ("Samos", 90),
    "JSI": ("Skiathos", 90),
    "MJT": ("Mytilene (Lesvos)", 90),
    # --- Cyprus & Malta ---
    "LCA": ("Larnaca (Cyprus)", 70),
    "PFO": ("Paphos (Cyprus)", 85),
    "MLA": ("Malta", 48),
    # --- Nordics ---
    "CPH": ("Copenhagen", 48),
    "AAR": ("Aarhus", 90),
    "AAL": ("Aalborg", 100),
    "BLL": ("Billund", 90),
    "RNN": ("Bornholm", 130),
    "ARN": ("Stockholm Arlanda", 48),
    "BMA": ("Stockholm Bromma", 65),
    "NYO": ("Stockholm Skavsta", 65),
    "GOT": ("Gothenburg", 60),
    "MMX": ("Malmo", 65),
    "VBY": ("Visby (Gotland)", 120),
    "UME": ("Umea", 150),
    "LLA": ("Lulea", 160),
    "OSL": ("Oslo", 55),
    "TRF": ("Oslo Torp", 65),
    "BGO": ("Bergen", 75),
    "SVG": ("Stavanger", 100),
    "TRD": ("Trondheim", 110),
    "AES": ("Alesund", 130),
    "MOL": ("Molde", 150),
    "KSU": ("Kristiansund", 160),
    "KRS": ("Kristiansand", 120),
    "BOO": ("Bodo (Lofoten)", 190),
    "EVE": ("Harstad/Narvik (Lofoten)", 210),
    "ALF": ("Alta (Arctic)", 230),
    "KKN": ("Kirkenes (Arctic)", 250),
    "LYR": ("Longyearbyen (Svalbard)", 380),
    "TOS": ("Tromso (Arctic)", 130),
    "HEL": ("Helsinki", 60),
    "TKU": ("Turku", 90),
    "TMP": ("Tampere", 90),
    "OUL": ("Oulu", 120),
    "KAJ": ("Kajaani", 140),
    "RVN": ("Rovaniemi (Lapland)", 110),
    "IVL": ("Ivalo (north Lapland)", 170),
    "KEF": ("Reykjavik", 110),
    "AEY": ("Akureyri (north Iceland)", 220),
    "FAE": ("Faroe Islands (Vagar)", 250),
    # --- Baltics ---
    "TLL": ("Tallinn", 48),
    "RIX": ("Riga", 48),
    "VNO": ("Vilnius", 48),
    "KUN": ("Kaunas", 55),
    "PLQ": ("Palanga", 90),
    # --- Turkey (coastal/holiday; Istanbul covered by separate bot) ---
    "AYT": ("Antalya", 70),
    "ADB": ("Izmir", 70),
    "BJV": ("Bodrum", 90),
    "DLM": ("Dalaman", 90),
}

# -------------------- Long-haul destinations (round-trip EUR, stops OK) ---------
#
# Visa quick reference for Turkish passport + Slovak EU residence:
#   * Visa needed: USA, Canada, Australia, NZ, China (rules eased 2024-25
#                  - verify), parts of Africa, Iran (skipped here)
#   * eVisa / VoA: India, Sri Lanka, Nepal, Kenya, Egypt, Indonesia, Cambodia
#   * Visa-free:   most of Latin America, Japan, South Korea, Thailand,
#                  Malaysia, Singapore, Hong Kong, UAE, Qatar, Georgia,
#                  Morocco, South Africa, Mexico, Cuba, Dominican Republic

LONG_HAUL_DESTINATIONS = {
    # --- Middle East / Levant ---
    "DXB": ("Dubai", 140),
    "AUH": ("Abu Dhabi", 160),
    "SHJ": ("Sharjah", 160),
    "DOH": ("Doha", 200),
    "BAH": ("Bahrain", 200),
    "KWI": ("Kuwait", 200),
    "MCT": ("Muscat", 220),
    "RUH": ("Riyadh", 280),
    "JED": ("Jeddah", 280),
    "DMM": ("Dammam", 280),
    "MED": ("Medina", 320),
    "TLV": ("Tel Aviv", 200),
    "ETM": ("Eilat", 250),
    "BEY": ("Beirut", 200),
    "AMM": ("Amman", 200),
    "BGW": ("Baghdad (visa)", 320),
    "EBL": ("Erbil (Kurdistan)", 280),
    "IKA": ("Tehran (eVisa)", 240),
    "MHD": ("Mashhad (eVisa)", 300),
    # --- North Africa ---
    "CAI": ("Cairo (eVisa)", 200),
    "HRG": ("Hurghada (eVisa)", 240),
    "SSH": ("Sharm el-Sheikh (eVisa)", 240),
    "LXR": ("Luxor (eVisa)", 300),
    "RAK": ("Marrakech", 120),
    "CMN": ("Casablanca", 160),
    "AGA": ("Agadir", 200),
    "FEZ": ("Fez", 220),
    "TNG": ("Tangier", 180),
    "OUD": ("Oujda", 200),
    "NDR": ("Nador", 200),
    "TUN": ("Tunis", 160),
    "DJE": ("Djerba", 220),
    "ALG": ("Algiers", 200),
    "ORN": ("Oran", 220),
    # --- Caucasus / Central Asia / Mongolia ---
    "TBS": ("Tbilisi", 200),
    "KUT": ("Kutaisi (Wizz hub)", 80),
    "BUS": ("Batumi", 320),
    "GYD": ("Baku", 190),
    "TAS": ("Tashkent", 430),
    "SKD": ("Samarkand", 620),
    "ALA": ("Almaty", 400),
    "NQZ": ("Astana", 460),
    "DYU": ("Dushanbe", 590),
    # --- East Asia ---
    "NRT": ("Tokyo Narita", 850),
    "HND": ("Tokyo Haneda", 840),
    "KIX": ("Osaka Kansai", 940),
    "NGO": ("Nagoya", 1240),
    "FUK": ("Fukuoka", 910),
    "CTS": ("Sapporo", 1070),
    "OKA": ("Okinawa", 980),
    "ICN": ("Seoul Incheon", 590),
    "PUS": ("Busan", 890),
    "TPE": ("Taipei", 640),
    "HKG": ("Hong Kong", 630),
    "MFM": ("Macau", 860),
    "PVG": ("Shanghai (visa)", 580),
    "PEK": ("Beijing (visa)", 510),
    "CAN": ("Guangzhou (visa)", 570),
    "CTU": ("Chengdu (visa)", 710),
    "XIY": ("Xian (visa)", 840),
    "KMG": ("Kunming (visa)", 570),
    "SYX": ("Sanya (Hainan, visa-free)", 540),
    # --- Southeast Asia ---
    "BKK": ("Bangkok", 500),
    "DMK": ("Bangkok Don Mueang", 710),
    "HKT": ("Phuket", 540),
    "USM": ("Koh Samui", 890),
    "CNX": ("Chiang Mai", 760),
    "KBV": ("Krabi", 640),
    "KUL": ("Kuala Lumpur", 540),
    "SIN": ("Singapore", 620),
    "DPS": ("Bali", 760),
    "CGK": ("Jakarta", 610),
    "LOP": ("Lombok", 1040),
    "MNL": ("Manila", 640),
    "CEB": ("Cebu", 810),
    "SGN": ("Ho Chi Minh City", 500),
    "HAN": ("Hanoi", 660),
    "DAD": ("Da Nang", 790),
    "CXR": ("Nha Trang", 680),
    "VTE": ("Vientiane", 800),
    "LPQ": ("Luang Prabang", 850),
    "RGN": ("Yangon", 700),
    "BWN": ("Bandar Seri Begawan", 900),
    "ULN": ("Ulaanbaatar", 750),
    "FRU": ("Bishkek", 500),
    "REP": ("Siem Reap (eVisa)", 700),
    "PNH": ("Phnom Penh (eVisa)", 700),
    # --- South Asia ---
    "DEL": ("Delhi (eVisa)", 540),
    "BOM": ("Mumbai (eVisa)", 490),
    "BLR": ("Bangalore (eVisa)", 490),
    "MAA": ("Chennai (eVisa)", 510),
    "HYD": ("Hyderabad (eVisa)", 530),
    "CCU": ("Kolkata (eVisa)", 610),
    "GOI": ("Goa (eVisa)", 690),
    "JAI": ("Jaipur (eVisa)", 460),
    "COK": ("Cochin (eVisa)", 710),
    "KTM": ("Kathmandu", 760),
    "CMB": ("Colombo (eVisa)", 560),
    "MLE": ("Maldives", 620),
    # --- North America (visa needed) ---
    "JFK": ("New York JFK (visa)", 570),
    "EWR": ("New York Newark (visa)", 590),
    "BOS": ("Boston (visa)", 440),
    "IAD": ("Washington Dulles (visa)", 760),
    "PHL": ("Philadelphia (visa)", 760),
    "MIA": ("Miami (visa)", 670),
    "FLL": ("Fort Lauderdale (visa)", 880),
    "MCO": ("Orlando (visa)", 760),
    "TPA": ("Tampa (visa)", 850),
    "ATL": ("Atlanta (visa)", 770),
    "ORD": ("Chicago (visa)", 740),
    "MSP": ("Minneapolis (visa)", 950),
    "DFW": ("Dallas (visa)", 910),
    "IAH": ("Houston (visa)", 860),
    "AUS": ("Austin (visa)", 1010),
    "DEN": ("Denver (visa)", 480),
    "PHX": ("Phoenix (visa)", 970),
    "LAS": ("Las Vegas (visa)", 890),
    "LAX": ("Los Angeles (visa)", 570),
    "SFO": ("San Francisco (visa)", 540),
    "SEA": ("Seattle (visa)", 800),
    "PDX": ("Portland OR (visa)", 960),
    "HNL": ("Honolulu (visa)", 920),
    "OGG": ("Maui (visa)", 700),
    "ANC": ("Anchorage (visa)", 560),
    "YYZ": ("Toronto (visa)", 360),
    "YUL": ("Montreal (visa)", 360),
    "YOW": ("Ottawa (visa)", 400),
    "YQB": ("Quebec City (visa)", 440),
    "YHZ": ("Halifax (visa)", 440),
    "YYC": ("Calgary (visa)", 520),
    "YEG": ("Edmonton (visa)", 520),
    "YVR": ("Vancouver (visa)", 480),
    # --- Mexico / Caribbean / Central America ---
    "MEX": ("Mexico City", 910),
    "GDL": ("Guadalajara", 480),
    "MTY": ("Monterrey", 480),
    "CUN": ("Cancun", 750),
    "CZM": ("Cozumel", 480),
    "PVR": ("Puerto Vallarta", 1090),
    "SJD": ("Los Cabos", 1590),
    "MID": ("Merida", 520),
    "HAV": ("Havana", 980),
    "PUJ": ("Punta Cana", 810),
    "SDQ": ("Santo Domingo", 1040),
    "MBJ": ("Montego Bay (Jamaica)", 870),
    "KIN": ("Kingston (Jamaica)", 1070),
    "AUA": ("Aruba", 1380),
    "CUR": ("Curacao", 1240),
    "BGI": ("Barbados", 1280),
    "POS": ("Trinidad", 1460),
    "PTY": ("Panama City", 970),
    "SJO": ("San Jose Costa Rica", 830),
    "LIR": ("Liberia (Costa Rica)", 1720),
    "GUA": ("Guatemala City", 560),
    "SAL": ("San Salvador", 600),
    "BZE": ("Belize", 1440),
    # --- South America ---
    "GRU": ("Sao Paulo", 910),
    "GIG": ("Rio de Janeiro", 1000),
    "BSB": ("Brasilia", 1110),
    "FOR": ("Fortaleza", 1040),
    "EZE": ("Buenos Aires", 990),
    "COR": ("Cordoba", 1380),
    "SCL": ("Santiago de Chile", 980),
    "LIM": ("Lima", 990),
    "BOG": ("Bogota", 920),
    "CTG": ("Cartagena", 940),
    "MDE": ("Medellin", 1050),
    "CLO": ("Cali", 1240),
    "UIO": ("Quito", 1270),
    "GYE": ("Guayaquil", 1160),
    "MVD": ("Montevideo", 1110),
    # --- Africa (sub-Saharan) ---
    "NBO": ("Nairobi (eVisa)", 420),
    "MBA": ("Mombasa (eVisa)", 500),
    "JRO": ("Kilimanjaro (eVisa)", 520),
    "DAR": ("Dar es Salaam (eVisa)", 500),
    "ZNZ": ("Zanzibar (eVisa)", 500),
    "EBB": ("Entebbe (eVisa)", 560),
    "KGL": ("Kigali", 560),
    "ADD": ("Addis Ababa", 420),
    "JNB": ("Johannesburg", 440),
    "CPT": ("Cape Town", 440),
    "WDH": ("Windhoek", 640),
    "GBE": ("Gaborone", 780),
    "LUN": ("Lusaka", 750),
    "VFA": ("Victoria Falls", 800),
    "HRE": ("Harare", 640),
    "TNR": ("Antananarivo (Madagascar)", 640),
    "MRU": ("Mauritius", 560),
    "SEZ": ("Seychelles", 640),
    "LOS": ("Lagos (visa)", 560),
    "ABV": ("Abuja (visa)", 600),
    "ACC": ("Accra", 560),
    "DKR": ("Dakar", 560),
    "ABJ": ("Abidjan", 560),
    "LAD": ("Luanda (visa)", 640),
    "DLA": ("Douala", 640),
    "NKC": ("Nouakchott (Mauritania)", 640),
    "SID": ("Sal (Cape Verde)", 320),
    "RAI": ("Praia (Cape Verde)", 400),
    # --- Oceania (visa needed for AU/NZ) ---
    "SYD": ("Sydney (visa)", 750),
    "MEL": ("Melbourne (visa)", 750),
    "BNE": ("Brisbane (visa)", 800),
    "PER": ("Perth (visa)", 820),
    "ADL": ("Adelaide (visa)", 850),
    "OOL": ("Gold Coast (visa)", 800),
    "CNS": ("Cairns (visa)", 850),
    "AKL": ("Auckland (visa)", 820),
    "CHC": ("Christchurch (visa)", 900),
    "ZQN": ("Queenstown (visa)", 900),
    "WLG": ("Wellington (visa)", 900),
    "ASB": ("Ashgabat (Turkmenistan, visa)", 1010),
}

HISTORY_FILE = "price_history.json"
API_BASE = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
AVIASALES = "https://www.aviasales.com"


# -------------------- API fetch --------------------

def upcoming_months(n: int) -> list[str]:
    today = date.today()
    months = []
    y, m = today.year, today.month
    for _ in range(n):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return months


def fmt_date(d: str) -> str:
    """Convert an API date 'YYYY-MM-DD' to display format 'DD/MM/YYYY'.
    Leaves anything unparseable (e.g. 'flexible') untouched."""
    if not d:
        return d
    try:
        return date.fromisoformat(d).strftime("%d/%m/%Y")
    except ValueError:
        return d


def trip_nights(dep: str, ret: str) -> int | None:
    """Number of nights between departure and return (YYYY-MM-DD strings)."""
    if not dep or not ret:
        return None
    try:
        d = date.fromisoformat(dep)
        r = date.fromisoformat(ret)
        return (r - d).days
    except ValueError:
        return None


def passes_filter(dep: str, nights: int | None, cfg: dict) -> bool:
    """Decide whether a fare qualifies.

    Always: reject trips longer than the tier's max_nights (kills the random
    23-night fares). When WEEKEND_ONLY is on, additionally require the trip to
    be MIN_NIGHTS..MAX_NIGHTS and depart on an allowed weekday.
    """
    cap = cfg.get("max_nights")
    # Enforce the per-tier cap in every mode. If we can't tell the length
    # (missing return date), keep it -- better to show than silently drop.
    if cap is not None and nights is not None and nights > cap:
        return False

    if not WEEKEND_ONLY:
        return True

    if nights is None or not (MIN_NIGHTS <= nights <= MAX_NIGHTS):
        return False
    try:
        return date.fromisoformat(dep).weekday() in DEP_DAYS
    except ValueError:
        return False


# Travelpayouts Data API allows 60 requests/minute. We pace under that, but
# testing showed 429s were NOT the coverage bottleneck: the Data API serves
# cached fares from real Aviasales searches, so low-traffic routes simply have
# no data. 0.6s (~100/min burst, well-behaved in practice) keeps runtime sane;
# the 429 retry logic below still protects us if we ever do get limited.
_pacing_seconds = env_float("PACING_SECONDS", 0.6)
_pacing_lock_min = 0.5

# When a route returns nothing for the queried months, retry once with no month
# constraint. Catches routes whose cached fares sit outside the scan window.
# Set BROAD_FALLBACK=false to disable (saves ~1 call per empty route).
BROAD_FALLBACK = os.environ.get("BROAD_FALLBACK", "true").strip().lower() != "false"


def get_cheapest(origin: str, destination: str, cfg: dict) -> dict | None:
    """Cheapest fare origin->destination under the tier's rules.

    API quirks of Travelpayouts at this scale:
      * 429 Too Many Requests   -> globally slow down for the rest of the run,
                                   skip this month's data point (don't retry).
                                   Retrying makes the rate limit worse.
      * 400 Bad Request         -> route+month not indexed, skip silently.
    """
    global _pacing_seconds
    best = None
    fetch_limit = env_int("FETCH_LIMIT", 8)
    for ym in upcoming_months(cfg["months_ahead"]):
        params = {
            "origin": origin,
            "destination": destination,
            "departure_at": ym,
            "currency": "eur",
            "one_way": cfg["one_way"],
            "direct": cfg["direct"],
            "sorting": "price",
            "limit": fetch_limit,
            "token": TRAVELPAYOUTS_TOKEN,
        }
        data = None
        # Retry loop: on 429 we wait and try the SAME call again (up to a cap),
        # so a rate-limit hit no longer means losing this route's data.
        attempts = 0
        while True:
            attempts += 1
            try:
                r = requests.get(
                    API_BASE,
                    params=params,
                    headers={"X-Access-Token": TRAVELPAYOUTS_TOKEN},
                    timeout=30,
                )
                if r.status_code == 429:
                    # Blocked until the 60/min window clears. Back off and retry
                    # the same call. Honor Retry-After if the API sends it.
                    wait = int(r.headers.get("Retry-After", "0")) or min(15, 3 * attempts)
                    print(
                        f"  ~ {origin}->{destination} {ym}: 429, waiting {wait}s "
                        f"(attempt {attempts})",
                        file=sys.stderr,
                    )
                    time.sleep(wait)
                    if attempts < 5:
                        continue  # retry same call
                    break         # give up on this month after 5 tries
                if r.status_code == 400:
                    break  # route/month not indexed -- expected, skip
                r.raise_for_status()
                data = r.json().get("data", [])
                break
            except Exception as e:  # noqa: BLE001
                print(f"  ! {origin}->{destination} {ym}: API error {e}", file=sys.stderr)
                break

        if data:
            for item in data:
                price = item.get("price")
                dep = (item.get("departure_at") or "")[:10]
                ret = (item.get("return_at") or "")[:10]
                nights = trip_nights(dep, ret)
                if price is None or not passes_filter(dep, nights, cfg):
                    continue
                if best is None or price < best["price"]:
                    best = {
                        "price": int(round(price)),
                        "airline": item.get("airline", "?"),
                        "departure_at": dep,
                        "return_at": ret,
                        "nights": nights,
                        "transfers": item.get("transfers"),
                        "link": AVIASALES + item.get("link", "") if item.get("link") else None,
                    }
        time.sleep(_pacing_seconds)

    # Fallback: the month-by-month loop only sees fares cached for those exact
    # months. Many routes have cached fares in OTHER months and so look empty.
    # One broad query (no departure_at) catches those. Only runs for routes that
    # found nothing, so it costs ~1 extra call per otherwise-empty route.
    if best is None and BROAD_FALLBACK:
        params = {
            "origin": origin,
            "destination": destination,
            "currency": "eur",
            "one_way": cfg["one_way"],
            "direct": cfg["direct"],
            "sorting": "price",
            "limit": env_int("FETCH_LIMIT", 8),
            "token": TRAVELPAYOUTS_TOKEN,
        }
        try:
            r = requests.get(
                API_BASE,
                params=params,
                headers={"X-Access-Token": TRAVELPAYOUTS_TOKEN},
                timeout=30,
            )
            if r.status_code == 200:
                for item in r.json().get("data", []):
                    price = item.get("price")
                    dep = (item.get("departure_at") or "")[:10]
                    ret = (item.get("return_at") or "")[:10]
                    nights = trip_nights(dep, ret)
                    if price is None or not passes_filter(dep, nights, cfg):
                        continue
                    if best is None or price < best["price"]:
                        best = {
                            "price": int(round(price)),
                            "airline": item.get("airline", "?"),
                            "departure_at": dep,
                            "return_at": ret,
                            "nights": nights,
                            "transfers": item.get("transfers"),
                            "link": AVIASALES + item.get("link", "") if item.get("link") else None,
                            "broad": True,  # found outside the normal month window
                        }
        except Exception as e:  # noqa: BLE001
            print(f"  ! {origin}->{destination} broad: {e}", file=sys.stderr)
        time.sleep(_pacing_seconds)

    return best


# -------------------- History --------------------

def load_history() -> dict:
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}


def save_history(history: dict) -> None:
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


def rolling_avg(prices: list[dict]) -> float | None:
    if len(prices) < 3:
        return None
    recent = prices[-ROLLING_WINDOW_DAYS:]
    return mean(p["price"] for p in recent)


# -------------------- Telegram --------------------

def _post_telegram(text: str) -> None:
    """Send one message to Telegram (assumes text is already under the limit)."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=30)
    if not r.ok:
        print(f"Telegram error {r.status_code}: {r.text}", file=sys.stderr)
        r.raise_for_status()


DASHBOARD_URL = os.environ.get("DASHBOARD_URL") or "https://rizabalci.github.io/worldwide-flight-bot/dashboard.html"


def send_telegram(text: str) -> None:
    """Send a message to Telegram, splitting into chunks if over the limit.

    Telegram caps messages at 4096 characters. On day one (or any busy day)
    a digest can exceed that, so we split on line boundaries -- never mid-deal --
    and send sequentially with a continuation marker.

    If the MUTE variable is set to "true", the bot still scans and records
    price history but sends nothing to Telegram. Useful when you're not
    travelling but want history to keep accumulating for later tuning.
    """
    if os.environ.get("MUTE", "").strip().lower() == "true":
        print(f"[muted] Would have sent {len(text)} chars to Telegram.")
        return
    LIMIT = 3500  # safety margin under Telegram's 4096 hard cap
    if len(text) <= LIMIT:
        _post_telegram(text)
        return

    lines = text.split("\n")
    chunk: list[str] = []
    size = 0
    chunks: list[str] = []
    for line in lines:
        # +1 for the newline that will rejoin them
        if size + len(line) + 1 > LIMIT and chunk:
            chunks.append("\n".join(chunk))
            chunk = []
            size = 0
        chunk.append(line)
        size += len(line) + 1
    if chunk:
        chunks.append("\n".join(chunk))

    total = len(chunks)
    for i, c in enumerate(chunks, 1):
        suffix = f"\n\n<i>(part {i}/{total})</i>" if total > 1 else ""
        _post_telegram(c + suffix)
        time.sleep(0.5)  # avoid Telegram's per-chat send rate limit


# Airlines whose advertised fares are almost always hand-luggage only.
# A hint, NOT a guarantee -- bundles vary, so the label is marked "~cabin?".
_HAND_LUGGAGE_CARRIERS = {
    "FR",  # Ryanair
    "RK",  # Ryanair UK
    "W6",  # Wizz Air
    "W4",  # Wizz Air Malta
    "W9",  # Wizz Air UK
    "U2",  # easyJet
    "EC",  # easyJet Europe
    "VY",  # Vueling
    "EW",  # Eurowings
    "PC",  # Pegasus
    "FY",  # Firefly
    "0B",  # Blue Air
    "HV",  # Transavia
    "TO",  # Transavia France
    "DY",  # Norwegian
    "F9",  # Frontier
    "NK",  # Spirit
    "JU",  # Air Serbia (basic fares)
    "WZZ", # Wizz (alt code)
}


# Precise best-season notes for popular/watchlist-likely destinations.
# "Best" = best weather / peak experience. NOTE: often the OPPOSITE of cheapest,
# since peak season = peak prices. This is travel context, not a buy signal.
_SEASON_PRECISE = {
    # SE Asia / Indian Ocean
    "DPS": "Apr–Oct (dry)",
    "HKT": "Nov–Apr (dry)", "USM": "Feb–Apr", "BKK": "Nov–Feb (cool/dry)",
    "DMK": "Nov–Feb (cool/dry)", "CNX": "Nov–Feb", "KBV": "Nov–Apr",
    "VTE": "Nov–Feb (cool/dry)", "LPQ": "Nov–Feb (cool/dry)",
    "RGN": "Nov–Feb (dry)", "BWN": "Jan–Apr (drier)",
    "ULN": "Jun–Sep (warm) or Feb (ice festival)", "FRU": "Jun–Sep (trekking)",
    "SGN": "Dec–Apr (dry)", "HAN": "Oct–Apr", "DAD": "Feb–May",
    "REP": "Nov–Mar (dry)", "PNH": "Nov–Mar (dry)",
    "KUL": "Dec–Feb & Jun–Aug", "SIN": "Feb–Apr", "DPS_": "",
    "MLE": "Nov–Apr (dry)", "CMB": "Dec–Mar (west coast)",
    "DEL": "Oct–Mar (cool)", "BOM": "Nov–Feb", "GOI": "Nov–Mar",
    "KTM": "Oct–Nov & Mar–Apr (clear, trekking)",
    "CGK": "May–Sep (dry)", "MNL": "Dec–Apr (dry)", "CEB": "Dec–May",
    # East Asia
    "NRT": "Mar–Apr (blossom) & Oct–Nov (autumn)",
    "HND": "Mar–Apr (blossom) & Oct–Nov (autumn)",
    "KIX": "Mar–Apr & Oct–Nov", "NGO": "Mar–Apr & Oct–Nov",
    "FUK": "Mar–May & Oct–Nov", "CTS": "Dec–Mar (snow) & Jun–Aug (cool)",
    "OKA": "Apr–Jun & Oct (warm, less rain)",
    "ICN": "Apr–Jun & Sep–Nov", "PUS": "Apr–Jun & Sep–Oct",
    "TPE": "Oct–Apr (cooler/drier)", "HKG": "Oct–Dec (mild/dry)",
    "PVG": "Mar–May & Sep–Nov", "PEK": "Sep–Oct (clear)",
    # Middle East / N. Africa
    "DXB": "Nov–Mar (mild)", "AUH": "Nov–Mar", "DOH": "Nov–Mar",
    "MCT": "Oct–Mar", "RUH": "Nov–Mar", "JED": "Nov–Mar",
    "TLV": "Apr–Jun & Sep–Nov", "AMM": "Mar–May & Sep–Nov",
    "BEY": "Apr–Jun & Sep–Oct",
    "CAI": "Oct–Apr (cooler)", "HRG": "Mar–May & Oct–Nov",
    "SSH": "Mar–May & Oct–Nov", "RAK": "Mar–May & Sep–Nov",
    "AGA": "Apr–Nov", "CMN": "Apr–Jun & Sep–Nov", "TUN": "Apr–Jun & Sep–Oct",
    "DJE": "Apr–Jun & Sep–Oct",
    # Turkey coast
    "AYT": "Apr–Jun & Sep–Oct (beach, less heat)",
    "ADB": "May–Jun & Sep–Oct", "BJV": "May–Jun & Sep–Oct",
    "DLM": "May–Jun & Sep–Oct",
    # Caucasus
    "TBS": "May–Jun & Sep–Oct", "EVN": "May–Jun & Sep–Oct",
    "GYD": "Apr–Jun & Sep–Oct", "KUT": "May–Oct", "BUS": "Jun–Sep (beach)",
    # Americas
    "JFK": "Apr–Jun & Sep–Nov", "EWR": "Apr–Jun & Sep–Nov",
    "LAX": "Mar–May & Sep–Nov", "SFO": "Sep–Nov (warmest)",
    "MIA": "Nov–Apr (dry/warm)", "MCO": "Nov–Apr", "LAS": "Mar–May & Oct–Nov",
    "YYZ": "May–Jun & Sep–Oct", "YVR": "Jun–Sep",
    "CUN": "Nov–Apr (dry)", "MEX": "Nov–Apr (dry)", "HAV": "Nov–Apr (dry)",
    "PUJ": "Nov–Apr", "MBJ": "Nov–Apr", "SJO": "Dec–Apr (dry)",
    "GRU": "Apr–Oct (drier)", "GIG": "Dec–Mar (summer) / May–Sep (mild)",
    "EZE": "Oct–Nov & Mar–Apr", "SCL": "Oct–Apr", "LIM": "Dec–Apr",
    "CUZ": "May–Sep (dry, trekking)", "BOG": "Dec–Mar & Jul–Aug (drier)",
    # Africa (sub-Saharan / islands)
    "NBO": "Jun–Oct & Jan–Feb (dry, safari)", "JRO": "Jun–Oct & Jan–Feb",
    "ZNZ": "Jun–Oct (dry)", "DAR": "Jun–Oct", "JNB": "Apr–Oct (dry)",
    "CPT": "Nov–Mar (summer/dry)", "MRU": "May–Dec (drier)",
    "SEZ": "Apr–May & Oct–Nov", "ADD": "Oct–Mar (dry)", "SID": "Nov–Jun",
    # Oceania
    "SYD": "Sep–Nov & Mar–May", "MEL": "Mar–May (autumn)",
    "BNE": "Mar–May & Sep–Nov", "PER": "Sep–Nov & Mar–May",
    "AKL": "Dec–Mar (summer)", "CHC": "Dec–Mar", "NAN": "May–Oct (dry)",
    "PPT": "May–Oct (dry)",
}

# Regional fallback by destination grouping — rough but sensible for the rest.
def _season_region(dest: str) -> str:
    s = SHORT_HAUL_DESTINATIONS.get(dest)
    l = LONG_HAUL_DESTINATIONS.get(dest)
    label = (s or l or ("",))[0].lower()
    # Nordic / Arctic / Baltic
    if any(k in label for k in ("arctic", "lapland", "svalbard", "iceland",
                                "faroe", "tromso", "rovaniemi")):
        return "Jun–Aug (mild) or Dec–Mar (aurora/snow)"
    if dest in {"OSL","BGO","SVG","TRD","CPH","ARN","GOT","HEL","TLL","RIX",
                "VNO","KUN","PLQ","AAR","AAL","BLL","TKU","TMP","OUL"}:
        return "May–Aug (long days, mildest)"
    # Mediterranean / Iberia / Greek isles / Italy south / Cyprus / Malta
    if any(k in label for k in ("crete","rhodes","santorini","mykonos","corfu",
            "kos","sicily","sardinia","cyprus","malta","ibiza","mallorca",
            "menorca","canar","madeira","azores","algarve","corsica")):
        return "May–Jun & Sep–Oct (warm, fewer crowds)"
    # Long-haul tier default by broad region keywords
    if l:
        if any(k in label for k in ("caribbean","cancun","havana","punta",
                "jamaica","aruba","bahamas","barbados")):
            return "Nov–Apr (dry season)"
        return "shoulder months (Apr–Jun, Sep–Oct) usually best"
    # Generic Europe short-haul
    return "May–Sep (warmest)"


def season_note(dest: str) -> str:
    """Best-season note: precise if known, else regional fallback. Always
    flagged as weather/peak context, not a price signal."""
    note = _SEASON_PRECISE.get(dest) or _season_region(dest)
    return note


def baggage_hint(airline: str) -> str:
    """Best-effort baggage note. The API doesn't return baggage data, so this
    is inferred from the airline alone and always flagged as unverified."""
    if airline and airline.upper() in _HAND_LUGGAGE_CARRIERS:
        return " · <i>~cabin?</i>"
    return ""


COUNTRY_MAP = {
 **{c:"UK" for c in "LHR LGW STN LTN LCY MAN EDI GLA ABZ INV BHX BRS EMA LBA LPL NCL SOU BFS BHD LDY".split()},
 **{c:"Ireland" for c in "DUB ORK SNN NOC KIR".split()},
 **{c:"Italy" for c in "FCO CIA MXP BGY LIN TRN RMI BLQ FLR PSA VCE TSF VRN TRS GOA AOI PSR NAP BRI SUF CTA PMO TPS OLB CAG AHO".split()},
 **{c:"Spain" for c in "BCN GRO REU ZAZ VLL RGS MAD VLC ALC MJV AGP GRX LEI SVQ BJZ BIO EAS SDR OVD LEN SCQ VGO PMI IBZ MAH TFS TFN LPA ACE FUE".split()},
 **{c:"Portugal" for c in "LIS OPO FAO FNC PDL".split()},
 **{c:"France" for c in "CDG ORY BVA NCE MRS TLN LYS TLS BOD BIQ FNI MPL NTE RNS LIL SXB AJA BIA".split()},
 **{c:"Netherlands" for c in "AMS EIN RTM GRQ MST".split()},
 **{c:"Belgium" for c in "BRU CRL ANR OST LGG".split()}, "LUX":"Luxembourg",
 **{c:"Germany" for c in "BER HAM MUC NUE FMM FRA HHN NRN DUS CGN DTM PAD STR FKB FDH LEJ DRS ERF BRE HAJ RLG".split()},
 **{c:"Switzerland" for c in "ZRH GVA BSL BRN LUG".split()},
 **{c:"Czechia" for c in "PRG BRQ OSR PED".split()}, "KSC":"Slovakia",
 **{c:"Poland" for c in "WAW KRK GDN POZ WRO KTW LCJ LUZ BZG RZE SZZ".split()},
 **{c:"Romania" for c in "OTP CLJ IAS TSR SBZ CND".split()},
 **{c:"Bulgaria" for c in "SOF VAR BOJ PDV".split()}, "KIV":"Moldova",
 **{c:"Croatia" for c in "ZAG SPU DBV ZAD PUY RJK OSI".split()},
 **{c:"Serbia" for c in "BEG INI".split()},
 **{c:"Bosnia" for c in "SJJ TZL BNX".split()},
 **{c:"Montenegro" for c in "TGD TIV".split()}, "TIA":"Albania",
 **{c:"N. Macedonia" for c in "SKP OHD".split()}, "PRN":"Kosovo",
 **{c:"Greece" for c in "ATH SKG KVA JTY JIK JKH LXS MJT SMI JSI SKU VOL JNX PAS MLO JSH HER CHQ RHO AOK JTR JMK CFU PVK KGS ZTH JKL EFL".split()},
 **{c:"Cyprus" for c in "LCA PFO".split()}, "MLA":"Malta",
 **{c:"Denmark" for c in "CPH AAR AAL BLL RNN".split()},
 **{c:"Sweden" for c in "ARN BMA NYO GOT MMX VBY UME LLA".split()},
 **{c:"Norway" for c in "OSL TRF BGO TOS SVG TRD AES MOL KSU KRS BOO EVE ALF KKN LYR".split()},
 **{c:"Finland" for c in "HEL TKU TMP OUL KAJ RVN IVL".split()},
 "KEF":"Iceland", "AEY":"Iceland", "FAE":"Faroe Islands",
 "TLL":"Estonia", "RIX":"Latvia", **{c:"Lithuania" for c in "VNO KUN PLQ".split()},
 **{c:"Turkey" for c in "AYT ADB BJV DLM".split()},
 **{c:"UAE" for c in "DXB AUH SHJ".split()}, "DOH":"Qatar", "BAH":"Bahrain",
 "KWI":"Kuwait", "MCT":"Oman",
 **{c:"Saudi Arabia" for c in "RUH JED DMM MED".split()},
 **{c:"Israel" for c in "TLV ETM".split()}, "BEY":"Lebanon", "AMM":"Jordan",
 **{c:"Iraq" for c in "BGW EBL".split()}, **{c:"Iran" for c in "IKA MHD".split()},
 **{c:"Egypt" for c in "CAI HRG SSH LXR".split()},
 **{c:"Morocco" for c in "RAK CMN AGA FEZ TNG OUD NDR".split()},
 **{c:"Tunisia" for c in "TUN DJE".split()}, **{c:"Algeria" for c in "ALG ORN".split()},
 **{c:"Kenya" for c in "NBO MBA".split()}, **{c:"Tanzania" for c in "JRO DAR ZNZ".split()},
 "EBB":"Uganda", "KGL":"Rwanda", "ADD":"Ethiopia",
 **{c:"South Africa" for c in "JNB CPT".split()}, "WDH":"Namibia", "HRE":"Zimbabwe", "GBE":"Botswana", "LUN":"Zambia", "VFA":"Zimbabwe",
 "TNR":"Madagascar", "MRU":"Mauritius", "SEZ":"Seychelles",
 **{c:"Nigeria" for c in "LOS ABV".split()}, "ACC":"Ghana", "DKR":"Senegal",
 "ABJ":"Ivory Coast", "LAD":"Angola", "DLA":"Cameroon", "NKC":"Mauritania",
 **{c:"Cape Verde" for c in "SID RAI".split()},
 **{c:"Georgia" for c in "TBS KUT BUS".split()}, "EVN":"Armenia", "GYD":"Azerbaijan",
 **{c:"Uzbekistan" for c in "TAS SKD".split()}, **{c:"Kazakhstan" for c in "ALA NQZ".split()},
 "DYU":"Tajikistan", "ASB":"Turkmenistan",
 **{c:"Laos" for c in "VTE LPQ".split()}, "RGN":"Myanmar",
 "BWN":"Brunei", "ULN":"Mongolia", "FRU":"Kyrgyzstan",
 **{c:"Japan" for c in "NRT HND KIX NGO FUK CTS OKA".split()},
 **{c:"South Korea" for c in "ICN PUS".split()}, "TPE":"Taiwan",
 "HKG":"Hong Kong", "MFM":"Macau",
 **{c:"China" for c in "PVG PEK CAN CTU XIY KMG SYX".split()},
 **{c:"Thailand" for c in "BKK DMK HKT USM CNX KBV".split()},
 "KUL":"Malaysia", "SIN":"Singapore",
 **{c:"Indonesia" for c in "DPS CGK LOP".split()},
 **{c:"Philippines" for c in "MNL CEB".split()},
 **{c:"Vietnam" for c in "SGN HAN DAD CXR".split()},
 **{c:"Cambodia" for c in "REP PNH".split()},
 **{c:"India" for c in "DEL BOM BLR MAA HYD CCU GOI JAI COK".split()},
 "KTM":"Nepal", "CMB":"Sri Lanka", "MLE":"Maldives", "DAC":"Bangladesh",
 **{c:"Pakistan" for c in "ISB LHE KHI".split()},
 **{c:"USA" for c in "JFK EWR BOS IAD PHL MIA FLL MCO TPA ATL ORD MSP DFW IAH AUS DEN PHX LAS LAX SFO SEA PDX HNL OGG ANC".split()},
 **{c:"Canada" for c in "YYZ YUL YOW YQB YHZ YYC YEG YVR".split()},
 **{c:"Mexico" for c in "MEX GDL MTY CUN CZM PVR SJD MID".split()},
 "HAV":"Cuba", **{c:"Dominican Rep." for c in "PUJ SDQ".split()},
 **{c:"Jamaica" for c in "MBJ KIN".split()}, "AUA":"Aruba", "CUR":"Curacao",
 "BGI":"Barbados", "POS":"Trinidad", "PTY":"Panama",
 **{c:"Costa Rica" for c in "SJO LIR".split()}, "GUA":"Guatemala",
 "SAL":"El Salvador", "BZE":"Belize",
 **{c:"Brazil" for c in "GRU GIG BSB FOR".split()},
 **{c:"Argentina" for c in "EZE COR".split()}, "SCL":"Chile", "LIM":"Peru",
 **{c:"Colombia" for c in "BOG CTG MDE CLO".split()},
 **{c:"Ecuador" for c in "UIO GYE".split()}, "MVD":"Uruguay",
 **{c:"Australia" for c in "SYD MEL BNE PER ADL OOL CNS".split()},
 **{c:"New Zealand" for c in "AKL CHC ZQN WLG".split()},
}


def city_label(dest: str, city: str) -> str:
    country = COUNTRY_MAP.get(dest)
    if not country:
        return city
    m = re.match(r"^(.*?)\s*\((.+)\)\s*$", city)
    if m:
        return f"{m.group(1)} ({country}, {m.group(2)})"
    return f"{city} ({country})"


def stops_label(transfers) -> str:
    if transfers is None:
        return ""
    if transfers == 0:
        return " · direct"
    if transfers == 1:
        return " · 1 stop"
    return f" · {transfers} stops"


def fmt_deal(d: dict) -> str:
    arrow = d["cfg"]["arrow"]
    city = d["city"]
    origin = d["origin"]
    price = d["price"]
    target = d["target"]
    dep = d["departure_at"] or "flexible"
    ret = d["return_at"]
    air = d["airline"]
    when = f"{fmt_date(dep)} → {fmt_date(ret)}" if ret else fmt_date(dep)
    nights = d.get("nights")
    if nights:
        when += f" ({nights} night{'s' if nights != 1 else ''})"
    tag = ""
    if d.get("all_time_low"):
        lo = d.get("prev_lo")
        tag += f"  🏆 lowest ever" + (f" (was €{int(lo)})" if lo else "")
    if d["below_target"]:
        tag += f"  (target €{target})"
    if d["big_drop"]:
        tag += f"  ↓ from €{int(d['avg'])} avg"
    line = f"<b>{city}</b> {origin}{arrow} €{price}{tag}\n   {when} · {air}{stops_label(d.get('transfers'))}{baggage_hint(air)}"
    if d["link"]:
        line += f' · <a href="{d["link"]}">book</a>'
    if d.get("broad"):
        line += "  <i>·outside scan window</i>"
    _dest = d.get("dest")
    if _dest:
        line += f"\n   🗓 best season: {season_note(_dest)}"
    return line


# -------------------- Scan + digest --------------------

_origin_stats: dict[str, list[int]] = {}  # origin -> [routes_with_data, routes_tried]


def scan_tier(cfg: dict, destinations: dict, history: dict, today: str) -> tuple[list, int]:
    deals = []
    checked = 0
    for origin in ORIGINS:
        stats = _origin_stats.setdefault(origin, [0, 0])
        for dest, (city, target) in destinations.items():
            if FOCUS and dest not in FOCUS:
                continue
            city = city_label(dest, city)
            key = f"{origin}-{dest}-{cfg['trip_type']}"
            cheapest = get_cheapest(origin, dest, cfg)
            stats[1] += 1
            if cheapest is None:
                print(f"  [{cfg['name']}] {origin}->{dest} {city}: no fares")
                continue
            checked += 1
            stats[0] += 1
            price = cheapest["price"]
            entry = history.get(key, {})
            # Back-compat: old history stored a bare list; wrap it.
            if isinstance(entry, list):
                entry = {"series": entry, "lo": None}
            series = entry.get("series", [])
            prev_lo = entry.get("lo")

            avg = rolling_avg(series)
            # "Below target" now requires a real margin, not a €1 squeak under.
            below_target = price <= target * (1 - TARGET_MARGIN_PCT)
            big_drop = avg is not None and price <= avg * (1 - ROLLING_DROP_PCT)
            # All-time low: cheaper than anything seen before for this route.
            # Needs some history first, else day-one would flag everything.
            all_time_low = (
                prev_lo is not None
                and len(series) >= 3
                and price < prev_lo
            )

            print(
                f"  [{cfg['name']}] {origin}->{dest} {city}: €{price}"
                f" | target €{target}"
                f" | avg {('€%d' % avg) if avg else 'n/a'}"
                f" | lo {('€%d' % prev_lo) if prev_lo else 'n/a'}"
                f" | {'HIT' if (below_target or big_drop or all_time_low) else '-'}"
            )

            is_deal = below_target or big_drop or all_time_low
            if True:
                deals.append({
                    "is_deal": is_deal,
                    "tier": cfg["name"],
                    "cfg": cfg,
                    "origin": origin,
                    "dest": dest,
                    "city": city,
                    "price": price,
                    "target": target,
                    "avg": avg,
                    "prev_lo": prev_lo,
                    "airline": cheapest["airline"],
                    "departure_at": cheapest["departure_at"],
                    "return_at": cheapest["return_at"],
                    "nights": cheapest.get("nights"),
                    "transfers": cheapest.get("transfers"),
                    "broad": cheapest.get("broad", False),
                    "link": cheapest["link"],
                    "below_target": below_target,
                    "big_drop": big_drop,
                    "all_time_low": all_time_low,
                    "score": (target - price) / target,
                })

            # Update history: append today, trim series, track all-time low.
            series.append({"date": today, "price": price})
            new_lo = price if prev_lo is None else min(prev_lo, price)
            history[key] = {
                "series": series[-(ROLLING_WINDOW_DAYS * 2):],
                "lo": new_lo,
            }
    return deals, checked


def build_digest(deals: list, header: str) -> str | None:
    """Build a Telegram digest for one tier. Returns None if no deals."""
    deals = [d for d in deals if d.get("is_deal")]
    if not deals:
        return None
    # Priority order: all-time lows first, then big drops, then below-target.
    # Each deal appears once, in its highest-priority bucket.
    lows = sorted([d for d in deals if d.get("all_time_low")],
                  key=lambda d: d["score"], reverse=True)
    low_keys = {(d["origin"], d["dest"]) for d in lows}
    big = sorted([d for d in deals
                  if d["big_drop"] and (d["origin"], d["dest"]) not in low_keys],
                 key=lambda d: d["score"], reverse=True)
    big_keys = low_keys | {(d["origin"], d["dest"]) for d in big}
    cheap = sorted([d for d in deals
                    if d["below_target"] and (d["origin"], d["dest"]) not in big_keys],
                   key=lambda d: d["score"], reverse=True)
    lines = [header, ""]
    if lows:
        lines.append("🏆 <b>All-time lows</b>")
        lines.append("")
        lines += ["\n".join([fmt_deal(d), "", "<code>──────────</code>", ""]) for d in lows]
        if big or cheap:
            lines.append("")
    if big:
        lines.append("🔥 <b>Big drops vs recent average</b>")
        lines.append("")
        lines += ["\n".join([fmt_deal(d), "", "<code>──────────</code>", ""]) for d in big]
        if cheap:
            lines.append("")
    if cheap:
        lines.append("✅ <b>Below target</b>")
        lines.append("")
        lines += ["\n".join([fmt_deal(d), "", "<code>──────────</code>", ""]) for d in cheap]
    # Footer: only show the baggage reminder if any deal involves a hinted carrier.
    if any(d["airline"].upper() in _HAND_LUGGAGE_CARRIERS for d in deals):
        lines.append("")
        lines.append(
            "<i>~cabin? = budget carrier, likely hand-luggage only. "
            "Always confirm baggage at booking.</i>"
        )
    return "\n".join(lines).strip()


def _tier_for(dest: str) -> tuple[dict, dict] | tuple[None, None]:
    """Return (cfg, destinations_dict) for whichever tier a code belongs to."""
    if dest in SHORT_HAUL_DESTINATIONS:
        return SHORT_HAUL, SHORT_HAUL_DESTINATIONS
    if dest in LONG_HAUL_DESTINATIONS:
        return LONG_HAUL, LONG_HAUL_DESTINATIONS
    return None, None


def scan_watchlist(history: dict, today: str) -> str | None:
    """Fetch current cheapest fare for each watchlist route and format a section
    that always shows, regardless of whether the fare is a deal."""
    if not WATCHLIST:
        return None
    rows = []
    for dest in WATCHLIST:
        cfg, dests = _tier_for(dest)
        if cfg is None:
            print(f"  [watch] {dest}: not in any tier, skipping")
            continue
        label, target = dests[dest]
        label = city_label(dest, label)
        # Cheapest across origins for this route.
        best = None
        for origin in ORIGINS:
            c = get_cheapest(origin, dest, cfg)
            if c and (best is None or c["price"] < best["price"]):
                best = c
                best["origin"] = origin
        if best is None:
            rows.append((dest, label, None, None, target, None, ""))
            continue
        # Compare against stored all-time low for trend context.
        key = f"{best['origin']}-{dest}-{cfg['trip_type']}"
        entry = history.get(key, {})
        if isinstance(entry, list):
            entry = {"series": entry, "lo": None}
        lo = entry.get("lo")
        avg = rolling_avg(entry.get("series", []))
        # Trend vs recent average: needs a few points to be meaningful.
        trend = ""
        if avg:
            if best["price"] <= avg * 0.95:
                trend = f" ↘ (avg €{int(avg)})"   # >=5% below recent avg
            elif best["price"] >= avg * 1.05:
                trend = f" ↗ (avg €{int(avg)})"   # >=5% above recent avg
            else:
                trend = f" → (avg €{int(avg)})"   # within ±5%
        is_deal = best["price"] <= target * (1 - TARGET_MARGIN_PCT)
        rows.append((dest, label, best, lo, target, is_deal, trend))

    if not rows:
        return None
    lines = [f"<b>👀 Watching — {fmt_date(today)}</b>", ""]
    for dest, label, best, lo, target, is_deal, trend in rows:
        if best is None:
            lines.append(f"<b>{label}</b>: no fares found (target €{target})")
            lines.append("")
            lines.append("<code>──────────</code>")
            lines.append("")
            continue
        arrow = "↔"
        price = best["price"]
        when = f"{fmt_date(best['departure_at'])} → {fmt_date(best['return_at'])}"
        nights = best.get("nights")
        when += f" ({nights} nights)" if nights else ""
        flag = "  🎯 <b>DEAL</b>" if is_deal else ""
        lo_str = f"  · lowest seen €{int(lo)}" if lo else ""
        line = (
            f"<b>{label}</b> {best['origin']}{arrow} <b>€{price}</b>{trend}"
            f"  (target €{target}){flag}{lo_str}\n"
            f"   {when} · {best['airline']}{stops_label(best.get('transfers'))}{baggage_hint(best['airline'])}"
        )
        if best["link"]:
            line += f' · <a href="{best["link"]}">book</a>'
        line += f"\n   🗓 best season: {season_note(dest)}"
        lines.append(line)
        lines.append("")
        lines.append("<code>──────────</code>")
        lines.append("")
    return "\n".join(lines).strip()


def write_site_data(short_deals, long_deals, origins_list):
    """Write deals.json for the dashboard: europe + worldwide arrays, same fields
    the digest uses. Only firing deals are written (matches bot behaviour)."""
    def clean(dl, tier):
        out = []
        for d in dl:
            out.append({
                "tier": tier,
                "city": d.get("city"),
                "country": COUNTRY_MAP.get(d.get("dest")),
                "origin": d.get("origin"),
                "dest": d.get("dest"),
                "price": d.get("price"),
                "target": d.get("target"),
                "avg": round(d["avg"]) if d.get("avg") is not None else None,
                "prev_lo": round(d["prev_lo"]) if d.get("prev_lo") is not None else None,
                "airline": d.get("airline"),
                "departure_at": d.get("departure_at"),
                "return_at": d.get("return_at"),
                "nights": d.get("nights"),
                "transfers": d.get("transfers"),
                "link": d.get("link"),
                "is_deal": d.get("is_deal", False),
                "below_target": d.get("below_target", False),
                "big_drop": d.get("big_drop", False),
                "all_time_low": d.get("all_time_low", False),
                "cabin": d.get("cabin", False),
                "score": d.get("score", 0),
            })
        return out
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "origins": origins_list,
        "counts": {"europe": len(short_deals), "worldwide": len(long_deals)},
        "europe": clean(short_deals, "short"),
        "worldwide": clean(long_deals, "long"),
    }
    with open("deals.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1, ensure_ascii=False)
    print(f"Wrote deals.json: {len(short_deals)} europe + {len(long_deals)} worldwide")


def main() -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_disp = fmt_date(today)  # DD/MM/YYYY for message headers
    history = load_history()

    short_deals, short_checked = scan_tier(SHORT_HAUL, SHORT_HAUL_DESTINATIONS, history, today)
    long_deals, long_checked = scan_tier(LONG_HAUL, LONG_HAUL_DESTINATIONS, history, today)

    # Coverage guard: if the API returned data for almost no routes, it was
    # almost certainly throttling or down. Writing deals.json and sending a
    # digest anyway would look like "no deals today" — misleading. Bail loudly
    # instead, leaving yesterday's data intact.
    short_total = len(ORIGINS) * len([d for d in SHORT_HAUL_DESTINATIONS if not FOCUS or d in FOCUS])
    long_total = len(ORIGINS) * len([d for d in LONG_HAUL_DESTINATIONS if not FOCUS or d in FOCUS])
    total_checked = short_checked + long_checked
    total_routes = short_total + long_total
    coverage = total_checked / max(total_routes, 1)
    print(f"Coverage: {total_checked}/{total_routes} routes returned data ({coverage:.0%}) "
          f"[EU {short_checked}/{short_total}, WW {long_checked}/{long_total}]")
    # Per-origin breakdown: the Data API serves cached fares from real Aviasales
    # searches, so a low-traffic origin returns far less. This shows which
    # origins are actually earning their runtime.
    for origin in ORIGINS:
        hit, tried = _origin_stats.get(origin, [0, 0])
        pct = hit / max(tried, 1)
        print(f"  origin {origin}: {hit}/{tried} routes returned data ({pct:.0%})")
    if coverage < 0.25:
        print("! Fewer than a quarter of routes returned data — API likely throttled "
              "or down. Leaving existing deals.json untouched, not sending a digest.",
              file=sys.stderr)
        # Exit cleanly rather than red-failing: a throttled day is expected
        # occasionally and shouldn't spam failure notifications. History is
        # deliberately not saved so a sparse scan can't pollute the record.
        return 0

    watch_digest = scan_watchlist(history, today)
    save_history(history)
    try:
        write_site_data(short_deals, long_deals, list(ORIGINS))
    except Exception as exc:
        print(f'  ! could not write deals.json: {exc}')

    if WEEKEND_ONLY:
        _rev = {v: k for k, v in _DAY_NAMES.items()}
        if DEP_DAYS == set(range(7)):
            dep_str = "any day"
        else:
            dep_str = "/".join(_rev[d].capitalize() for d in sorted(DEP_DAYS))
        if MIN_NIGHTS == MAX_NIGHTS:
            len_str = f"{MIN_NIGHTS} nights"
        else:
            len_str = f"{MIN_NIGHTS}-{MAX_NIGHTS} nights"
        mode_label = f" · {len_str}, {dep_str} dep"
    else:
        mode_label = " (round-trip)"
    short_digest = build_digest(
        short_deals,
        f"<b>🇪🇺 Europe deal scan — {today_disp}</b>{mode_label}\n"
        f"📊 <a href=\"{DASHBOARD_URL}\">Compare all in browser</a>"
    )
    long_digest = build_digest(
        long_deals,
        f"<b>🌍 Worldwide deal scan — {today_disp}</b>{mode_label}\n"
        f"📊 <a href=\"{DASHBOARD_URL}\">Compare all in browser</a>"
    )

    sent = 0
    if short_digest:
        send_telegram(short_digest)
        sent += 1
    if long_digest:
        send_telegram(long_digest)
        sent += 1
    if watch_digest:
        send_telegram(watch_digest)
        sent += 1

    # Heartbeat: if NO deals fired (neither Europe nor Worldwide digest), send a
    # short "still alive" note so silence never looks like a broken bot.
    # Disable by setting GitHub variable HEARTBEAT=false.
    heartbeat_on = os.environ.get("HEARTBEAT", "true").strip().lower() != "false"
    no_deals = not short_digest and not long_digest
    if heartbeat_on and no_deals:
        total = short_checked + long_checked
        send_telegram(
            f"<b>✈️ Flight scan — {today_disp}</b>\n"
            f"No deals today (scanned {total} routes). Targets are tight; "
            f"this just means nothing cleared them. Bot is running fine."
        )
        sent += 1

    if sent == 0:
        print(f"No messages sent. Checked {short_checked + long_checked} routes.")
    else:
        print(
            f"Sent {sent} message(s): {len(short_deals)} short + {len(long_deals)} long"
            f" across {short_checked + long_checked} routes."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
