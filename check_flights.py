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

ROLLING_WINDOW_DAYS = int(os.environ.get("ROLLING_WINDOW_DAYS", "14"))
ROLLING_DROP_PCT = float(os.environ.get("ROLLING_DROP_PCT", "0.20"))
ORIGINS = [o.strip().upper() for o in os.environ.get("ORIGINS", "VIE,BTS").split(",") if o.strip()]

# -------------------- Tier rules --------------------

SHORT_HAUL = {
    "name": "short",
    "label": "Europe",
    "arrow": "↔",
    "trip_word": "round-trip",
    "trip_type": "rt",
    "one_way": "false",
    "direct": "true",
    "months_ahead": int(os.environ.get("SHORT_HAUL_MONTHS", "4")),
}

LONG_HAUL = {
    "name": "long",
    "label": "Worldwide",
    "arrow": "↔",
    "trip_word": "round-trip",
    "trip_type": "rt",
    "one_way": "false",
    "direct": "false",
    "months_ahead": int(os.environ.get("LONG_HAUL_MONTHS", "6")),
}

# -------------------- Short-haul destinations (round-trip EUR, direct) --------------------
#
# UK is INCLUDED. Turkish passport + Slovak EU residence = UK Standard
# Visitor Visa needed (~3 weeks lead). Ireland is EU-but-non-Schengen, no
# visa needed. Cities labelled "(visa)" need real visa effort.

SHORT_HAUL_DESTINATIONS = {
    # --- United Kingdom & Crown Dependencies (visa needed) ---
    "LHR": ("London Heathrow (visa)", 130),
    "LGW": ("London Gatwick (visa)", 100),
    "STN": ("London Stansted (visa)", 80),
    "LTN": ("London Luton (visa)", 80),
    "LCY": ("London City (visa)", 130),
    "MAN": ("Manchester (visa)", 100),
    "EDI": ("Edinburgh (visa)", 100),
    "GLA": ("Glasgow (visa)", 110),
    "ABZ": ("Aberdeen (visa)", 130),
    "INV": ("Inverness (visa)", 150),
    "BHX": ("Birmingham (visa)", 100),
    "BRS": ("Bristol (visa)", 110),
    "EMA": ("East Midlands (visa)", 110),
    "LBA": ("Leeds Bradford (visa)", 110),
    "LPL": ("Liverpool (visa)", 110),
    "NCL": ("Newcastle (visa)", 110),
    "HUY": ("Humberside (visa)", 150),
    "SOU": ("Southampton (visa)", 130),
    "EXT": ("Exeter (visa)", 150),
    "NQY": ("Newquay (visa)", 160),
    "NWI": ("Norwich (visa)", 150),
    "CWL": ("Cardiff (visa)", 120),
    "BFS": ("Belfast Intl (visa)", 120),
    "BHD": ("Belfast City (visa)", 130),
    "LDY": ("Derry (visa)", 150),
    "JER": ("Jersey (visa)", 150),
    "GCI": ("Guernsey (visa)", 160),
    "IOM": ("Isle of Man (visa)", 160),
    # --- Ireland ---
    "DUB": ("Dublin", 80),
    "ORK": ("Cork", 100),
    "SNN": ("Shannon", 110),
    "NOC": ("Knock", 110),
    "KIR": ("Kerry", 130),
    # --- Italy ---
    "FCO": ("Rome Fiumicino", 55),
    "CIA": ("Rome Ciampino", 55),
    "MXP": ("Milan Malpensa", 55),
    "BGY": ("Milan Bergamo", 45),
    "LIN": ("Milan Linate", 80),
    "TRN": ("Turin", 65),
    "AOT": ("Aosta", 130),
    "CUF": ("Cuneo", 130),
    "PMF": ("Parma", 100),
    "RMI": ("Rimini", 95),
    "BLQ": ("Bologna", 45),
    "FLR": ("Florence", 75),
    "PSA": ("Pisa", 55),
    "VCE": ("Venice Marco Polo", 45),
    "TSF": ("Venice Treviso", 45),
    "VRN": ("Verona", 65),
    "TRS": ("Trieste", 75),
    "GOA": ("Genoa", 80),
    "AOI": ("Ancona", 75),
    "PSR": ("Pescara", 75),
    "NAP": ("Naples", 65),
    "FOG": ("Foggia", 100),
    "BRI": ("Bari", 65),
    "BDS": ("Brindisi", 65),
    "SUF": ("Lamezia Terme (Calabria)", 75),
    "REG": ("Reggio Calabria", 95),
    "CTA": ("Catania (Sicily)", 75),
    "PMO": ("Palermo (Sicily)", 75),
    "TPS": ("Trapani (Sicily)", 75),
    "CIY": ("Comiso (Sicily)", 110),
    "LMP": ("Lampedusa", 200),
    "PNL": ("Pantelleria", 200),
    "OLB": ("Olbia (Sardinia)", 95),
    "CAG": ("Cagliari (Sardinia)", 85),
    "AHO": ("Alghero (Sardinia)", 85),
    # --- Iberia ---
    "BCN": ("Barcelona", 65),
    "GRO": ("Girona", 75),
    "REU": ("Reus", 80),
    "ZAZ": ("Zaragoza", 95),
    "VLL": ("Valladolid", 130),
    "RGS": ("Burgos", 150),
    "MAD": ("Madrid", 85),
    "VLC": ("Valencia", 75),
    "ALC": ("Alicante", 80),
    "MJV": ("Murcia", 95),
    "AGP": ("Malaga", 85),
    "GRX": ("Granada", 110),
    "LEI": ("Almeria", 100),
    "SVQ": ("Seville", 95),
    "BJZ": ("Badajoz", 150),
    "BIO": ("Bilbao", 110),
    "EAS": ("San Sebastian", 130),
    "SDR": ("Santander", 110),
    "OVD": ("Asturias", 120),
    "LEN": ("Leon", 150),
    "SCQ": ("Santiago de Compostela", 110),
    "VGO": ("Vigo", 110),
    "PMI": ("Palma de Mallorca", 75),
    "IBZ": ("Ibiza", 95),
    "MAH": ("Menorca", 100),
    "TFS": ("Tenerife South", 130),
    "TFN": ("Tenerife North", 130),
    "LPA": ("Gran Canaria", 130),
    "ACE": ("Lanzarote", 130),
    "FUE": ("Fuerteventura", 130),
    "VDE": ("El Hierro (Canaries)", 200),
    "SPC": ("La Palma (Canaries)", 180),
    "LIS": ("Lisbon", 85),
    "OPO": ("Porto", 85),
    "FAO": ("Faro (Algarve)", 100),
    "FNC": ("Funchal (Madeira)", 150),
    "PXO": ("Porto Santo (Madeira)", 200),
    "PDL": ("Ponta Delgada (Azores)", 200),
    "TER": ("Terceira (Azores)", 220),
    "HOR": ("Horta (Azores)", 250),
    # --- France ---
    "CDG": ("Paris CDG", 75),
    "ORY": ("Paris Orly", 75),
    "BVA": ("Paris Beauvais", 65),
    "NCE": ("Nice", 85),
    "MRS": ("Marseille", 95),
    "TLN": ("Toulon-Hyeres", 110),
    "LYS": ("Lyon", 100),
    "GNB": ("Grenoble", 110),
    "CMF": ("Chambery (ski)", 130),
    "TLS": ("Toulouse", 100),
    "BOD": ("Bordeaux", 100),
    "BIQ": ("Biarritz", 110),
    "PUF": ("Pau", 130),
    "LDE": ("Lourdes-Tarbes", 130),
    "EGC": ("Bergerac", 130),
    "LIG": ("Limoges", 130),
    "CFE": ("Clermont-Ferrand", 130),
    "AVN": ("Avignon", 130),
    "FNI": ("Nimes", 110),
    "MPL": ("Montpellier", 110),
    "NTE": ("Nantes", 100),
    "RNS": ("Rennes", 110),
    "BES": ("Brest", 110),
    "CFR": ("Caen", 150),
    "DNR": ("Dinard", 150),
    "LIL": ("Lille", 95),
    "SXB": ("Strasbourg", 110),
    "AJA": ("Ajaccio (Corsica)", 150),
    "BIA": ("Bastia (Corsica)", 150),
    "FSC": ("Figari (Corsica)", 150),
    "CLY": ("Calvi (Corsica)", 160),
    # --- Benelux ---
    "AMS": ("Amsterdam", 75),
    "EIN": ("Eindhoven", 75),
    "RTM": ("Rotterdam", 95),
    "GRQ": ("Groningen", 110),
    "MST": ("Maastricht", 110),
    "BRU": ("Brussels", 65),
    "CRL": ("Brussels Charleroi", 55),
    "ANR": ("Antwerp", 95),
    "OST": ("Ostend", 110),
    "LGG": ("Liege", 110),
    "LUX": ("Luxembourg", 110),
    # --- Germany ---
    "BER": ("Berlin", 55),
    "HAM": ("Hamburg", 65),
    "MUC": ("Munich", 75),
    "NUE": ("Nuremberg", 75),
    "FMM": ("Memmingen", 75),
    "FRA": ("Frankfurt", 75),
    "HHN": ("Frankfurt-Hahn", 75),
    "NRN": ("Weeze (Ryanair hub)", 65),
    "DUS": ("Dusseldorf", 75),
    "CGN": ("Cologne", 65),
    "DTM": ("Dortmund", 95),
    "PAD": ("Paderborn", 110),
    "STR": ("Stuttgart", 75),
    "FKB": ("Karlsruhe/Baden-Baden", 95),
    "FDH": ("Friedrichshafen", 100),
    "LEJ": ("Leipzig", 75),
    "DRS": ("Dresden", 75),
    "ERF": ("Erfurt", 110),
    "BRE": ("Bremen", 95),
    "HAJ": ("Hannover", 95),
    "RLG": ("Rostock-Laage", 130),
    # --- Switzerland ---
    "ZRH": ("Zurich", 130),
    "GVA": ("Geneva", 130),
    "BSL": ("Basel", 100),
    "BRN": ("Bern", 150),
    "LUG": ("Lugano", 130),
    "SIR": ("Sion (ski)", 180),
    # --- Czechia / Slovakia ---
    "PRG": ("Prague", 55),
    "BRQ": ("Brno", 75),
    "OSR": ("Ostrava", 75),
    "PED": ("Pardubice", 95),
    "KLV": ("Karlovy Vary", 120),
    "KSC": ("Kosice", 65),
    "TAT": ("Poprad-Tatry", 95),
    "ILZ": ("Zilina", 110),
    "PZY": ("Piestany", 130),
    # --- Poland ---
    "WAW": ("Warsaw", 55),
    "KRK": ("Krakow", 55),
    "GDN": ("Gdansk", 65),
    "POZ": ("Poznan", 65),
    "WRO": ("Wroclaw", 55),
    "KTW": ("Katowice", 55),
    "LCJ": ("Lodz", 75),
    "LUZ": ("Lublin", 80),
    "BZG": ("Bydgoszcz", 80),
    "RZE": ("Rzeszow", 75),
    "SZZ": ("Szczecin", 75),
    "RDO": ("Radom", 110),
    "OSP": ("Olsztyn", 110),
    # --- Romania ---
    "OTP": ("Bucharest", 45),
    "CLJ": ("Cluj-Napoca", 65),
    "IAS": ("Iasi", 75),
    "TSR": ("Timisoara", 65),
    "SBZ": ("Sibiu", 80),
    "BCM": ("Bacau", 80),
    "SCV": ("Suceava", 95),
    "CRA": ("Craiova", 95),
    "OMR": ("Oradea", 95),
    "CND": ("Constanta", 85),
    "ARW": ("Arad", 110),
    # --- Bulgaria ---
    "SOF": ("Sofia", 45),
    "VAR": ("Varna", 75),
    "BOJ": ("Burgas", 75),
    "PDV": ("Plovdiv", 95),
    # --- Moldova ---
    "KIV": ("Chisinau", 110),
    # --- Croatia ---
    "ZAG": ("Zagreb", 75),
    "SPU": ("Split", 85),
    "DBV": ("Dubrovnik", 95),
    "ZAD": ("Zadar", 75),
    "PUY": ("Pula", 75),
    "RJK": ("Rijeka", 95),
    "OSI": ("Osijek", 100),
    "BWK": ("Brac", 130),
    "LSZ": ("Losinj", 150),
    # --- Balkans ---
    "BEG": ("Belgrade", 65),
    "INI": ("Nis", 95),
    "SJJ": ("Sarajevo", 85),
    "TZL": ("Tuzla", 95),
    "BNX": ("Banja Luka", 110),
    "TGD": ("Podgorica", 95),
    "TIV": ("Tivat", 100),
    "TIA": ("Tirana", 85),
    "SKP": ("Skopje", 75),
    "OHD": ("Ohrid", 100),
    "PRN": ("Pristina", 95),
    # --- Greece ---
    "ATH": ("Athens", 65),
    "SKG": ("Thessaloniki", 65),
    "KVA": ("Kavala", 95),
    "JTY": ("Astypalaia", 180),
    "JIK": ("Ikaria", 160),
    "JKH": ("Chios", 130),
    "LXS": ("Limnos", 150),
    "MJT": ("Mytilene (Lesvos)", 130),
    "SMI": ("Samos", 130),
    "JSI": ("Skiathos", 130),
    "SKU": ("Skyros", 160),
    "VOL": ("Volos", 110),
    "JNX": ("Naxos", 150),
    "PAS": ("Paros", 150),
    "MLO": ("Milos", 160),
    "JSH": ("Sitia (Crete)", 160),
    "HER": ("Heraklion (Crete)", 100),
    "CHQ": ("Chania (Crete)", 100),
    "RHO": ("Rhodes", 100),
    "AOK": ("Karpathos", 160),
    "JTR": ("Santorini", 110),
    "JMK": ("Mykonos", 130),
    "CFU": ("Corfu", 95),
    "PVK": ("Preveza/Lefkada", 130),
    "KGS": ("Kos", 110),
    "ZTH": ("Zakynthos", 110),
    "JKL": ("Kalamata", 110),
    "EFL": ("Kefalonia", 110),
    # --- Cyprus & Malta ---
    "LCA": ("Larnaca (Cyprus)", 110),
    "PFO": ("Paphos (Cyprus)", 130),
    "MLA": ("Malta", 75),
    # --- Nordics ---
    "CPH": ("Copenhagen", 75),
    "AAR": ("Aarhus", 100),
    "AAL": ("Aalborg", 110),
    "BLL": ("Billund", 110),
    "RNN": ("Bornholm", 150),
    "ARN": ("Stockholm Arlanda", 75),
    "BMA": ("Stockholm Bromma", 95),
    "NYO": ("Stockholm Skavsta", 100),
    "GOT": ("Gothenburg", 95),
    "MMX": ("Malmo", 100),
    "VBY": ("Visby (Gotland)", 130),
    "UME": ("Umea", 150),
    "LLA": ("Lulea", 160),
    "OSL": ("Oslo", 85),
    "TRF": ("Oslo Torp", 100),
    "BGO": ("Bergen", 110),
    "SVG": ("Stavanger", 130),
    "TRD": ("Trondheim", 130),
    "AES": ("Alesund", 150),
    "MOL": ("Molde", 160),
    "KSU": ("Kristiansund", 160),
    "KRS": ("Kristiansand", 150),
    "BOO": ("Bodo", 180),
    "EVE": ("Harstad/Narvik", 200),
    "TOS": ("Tromso (Arctic)", 180),
    "ALF": ("Alta (Arctic)", 220),
    "KKN": ("Kirkenes (Arctic)", 240),
    "LYR": ("Longyearbyen (Svalbard)", 350),
    "HEL": ("Helsinki", 95),
    "TKU": ("Turku", 130),
    "TMP": ("Tampere", 110),
    "OUL": ("Oulu", 150),
    "KAJ": ("Kajaani", 180),
    "RVN": ("Rovaniemi (Lapland)", 150),
    "IVL": ("Ivalo (north Lapland)", 200),
    "KEF": ("Reykjavik", 150),
    "AEY": ("Akureyri (north Iceland)", 250),
    "FAE": ("Faroe Islands (Vagar)", 200),
    # --- Baltics ---
    "TLL": ("Tallinn", 75),
    "RIX": ("Riga", 75),
    "VNO": ("Vilnius", 75),
    "KUN": ("Kaunas", 80),
    "PLQ": ("Palanga", 110),
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
    "DXB": ("Dubai", 180),
    "AUH": ("Abu Dhabi", 200),
    "SHJ": ("Sharjah", 200),
    "DOH": ("Doha", 250),
    "BAH": ("Bahrain", 250),
    "KWI": ("Kuwait", 250),
    "MCT": ("Muscat", 280),
    "SLL": ("Salalah (Oman)", 350),
    "RUH": ("Riyadh", 350),
    "JED": ("Jeddah", 350),
    "DMM": ("Dammam", 350),
    "MED": ("Medina", 400),
    "TLV": ("Tel Aviv", 250),
    "ETM": ("Eilat", 300),
    "BEY": ("Beirut", 250),
    "AMM": ("Amman", 250),
    "BGW": ("Baghdad (visa)", 400),
    "EBL": ("Erbil (Kurdistan)", 350),
    "IKA": ("Tehran (eVisa)", 300),
    "MHD": ("Mashhad (eVisa)", 350),
    # --- North Africa ---
    "CAI": ("Cairo (eVisa)", 250),
    "HRG": ("Hurghada (eVisa)", 300),
    "SSH": ("Sharm el-Sheikh (eVisa)", 300),
    "LXR": ("Luxor (eVisa)", 350),
    "RAK": ("Marrakech", 150),
    "CMN": ("Casablanca", 200),
    "AGA": ("Agadir", 250),
    "FEZ": ("Fez", 280),
    "TNG": ("Tangier", 220),
    "OUD": ("Oujda", 250),
    "NDR": ("Nador", 250),
    "TUN": ("Tunis", 200),
    "DJE": ("Djerba", 280),
    "ALG": ("Algiers", 250),
    "ORN": ("Oran", 280),
    # --- Caucasus / Central Asia / Mongolia ---
    "TBS": ("Tbilisi", 150),
    "KUT": ("Kutaisi (Wizz hub)", 120),
    "BUS": ("Batumi", 200),
    "EVN": ("Yerevan", 180),
    "GYD": ("Baku", 200),
    "TAS": ("Tashkent", 300),
    "SKD": ("Samarkand", 350),
    "ALA": ("Almaty", 350),
    "NQZ": ("Astana", 400),
    "FRU": ("Bishkek", 400),
    "DYU": ("Dushanbe", 400),
    "ULN": ("Ulaanbaatar", 700),
    # --- East Asia ---
    "NRT": ("Tokyo Narita", 500),
    "HND": ("Tokyo Haneda", 550),
    "KIX": ("Osaka Kansai", 550),
    "NGO": ("Nagoya", 600),
    "FUK": ("Fukuoka", 600),
    "CTS": ("Sapporo", 700),
    "OKA": ("Okinawa", 700),
    "ICN": ("Seoul Incheon", 500),
    "PUS": ("Busan", 600),
    "TPE": ("Taipei", 600),
    "KHH": ("Kaohsiung", 700),
    "HKG": ("Hong Kong", 500),
    "MFM": ("Macau", 600),
    "PVG": ("Shanghai (visa)", 500),
    "PEK": ("Beijing (visa)", 500),
    "CAN": ("Guangzhou (visa)", 500),
    "CTU": ("Chengdu (visa)", 600),
    "XIY": ("Xian (visa)", 650),
    "KMG": ("Kunming (visa)", 650),
    "NKG": ("Nanjing (visa)", 600),
    "SYX": ("Sanya (Hainan, visa-free)", 600),
    # --- Southeast Asia ---
    "BKK": ("Bangkok", 450),
    "DMK": ("Bangkok Don Mueang", 500),
    "HKT": ("Phuket", 500),
    "USM": ("Koh Samui", 600),
    "CNX": ("Chiang Mai", 550),
    "KBV": ("Krabi", 550),
    "KUL": ("Kuala Lumpur", 500),
    "PEN": ("Penang", 600),
    "LGK": ("Langkawi", 700),
    "BKI": ("Kota Kinabalu", 700),
    "KCH": ("Kuching", 700),
    "SIN": ("Singapore", 550),
    "DPS": ("Bali", 550),
    "CGK": ("Jakarta", 600),
    "SUB": ("Surabaya", 700),
    "JOG": ("Yogyakarta", 700),
    "LOP": ("Lombok", 700),
    "MNL": ("Manila", 600),
    "CEB": ("Cebu", 700),
    "CRK": ("Clark", 700),
    "SGN": ("Ho Chi Minh City", 500),
    "HAN": ("Hanoi", 550),
    "DAD": ("Da Nang", 650),
    "CXR": ("Nha Trang", 650),
    "REP": ("Siem Reap (eVisa)", 700),
    "PNH": ("Phnom Penh (eVisa)", 700),
    "RGN": ("Yangon", 700),
    "VTE": ("Vientiane", 700),
    "BWN": ("Bandar Seri Begawan (Brunei)", 800),
    # --- South Asia ---
    "DEL": ("Delhi (eVisa)", 400),
    "BOM": ("Mumbai (eVisa)", 450),
    "BLR": ("Bangalore (eVisa)", 500),
    "MAA": ("Chennai (eVisa)", 550),
    "HYD": ("Hyderabad (eVisa)", 500),
    "CCU": ("Kolkata (eVisa)", 550),
    "GOI": ("Goa (eVisa)", 550),
    "JAI": ("Jaipur (eVisa)", 550),
    "COK": ("Cochin (eVisa)", 600),
    "KTM": ("Kathmandu", 500),
    "CMB": ("Colombo (eVisa)", 500),
    "MLE": ("Maldives", 600),
    "DAC": ("Dhaka", 600),
    "ISB": ("Islamabad", 500),
    "LHE": ("Lahore", 500),
    "KHI": ("Karachi", 500),
    # --- North America (visa needed) ---
    "JFK": ("New York JFK (visa)", 400),
    "EWR": ("New York Newark (visa)", 400),
    "BOS": ("Boston (visa)", 450),
    "IAD": ("Washington Dulles (visa)", 500),
    "PHL": ("Philadelphia (visa)", 500),
    "MIA": ("Miami (visa)", 500),
    "FLL": ("Fort Lauderdale (visa)", 500),
    "MCO": ("Orlando (visa)", 500),
    "TPA": ("Tampa (visa)", 500),
    "ATL": ("Atlanta (visa)", 500),
    "ORD": ("Chicago (visa)", 450),
    "MSP": ("Minneapolis (visa)", 600),
    "DFW": ("Dallas (visa)", 550),
    "IAH": ("Houston (visa)", 550),
    "AUS": ("Austin (visa)", 600),
    "DEN": ("Denver (visa)", 600),
    "PHX": ("Phoenix (visa)", 600),
    "LAS": ("Las Vegas (visa)", 550),
    "LAX": ("Los Angeles (visa)", 500),
    "SFO": ("San Francisco (visa)", 550),
    "SEA": ("Seattle (visa)", 550),
    "PDX": ("Portland OR (visa)", 600),
    "HNL": ("Honolulu (visa)", 800),
    "OGG": ("Maui (visa)", 850),
    "ANC": ("Anchorage (visa)", 700),
    "YYZ": ("Toronto (visa)", 450),
    "YUL": ("Montreal (visa)", 450),
    "YOW": ("Ottawa (visa)", 500),
    "YQB": ("Quebec City (visa)", 550),
    "YHZ": ("Halifax (visa)", 550),
    "YYC": ("Calgary (visa)", 650),
    "YEG": ("Edmonton (visa)", 650),
    "YVR": ("Vancouver (visa)", 600),
    # --- Mexico / Caribbean / Central America ---
    "MEX": ("Mexico City", 500),
    "GDL": ("Guadalajara", 600),
    "MTY": ("Monterrey", 600),
    "CUN": ("Cancun", 500),
    "CZM": ("Cozumel", 600),
    "PVR": ("Puerto Vallarta", 600),
    "SJD": ("Los Cabos", 650),
    "MID": ("Merida", 650),
    "HAV": ("Havana", 600),
    "VRA": ("Varadero", 600),
    "PUJ": ("Punta Cana", 600),
    "SDQ": ("Santo Domingo", 600),
    "NAS": ("Nassau (Bahamas)", 700),
    "MBJ": ("Montego Bay (Jamaica)", 700),
    "KIN": ("Kingston (Jamaica)", 750),
    "GCM": ("Grand Cayman", 800),
    "AUA": ("Aruba", 700),
    "CUR": ("Curacao", 750),
    "SXM": ("Sint Maarten", 750),
    "ANU": ("Antigua", 800),
    "BGI": ("Barbados", 800),
    "POS": ("Trinidad", 800),
    "FDF": ("Martinique", 750),
    "PTP": ("Guadeloupe", 750),
    "PTY": ("Panama City", 600),
    "SJO": ("San Jose Costa Rica", 700),
    "LIR": ("Liberia (Costa Rica)", 750),
    "GUA": ("Guatemala City", 700),
    "SAL": ("San Salvador", 750),
    "BZE": ("Belize", 800),
    "RTB": ("Roatan", 800),
    # --- South America ---
    "GRU": ("Sao Paulo", 550),
    "GIG": ("Rio de Janeiro", 600),
    "BSB": ("Brasilia", 700),
    "FOR": ("Fortaleza", 600),
    "REC": ("Recife", 650),
    "SSA": ("Salvador", 700),
    "EZE": ("Buenos Aires", 600),
    "COR": ("Cordoba", 750),
    "MDZ": ("Mendoza", 800),
    "IGR": ("Iguazu (Argentina)", 800),
    "USH": ("Ushuaia", 900),
    "SCL": ("Santiago de Chile", 700),
    "LIM": ("Lima", 650),
    "CUZ": ("Cusco", 800),
    "AQP": ("Arequipa", 800),
    "BOG": ("Bogota", 600),
    "CTG": ("Cartagena", 700),
    "MDE": ("Medellin", 650),
    "CLO": ("Cali", 700),
    "UIO": ("Quito", 700),
    "GYE": ("Guayaquil", 700),
    "MVD": ("Montevideo", 700),
    "ASU": ("Asuncion", 800),
    "LPB": ("La Paz (Bolivia)", 800),
    "VVI": ("Santa Cruz (Bolivia)", 800),
    # --- Africa (sub-Saharan) ---
    "NBO": ("Nairobi (eVisa)", 500),
    "MBA": ("Mombasa (eVisa)", 600),
    "JRO": ("Kilimanjaro (eVisa)", 600),
    "DAR": ("Dar es Salaam (eVisa)", 600),
    "ZNZ": ("Zanzibar (eVisa)", 600),
    "EBB": ("Entebbe (eVisa)", 700),
    "KGL": ("Kigali", 700),
    "ADD": ("Addis Ababa", 500),
    "JNB": ("Johannesburg", 500),
    "CPT": ("Cape Town", 500),
    "DUR": ("Durban", 700),
    "WDH": ("Windhoek", 800),
    "MPM": ("Maputo", 800),
    "HRE": ("Harare", 800),
    "VFA": ("Victoria Falls", 850),
    "LUN": ("Lusaka", 800),
    "GBE": ("Gaborone", 800),
    "TNR": ("Antananarivo (Madagascar)", 800),
    "MRU": ("Mauritius", 700),
    "SEZ": ("Seychelles", 800),
    "RUN": ("Reunion (France OT)", 800),
    "LOS": ("Lagos (visa)", 700),
    "ABV": ("Abuja (visa)", 750),
    "ACC": ("Accra", 700),
    "DKR": ("Dakar", 700),
    "ABJ": ("Abidjan", 700),
    "LAD": ("Luanda (visa)", 800),
    "DLA": ("Douala", 800),
    "SID": ("Sal (Cape Verde)", 400),
    "RAI": ("Praia (Cape Verde)", 500),
    # --- Oceania (visa needed for AU/NZ) ---
    "SYD": ("Sydney (visa)", 900),
    "MEL": ("Melbourne (visa)", 900),
    "BNE": ("Brisbane (visa)", 950),
    "PER": ("Perth (visa)", 1000),
    "ADL": ("Adelaide (visa)", 1000),
    "OOL": ("Gold Coast (visa)", 950),
    "CNS": ("Cairns (visa)", 1000),
    "AKL": ("Auckland (visa)", 1000),
    "CHC": ("Christchurch (visa)", 1100),
    "ZQN": ("Queenstown (visa)", 1100),
    "WLG": ("Wellington (visa)", 1100),
    "NAN": ("Nadi (Fiji)", 1200),
    "PPT": ("Papeete (Tahiti)", 1500),
    "NOU": ("Noumea (New Caledonia)", 1400),
    # --- Extra Caribbean islands ---
    "UVF": ("St Lucia", 800),
    "DOM": ("Dominica", 850),
    "GND": ("Grenada", 850),
    "SKB": ("St Kitts", 850),
    "NEV": ("Nevis", 900),
    "EIS": ("Tortola (BVI)", 900),
    "STT": ("St Thomas (visa)", 700),
    # --- Extra US (visa needed) ---
    "CLT": ("Charlotte (visa)", 550),
    "BNA": ("Nashville (visa)", 600),
    "MSY": ("New Orleans (visa)", 600),
    "RDU": ("Raleigh-Durham (visa)", 600),
    "SAT": ("San Antonio (visa)", 650),
    "SLC": ("Salt Lake City (visa)", 700),
    "PBI": ("West Palm Beach (visa)", 600),
    "RSW": ("Fort Myers (visa)", 600),
    "SAV": ("Savannah (visa)", 700),
    "MEM": ("Memphis (visa)", 700),
    "OMA": ("Omaha (visa)", 750),
    "ABQ": ("Albuquerque (visa)", 750),
    "KOA": ("Kona (Big Island, visa)", 900),
    "LIH": ("Kauai (visa)", 900),
    "ITO": ("Hilo (Big Island, visa)", 950),
    # --- Africa gaps ---
    "BKO": ("Bamako (Mali)", 750),
    "OUA": ("Ouagadougou (Burkina Faso)", 800),
    "COO": ("Cotonou (Benin)", 800),
    "LFW": ("Lome (Togo)", 800),
    "NIM": ("Niamey (Niger)", 850),
    "NDJ": ("N'Djamena (Chad)", 900),
    "NKC": ("Nouakchott (Mauritania)", 800),
    "FNA": ("Freetown (Sierra Leone)", 800),
    "ROB": ("Monrovia (Liberia)", 800),
    "HAH": ("Moroni (Comoros)", 900),
    "ASM": ("Asmara (Eritrea, visa)", 900),
    "JIB": ("Djibouti", 700),
    # --- Asia / Pacific gaps ---
    "PBH": ("Paro (Bhutan, visa)", 900),
    "TBU": ("Tongatapu (Tonga)", 1800),
    "APW": ("Apia (Samoa)", 1700),
    "VLI": ("Port Vila (Vanuatu)", 1500),
    "HIR": ("Honiara (Solomon Is)", 1500),
    "POM": ("Port Moresby (PNG, visa)", 1200),
    "ASB": ("Ashgabat (Turkmenistan, visa)", 600),
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
    for ym in upcoming_months(cfg["months_ahead"]):
        params = {
            "origin": origin,
            "destination": destination,
            "departure_at": ym,
            "currency": "eur",
            "one_way": cfg["one_way"],
            "direct": cfg["direct"],
            "sorting": "price",
            "limit": 1,
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
            item = data[0]
            price = item.get("price")
            if price is not None:
                if best is None or price < best["price"]:
                    best = {
                        "price": int(round(price)),
                        "airline": item.get("airline", "?"),
                        "departure_at": (item.get("departure_at") or "")[:10],
                        "return_at": (item.get("return_at") or "")[:10],
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

def send_telegram(text: str) -> None:
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


def fmt_deal(d: dict) -> str:
    arrow = d["cfg"]["arrow"]
    city = d["city"]
    origin = d["origin"]
    price = d["price"]
    target = d["target"]
    dep = d["departure_at"] or "flexible"
    ret = d["return_at"]
    air = d["airline"]
    when = f"{dep} → {ret}" if ret else dep
    tag = ""
    if d["below_target"]:
        tag += f"  (target €{target})"
    if d["big_drop"]:
        tag += f"  ↓ from €{int(d['avg'])} avg"
    line = f"<b>{city}</b> {origin}{arrow} €{price}{tag}\n   {when} · {air}"
    if d["link"]:
        line += f' · <a href="{d["link"]}">book</a>'
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
            series = history.get(key, [])
            avg = rolling_avg(series)
            below_target = price <= target
            big_drop = avg is not None and price <= avg * (1 - ROLLING_DROP_PCT)

            print(
                f"  [{cfg['name']}] {origin}->{dest} {city}: €{price}"
                f" | target €{target}"
                f" | avg {('€%d' % avg) if avg else 'n/a'}"
                f" | {'HIT' if (below_target or big_drop) else '-'}"
            )

            if below_target or big_drop:
                deals.append({
                    "tier": cfg["name"],
                    "cfg": cfg,
                    "origin": origin,
                    "dest": dest,
                    "city": city,
                    "price": price,
                    "target": target,
                    "avg": avg,
                    "airline": cheapest["airline"],
                    "departure_at": cheapest["departure_at"],
                    "return_at": cheapest["return_at"],
                    "link": cheapest["link"],
                    "below_target": below_target,
                    "big_drop": big_drop,
                    "score": (target - price) / target,
                })

            series.append({"date": today, "price": price})
            history[key] = series[-(ROLLING_WINDOW_DAYS * 2):]
    return deals, checked


def build_digest(deals: list, header: str) -> str | None:
    """Build a Telegram digest for one tier. Returns None if no deals."""
    if not deals:
        return None
    big = sorted([d for d in deals if d["big_drop"]],
                 key=lambda d: d["score"], reverse=True)
    cheap = sorted([d for d in deals if d["below_target"] and not d["big_drop"]],
                   key=lambda d: d["score"], reverse=True)
    lines = [header, ""]
    if big:
        lines.append("🔥 <b>Big drops vs recent average</b>")
        lines += [fmt_deal(d) for d in big]
        if cheap:
            lines.append("")
    if cheap:
        lines.append("✅ <b>Below target</b>")
        lines += [fmt_deal(d) for d in cheap]
    return "\n".join(lines).strip()


def main() -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    history = load_history()

    short_deals, short_checked = scan_tier(SHORT_HAUL, SHORT_HAUL_DESTINATIONS, history, today)
    long_deals, long_checked = scan_tier(LONG_HAUL, LONG_HAUL_DESTINATIONS, history, today)
    save_history(history)

    short_digest = build_digest(
        short_deals, f"<b>🇪🇺 Europe deal scan — {today}</b> (round-trip)"
    )
    long_digest = build_digest(
        long_deals, f"<b>🌍 Worldwide deal scan — {today}</b> (round-trip)"
    )

    sent = 0
    if short_digest:
        send_telegram(short_digest)
        sent += 1
    if long_digest:
        send_telegram(long_digest)
        sent += 1

    if sent == 0:
        print(f"No deals today. Checked {short_checked + long_checked} routes.")
    else:
        print(
            f"Sent {sent} digest(s): {len(short_deals)} short + {len(long_deals)} long"
            f" across {short_checked + long_checked} routes."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
