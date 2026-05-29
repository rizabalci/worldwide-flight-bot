# Worldwide Flight Bot

Daily round-trip deal scanner from **Vienna (VIE)** and **Bratislava (BTS)** to **596 cities worldwide** in two tiers with different rules. Sibling of the Istanbul bot — same free stack (Travelpayouts + Telegram + GitHub Actions), no Anthropic API, zero cost.

It sends **up to two ranked Telegram digests per day** — one for Europe, one for worldwide — only when there's something worth booking in that tier. Silent on dealless days. Mute either stream without losing the other.

## Two tiers, two rule sets

| | Short-haul | Long-haul |
|---|---|---|
| Region | Europe + UK | Worldwide |
| Cities | 309 | 287 |
| Trip type | Round-trip | Round-trip |
| Stops | **Direct only** | **Stops allowed** |
| Months ahead | 4 | 6 |
| Target range | €45–200 | €150–1,200 |
| Message header | 🇪🇺 Europe deal scan | 🌍 Worldwide deal scan |

**Both tiers track round-trip** so the target = the actual price you'd pay for a real trip, no mental doubling required. Short-haul stays **direct-only** because European routes have direct options from VIE/BTS almost everywhere worth flying. Long-haul **allows stops** because intercontinental direct flights from VIE are sparse and expensive — letting the cheapest itinerary win usually means Turkish via IST, Emirates via DXB, Qatar via DOH, which is how good long-haul deals actually look.

## How an alert fires

Each city has its own round-trip target price. A route triggers if **either**:

1. **Below target** — the fare is at or under the target.
2. **Big drop** — the fare is 20%+ below the route's rolling 14-day average.

Each tier produces its own message. Inside a message, big drops come first, then below-target deals, sorted by best value. If a tier has nothing today, no message is sent for it.

History is keyed by `origin-destination-triptype` so unrelated price series never mix in the rolling average.

## Coverage

### Short-haul (Europe + UK)

231 cities across:

- **UK** (visa needed) — all 4 London airports plus London City, Manchester, Edinburgh, Glasgow, Aberdeen, Birmingham, Bristol, East Midlands, Leeds Bradford, Liverpool, Newcastle, Southampton, Cardiff, Belfast, Jersey
- **Ireland** — Dublin, Cork, Shannon, Knock, Kerry
- **Italy** — Rome (FCO/CIA), Milan (MXP/BGY/LIN), Turin, Bologna, Florence, Pisa, Venice (VCE/TSF), Verona, Trieste, Genoa, Ancona, Pescara, Naples, Foggia, Bari, Brindisi, Lamezia Terme, Reggio Calabria, Sicily (Catania/Palermo/Trapani), Sardinia (Olbia/Cagliari/Alghero)
- **Spain** — Barcelona, Girona, Reus, Madrid, Valencia, Alicante, Murcia, Malaga, Granada, Almería, Seville, Bilbao, Santander, Asturias, Santiago, Vigo, Zaragoza, all Balearics (Mallorca, Ibiza, Menorca), all Canaries (Tenerife N/S, Gran Canaria, Lanzarote, Fuerteventura)
- **Portugal** — Lisbon, Porto, Faro, Madeira (Funchal), Azores (Ponta Delgada, Terceira)
- **France** — Paris (CDG/Orly/Beauvais), Nice, Marseille, Lyon, Grenoble, Chambéry (ski), Toulouse, Bordeaux, Biarritz, Nîmes, Montpellier, Nantes, Rennes, Brest, Lille, Strasbourg, all Corsica (Ajaccio, Bastia, Figari)
- **Benelux** — Amsterdam, Eindhoven, Rotterdam, Groningen, Maastricht, Brussels, Charleroi, Antwerp, Ostend, Liège, Luxembourg
- **Germany** — Berlin, Hamburg, Munich, Nuremberg, Memmingen, Frankfurt (FRA + Hahn), **Weeze** (Ryanair hub), Düsseldorf, Cologne, Dortmund, Paderborn, Stuttgart, Karlsruhe/Baden-Baden, Friedrichshafen, Leipzig, Dresden, Erfurt, Bremen, Hannover
- **Switzerland** — Zurich, Geneva, Basel, Bern, Lugano
- **Czechia / Slovakia** — Prague, Brno, Ostrava, Pardubice, Košice, Poprad-Tatry
- **Poland** — Warsaw, Krakow, Gdansk, Poznan, Wroclaw, Katowice, Łódź, Lublin, Bydgoszcz, Rzeszów, Szczecin
- **Romania** — Bucharest, Cluj-Napoca, Iași, Timișoara, Sibiu, Bacău, Suceava, Craiova, Oradea, Constanța
- **Bulgaria** — Sofia, Varna, Burgas, Plovdiv
- **Moldova** — Chișinău
- **Croatia** — Zagreb, Split, Dubrovnik, Zadar, Pula, Rijeka, Osijek
- **Balkans** — Belgrade, Niš, Sarajevo, Tuzla, Banja Luka, Podgorica, Tivat, Tirana, Skopje, Ohrid, Pristina
- **Greece** — Athens, Thessaloniki, Kavala, Crete (Heraklion + Chania), Rhodes, Santorini, Mykonos, Corfu, Kos, Zakynthos, Kalamata, Kefalonia, Samos, Skiathos, Mytilene (Lesvos)
- **Cyprus & Malta** — Larnaca, Paphos, Malta
- **Nordics** — Copenhagen, Aarhus, Aalborg, Billund, Stockholm (Arlanda/Skavsta), Gothenburg, Malmö, Oslo, Bergen, Stavanger, Trondheim, Ålesund, Tromsø (Arctic), Helsinki, Turku, **Rovaniemi (Lapland)**, Reykjavik
- **Baltics** — Tallinn, Riga, Vilnius, Kaunas

### Long-haul (Worldwide)

246 cities across:

- **Middle East / Levant** — Dubai, Abu Dhabi, Sharjah, Doha, Bahrain, Kuwait, Muscat, Salalah, Riyadh, Jeddah, Dammam, Medina, Tel Aviv, Eilat, Beirut, Amman, Baghdad, Erbil, Tehran, Mashhad
- **North Africa** — Cairo, Hurghada, Sharm el-Sheikh, Luxor, Marrakech, Casablanca, Agadir, Fez, Tangier, Oujda, Nador, Tunis, Djerba, Algiers, Oran
- **Caucasus / Central Asia / Mongolia** — Tbilisi, Kutaisi (Wizz hub), Batumi, Yerevan, Baku, Tashkent, Samarkand, Almaty, Astana, Bishkek, Dushanbe, Ulaanbaatar
- **East Asia** — Tokyo (NRT/HND), Osaka, Nagoya, Fukuoka, Sapporo, Okinawa, Seoul, Busan, Taipei, Kaohsiung, Hong Kong, Macau, Shanghai, Beijing, Guangzhou, Chengdu, Xi'an, Kunming, Nanjing, Sanya (Hainan)
- **Southeast Asia** — Bangkok (BKK/DMK), Phuket, Koh Samui, Chiang Mai, Krabi, Kuala Lumpur, Penang, Langkawi, Kota Kinabalu, Kuching, Singapore, Bali, Jakarta, Surabaya, Yogyakarta, Lombok, Manila, Cebu, Clark, Saigon, Hanoi, Da Nang, Nha Trang, Siem Reap, Phnom Penh, Yangon, Vientiane, Bandar Seri Begawan
- **South Asia** — Delhi, Mumbai, Bangalore, Chennai, Hyderabad, Kolkata, Goa, Jaipur, Cochin, Kathmandu, Colombo, Maldives, Dhaka, Islamabad, Lahore, Karachi
- **North America** — full US and Canadian map: NYC (JFK/EWR), Boston, Washington, Philly, Miami, Fort Lauderdale, Orlando, Tampa, Atlanta, Chicago, Minneapolis, Dallas, Houston, Austin, Denver, Phoenix, Vegas, LA, SF, Seattle, Portland, Hawaii (Honolulu/Maui), Anchorage, Toronto, Montreal, Ottawa, Quebec City, Halifax, Calgary, Edmonton, Vancouver
- **Mexico / Caribbean / Central America** — Mexico City, Guadalajara, Monterrey, Cancun, Cozumel, Puerto Vallarta, Los Cabos, Merida, Havana, Varadero, Punta Cana, Santo Domingo, Nassau, Montego Bay, Kingston, Grand Cayman, Aruba, Curaçao, Sint Maarten, Antigua, Barbados, Trinidad, Martinique, Guadeloupe, Panama City, Costa Rica (San José + Liberia), Guatemala City, San Salvador, Belize, Roatán
- **South America** — São Paulo, Rio, Brasília, Fortaleza, Recife, Salvador, Buenos Aires, Córdoba, Mendoza, Iguazu, Ushuaia, Santiago, Lima, Cusco, Arequipa, Bogotá, Cartagena, Medellín, Cali, Quito, Guayaquil, Montevideo, Asunción, La Paz, Santa Cruz
- **Africa** — Nairobi, Mombasa, Kilimanjaro, Dar es Salaam, Zanzibar, Entebbe, Kigali, Addis Ababa, Johannesburg, Cape Town, Durban, Windhoek, Maputo, Harare, Victoria Falls, Lusaka, Gaborone, Madagascar, Mauritius, Seychelles, Réunion, Lagos, Abuja, Accra, Dakar, Abidjan, Luanda, Douala, Cape Verde (Sal + Praia)
- **Oceania** — Sydney, Melbourne, Brisbane, Perth, Adelaide, Gold Coast, Cairns, Auckland, Christchurch, Queenstown, Wellington, Fiji, Tahiti, New Caledonia

### Visa labels (Turkish passport + Slovak EU residence)

City names include `(visa)` or `(eVisa)` where extra paperwork is needed, so every alert tells you up front whether you can act fast or need lead time. Quick reference:

- **Visa needed (plan ahead)** — UK (Standard Visitor Visa), USA, Canada, Australia, New Zealand, China (rules eased in 2024–25, verify before booking), Iraq, Nigeria, Angola
- **eVisa / VoA** — India, Sri Lanka, Nepal, Kenya, Egypt, Indonesia, Cambodia, Iran
- **Visa-free for you** — everything else (most of Latin America, Japan, South Korea, Thailand, Malaysia, Singapore, Hong Kong, Macau, Sanya/Hainan, UAE, Qatar, Bahrain, Kuwait, Oman, Saudi Arabia (e-visa via tourist scheme), Georgia, Armenia, Azerbaijan, Morocco, Tunisia, Algeria, South Africa, Mauritius, Seychelles, Mexico, Cuba, Dominican Republic, etc.)

**Not included:** Turkey (Istanbul bot covers it), Russia / Belarus / Ukraine (no operating flights from VIE/BTS), Syria / Yemen / Libya / Afghanistan / Somalia / North Korea (sanctions, conflict, or no air access from Europe). Otherwise everything from London City to Longyearbyen (Svalbard) and Lampedusa to Tahiti is in.

## Setup — about 15 minutes

1. **Travelpayouts token** → travelpayouts.com → Developers → API. (Reuse the Istanbul bot's token.)
2. **Telegram** → reuse the Istanbul bot's bot/chat, or make a new @BotFather bot for a "Flight deals" stream.
3. **New GitHub repo** → upload all files, keep `.github/workflows/` path intact.
4. **Settings → Secrets and variables → Actions:**
   - **Secrets:** `TRAVELPAYOUTS_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
   - **Variables:** `ROLLING_WINDOW_DAYS=14`, `ROLLING_DROP_PCT=0.20`, `SHORT_HAUL_MONTHS=4`, `LONG_HAUL_MONTHS=6`, `ORIGINS=VIE,BTS`
5. **Actions tab → Daily Worldwide Flight Scan → Run workflow** to test. Log prints every route's price, target, average.

## Cost

Zero. ~20-minute daily run × 30 = ~600 min/month against the 2,000-min GitHub Actions free tier (unlimited on public repos). Travelpayouts cached endpoint is free; Telegram bots are free.

## Editing the city list

Two dictionaries at the top of `check_flights.py`:

- `SHORT_HAUL_DESTINATIONS` — `IATA: (City, round-trip target €)` — direct European routes
- `LONG_HAUL_DESTINATIONS` — `IATA: (City, round-trip target €)` — worldwide with stops

One line per city. Add, delete, change a number. That's all the configuration.

## Common adjustments

| Want | Change |
|------|--------|
| Only scan from Vienna | Variable `ORIGINS=VIE` |
| Short-haul: allow stops too | In `SHORT_HAUL`, set `"direct": "false"` |
| Tighter drop trigger | Variable `ROLLING_DROP_PCT=0.30` |
| Look further out | Variables `SHORT_HAUL_MONTHS=6`, `LONG_HAUL_MONTHS=9` |
| Slower cadence | Edit `cron` in `.github/workflows/check.yml` (e.g. `"0 7 * * 1"` for Mondays only) |
| Drop a whole tier | Comment out one `scan_tier(...)` call in `main()` |
| Two separate bots/chats per tier | Add `TELEGRAM_BOT_TOKEN_SHORT/LONG` + `TELEGRAM_CHAT_ID_SHORT/LONG`; small change in `send_telegram()` |

## Notes

- **First ~3 days are calibration.** Big-drop trigger needs 3 days of history per route. Two weeks until the rolling average is fully meaningful.
- **Round-trip prices are noisier than one-way.** Expect more "big drop" alerts on long-haul in week one as the rolling average settles. If too chatty, bump `ROLLING_DROP_PCT` to `0.30`.
- **API load** — ~5,916 calls per daily run (paced at 0.2s, ~20-minute runtime). Well inside Travelpayouts' cached endpoint limits.
- The `book` links go to Aviasales for a quick sanity-check; cross-check on Google Flights / the airline / Skyscanner before paying.
- Runs at 07:00 UTC daily and survives your June move — nothing tied to your laptop or a VPS.

## Files

- `check_flights.py` — the scanner (all config and city lists at the top)
- `.github/workflows/check.yml` — the daily schedule
- `price_history.json` — rolling price log, committed back each run
- `requirements.txt`, `.env.example`, `.gitignore`
