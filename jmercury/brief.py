"""Business brief: the pydantic model (DESIGN.md section 2.1) and a deterministic generator.

Every brief is synthetic. ``generate(seed)`` uses only ``random.Random(seed)`` and the
embedded tables below, so the same seed always gives byte-identical JSON.

    python -m jmercury.brief --seed 42
"""

from __future__ import annotations

import argparse
import json
import random
import re
import unicodedata
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator

TRADES = ("lawn", "roofing", "hvac", "plumbing", "dental", "remodeling", "aesthetics", "commercial")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SECTIONS = ("call", "services", "area", "hours", "about")

Trade = Literal["lawn", "roofing", "hvac", "plumbing", "dental", "remodeling", "aesthetics", "commercial"]
Section = Literal["call", "services", "area", "hours", "about"]

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
PHONE_RE = re.compile(r"^\(\d{3}\) 555-01\d{2}$")


class Service(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    detail: str


class Facts(BaseModel):
    """The only facts a site may claim. A field that is absent was not given by the owner."""

    model_config = ConfigDict(extra="forbid")
    founded: Optional[int] = None
    license: Optional[str] = None
    emergency: Optional[bool] = None
    insured: Optional[bool] = None
    languages: Optional[list[str]] = None


class Brief(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(pattern=r"^[0-9a-f]{16,64}$")
    trade: Trade
    name: str = Field(min_length=1)
    city: str
    state: str = Field(pattern=r"^[A-Z]{2}$")
    service_area: list[str] = Field(min_length=1)
    phone: str = Field(pattern=PHONE_RE.pattern)
    email: str = Field(pattern=r"^[a-z0-9.-]+@[a-z0-9-]+\.example$")
    address: Optional[str]
    hours: dict[str, Optional[tuple[str, str]]]
    services: list[Service] = Field(min_length=3, max_length=8)
    facts: Facts
    owner_notes: str = Field(min_length=1)
    required: list[Section] = Field(min_length=1)

    @field_validator("hours")
    @classmethod
    def _check_hours(cls, v: dict) -> dict:
        if list(v.keys()) != list(DAYS):
            raise ValueError(f"hours must have exactly the keys {list(DAYS)} in that order")
        open_days = 0
        for day, span in v.items():
            if span is None:
                continue
            opens, closes = span
            if not (_TIME_RE.match(opens) and _TIME_RE.match(closes)):
                raise ValueError(f"{day}: times must be HH:MM (24 h)")
            if opens >= closes:
                raise ValueError(f"{day}: opening time must be before closing time")
            open_days += 1
        if open_days == 0:
            raise ValueError("at least one day must be open")
        return v

    @model_validator(mode="after")
    def _check_required(self) -> "Brief":
        if len(set(self.required)) != len(self.required):
            raise ValueError("required has duplicates")
        return self

    @field_serializer("facts")
    def _ser_facts(self, facts: Facts) -> dict:
        # Absent facts are omitted, not null: "only these may be claimed".
        return facts.model_dump(exclude_none=True)

    def phone_digits(self) -> str:
        return re.sub(r"\D", "", self.phone)

    def to_json(self, indent: Optional[int] = 2) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=indent, ensure_ascii=False)


# --------------------------------------------------------------------------------------
# Fact matching helpers, shared by the validator gates and the miner's self-check
# --------------------------------------------------------------------------------------

PHONE_TOKEN = re.compile(r"(?<!\d)(?:\+?1[\s.\-]?)?(?:\(\d{3}\)|\d{3})[\s.\-]?\d{3}[\s.\-]?\d{4}(?!\d)")


def phone_digits(s: str) -> str:
    """The ten national digits of a US phone number (a leading country code 1 is dropped)."""
    d = re.sub(r"\D", "", s)
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d


def time_pattern(hhmm: str) -> re.Pattern:
    """Regex matching one brief time in any accepted format: 8:00 AM, 8am, 8 a.m., 08:00."""
    h, m = int(hhmm[:2]), int(hhmm[3:])
    h12 = h % 12 or 12
    mer = "a" if h < 12 else "p"
    mer_re = r"\s?(?:a\.?\s?m\.?)(?![a-z])" if mer == "a" else r"\s?(?:p\.?\s?m\.?)(?![a-z])"
    alts = [rf"(?<![\d:]){h12}:{m:02d}{mer_re}"]
    if m == 0:
        alts.append(rf"(?<![\d:]){h12}{mer_re}")
    # 24-hour clock, not followed by a meridiem (08:00, 8:00, 17:00)
    h24 = rf"0?{h}" if h < 10 else f"{h}"
    alts.append(rf"(?<![\d:]){h24}:{m:02d}(?!\d)(?!\s?[ap]\.?\s?m\b)")
    return re.compile("|".join(f"(?:{a})" for a in alts), re.I)


def normalise_text(s: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", s).casefold().split())


# --------------------------------------------------------------------------------------
# Embedded data
# --------------------------------------------------------------------------------------

# (city, state, area code, nearby towns)
CITIES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("Dayton", "OH", "937", ("Kettering", "Centerville", "Beavercreek", "Huber Heights", "Miamisburg", "Vandalia", "Xenia")),
    ("Columbus", "OH", "614", ("Dublin", "Westerville", "Hilliard", "Grove City", "Gahanna", "Upper Arlington", "Worthington")),
    ("Cincinnati", "OH", "513", ("Mason", "West Chester", "Blue Ash", "Norwood", "Fairfield", "Loveland")),
    ("Indianapolis", "IN", "317", ("Carmel", "Fishers", "Greenwood", "Noblesville", "Zionsville", "Plainfield")),
    ("Louisville", "KY", "502", ("Jeffersontown", "St. Matthews", "Shively", "Middletown", "Prospect")),
    ("Nashville", "TN", "615", ("Franklin", "Brentwood", "Hendersonville", "Mount Juliet", "Gallatin", "Murfreesboro")),
    ("Knoxville", "TN", "865", ("Farragut", "Maryville", "Powell", "Oak Ridge", "Alcoa", "Seymour")),
    ("Charlotte", "NC", "704", ("Matthews", "Huntersville", "Concord", "Mint Hill", "Cornelius", "Gastonia")),
    ("Raleigh", "NC", "919", ("Cary", "Apex", "Wake Forest", "Garner", "Holly Springs", "Knightdale")),
    ("Greenville", "SC", "864", ("Greer", "Mauldin", "Simpsonville", "Taylors", "Easley", "Travelers Rest")),
    ("Atlanta", "GA", "404", ("Decatur", "Sandy Springs", "East Point", "Brookhaven", "Dunwoody")),
    ("Birmingham", "AL", "205", ("Hoover", "Vestavia Hills", "Homewood", "Mountain Brook", "Trussville", "Pelham")),
    ("Tampa", "FL", "813", ("Brandon", "Riverview", "Temple Terrace", "Plant City", "Lutz", "Valrico")),
    ("Orlando", "FL", "407", ("Winter Park", "Kissimmee", "Apopka", "Oviedo", "Winter Garden", "Altamonte Springs")),
    ("Jacksonville", "FL", "904", ("Orange Park", "Jacksonville Beach", "Fernandina Beach", "Ponte Vedra Beach", "Fleming Island")),
    ("Richmond", "VA", "804", ("Glen Allen", "Midlothian", "Mechanicsville", "Short Pump", "Chester", "Ashland")),
    ("Pittsburgh", "PA", "412", ("Mount Lebanon", "Bethel Park", "Monroeville", "Upper St. Clair", "McKeesport", "Moon Township")),
    ("Austin", "TX", "512", ("Round Rock", "Pflugerville", "Cedar Park", "Georgetown", "Leander", "Kyle", "Buda")),
    ("San Antonio", "TX", "210", ("Alamo Heights", "Converse", "Leon Valley", "Universal City", "Helotes", "Live Oak")),
    ("Houston", "TX", "713", ("Bellaire", "Pasadena", "West University Place", "South Houston", "Galena Park")),
    ("Dallas", "TX", "214", ("Richardson", "Garland", "Mesquite", "Irving", "Carrollton", "University Park")),
    ("Fort Worth", "TX", "817", ("Arlington", "Keller", "Hurst", "Burleson", "Benbrook", "Haltom City")),
    ("Oklahoma City", "OK", "405", ("Edmond", "Moore", "Norman", "Yukon", "Midwest City", "Mustang")),
    ("Tulsa", "OK", "918", ("Broken Arrow", "Owasso", "Jenks", "Bixby", "Sand Springs", "Sapulpa")),
    ("Little Rock", "AR", "501", ("North Little Rock", "Sherwood", "Maumelle", "Benton", "Bryant", "Conway")),
    ("Kansas City", "MO", "816", ("Independence", "Lee's Summit", "Liberty", "Blue Springs", "Raytown", "Gladstone")),
    ("St. Louis", "MO", "314", ("Clayton", "Kirkwood", "Webster Groves", "Florissant", "Maplewood", "Creve Coeur")),
    ("Omaha", "NE", "402", ("Bellevue", "Papillion", "La Vista", "Ralston", "Elkhorn", "Council Bluffs")),
    ("Des Moines", "IA", "515", ("West Des Moines", "Ankeny", "Urbandale", "Johnston", "Clive", "Altoona")),
    ("Minneapolis", "MN", "612", ("Richfield", "St. Louis Park", "Edina", "Bloomington", "Golden Valley", "Roseville")),
    ("Milwaukee", "WI", "414", ("Wauwatosa", "West Allis", "Greenfield", "Shorewood", "Cudahy", "Oak Creek")),
    ("Madison", "WI", "608", ("Middleton", "Fitchburg", "Sun Prairie", "Verona", "Monona", "Waunakee")),
    ("Grand Rapids", "MI", "616", ("Wyoming", "Kentwood", "Grandville", "Walker", "Rockford", "Hudsonville")),
    ("Denver", "CO", "303", ("Aurora", "Lakewood", "Englewood", "Littleton", "Arvada", "Westminster")),
    ("Colorado Springs", "CO", "719", ("Manitou Springs", "Fountain", "Monument", "Security-Widefield", "Woodland Park")),
    ("Albuquerque", "NM", "505", ("Rio Rancho", "Corrales", "Los Ranchos de Albuquerque", "Bernalillo", "Los Lunas")),
    ("Phoenix", "AZ", "602", ("Scottsdale", "Tempe", "Glendale", "Peoria", "Chandler", "Mesa")),
    ("Tucson", "AZ", "520", ("Oro Valley", "Marana", "Sahuarita", "South Tucson", "Vail", "Catalina Foothills")),
    ("Salt Lake City", "UT", "801", ("Murray", "Sandy", "West Valley City", "Holladay", "South Salt Lake", "Draper")),
    ("Boise", "ID", "208", ("Meridian", "Nampa", "Eagle", "Kuna", "Garden City", "Star")),
    ("Spokane", "WA", "509", ("Spokane Valley", "Liberty Lake", "Cheney", "Airway Heights", "Mead", "Deer Park")),
    ("Portland", "OR", "503", ("Beaverton", "Gresham", "Lake Oswego", "Tigard", "Milwaukie", "Hillsboro")),
    ("Sacramento", "CA", "916", ("Elk Grove", "Roseville", "Folsom", "Citrus Heights", "Rancho Cordova", "West Sacramento")),
    ("Fresno", "CA", "559", ("Clovis", "Sanger", "Selma", "Kerman", "Madera", "Fowler")),
)

NAME_WORDS = (
    "Ridgeline", "Bluestem", "Copperleaf", "Northfield", "Stonebridge", "Cedar Point", "Harborview", "Ironwood",
    "Maple Run", "Silver Creek", "Kestrel", "Summit", "Riverbend", "Prairie Wind", "Lantern", "Juniper", "Highmark",
    "Fieldstone", "Granite Peak", "Oak Hollow", "Bright Path", "Clearwater", "Red Fox", "True North", "Willow Bend",
    "Blue Heron", "Evergreen", "Sandstone", "Heartland", "Brightwater", "Pinecrest", "Keystone", "Sweetgrass",
    "Hearthstone", "Larkspur", "Cobalt", "Trailhead", "Westwind", "Golden Hour", "Arbor Lane",
)

NAME_SUFFIXES: dict[str, tuple[str, ...]] = {
    "lawn": ("Lawn & Landscape", "Lawn Care", "Landscaping", "Grounds Co.", "Yard & Garden"),
    "roofing": ("Roofing Co.", "Roofing", "Roof & Exteriors", "Roofing & Gutters"),
    "hvac": ("Heating & Air", "Comfort Systems", "HVAC", "Heating & Cooling"),
    "plumbing": ("Plumbing", "Plumbing & Drain", "Plumbing Co.", "Plumbing Services"),
    "dental": ("Dental", "Family Dentistry", "Dental Care", "Dental Studio"),
    "remodeling": ("Remodeling", "Home Renovation", "Builders", "Kitchen & Bath"),
    "aesthetics": ("Aesthetics", "Med Spa", "Skin Studio", "Aesthetic Clinic"),
    "commercial": ("Commercial Contractors", "Construction Group", "Facility Services", "Builders Inc."),
}

SERVICES: dict[str, tuple[tuple[str, str], ...]] = {
    "lawn": (
        ("Weekly mowing", "Mow, edge and blow on a set weekly route, April through October."),
        ("Spring cleanup", "Leaf and debris removal, bed edging and a first cut."),
        ("Fall leaf removal", "Leaves cleared from lawn and beds and hauled away."),
        ("Mulch installation", "Fresh mulch spread in beds, with edges recut."),
        ("Shrub trimming", "Hedges and shrubs shaped by hand, clippings removed."),
        ("Aeration and overseeding", "Core aeration followed by seed for thin lawns."),
        ("Fertilization program", "Seasonal feeding plan for a thicker, greener lawn."),
        ("Landscape design", "Planting plans for beds, borders and front entries."),
        ("Sod installation", "New sod laid on prepared, graded soil."),
        ("Snow removal", "Driveway and walk clearing after storms for contract customers."),
        ("Patio and paver work", "Small paver patios and walkways."),
    ),
    "roofing": (
        ("Roof replacement", "Tear-off and new asphalt shingle roof, including underlayment."),
        ("Roof repair", "Leaks, missing shingles and flashing fixed."),
        ("Storm damage inspection", "Inspection after hail or wind, with photos of what we find."),
        ("Insurance claim help", "We document damage and meet the adjuster on site."),
        ("Gutter installation", "Seamless gutters and downspouts sized for the roof."),
        ("Metal roofing", "Standing-seam metal roofs for homes and outbuildings."),
        ("Flat roof repair", "Repairs and coatings for low-slope roofs."),
        ("Skylight replacement", "Old or leaking skylights replaced and reflashed."),
        ("Attic ventilation", "Ridge and soffit vents to keep the attic dry."),
        ("Chimney flashing", "Flashing and counterflashing rebuilt around chimneys."),
        ("Siding repair", "Damaged siding panels matched and replaced."),
    ),
    "hvac": (
        ("AC repair", "Diagnosis and repair of central air systems, all major brands."),
        ("Furnace repair", "Gas and electric furnace repair."),
        ("AC installation", "New central air systems sized to the home."),
        ("Furnace replacement", "High-efficiency furnace installation."),
        ("Heat pump service", "Install, repair and seasonal checks for heat pumps."),
        ("Seasonal tune-up", "Spring cooling and fall heating checkups."),
        ("Ductless mini-splits", "Ductless systems for additions, garages and sunrooms."),
        ("Thermostat installation", "Programmable and smart thermostats installed and set up."),
        ("Indoor air quality", "Air filtration and humidifier installation."),
        ("Duct cleaning", "Supply and return ducts cleaned."),
        ("Maintenance plans", "Two visits a year with priority scheduling for plan members."),
    ),
    "plumbing": (
        ("Drain cleaning", "Clogged sinks, tubs and main lines cleared."),
        ("Water heater repair", "Tank and tankless water heaters repaired."),
        ("Water heater installation", "Same-week replacement of tank and tankless units."),
        ("Leak repair", "Leaks found and fixed in supply and drain lines."),
        ("Sewer line camera inspection", "A camera inspection shows what is in the line before any digging."),
        ("Toilet repair and install", "Running, leaking and clogged toilets fixed or replaced."),
        ("Faucet and fixture install", "Kitchen and bath faucets, sinks and fixtures installed."),
        ("Sump pump service", "Sump pump installation, testing and replacement."),
        ("Gas line work", "Gas lines for ranges, dryers and grills."),
        ("Repiping", "Old galvanized or polybutylene pipe replaced."),
        ("Garbage disposal service", "Disposals repaired or replaced."),
    ),
    "dental": (
        ("Cleanings and exams", "Routine cleanings, exams and X-rays for the whole family."),
        ("Fillings", "Tooth-colored fillings."),
        ("Crowns", "Crowns to restore cracked or heavily filled teeth."),
        ("Root canal therapy", "Treatment to save an infected tooth."),
        ("Teeth whitening", "In-office and take-home whitening."),
        ("Children's dentistry", "Gentle first visits and checkups for kids."),
        ("Dental implants", "Implant restorations to replace missing teeth."),
        ("Clear aligners", "Clear aligner treatment for straightening teeth."),
        ("Emergency dental visits", "Same-day appointments for tooth pain when the schedule allows."),
        ("Night guards", "Custom guards for grinding and clenching."),
        ("Extractions", "Simple tooth extractions."),
    ),
    "remodeling": (
        ("Kitchen remodeling", "Full kitchen remodels from layout to final trim."),
        ("Bathroom remodeling", "Bathroom updates and full gut remodels."),
        ("Basement finishing", "Unfinished basements turned into living space."),
        ("Room additions", "Additions designed to match the existing house."),
        ("Custom cabinetry", "Built-ins and cabinets made to fit."),
        ("Flooring installation", "Hardwood, tile and luxury vinyl plank floors."),
        ("Accessibility updates", "Walk-in showers, grab bars and wider doorways."),
        ("Deck building", "Wood and composite decks."),
        ("Interior carpentry", "Trim, doors and stair work."),
        ("Whole-home renovation", "Larger renovations managed from design to finish."),
        ("Design consultation", "An in-home visit to plan scope and budget."),
    ),
    "aesthetics": (
        ("Botox and neuromodulators", "Wrinkle relaxers for forehead lines, frown lines and crow's feet."),
        ("Dermal fillers", "Fillers for lips, cheeks and smile lines."),
        ("Chemical peels", "Light and medium peels for tone and texture."),
        ("Microneedling", "Collagen induction for scars and fine lines."),
        ("Laser hair removal", "Laser hair removal for face and body."),
        ("HydraFacial", "Cleansing, exfoliation and hydration in one treatment."),
        ("IPL photofacial", "Light treatment for sun spots and redness."),
        ("Skin consultations", "A one-on-one skin assessment and treatment plan."),
        ("Medical-grade skincare", "Skincare products chosen for your skin type."),
        ("Body contouring", "Non-surgical body contouring treatments."),
        ("Lash and brow services", "Lash lifts and brow shaping."),
    ),
    "commercial": (
        ("Tenant improvements", "Build-outs for office, retail and medical tenants."),
        ("Office renovations", "Office refreshes completed around business hours."),
        ("Retail build-outs", "Storefront and retail interiors built to brand standards."),
        ("Restaurant construction", "Kitchen and dining room construction for restaurants."),
        ("Warehouse improvements", "Racking, lighting and office space inside warehouses."),
        ("ADA compliance upgrades", "Ramps, restrooms and entrances updated for accessibility."),
        ("Facility maintenance", "Scheduled repairs and upkeep for commercial buildings."),
        ("Pre-construction planning", "Budgeting, scheduling and scope before the job starts."),
        ("Design-build projects", "One contract covering design and construction."),
        ("Medical office build-outs", "Exam rooms and clinic spaces built to code."),
        ("Steel building erection", "Pre-engineered steel buildings erected on prepared sites."),
    ),
}

OWNER_NOTES: dict[str, tuple[str, ...]] = {
    "lawn": (
        "Most of our customers are busy families in {town1} and {town2}. We show up on the same day every week and we answer the phone.",
        "We are a small crew and we keep it that way on purpose. I want people to know they will get the same faces every visit.",
        "Spring fills up fast, so I want the phone number big and easy to tap. We care more about doing {service} right than about being the cheapest.",
    ),
    "roofing": (
        "After a storm people are stressed and want someone who calls back. Please make the phone number the first thing they see.",
        "We do honest inspections. If a roof only needs a repair, we say so. I want the site to feel straightforward, not salesy.",
        "Most of our work is in {town1} and {town2}. {service} is the job we are best known for.",
    ),
    "hvac": (
        "When the AC quits in July, people just want to call someone fast. Put the phone number everywhere it makes sense.",
        "We are a family business and we explain the repair before we start. No pressure to replace a system that can be fixed.",
        "A lot of our calls come from {town1} and {town2}. I want people to see right away that we do {service}.",
    ),
    "plumbing": (
        "Nobody plans for a burst pipe. The site should make it obvious how to reach us right now.",
        "We give the price before we start the work. I want the site to feel calm and clear, not like an ad.",
        "Most of our customers are homeowners in {town1}, {town2} and around {city}. {service} is a big part of what we do.",
    ),
    "dental": (
        "Many of our patients are nervous about the dentist. I want the site to feel calm and welcoming.",
        "We see whole families, from kids to grandparents. It should be easy to call and book a cleaning.",
        "We are a small practice and we like it that way. Patients from {town1} and {town2} tell us they like that we run on time.",
    ),
    "remodeling": (
        "A remodel is a big decision for a family. I want the site to show we plan carefully and communicate every step.",
        "We take on a limited number of projects at a time. People should be able to call and set up a visit easily.",
        "Most of our work is {service} in {town1} and {town2}. Clean job sites matter to us.",
    ),
    "aesthetics": (
        "Our clients want natural-looking results. The site should feel calm, clean and private.",
        "Every new client starts with a consultation. I want booking by phone to be the obvious next step.",
        "Clients come to us from {town1} and {town2}. {service} is our most requested treatment.",
    ),
    "commercial": (
        "Our clients are property managers and business owners. They want to know we finish on schedule and keep them informed.",
        "We work around business hours so tenants can stay open. The site should be direct and easy to scan.",
        "Most of our projects are in {city} and {town1}. {service} is a large share of our work.",
    ),
}

STREET_NAMES = (
    "Brookfield", "Old Mill", "Lakeview", "Parkside", "Hillcrest", "Meadowbrook", "Commerce", "Industrial",
    "Willow Creek", "Station", "Orchard", "Foundry", "Quarry", "Canal", "Market", "Sycamore",
)
STREET_TYPES = ("Ave", "St", "Rd", "Blvd", "Dr", "Pkwy", "Ln", "Way")
EMAIL_WORDS = ("office", "hello", "info", "contact", "service", "frontdesk", "team")
LANG_EXTRA = ("Spanish", "Vietnamese", "Portuguese", "Polish", "Arabic", "Korean", "Tagalog")

LICENSE_CODES = {
    "lawn": "LC",
    "roofing": "RC",
    "hvac": "HV",
    "plumbing": "PL",
    "dental": "DDS",
    "remodeling": "GC",
    "aesthetics": "MS",
    "commercial": "CC",
}

# Posted-hours patterns per trade. Each pattern is a dict day -> (open, close) for open days.
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri")


def _week(days: tuple[str, ...], opens: str, closes: str) -> dict[str, tuple[str, str]]:
    return {d: (opens, closes) for d in days}


def _hours_for(trade: str, rng: random.Random) -> dict[str, Optional[tuple[str, str]]]:
    early = rng.choice(("07:00", "07:30", "08:00"))
    late = rng.choice(("16:00", "17:00", "17:30", "18:00"))
    pattern: dict[str, tuple[str, str]]
    if trade == "dental":
        choice = rng.randrange(3)
        if choice == 0:
            pattern = _week(("mon", "tue", "wed", "thu"), "08:00", "17:00")
            pattern["fri"] = ("08:00", "13:00")
        elif choice == 1:
            pattern = _week(("mon", "wed", "thu"), "08:00", "17:00")
            pattern["tue"] = ("10:00", "19:00")
            pattern["fri"] = ("08:00", "14:00")
        else:
            pattern = _week(("mon", "tue", "wed", "thu"), "07:30", "16:30")
            pattern["sat"] = ("08:00", "12:00")
    elif trade == "aesthetics":
        choice = rng.randrange(3)
        if choice == 0:
            pattern = _week(("tue", "wed", "thu", "fri"), "10:00", "18:00")
            pattern["sat"] = ("09:00", "15:00")
        elif choice == 1:
            pattern = _week(("mon", "tue", "wed"), "09:00", "17:00")
            pattern["thu"] = ("11:00", "19:00")
            pattern["fri"] = ("09:00", "17:00")
            pattern["sat"] = ("10:00", "14:00")
        else:
            pattern = _week(("tue", "thu"), "10:00", "19:00")
            pattern.update(_week(("wed", "fri"), "09:00", "17:00"))
            pattern["sat"] = ("09:00", "13:00")
    elif trade == "commercial":
        pattern = _week(_WEEKDAYS, early, late)
    else:
        pattern = _week(_WEEKDAYS, early, late)
        sat = rng.randrange(3)
        if sat == 1:
            pattern["sat"] = ("08:00", "12:00")
        elif sat == 2:
            pattern["sat"] = ("09:00", "14:00")
        if trade in ("hvac", "plumbing") and rng.random() < 0.2:
            pattern["sun"] = ("10:00", "14:00")
    return {d: pattern.get(d) for d in DAYS}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def generate(seed: int) -> Brief:
    """Deterministic brief for ``seed``: same seed, identical JSON."""
    rng = random.Random(f"jmercury-brief-v1:{seed}")
    task_id = f"{rng.getrandbits(64):016x}"
    trade = rng.choice(TRADES)
    city, state, area_code, towns = rng.choice(CITIES)

    word = rng.choice(NAME_WORDS)
    suffix = rng.choice(NAME_SUFFIXES[trade])
    name = f"{word} {suffix}"

    n_towns = rng.randint(min(4, len(towns)), len(towns))
    service_area = rng.sample(list(towns), n_towns)

    phone = f"({area_code}) 555-01{rng.randrange(100):02d}"
    email = f"{rng.choice(EMAIL_WORDS)}@{_slug(word)}{_slug(suffix.split()[0])}.example"

    if rng.random() < 0.5 or trade in ("dental", "aesthetics") and rng.random() < 0.8:
        address = f"{rng.randint(100, 9899)} {rng.choice(STREET_NAMES)} {rng.choice(STREET_TYPES)}, {city}, {state}"
    else:
        address = None

    hours = _hours_for(trade, rng)

    pool = SERVICES[trade]
    n_services = rng.randint(3, 8)
    services = [Service(name=n, detail=d) for n, d in rng.sample(list(pool), n_services)]

    facts = Facts()
    if rng.random() < 0.5:
        facts.founded = rng.randint(1978, 2021)
    if rng.random() < 0.5:
        facts.license = f"{state}-{LICENSE_CODES[trade]}-{rng.randint(10000, 99999)}"
    if trade in ("hvac", "plumbing", "roofing"):
        if rng.random() < 0.6:
            facts.emergency = True
    elif trade == "dental" and rng.random() < 0.3:
        facts.emergency = True
    if rng.random() < 0.5:
        facts.insured = True
    if rng.random() < 0.25:
        facts.languages = ["English", rng.choice(LANG_EXTRA)]

    template = rng.choice(OWNER_NOTES[trade])
    owner_notes = template.format(
        town1=service_area[0],
        town2=service_area[1] if len(service_area) > 1 else city,
        city=city,
        service="\x00",
    )
    # The service name keeps its capital letter at the start of a sentence, lower case elsewhere.
    svc = services[0].name
    owner_notes = re.sub(r"(^|\. )\x00", lambda m: m.group(1) + svc, owner_notes)
    first = svc.split()[0]
    keep_case = any(c.isupper() for c in first[1:]) or first in ("Botox",)
    owner_notes = owner_notes.replace("\x00", svc if keep_case else svc[0].lower() + svc[1:])

    required: list[str] = ["call", "services", "hours"]
    if address is None or rng.random() < 0.7:
        required.insert(2, "area")
    if rng.random() < 0.8:
        required.append("about")

    return Brief(
        task_id=task_id,
        trade=trade,
        name=name,
        city=city,
        state=state,
        service_area=service_area,
        phone=phone,
        email=email,
        address=address,
        hours=hours,
        services=services,
        facts=facts,
        owner_notes=owner_notes,
        required=required,
    )


def load(path: str) -> Brief:
    with open(path, encoding="utf-8") as fh:
        return Brief.model_validate_json(fh.read())


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m jmercury.brief", description="Print a synthetic business brief as JSON.")
    ap.add_argument("--seed", type=int, required=True, help="integer seed; the same seed gives the same brief")
    args = ap.parse_args(argv)
    print(generate(args.seed).to_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
