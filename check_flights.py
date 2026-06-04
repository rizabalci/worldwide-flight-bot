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
    "LHR": ("London Heathrow (visa)", 80),
    "LGW": ("London Gatwick (visa)", 60),
    "STN": ("London Stansted (visa)", 45),
    "LTN": ("London Luton (visa)", 45),
    "LCY": ("London City (visa)", 90),
    "MAN": ("Manchester (visa)", 60),
    "EDI": ("Edinburgh (visa)", 65),
    "GLA": ("Glasgow (visa)", 70),
    "ABZ": ("Aberdeen (visa)", 90),
    "INV": ("Inverness (visa)", 110),
    "BHX": ("Birmingham (visa)", 60),
    "BRS": ("Bristol (visa)", 70),
    "EMA": ("East Midlands (visa)", 70),
    "LBA": ("Leeds Bradford (visa)", 75),
    "LPL": ("Liverpool (visa)", 70),
    "NCL": ("Newcastle (visa)", 75),
    "HUY": ("Humberside (visa)", 110),
    "SOU": ("Southampton (visa)", 95),
    "EXT": ("Exeter (visa)", 110),
    "NQY": ("Newquay (visa)", 120),
    "NWI": ("Norwich (visa)", 110),
    "CWL": ("Cardiff (visa)", 85),
    "BFS": ("Belfast Intl (visa)", 80),
    "BHD": ("Belfast City (visa)", 90),
    "LDY": ("Derry (visa)", 110),
    "JER": ("Jersey (visa)", 120),
    "GCI": ("Guernsey (visa)", 130),
    "IOM": ("Isle of Man (visa)", 130),
    # --- Ireland ---
    "DUB": ("Dublin", 50),
    "ORK": ("Cork", 70),
    "SNN": ("Shannon", 80),
    "NOC": ("Knock", 80),
    "KIR": ("Kerry", 95),
    # --- Italy ---
    "FCO": ("Rome Fiumicino", 30),
    "CIA": ("Rome Ciampino", 30),
    "MXP": ("Milan Malpensa", 30),
    "BGY": ("Milan Bergamo", 25),
    "LIN": ("Milan Linate", 50),
    "TRN": ("Turin", 40),
    "AOT": ("Aosta", 95),
    "CUF": ("Cuneo", 90),
    "PMF": ("Parma", 65),
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
    "FOG": ("Foggia", 70),
    "BRI": ("Bari", 40),
    "BDS": ("Brindisi", 42),
    "SUF": ("Lamezia Terme (Calabria)", 50),
    "REG": ("Reggio Calabria", 65),
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
    "VDE": ("El Hierro (Canaries)", 150),
    "SPC": ("La Palma (Canaries)", 130),
    "LIS": ("Lisbon", 55),
    "OPO": ("Porto", 55),
    "FAO": ("Faro (Algarve)", 65),
    "FNC": ("Funchal (Madeira)", 100),
    "PXO": ("Porto Santo (Madeira)", 150),
    "PDL": ("Ponta Delgada (Azores)", 130),
    "TER": ("Terceira (Azores)", 150),
    "HOR": ("Horta (Azores)", 180),
    # --- France ---
    "CDG": ("Paris CDG", 48),
    "ORY": ("Paris Orly", 48),
    "BVA": ("Paris Beauvais", 42),
    "NCE": ("Nice", 55),
    "MRS": ("Marseille", 60),
    "TLN": ("Toulon-Hyeres", 75),
    "LYS": ("Lyon", 65),
    "GNB": ("Grenoble", 75),
    "CMF": ("Chambery (ski)", 90),
    "TLS": ("Toulouse", 65),
    "BOD": ("Bordeaux", 65),
    "BIQ": ("Biarritz", 75),
    "PUF": ("Pau", 90),
    "LDE": ("Lourdes-Tarbes", 90),
    "EGC": ("Bergerac", 90),
    "LIG": ("Limoges", 90),
    "CFE": ("Clermont-Ferrand", 90),
    "AVN": ("Avignon", 90),
    "FNI": ("Nimes", 75),
    "MPL": ("Montpellier", 70),
    "NTE": ("Nantes", 65),
    "RNS": ("Rennes", 75),
    "BES": ("Brest", 75),
    "CFR": ("Caen", 100),
    "DNR": ("Dinard", 100),
    "LIL": ("Lille", 60),
    "SXB": ("Strasbourg", 70),
    "AJA": ("Ajaccio (Corsica)", 100),
    "BIA": ("Bastia (Corsica)", 100),
    "FSC": ("Figari (Corsica)", 100),
    "CLY": ("Calvi (Corsica)", 110),
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
    "SIR": ("Sion (ski)", 150),
    # --- Czechia / Slovakia ---
    "PRG": ("Prague", 35),
    "BRQ": ("Brno", 48),
    "OSR": ("Ostrava", 48),
    "PED": ("Pardubice", 60),
    "KLV": ("Karlovy Vary", 90),
    "KSC": ("Kosice", 42),
    "TAT": ("Poprad-Tatry", 60),
    "ILZ": ("Zilina", 75),
    "PZY": ("Piestany", 90),
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
    "RDO": ("Radom", 75),
    "OSP": ("Olsztyn", 75),
    # --- Romania ---
    "OTP": ("Bucharest", 30),
    "CLJ": ("Cluj-Napoca", 42),
    "IAS": ("Iasi", 50),
    "TSR": ("Timisoara", 42),
    "SBZ": ("Sibiu", 55),
    "BCM": ("Bacau", 55),
    "SCV": ("Suceava", 65),
    "CRA": ("Craiova", 65),
    "OMR": ("Oradea", 65),
    "CND": ("Constanta", 55),
    "ARW": ("Arad", 75),
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
    "BWK": ("Brac", 110),
    "LSZ": ("Losinj", 130),
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
    "AAR": ("Aarhus", 70),
    "AAL": ("Aalborg", 75),
    "BLL": ("Billund", 75),
    "RNN": ("Bornholm", 110),
    "ARN": ("Stockholm Arlanda", 48),
    "BMA": ("Stockholm Bromma", 65),
    "NYO": ("Stockholm Skavsta", 65),
    "GOT": ("Gothenburg", 60),
    "MMX": ("Malmo", 65),
    "VBY": ("Visby (Gotland)", 90),
    "UME": ("Umea", 110),
    "LLA": ("Lulea", 120),
    "OSL": ("Oslo", 55),
    "TRF": ("Oslo Torp", 65),
    "BGO": ("Bergen", 75),
    "SVG": ("Stavanger", 90),
    "TRD": ("Trondheim", 90),
    "AES": ("Alesund", 100),
    "MOL": ("Molde", 110),
    "KSU": ("Kristiansund", 110),
    "KRS": ("Kristiansand", 100),
    "BOO": ("Bodo", 130),
    "EVE": ("Harstad/Narvik", 150),
    "TOS": ("Tromso (Arctic)", 130),
    "ALF": ("Alta (Arctic)", 160),
    "KKN": ("Kirkenes (Arctic)", 180),
    "LYR": ("Longyearbyen (Svalbard)", 280),
    "HEL": ("Helsinki", 60),
    "TKU": ("Turku", 90),
    "TMP": ("Tampere", 75),
    "OUL": ("Oulu", 110),
    "KAJ": ("Kajaani", 130),
    "RVN": ("Rovaniemi (Lapland)", 110),
    "IVL": ("Ivalo (north Lapland)", 150),
    "KEF": ("Reykjavik", 110),
    "AEY": ("Akureyri (north Iceland)", 200),
    "FAE": ("Faroe Islands (Vagar)", 150),
    # --- Baltics ---
    "TLL": ("Tallinn", 48),
    "RIX": ("Riga", 48),
    "VNO": ("Vilnius", 48),
    "KUN": ("Kaunas", 55),
    "PLQ": ("Palanga", 75),
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
    "SLL": ("Salalah (Oman)", 300),
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
    "TBS": ("Tbilisi", 120),
    "KUT": ("Kutaisi (Wizz hub)", 90),
    "BUS": ("Batumi", 160),
    "EVN": ("Yerevan", 140),
    "GYD": ("Baku", 160),
    "TAS": ("Tashkent", 240),
    "SKD": ("Samarkand", 280),
    "ALA": ("Almaty", 280),
    "NQZ": ("Astana", 320),
    "FRU": ("Bishkek", 320),
    "DYU": ("Dushanbe", 320),
    "ULN": ("Ulaanbaatar", 600),
    # --- East Asia ---
    "NRT": ("Tokyo Narita", 420),
    "HND": ("Tokyo Haneda", 460),
    "KIX": ("Osaka Kansai", 460),
    "NGO": ("Nagoya", 520),
    "FUK": ("Fukuoka", 520),
    "CTS": ("Sapporo", 600),
    "OKA": ("Okinawa", 600),
    "ICN": ("Seoul Incheon", 420),
    "PUS": ("Busan", 520),
    "TPE": ("Taipei", 500),
    "KHH": ("Kaohsiung", 600),
    "HKG": ("Hong Kong", 420),
    "MFM": ("Macau", 520),
    "PVG": ("Shanghai (visa)", 420),
    "PEK": ("Beijing (visa)", 420),
    "CAN": ("Guangzhou (visa)", 440),
    "CTU": ("Chengdu (visa)", 520),
    "XIY": ("Xian (visa)", 560),
    "KMG": ("Kunming (visa)", 560),
    "NKG": ("Nanjing (visa)", 520),
    "SYX": ("Sanya (Hainan, visa-free)", 520),
    # --- Southeast Asia ---
    "BKK": ("Bangkok", 380),
    "DMK": ("Bangkok Don Mueang", 420),
    "HKT": ("Phuket", 420),
    "USM": ("Koh Samui", 520),
    "CNX": ("Chiang Mai", 460),
    "KBV": ("Krabi", 460),
    "KUL": ("Kuala Lumpur", 420),
    "PEN": ("Penang", 500),
    "LGK": ("Langkawi", 600),
    "BKI": ("Kota Kinabalu", 600),
    "KCH": ("Kuching", 600),
    "SIN": ("Singapore", 460),
    "DPS": ("Bali", 460),
    "CGK": ("Jakarta", 500),
    "SUB": ("Surabaya", 600),
    "JOG": ("Yogyakarta", 600),
    "LOP": ("Lombok", 600),
    "MNL": ("Manila", 500),
    "CEB": ("Cebu", 600),
    "CRK": ("Clark", 600),
    "SGN": ("Ho Chi Minh City", 420),
    "HAN": ("Hanoi", 460),
    "DAD": ("Da Nang", 540),
    "CXR": ("Nha Trang", 540),
    "REP": ("Siem Reap (eVisa)", 600),
    "PNH": ("Phnom Penh (eVisa)", 600),
    "RGN": ("Yangon", 600),
    "VTE": ("Vientiane", 600),
    "BWN": ("Bandar Seri Begawan (Brunei)", 680),
    # --- South Asia ---
    "DEL": ("Delhi (eVisa)", 340),
    "BOM": ("Mumbai (eVisa)", 380),
    "BLR": ("Bangalore (eVisa)", 420),
    "MAA": ("Chennai (eVisa)", 460),
    "HYD": ("Hyderabad (eVisa)", 420),
    "CCU": ("Kolkata (eVisa)", 460),
    "GOI": ("Goa (eVisa)", 460),
    "JAI": ("Jaipur (eVisa)", 460),
    "COK": ("Cochin (eVisa)", 500),
    "KTM": ("Kathmandu", 420),
    "CMB": ("Colombo (eVisa)", 420),
    "MLE": ("Maldives", 500),
    "DAC": ("Dhaka", 500),
    "ISB": ("Islamabad", 420),
    "LHE": ("Lahore", 420),
    "KHI": ("Karachi", 420),
    # --- North America (visa needed) ---
    "JFK": ("New York JFK (visa)", 320),
    "EWR": ("New York Newark (visa)", 320),
    "BOS": ("Boston (visa)", 360),
    "IAD": ("Washington Dulles (visa)", 400),
    "PHL": ("Philadelphia (visa)", 400),
    "MIA": ("Miami (visa)", 400),
    "FLL": ("Fort Lauderdale (visa)", 400),
    "MCO": ("Orlando (visa)", 400),
    "TPA": ("Tampa (visa)", 420),
    "ATL": ("Atlanta (visa)", 400),
    "ORD": ("Chicago (visa)", 360),
    "MSP": ("Minneapolis (visa)", 480),
    "DFW": ("Dallas (visa)", 440),
    "IAH": ("Houston (visa)", 440),
    "AUS": ("Austin (visa)", 500),
    "DEN": ("Denver (visa)", 480),
    "PHX": ("Phoenix (visa)", 480),
    "LAS": ("Las Vegas (visa)", 440),
    "LAX": ("Los Angeles (visa)", 400),
    "SFO": ("San Francisco (visa)", 440),
    "SEA": ("Seattle (visa)", 440),
    "PDX": ("Portland OR (visa)", 480),
    "HNL": ("Honolulu (visa)", 650),
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
    "MEX": ("Mexico City", 400),
    "GDL": ("Guadalajara", 480),
    "MTY": ("Monterrey", 480),
    "CUN": ("Cancun", 400),
    "CZM": ("Cozumel", 480),
    "PVR": ("Puerto Vallarta", 480),
    "SJD": ("Los Cabos", 520),
    "MID": ("Merida", 520),
    "HAV": ("Havana", 480),
    "VRA": ("Varadero", 480),
    "PUJ": ("Punta Cana", 480),
    "SDQ": ("Santo Domingo", 480),
    "NAS": ("Nassau (Bahamas)", 560),
    "MBJ": ("Montego Bay (Jamaica)", 560),
    "KIN": ("Kingston (Jamaica)", 600),
    "GCM": ("Grand Cayman", 640),
    "AUA": ("Aruba", 560),
    "CUR": ("Curacao", 600),
    "SXM": ("Sint Maarten", 600),
    "ANU": ("Antigua", 640),
    "BGI": ("Barbados", 640),
    "POS": ("Trinidad", 640),
    "FDF": ("Martinique", 600),
    "PTP": ("Guadeloupe", 600),
    "UVF": ("St Lucia", 640),
    "DOM": ("Dominica", 680),
    "GND": ("Grenada", 680),
    "SKB": ("St Kitts", 680),
    "NEV": ("Nevis", 720),
    "EIS": ("Tortola (BVI)", 720),
    "STT": ("St Thomas (visa)", 560),
    "PTY": ("Panama City", 480),
    "SJO": ("San Jose Costa Rica", 540),
    "LIR": ("Liberia (Costa Rica)", 600),
    "GUA": ("Guatemala City", 560),
    "SAL": ("San Salvador", 600),
    "BZE": ("Belize", 640),
    "RTB": ("Roatan", 640),
    # --- South America ---
    "GRU": ("Sao Paulo", 440),
    "GIG": ("Rio de Janeiro", 480),
    "BSB": ("Brasilia", 560),
    "FOR": ("Fortaleza", 480),
    "REC": ("Recife", 520),
    "SSA": ("Salvador", 560),
    "EZE": ("Buenos Aires", 480),
    "COR": ("Cordoba", 600),
    "MDZ": ("Mendoza", 640),
    "IGR": ("Iguazu (Argentina)", 640),
    "USH": ("Ushuaia", 720),
    "SCL": ("Santiago de Chile", 560),
    "LIM": ("Lima", 520),
    "CUZ": ("Cusco", 640),
    "AQP": ("Arequipa", 640),
    "BOG": ("Bogota", 480),
    "CTG": ("Cartagena", 560),
    "MDE": ("Medellin", 520),
    "CLO": ("Cali", 560),
    "UIO": ("Quito", 560),
    "GYE": ("Guayaquil", 560),
    "MVD": ("Montevideo", 560),
    "ASU": ("Asuncion", 640),
    "LPB": ("La Paz (Bolivia)", 640),
    "VVI": ("Santa Cruz (Bolivia)", 640),
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
    "DUR": ("Durban", 600),
    "WDH": ("Windhoek", 640),
    "MPM": ("Maputo", 640),
    "HRE": ("Harare", 640),
    "VFA": ("Victoria Falls", 680),
    "LUN": ("Lusaka", 640),
    "GBE": ("Gaborone", 640),
    "TNR": ("Antananarivo (Madagascar)", 640),
    "MRU": ("Mauritius", 560),
    "SEZ": ("Seychelles", 640),
    "RUN": ("Reunion (France OT)", 640),
    "LOS": ("Lagos (visa)", 560),
    "ABV": ("Abuja (visa)", 600),
    "ACC": ("Accra", 560),
    "DKR": ("Dakar", 560),
    "ABJ": ("Abidjan", 560),
    "LAD": ("Luanda (visa)", 640),
    "DLA": ("Douala", 640),
    "BKO": ("Bamako (Mali)", 600),
    "OUA": ("Ouagadougou (Burkina Faso)", 640),
    "COO": ("Cotonou (Benin)", 640),
    "LFW": ("Lome (Togo)", 640),
    "NIM": ("Niamey (Niger)", 680),
    "NDJ": ("N'Djamena (Chad)", 720),
    "NKC": ("Nouakchott (Mauritania)", 640),
    "FNA": ("Freetown (Sierra Leone)", 640),
    "ROB": ("Monrovia (Liberia)", 640),
    "HAH": ("Moroni (Comoros)", 720),
    "ASM": ("Asmara (Eritrea, visa)", 720),
    "JIB": ("Djibouti", 560),
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
    "NAN": ("Nadi (Fiji)", 1000),
    "PPT": ("Papeete (Tahiti)", 1300),
    "NOU": ("Noumea (New Caledonia)", 1200),
    "TBU": ("Tongatapu (Tonga)", 1500),
    "APW": ("Apia (Samoa)", 1400),
    "VLI": ("Port Vila (Vanuatu)", 1300),
    "HIR": ("Honiara (Solomon Is)", 1300),
    "POM": ("Port Moresby (PNG, visa)", 1000),
    "ASB": ("Ashgabat (Turkmenistan, visa)", 500),
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


_pacing_seconds = 0.5  # global, increases when 429s appear
_pacing_lock_min = 0.5


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
    # Pull a pool of fares per month (not just the single cheapest) so the
    # trip-length cap and weekend filter have alternatives to choose from.
    # Without this, the one cheapest fare might be a 23-night trip that gets
    # rejected, leaving nothing for a route that does have a good shorter option.
    fetch_limit = 30
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
        try:
            r = requests.get(
                API_BASE,
                params=params,
                headers={"X-Access-Token": TRAVELPAYOUTS_TOKEN},
                timeout=30,
            )
            if r.status_code == 429:
                # Rate limited: slow the whole run by 0.5s and move on.
                # One missed data point is fine; retrying makes it worse.
                _pacing_seconds = min(_pacing_seconds + 0.5, 5.0)
                print(
                    f"  ~ {origin}->{destination} {ym}: 429, pacing -> {_pacing_seconds:.1f}s",
                    file=sys.stderr,
                )
                time.sleep(_pacing_seconds)
                continue
            if r.status_code == 400:
                # Route/month not indexed -- expected for sparse routes.
                time.sleep(_pacing_seconds)
                continue
            r.raise_for_status()
            data = r.json().get("data", [])
        except Exception as e:  # noqa: BLE001
            print(f"  ! {origin}->{destination} {ym}: API error {e}", file=sys.stderr)
            time.sleep(_pacing_seconds)
            continue

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
                        "link": AVIASALES + item.get("link", "") if item.get("link") else None,
                    }
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


def send_telegram(text: str) -> None:
    """Send a message to Telegram, splitting into chunks if over the limit.

    Telegram caps messages at 4096 characters. On day one (or any busy day)
    a digest can exceed that, so we split on line boundaries -- never mid-deal --
    and send sequentially with a continuation marker.
    """
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
    line = f"<b>{city}</b> {origin}{arrow} €{price}{tag}\n   {when} · {air}{baggage_hint(air)}"
    if d["link"]:
        line += f' · <a href="{d["link"]}">book</a>'
    _dest = d.get("dest")
    if _dest:
        line += f"\n   🗓 best season: {season_note(_dest)}"
    return line


# -------------------- Scan + digest --------------------

def scan_tier(cfg: dict, destinations: dict, history: dict, today: str) -> tuple[list, int]:
    deals = []
    checked = 0
    for origin in ORIGINS:
        for dest, (city, target) in destinations.items():
            key = f"{origin}-{dest}-{cfg['trip_type']}"
            cheapest = get_cheapest(origin, dest, cfg)
            if cheapest is None:
                print(f"  [{cfg['name']}] {origin}->{dest} {city}: no fares")
                continue
            checked += 1
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

            if below_target or big_drop or all_time_low:
                deals.append({
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
        lines += [fmt_deal(d) for d in lows]
        if big or cheap:
            lines.append("")
    if big:
        lines.append("🔥 <b>Big drops vs recent average</b>")
        lines += [fmt_deal(d) for d in big]
        if cheap:
            lines.append("")
    if cheap:
        lines.append("✅ <b>Below target</b>")
        lines += [fmt_deal(d) for d in cheap]
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
        # Cheapest across origins for this route.
        best = None
        for origin in ORIGINS:
            c = get_cheapest(origin, dest, cfg)
            if c and (best is None or c["price"] < best["price"]):
                best = c
                best["origin"] = origin
        if best is None:
            rows.append((dest, label, None, None, target, None))
            continue
        # Compare against stored all-time low for trend context.
        key = f"{best['origin']}-{dest}-{cfg['trip_type']}"
        entry = history.get(key, {})
        if isinstance(entry, list):
            entry = {"series": entry, "lo": None}
        lo = entry.get("lo")
        is_deal = best["price"] <= target * (1 - TARGET_MARGIN_PCT)
        rows.append((dest, label, best, lo, target, is_deal))

    if not rows:
        return None
    lines = [f"<b>👀 Watching — {fmt_date(today)}</b>", ""]
    for dest, label, best, lo, target, is_deal in rows:
        if best is None:
            lines.append(f"<b>{label}</b>: no fares found (target €{target})")
            continue
        arrow = "↔"
        price = best["price"]
        when = f"{fmt_date(best['departure_at'])} → {fmt_date(best['return_at'])}"
        nights = best.get("nights")
        when += f" ({nights} nights)" if nights else ""
        flag = "  🎯 <b>DEAL</b>" if is_deal else ""
        lo_str = f"  · lowest seen €{int(lo)}" if lo else ""
        line = (
            f"<b>{label}</b> {best['origin']}{arrow} <b>€{price}</b>"
            f"  (target €{target}){flag}{lo_str}\n"
            f"   {when} · {best['airline']}{baggage_hint(best['airline'])}"
        )
        if best["link"]:
            line += f' · <a href="{best["link"]}">book</a>'
        line += f"\n   🗓 best season: {season_note(dest)}"
        lines.append(line)
    return "\n".join(lines).strip()


def main() -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_disp = fmt_date(today)  # DD/MM/YYYY for message headers
    history = load_history()

    short_deals, short_checked = scan_tier(SHORT_HAUL, SHORT_HAUL_DESTINATIONS, history, today)
    long_deals, long_checked = scan_tier(LONG_HAUL, LONG_HAUL_DESTINATIONS, history, today)
    watch_digest = scan_watchlist(history, today)
    save_history(history)

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
        short_deals, f"<b>🇪🇺 Europe deal scan — {today_disp}</b>{mode_label}"
    )
    long_digest = build_digest(
        long_deals, f"<b>🌍 Worldwide deal scan — {today_disp}</b>{mode_label}"
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
