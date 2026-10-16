"""The claim fields DBrief checks: which dataset columns a claimant could state, and how each is compared.

Kinds (the rules themselves live in ledger.surql, fn::verdict and fn::near):
  enum    exact match after normalisation                        else conflict
  yesno   YES/NO after aliases                                   else conflict
  hour    |a - b| <= tol on a 24h clock                          near-miss
  count   |a - b| <= tol                                         near-miss
  year    |a - b| <= tol                                         near-miss
  amount  |a - b| <= tol * record                                near-miss
  date    |a - b| <= tol days                                    near-miss
  street  same street name, different house number               near-miss
A record value of '?' or no value at all makes the claim unverified.
"""
from __future__ import annotations

FIELDS: dict[str, dict] = {
    "incident_date":               {"kind": "date",   "tol": 2,   "label": "incident date"},
    "incident_hour_of_the_day":    {"kind": "hour",   "tol": 1,   "label": "hour of the incident"},
    "incident_city":               {"kind": "enum",   "tol": 0,   "label": "city"},
    "incident_state":              {"kind": "enum",   "tol": 0,   "label": "state"},
    "incident_location":           {"kind": "street", "tol": 0,   "label": "street address"},
    "incident_type":               {"kind": "enum",   "tol": 0,   "label": "incident type"},
    "collision_type":              {"kind": "enum",   "tol": 0,   "label": "collision type"},
    "incident_severity":           {"kind": "enum",   "tol": 0,   "label": "severity"},
    "authorities_contacted":       {"kind": "enum",   "tol": 0,   "label": "authorities contacted"},
    "number_of_vehicles_involved": {"kind": "count",  "tol": 1,   "label": "vehicles involved"},
    "bodily_injuries":             {"kind": "count",  "tol": 1,   "label": "bodily injuries"},
    "witnesses":                   {"kind": "count",  "tol": 1,   "label": "witnesses"},
    "property_damage":             {"kind": "yesno",  "tol": 0,   "label": "property damage"},
    "police_report_available":     {"kind": "yesno",  "tol": 0,   "label": "police report"},
    "total_claim_amount":          {"kind": "amount", "tol": 0.1, "label": "total claim amount"},
    "injury_claim":                {"kind": "amount", "tol": 0.1, "label": "injury claim"},
    "property_claim":              {"kind": "amount", "tol": 0.1, "label": "property claim"},
    "vehicle_claim":               {"kind": "amount", "tol": 0.1, "label": "vehicle claim"},
    "auto_make":                   {"kind": "enum",   "tol": 0,   "label": "car make"},
    "auto_model":                  {"kind": "enum",   "tol": 0,   "label": "car model"},
    "auto_year":                   {"kind": "year",   "tol": 1,   "label": "car year"},
    "policy_deductable":           {"kind": "amount", "tol": 0,   "label": "deductible"},
    "policy_state":                {"kind": "enum",   "tol": 0,   "label": "policy state"},
}

# Vocabularies the extractor may map spoken words onto. These are the dataset's category
# names (schema), not any policyholder's values.
VOCAB: dict[str, list[str]] = {
    "incident_type": ["Single Vehicle Collision", "Multi-vehicle Collision", "Parked Car", "Vehicle Theft"],
    "collision_type": ["Front Collision", "Rear Collision", "Side Collision"],
    "incident_severity": ["Trivial Damage", "Minor Damage", "Major Damage", "Total Loss"],
    "authorities_contacted": ["Police", "Fire", "Ambulance", "Other", "None"],
    "property_damage": ["YES", "NO"],
    "police_report_available": ["YES", "NO"],
}

# Spoken synonym -> canonical value. Loaded into the `alias` table; applied inside SurrealDB.
ALIASES: dict[str, str] = {
    "yes": "YES", "y": "YES", "true": "YES", "yep": "YES",
    "no": "NO", "n": "NO", "false": "NO", "none filed": "NO",
}

ENTITY_TYPE = "policy"
KEY_FIELD = "policy_number"
