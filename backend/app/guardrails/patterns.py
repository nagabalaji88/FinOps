"""Deterministic detection patterns shared by the engine's guardrail node and the
NeMo rails.

Kept in one place so a pattern can never be tightened in one guardrail and left loose in
the other. Nothing here needs a model — these run with zero credentials.
"""

from __future__ import annotations

import re

# Order matters: the longest identifier is matched first. A 16-digit card number also
# satisfies the 12-digit Aadhaar pattern, so checking Aadhaar first would mask part of a
# card and label it as an Aadhaar. The reverse never happens — a 12-digit Aadhaar is too
# short for the card pattern.
PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("pan", re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")),
    ("card_number", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("aadhaar", re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
]

PROMPT_INJECTION_PATTERNS = [
    re.compile(r"ignore (all )?(previous|prior|above) instructions", re.I),
    re.compile(r"disregard (your|the) (system|safety) (prompt|rules)", re.I),
    re.compile(r"reveal (your|the) (system prompt|instructions)", re.I),
    re.compile(r"\bexfiltrate\b|\bdump (the )?(database|secrets)\b", re.I),
]

# Attempts to talk the agent past a control rather than past its instructions.
CONTROL_BYPASS_PATTERNS = [
    re.compile(r"\b(skip|bypass|without|no need for) (the )?(kyc|authentication|auth|"
               r"verification|approval|pin|otp|2fa|mfa)\b", re.I),
    re.compile(r"\b(don'?t|do not) (log|record|audit|report) (this|it|the)\b", re.I),
    re.compile(r"\bpretend (i am|to be|you are) (an? )?(admin|approver|compliance|auditor)\b", re.I),
    re.compile(r"\b(act|pretend) as if (the )?(approval|authentication) (was|is) (already )?"
               r"(granted|done|complete)\b", re.I),
]

# Requests for regulated advice a servicing agent is not licensed to give.
UNLICENSED_ADVICE_PATTERNS = [
    re.compile(r"\b(should|shall) i (buy|sell|invest in|put my money)\b", re.I),
    re.compile(r"\b(recommend|suggest|advise|pick) (me )?(a |an |the |some )?(?:\w+ ){0,3}"
               r"(stocks?|shares?|funds?|investments?|portfolios?|bonds?|etfs?)\b", re.I),
    re.compile(r"\bwhat (stock|share|fund|crypto) should i\b", re.I),
    re.compile(r"\b(guarantee|guaranteed) (returns?|profit)\b", re.I),
]

# Financial-crime facilitation. Asking an AML agent how to avoid its own detection is a
# rail breach regardless of who is asking.
EVASION_PATTERNS = [
    re.compile(r"\bhow (do|can|would) i (avoid|evade|get around|stay under)\b.{0,40}"
               r"\b(detection|reporting|threshold|monitoring|sar|str|ctr)\b", re.I),
    re.compile(r"\b(structure|smurf|split) (the )?(transactions?|deposits?|payments?)\b.{0,40}"
               r"\b(avoid|under|below|escape)\b", re.I),
    re.compile(r"\b(launder|hide|conceal|disguise) (the )?(money|funds|proceeds|source)\b", re.I),
    re.compile(r"\bsanctions?\b.{0,30}\b(evade|evasion|circumvent|work around|bypass)\b", re.I),
    re.compile(r"\b(evade|evading|circumvent|circumventing|bypass|bypassing|get around)\b"
               r".{0,30}\bsanctions?\b", re.I),
    re.compile(r"\b(circumvent|bypass|defeat|get around)\b.{0,30}"
               r"\b(screening|monitoring|detection|controls?)\b", re.I),
]

# "Tipping off" — telling a subject they are under investigation or that a suspicious
# activity report exists. A criminal offence in most jurisdictions (for example
# PMLA s.63 in India, POCA s.333A in the UK), so it must never leave an AML agent.
TIPPING_OFF_PATTERNS = [
    re.compile(r"\b(tell|inform|notify|warn|let) (the )?(customer|client|subject|account holder)\b"
               r".{0,60}\b(investigat|suspicious|sar\b|str\b|report(ed)?|flagged|monitor)", re.I),
    re.compile(r"\b(draft|write|compose) (an? )?(email|letter|message|sms)\b.{0,60}"
               r"\b(customer|client)\b.{0,60}\b(investigation|suspicious activity|sar\b)", re.I),
]

TIPPING_OFF_OUTPUT_PATTERNS = [
    re.compile(r"\byou are (currently )?(under|being) (investigat|monitor)", re.I),
    re.compile(r"\b(we|the bank) (have|has) (filed|submitted|raised) a (sar|str|suspicious)", re.I),
    re.compile(r"\byour account (is|has been) flagged (for|as) (suspicious|money laundering)", re.I),
]


# Characteristics that must never enter a credit decision. Equal Credit Opportunity Act
# s.701(a) in the US, the RBI Fair Practices Code and Article 15 of the Indian Constitution
# all prohibit them; a lender that reasons from any of these is discriminating.
PROHIBITED_CREDIT_FACTORS = [
    re.compile(r"\b(because|since|as|due to|given)\b.{0,40}\b(she|he) is\b.{0,20}"
               r"\b(married|single|divorced|widow(ed)?|pregnant|old|young|female|male)\b", re.I),
    re.compile(r"\b(decline|reject|refuse|approve|lower the limit|higher rate)\b.{0,60}"
               r"\b(because|due to|on account of)\b.{0,40}"
               r"\b(caste|religion|race|ethnic|gender|sex|marital status|pregnan|disab|"
               r"nationality|region|postcode|pin ?code|neighbourhood)\b", re.I),
    re.compile(r"\b(caste|religion|race|ethnicity|gender|sex|marital status|"
               r"sexual orientation|disability)\b.{0,30}\b(risk|score|factor|weight|"
               r"consideration|criteri)", re.I),
    re.compile(r"\bredlin(e|ing)\b", re.I),
]

# Threats and pressure tactics a collector may never use. RBI Fair Practices Code for
# recovery agents, and the FDCPA s.806-807 equivalents.
COLLECTIONS_THREAT_PATTERNS = [
    # "arrested", "jailed", "prosecuted" — the threat is the same whatever the inflection,
    # and it can be phrased about the customer in the third person.
    re.compile(r"\b(arrest|jail|imprison|prosecut|incarcerat)\w*\b", re.I),
    re.compile(r"\b(criminal (case|charge|charges|proceedings|action)|non-?bailable|"
               r"warrant)\b", re.I),
    re.compile(r"\b(tell|inform|contact|call|visit|speak to|notify|reach out to)\b.{0,40}"
               r"\b(your|their|his|her|the)\s+"
               r"(employer|boss|manager|family|relatives?|neighbours?|neighbors?|friends?|"
               r"colleagues?|hr\b)", re.I),
    re.compile(r"\b(seize|confiscate|repossess)\b.{0,30}\b(today|immediately|now|"
               r"within \d+ hours?)\b", re.I),
    re.compile(r"\b(publish|post|share)\b.{0,30}\b(your name|your photo|defaulter list|"
               r"social media)\b", re.I),
    re.compile(r"\byou (have no|don'?t have a) (choice|option)\b", re.I),
]

# Attempts to get the agent to work around a control rather than respect it.
COLLECTIONS_BYPASS_PATTERNS = [
    re.compile(r"\b(ignore|override|bypass|work around|get around)\b.{0,40}"
               r"\b(cease|consent|dispute|hardship|do.?not.?call|dnc)\b", re.I),
    re.compile(r"\b(call|contact|visit)\b.{0,40}\b(anyway|regardless|even though)\b"
               r".{0,40}\b(cease|consent|dispute|hardship|asked us to stop)\b", re.I),
    re.compile(r"\b(outside|after|before)\b.{0,20}\b(permitted|allowed|legal)\b.{0,20}"
               r"\bhours?\b", re.I),
]


def mask(value: str) -> str:
    """Leave the last four characters legible; a servicing agent needs that much."""
    digits = re.sub(r"\D", "", value)
    if len(digits) >= 4:
        return f"{'*' * max(len(value) - 4, 0)}{value[-4:]}"
    return "*" * len(value)


def first_match(patterns: list[re.Pattern[str]], text: str) -> str | None:
    """The pattern that fired, so a rail can name what it caught."""
    for pattern in patterns:
        if pattern.search(text):
            return pattern.pattern
    return None
