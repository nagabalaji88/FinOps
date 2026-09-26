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
    re.compile(
        r"\b(skip|bypass|without|no need for) (the )?(kyc|authentication|auth|"
        r"verification|approval|pin|otp|2fa|mfa)\b",
        re.I,
    ),
    re.compile(r"\b(don'?t|do not) (log|record|audit|report) (this|it|the)\b", re.I),
    re.compile(r"\bpretend (i am|to be|you are) (an? )?(admin|approver|compliance|auditor)\b", re.I),
    re.compile(
        r"\b(act|pretend) as if (the )?(approval|authentication) (was|is) (already )?"
        r"(granted|done|complete)\b",
        re.I,
    ),
]

# Requests for regulated advice a servicing agent is not licensed to give.
UNLICENSED_ADVICE_PATTERNS = [
    re.compile(r"\b(should|shall) i (buy|sell|invest in|put my money)\b", re.I),
    re.compile(
        r"\b(recommend|suggest|advise|pick) (me )?(a |an |the |some )?(?:\w+ ){0,3}"
        r"(stocks?|shares?|funds?|investments?|portfolios?|bonds?|etfs?)\b",
        re.I,
    ),
    re.compile(r"\bwhat (stock|share|fund|crypto) should i\b", re.I),
    re.compile(r"\b(guarantee|guaranteed) (returns?|profit)\b", re.I),
]

# Financial-crime facilitation. Asking an AML agent how to avoid its own detection is a
# rail breach regardless of who is asking.
EVASION_PATTERNS = [
    re.compile(
        r"\bhow (do|can|would) i (avoid|evade|get around|stay under)\b.{0,40}"
        r"\b(detection|reporting|threshold|monitoring|sar|str|ctr)\b",
        re.I,
    ),
    re.compile(
        r"\b(structure|smurf|split) (the )?(transactions?|deposits?|payments?)\b.{0,40}"
        r"\b(avoid|under|below|escape)\b",
        re.I,
    ),
    re.compile(r"\b(launder|hide|conceal|disguise) (the )?(money|funds|proceeds|source)\b", re.I),
    re.compile(r"\bsanctions?\b.{0,30}\b(evade|evasion|circumvent|work around|bypass)\b", re.I),
    re.compile(
        r"\b(evade|evading|circumvent|circumventing|bypass|bypassing|get around)\b"
        r".{0,30}\bsanctions?\b",
        re.I,
    ),
    re.compile(
        r"\b(circumvent|bypass|defeat|get around)\b.{0,30}"
        r"\b(screening|monitoring|detection|controls?)\b",
        re.I,
    ),
]

# "Tipping off" — telling a subject they are under investigation or that a suspicious
# activity report exists. A criminal offence in most jurisdictions (for example
# PMLA s.63 in India, POCA s.333A in the UK), so it must never leave an AML agent.
TIPPING_OFF_PATTERNS = [
    re.compile(
        r"\b(tell|inform|notify|warn|let) (the )?(customer|client|subject|account holder)\b"
        r".{0,60}\b(investigat|suspicious|sar\b|str\b|report(ed)?|flagged|monitor)",
        re.I,
    ),
    re.compile(
        r"\b(draft|write|compose) (an? )?(email|letter|message|sms)\b.{0,60}"
        r"\b(customer|client)\b.{0,60}\b(investigation|suspicious activity|sar\b)",
        re.I,
    ),
]

TIPPING_OFF_OUTPUT_PATTERNS = [
    re.compile(r"\byou are (currently )?(under|being) (investigat|monitor)", re.I),
    re.compile(r"\b(we|the bank) (have|has) (filed|submitted|raised) a (sar|str|suspicious)", re.I),
    re.compile(r"\byour account (is|has been) flagged (for|as) (suspicious|money laundering)", re.I),
]


# Characteristics that must never enter a credit decision. Equal Credit Opportunity Act
# s.701(a) in the US, the RBI Fair Practices Code and Article 15 of the Indian Constitution
# all prohibit them; a lender that reasons from any of these is discriminating.
#
#: The characteristics themselves, in the forms people actually write them. Kept as one
#: alternation so a characteristic added here is caught by every phrasing below rather than
#: by whichever pattern someone remembered to update.
#: `age of the applicant` is spelled out rather than a bare `age` so that the age of a
#: credit file — a legitimate factor — is not mistaken for the age of a person.
_PROTECTED = (
    r"caste|religion|religious|race|racial|ethnic\w*|gender|sex|female|male|"
    r"sexual orientation|marital status|married|unmarried|divorced|widow\w*|"
    r"pregnan\w*|maternity|paternity|childbearing|"
    r"disab\w*|handicap\w*|nationality|postcode|pin ?code|neighbourhood|"
    r"neighborhood|redlin\w*|elderly|too (old|young)|"
    r"age of the (applicant|borrower|customer)"
)

PROHIBITED_CREDIT_FACTORS = [
    # "... because she is married", "... because the applicant may take maternity leave".
    # The subject is named in the third person or as a role, and the verb may be a modal:
    # "is pregnant" and "may get pregnant" are the same prohibited reasoning.
    re.compile(
        r"\b(because|since|due to|given|owing to|on account of|on grounds of)\b.{0,50}"
        r"\b(she|he|they|the (applicant|customer|borrower))\b.{0,20}"
        r"\b(is|are|was|were|has|have|might|may|could|will|would|take|takes|taking|"
        r"get|gets|getting|go|goes|going)\b.{0,30}"
        r"\b(" + _PROTECTED + r")\b",
        re.I,
    ),
    # "decline this because of their caste", "lower the limit because they are disabled",
    # "higher rate due to her pregnancy" — a decision verb reasoning from a characteristic.
    re.compile(
        r"\b(declin\w*|reject\w*|refus\w*|deny|denied|denies|approv\w*|"
        r"(lower|reduce|cut|cap) the limit|higher (rate|pricing)|price up)\b.{0,70}"
        r"\b(because|due to|on account of|owing to|on grounds of|based on)\b.{0,50}"
        r"\b(" + _PROTECTED + r")\b",
        re.I,
    ),
    # "she is single", "the applicant is divorced". Marital status is kept out of
    # `_PROTECTED` and matched here instead: "single" is an ordinary quantifier in lending
    # prose — "a single missed instalment" — and only means marital status when it is said
    # of the person.
    re.compile(
        r"\b(she|he|they|the (applicant|customer|borrower))\s+(is|are)\s+"
        r"(a\s+)?(single|married|unmarried|divorced|widow\w*)\b",
        re.I,
    ),
    # "he is from the same community", "she belongs to that caste" — group membership used
    # as a reason without the characteristic being named outright. Anchored on the person so
    # that a community lending scheme, which is a product and not a characteristic, passes.
    re.compile(
        r"\b(she|he|they|the (applicant|customer|borrower))\s+"
        r"(is|are|comes?|belongs?|belonged)\b.{0,20}\b(from|to|of)\b.{0,20}"
        r"\b(communit\w*|caste|religion|sect|tribe|clan)\b",
        re.I,
    ),
    # The characteristic proposed as a model input at all.
    re.compile(
        r"\b(caste|religion|race|ethnicity|gender|sex|marital status|"
        r"sexual orientation|disability)\b.{0,30}\b(risk|score|factor|weight|"
        r"consideration|criteri)",
        re.I,
    ),
    re.compile(r"\bredlin(e|ing)\b", re.I),
]

# Payment operations. Two behaviours here are criminal rather than merely wrong: removing a
# party from a payment message to defeat sanctions screening ("wire stripping", prosecuted
# under IEEPA and the equivalent EU regulations), and releasing or returning funds that a
# confirmed match has frozen.
WIRE_STRIPPING_PATTERNS = [
    # "remove the beneficiary name", "strip the originator from field 50"
    re.compile(
        r"\b(strip|remove|delete|drop|omit|blank|erase|scrub|take out)\b.{0,40}"
        r"\b(originator|ordering customer|beneficiary|debtor|creditor|remitter|"
        r"applicant|payer|payee|sender)\b",
        re.I,
    ),
    # "change the beneficiary name to", "replace the originator with"
    re.compile(
        r"\b(change|alter|amend|replace|substitute|rewrite|edit|falsify|mask)\b.{0,30}"
        r"\b(originator|beneficiary|debtor|creditor|remitter|payer|payee|sender)\b"
        r".{0,20}\b(name|details?|field)\b",
        re.I,
    ),
    # The SWIFT field references people actually use when asking for this.
    re.compile(r"\b(field|tag)\s*(50|59|52|57)\b.{0,40}\b(blank|remove|strip|clear)\b", re.I),
    re.compile(r"\b(blank|clear|empty|remove|strip|drop)\b.{0,20}\b(field|tag)\s*(50|59|52|57)\b", re.I),
    re.compile(r"\bwire[- ]?strip\w*\b", re.I),
    # "so it doesn't get picked up by screening", "to avoid the filter"
    re.compile(
        r"\b(avoid|evade|bypass|get (a)?round|slip past|not trigger|defeat)\b.{0,30}"
        r"\b(screening|sanctions? filter|the filter|watchlist|ofac|detection)\b",
        re.I,
    ),
]

#: Attempts to move money a confirmed sanctions match has frozen, in either direction.
BLOCKED_FUNDS_PATTERNS = [
    re.compile(
        r"\b(releas\w*|unblock\w*|unfreez\w*|push (it )?through|let it go|send it anyway|"
        r"process (it )?anyway|override|return\w*|send (it )?back|refund\w*)\b.{0,60}"
        r"\b(sanction\w*|blocked|frozen|hit|match|ofac|watchlist)\b",
        re.I,
    ),
    re.compile(
        r"\b(sanction\w*|blocked|frozen|true hit|confirmed match|"
        r"(hit|match|alert) (is|was) confirmed)\b.{0,40}"
        r"\b(releas\w*|unblock\w*|unfreez\w*|return it|send it back|refund|"
        r"push (it )?through|pay (it )?anyway|process (it )?anyway)\b",
        re.I,
    ),
    # "clear the hit without", "mark it a false positive so we can pay"
    re.compile(
        r"\b(clear|dismiss|close|mark)\b.{0,25}\b(hit|match|alert)\b.{0,40}"
        r"\b(without|so (we|i) can|to (let|allow|release)|anyway)\b",
        re.I,
    ),
    re.compile(
        r"\b(skip|bypass|without|no need (for|to)|don'?t bother)\b.{0,25}"
        r"\b(screen\w*|sanctions? check|watchlist check|dual authorisation|"
        r"dual authorization|four eyes|second approver|maker[- ]checker)\b",
        re.I,
    ),
]


# Threats and pressure tactics a collector may never use. RBI Fair Practices Code for
# recovery agents, and the FDCPA s.806-807 equivalents.
COLLECTIONS_THREAT_PATTERNS = [
    # "arrested", "jailed", "prosecuted" — the threat is the same whatever the inflection,
    # and it can be phrased about the customer in the third person.
    re.compile(r"\b(arrest|jail|imprison|prosecut|incarcerat)\w*\b", re.I),
    re.compile(
        r"\b(criminal (case|charge|charges|proceedings|action)|non-?bailable|"
        r"warrant)\b",
        re.I,
    ),
    re.compile(
        r"\b(tell|inform|contact|call|visit|speak to|notify|reach out to)\b.{0,40}"
        r"\b(your|their|his|her|the)\s+"
        r"(employer|boss|manager|family|relatives?|neighbours?|neighbors?|friends?|"
        r"colleagues?|hr\b)",
        re.I,
    ),
    re.compile(
        r"\b(seize|confiscate|repossess)\b.{0,30}\b(today|immediately|now|"
        r"within \d+ hours?)\b",
        re.I,
    ),
    re.compile(
        r"\b(publish|post|share)\b.{0,30}\b(your name|your photo|defaulter list|"
        r"social media)\b",
        re.I,
    ),
    re.compile(r"\byou (have no|don'?t have a) (choice|option)\b", re.I),
]

# Attempts to get the agent to work around a control rather than respect it.
COLLECTIONS_BYPASS_PATTERNS = [
    re.compile(
        r"\b(ignore|override|bypass|work around|get around)\b.{0,40}"
        r"\b(cease|consent|dispute|hardship|do.?not.?call|dnc)\b",
        re.I,
    ),
    re.compile(
        r"\b(call|contact|visit)\b.{0,40}\b(anyway|regardless|even though)\b"
        r".{0,40}\b(cease|consent|dispute|hardship|asked us to stop)\b",
        re.I,
    ),
    re.compile(
        r"\b(outside|after|before)\b.{0,20}\b(permitted|allowed|legal)\b.{0,20}"
        r"\bhours?\b",
        re.I,
    ),
]


# --- KYC & onboarding ---------------------------------------------------------
# Requests to weaken customer due diligence. CDD is a statutory obligation under the PMLA
# and the FATF recommendations; it is not something an operator may waive on request.
KYC_INTEGRITY_PATTERNS = [
    re.compile(
        r"\b(skip|waive|bypass|omit|forget|drop|relax)\b.{0,40}"
        r"\b(kyc|cdd|edd|due diligence|screening|sanctions? check|pep check|"
        r"verification|document check|id check)\b",
        re.I,
    ),
    re.compile(
        r"\b(approve|onboard|clear|pass)\b.{0,40}\b(without|before|despite)\b"
        r".{0,40}\b(screening|verification|documents?|checks?|match)\b",
        re.I,
    ),
    re.compile(
        r"\b(mark|record|set|say)\b.{0,30}\b(as )?(verified|approved|clear)\b"
        r".{0,40}\b(anyway|regardless|without checking|even though)\b",
        re.I,
    ),
    re.compile(
        r"\b(ignore|overlook|dismiss)\b.{0,30}\b(the )?(sanctions?|pep|watchlist)\b"
        r".{0,20}\b(hit|match|alert)\b",
        re.I,
    ),
]

# Discriminatory onboarding. Jurisdiction and country risk are legitimate AML factors;
# refusing a person for who they are is not.
DISCRIMINATORY_ONBOARDING_PATTERNS = [
    re.compile(
        r"\b(reject\w*|refus\w*|declin\w*|deny|denial|denied|"
        r"do not onboard|don'?t onboard)\b.{0,50}"
        r"\b(because|due to|since|on account of|on grounds of)\b.{0,40}"
        r"\b(religion|religious|caste|race|racial|ethnic\w*|muslim|hindu|christian|"
        r"jew\w*|colour|color|tribe|tribal|indigenous)\b",
        re.I,
    ),
    re.compile(
        r"\b(religion|caste|race|ethnicity|skin colour|skin color)\b.{0,30}"
        r"\b(risk factor|risk score|onboarding criteri|red flag)\b",
        re.I,
    ),
]

# --- Investment research ------------------------------------------------------
# Market abuse. Insider dealing and manipulation are criminal offences (SEBI PFUTP
# regulations in India, MAR Article 14/15 in the EU, s.10(b) in the US).
MARKET_ABUSE_PATTERNS = [
    re.compile(
        r"\b(insider|non-?public|material non-?public|mnpi|unpublished price "
        r"sensitive)\b.{0,40}\b(information|data|tip|news)\b",
        re.I,
    ),
    re.compile(
        r"\b(front-?run|frontrunning|pump and dump|wash trade|wash sale|spoof\w*|"
        r"layering the book|marking the close|painting the tape|ramp the price)\b",
        re.I,
    ),
    re.compile(r"\b(manipulat\w*|rig|corner)\b.{0,25}\b(the )?(market|price|stock|share)\b", re.I),
    re.compile(
        r"\b(trade|buy|sell)\b.{0,30}\bahead of\b.{0,30}"
        r"\b(the )?(client|customer|order|announcement|research)\b",
        re.I,
    ),
]

# Promises no research note may make.
# Inflections matter: "returns" must match as surely as "return", and "rejection" as
# surely as "reject". A stem anchored with \b silently misses every inflected form, which
# is the worst way for a rail to fail.
GUARANTEED_RETURN_PATTERNS = [
    re.compile(
        r"\b(guarantee\w*|assured|risk-?free|riskless|no risk of loss|"
        r"cannot lose|can'?t lose)\b.{0,40}\b(returns?|profits?|gains?|upside|"
        r"yields?)\b",
        re.I,
    ),
    re.compile(
        r"\b(returns?|profits?|gains?|yields?)\b.{0,20}\b(are|is)\b.{0,15}"
        r"\b(guarantee\w*|assured|risk-?free)\b",
        re.I,
    ),
    re.compile(
        r"\b(will|is going to|is certain to)\b.{0,20}\b(double|triple|soar|"
        r"definitely rise|definitely fall)\b",
        re.I,
    ),
]

# --- Knowledge assistant ------------------------------------------------------
# Attempts to use enterprise retrieval as a credential store.
CORPUS_EXFILTRATION_PATTERNS = [
    re.compile(
        r"\b(find|search|show|give|list|retrieve|what is)\b.{0,40}"
        r"\b(password|passwords|api key|api keys|secret key|private key|"
        r"credential|credentials|token|access key|connection string)\b",
        re.I,
    ),
    re.compile(
        r"\b(dump|export|list all)\b.{0,30}\b(documents?|corpus|knowledge base|"
        r"everything)\b",
        re.I,
    ),
    re.compile(r"\b(\.env|id_rsa|ssh key|service account key|kubeconfig)\b", re.I),
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
