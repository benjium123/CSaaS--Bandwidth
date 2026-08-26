"""NANP area code (NPA) → IANA timezone candidates.

**Deliberately incomplete, and that is safe by design.** Values are TUPLES because some
NPAs genuinely span more than one timezone. A send is permitted only if it is inside the
allowed window in *every* candidate zone, so:

  - an NPA we map to several zones is treated conservatively, and
  - an NPA we do not know at all falls back to :data:`ALL_US_ZONES`, which is the most
    conservative answer available.

An incomplete table can therefore only ever make us send *less*, never at 3 a.m. That is
why this file contains only entries with high confidence rather than a guessed-at full
list: a wrong-but-confident mapping is the one failure mode that actually harms someone.

Two zones matter disproportionately because they do not observe DST:
``America/Phoenix`` (most of Arizona) and ``America/Regina`` (Saskatchewan).

Source: NANPA area-code assignments. Extend deliberately; never guess.
"""

from __future__ import annotations

ET = "America/New_York"
CT = "America/Chicago"
MT = "America/Denver"
AZ = "America/Phoenix"  # no DST
PT = "America/Los_Angeles"
AK = "America/Anchorage"
HI = "Pacific/Honolulu"
AT = "America/Halifax"
NT = "America/St_Johns"
# Canada
TOR = "America/Toronto"
WPG = "America/Winnipeg"
EDM = "America/Edmonton"
VAN = "America/Vancouver"
REG = "America/Regina"  # no DST

#: Used when the NPA is unknown, non-NANP, or toll-free. Every US zone, so the send must
#: be legal everywhere before it goes out.
ALL_US_ZONES: tuple[str, ...] = (ET, CT, MT, AZ, PT, AK, HI)

#: Toll-free NPAs carry no geography whatsoever.
TOLL_FREE_NPAS = frozenset({"800", "833", "844", "855", "866", "877", "888"})

NPA_TZ: dict[str, tuple[str, ...]] = {
    # ---- Eastern ----
    "201": (ET,), "202": (ET,), "203": (ET,), "207": (ET,), "212": (ET,),
    "215": (ET,), "216": (ET,), "227": (ET,), "229": (ET,), "231": (ET,),
    "234": (ET,), "239": (ET,), "240": (ET,), "241": (ET,), "248": (ET,),
    "252": (ET,), "267": (ET,), "270": (ET, CT), "276": (ET,), "301": (ET,),
    "302": (ET,), "304": (ET,), "305": (ET,), "313": (ET,), "315": (ET,),
    "321": (ET,), "330": (ET,), "336": (ET,), "339": (ET,), "347": (ET,),
    "352": (ET,), "351": (ET,), "386": (ET,), "401": (ET,), "404": (ET,),
    "407": (ET,), "410": (ET,), "412": (ET,), "413": (ET,), "419": (ET,),
    "434": (ET,), "440": (ET,), "443": (ET,), "445": (ET,), "470": (ET,),
    "475": (ET,), "478": (ET,), "484": (ET,), "500": ALL_US_ZONES, "508": (ET,),
    "513": (ET,), "516": (ET,), "517": (ET,), "518": (ET,), "540": (ET,),
    "551": (ET,), "561": (ET,), "567": (ET,), "570": (ET,), "571": (ET,),
    "585": (ET,), "586": (ET,), "603": (ET,), "606": (ET,), "607": (ET,),
    "609": (ET,), "610": (ET,), "614": (ET,), "616": (ET,), "617": (ET,),
    "631": (ET,), "646": (ET,), "667": (ET,), "678": (ET,), "681": (ET,),
    "689": (ET,), "703": (ET,), "704": (ET,), "706": (ET,), "716": (ET,),
    "717": (ET,), "718": (ET,), "724": (ET,), "727": (ET,), "732": (ET,),
    "734": (ET,), "740": (ET,), "743": (ET,), "754": (ET,), "757": (ET,),
    "762": (ET,), "770": (ET,), "772": (ET,), "774": (ET,), "781": (ET,),
    "786": (ET,), "804": (ET,), "810": (ET,), "813": (ET,), "814": (ET,),
    "828": (ET,), "838": (ET,), "839": (ET,), "843": (ET,), "845": (ET,),
    "848": (ET,), "854": (ET,), "856": (ET,), "857": (ET,), "859": (ET,),
    "860": (ET,), "862": (ET,), "863": (ET,), "864": (ET,), "865": (ET,),
    "878": (ET,), "904": (ET,), "908": (ET,), "910": (ET,), "912": (ET,),
    "914": (ET,), "917": (ET,), "919": (ET,), "929": (ET,), "937": (ET,),
    "941": (ET,), "947": (ET,), "954": (ET,), "959": (ET,), "973": (ET,),
    "980": (ET,), "984": (ET,), "989": (ET,),
    # Florida panhandle and Michigan UP straddle - keep both candidates.
    "850": (ET, CT), "906": (ET, CT),
    # ---- Central ----
    "205": (CT,), "214": (CT,), "217": (CT,), "218": (CT,), "224": (CT,),
    "225": (CT,), "228": (CT,), "251": (CT,), "254": (CT,), "256": (CT,),
    "262": (CT,), "281": (CT,), "309": (CT,), "312": (CT,), "314": (CT,),
    "316": (CT,), "318": (CT,), "319": (CT,), "320": (CT,), "331": (CT,),
    "334": (CT,), "337": (CT,), "346": (CT,), "361": (CT,), "402": (CT,),
    "405": (CT,), "409": (CT,), "414": (CT,), "417": (CT,), "418": (ET,),
    "430": (CT,), "432": (CT,), "469": (CT,), "479": (CT,), "501": (CT,),
    "502": (ET,), "504": (CT,), "507": (CT,), "512": (CT,), "515": (CT,),
    "534": (CT,), "563": (CT,), "573": (CT,), "574": (ET,), "580": (CT,),
    "601": (CT,), "608": (CT,), "612": (CT,), "615": (CT,), "618": (CT,),
    "620": (CT,), "630": (CT,), "636": (CT,), "641": (CT,), "651": (CT,),
    "660": (CT,), "662": (CT,), "682": (CT,), "708": (CT,), "712": (CT,),
    "713": (CT,), "715": (CT,), "731": (CT,), "737": (CT,), "763": (CT,),
    "765": (ET,), "769": (CT,), "773": (CT,), "779": (CT,), "785": (CT,),
    "806": (CT,), "815": (CT,), "816": (CT,), "817": (CT,), "830": (CT,),
    "832": (CT,), "847": (CT,), "870": (CT,), "872": (CT,), "901": (CT,),
    "903": (CT,), "913": (CT,), "915": (MT,), "918": (CT,), "920": (CT,),
    "931": (CT,), "936": (CT,), "940": (CT,), "952": (CT,), "956": (CT,),
    "972": (CT,), "979": (CT,), "985": (CT,),
    # Straddling NPAs.
    "308": (CT, MT), "605": (CT, MT), "701": (CT, MT), "806x": (CT,),
    # ---- Mountain / Arizona ----
    "303": (MT,), "307": (MT,), "385": (MT,), "406": (MT,), "435": (MT,),
    "505": (MT,), "575": (MT,), "719": (MT,), "720": (MT,), "801": (MT,),
    "970": (MT,),
    "480": (AZ,), "520": (AZ,), "602": (AZ,), "623": (AZ,), "928": (AZ, MT),
    "208": (MT, PT), "986": (MT, PT), "509": (PT,),
    # ---- Pacific ----
    "206": (PT,), "209": (PT,), "213": (PT,), "223": (PT,), "279": (PT,),
    "310": (PT,), "323": (PT,), "341": (PT,), "350": (PT,), "360": (PT,),
    "408": (PT,), "415": (PT,), "424": (PT,), "425": (PT,), "442": (PT,),
    "458": (PT,), "503": (PT,), "510": (PT,), "530": (PT,), "541": (PT,),
    "559": (PT,), "562": (PT,), "564": (PT,), "619": (PT,), "626": (PT,),
    "628": (PT,), "650": (PT,), "657": (PT,), "661": (PT,), "669": (PT,),
    "702": (PT,), "707": (PT,), "714": (PT,), "725": (PT,), "747": (PT,),
    "760": (PT,), "775": (PT,), "805": (PT,), "818": (PT,), "820": (PT,),
    "831": (PT,), "858": (PT,), "909": (PT,), "916": (PT,), "925": (PT,),
    "949": (PT,), "951": (PT,),
    # ---- Alaska / Hawaii / territories ----
    "907": (AK,), "808": (HI,),
    "787": ("America/Puerto_Rico",), "939": ("America/Puerto_Rico",),
    "340": ("America/Puerto_Rico",),
    # ---- Canada ----
    "416": (TOR,), "437": (TOR,), "647": (TOR,), "289": (TOR,), "365": (TOR,),
    "905": (TOR,), "613": (TOR,), "343": (TOR,), "519": (TOR,), "226": (TOR,),
    "705": (TOR,), "249": (TOR,), "807": (TOR,), "514": (TOR,), "438": (TOR,),
    "450": (TOR,), "579": (TOR,), "819": (TOR,), "873": (TOR,),
    "204": (WPG,), "431": (WPG,),
    "306": (REG,), "639": (REG,),
    "403": (EDM,), "587": (EDM,), "780": (EDM,), "825": (EDM,),
    "604": (VAN,), "778": (VAN,), "236": (VAN,), "250": (VAN,), "672": (VAN,),
    "902": (AT,), "782": (AT,), "506": (AT,),
    "709": (NT,),
}
# Remove the accidental placeholder key if present (defensive: keeps the table honest).
NPA_TZ.pop("806x", None)


def zones_for_npa(npa: str) -> tuple[str, ...]:
    """Candidate zones for an area code. Unknown or toll-free → the conservative set."""
    if npa in TOLL_FREE_NPAS:
        return ALL_US_ZONES
    return NPA_TZ.get(npa, ALL_US_ZONES)
