from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein


def _norm(v) -> str:
    return str(v).strip().lower() if v not in (None, "") else ""


def _local_part(email: str) -> str:
    return email.split("@")[0]


# An identifier that differs by no more than this is worth asking about, on the
# theory that it was mistyped. Anything further apart is a different identifier.
_IDENTIFIER_TYPO_DISTANCE = 2

# Deliberately inside the escalate band rather than above auto_merge: a suspected
# mistyped identifier is a question, not something to merge silently.
_TYPO_SCORE = 0.85


def _identifier_score(a: str, b: str) -> float:
    """Compare two identifiers as identifiers, not as names."""
    if a == b:
        return 1.0
    return _TYPO_SCORE if Levenshtein.distance(a, b) <= _IDENTIFIER_TYPO_DISTANCE else 0.0


def _keys_contradict(a: dict, b: dict) -> bool:
    """True when both records carry an employee id and the two are unrelated.

    An employee id is a natural key, so two different ones are two different
    people -- with one exception this dataset actually contains: a record
    re-entered under a derived id (EMP002196 -> EMP002196-D2) is the duplicate
    we most want to catch. A prefix relationship is therefore not a
    contradiction, an unrelated id is.
    """
    id_a, id_b = _norm(a.get("employee_id")), _norm(b.get("employee_id"))
    if not (id_a and id_b) or id_a == id_b:
        return False
    return not (id_a.startswith(id_b) or id_b.startswith(id_a))


def similarity_score(a: dict, b: dict) -> float:
    """Tiered, not averaged: email and phone are strong, low-collision identity
    signals and are used alone whenever both records have one. Names are a last
    resort, used only when neither record has email or phone at all -- a name
    match alone is not trustworthy at any real company's scale (two different
    employees can both be named Rohan Sharma), so the name fallback is gated by
    date_of_birth: different birthdates force a non-match regardless of how
    similar the names look.

    Email and phone are compared as identifiers, not as strings that can be
    "sort of" alike. Two different mailboxes belong to two different people, so
    the only reason to fuzzy-match them at all is to catch a transcription typo,
    which is an edit or two -- not a whole different surname. Scoring them by
    string ratio instead was the single largest source of false duplicate
    questions: daniel.tran92@x and daniel.mora1392@x are unrelated people but
    scored 0.79, because the shared "daniel." prefix and the digit runs carried
    the comparison. Email is additionally compared on the local part only, since
    every address here shares one domain and that constant suffix inflated the
    ratio further.
    """
    if _keys_contradict(a, b):
        return 0.0

    email_a, email_b = _norm(a.get("work_email")), _norm(b.get("work_email"))
    if email_a and email_b:
        return _identifier_score(_local_part(email_a), _local_part(email_b))

    phone_a, phone_b = _norm(a.get("phone")), _norm(b.get("phone"))
    if phone_a and phone_b:
        return _identifier_score(phone_a, phone_b)

    first_a, first_b = _norm(a.get("first_name")), _norm(b.get("first_name"))
    last_a, last_b = _norm(a.get("last_name")), _norm(b.get("last_name"))
    if not ((first_a or last_a) and (first_b or last_b)):
        return 0.0

    dob_a, dob_b = a.get("date_of_birth"), b.get("date_of_birth")
    if dob_a and dob_b and str(dob_a) != str(dob_b):
        return 0.0

    name_scores = []
    if first_a or first_b:
        name_scores.append(fuzz.ratio(first_a, first_b) / 100)
    if last_a or last_b:
        name_scores.append(fuzz.ratio(last_a, last_b) / 100)
    return sum(name_scores) / len(name_scores) if name_scores else 0.0


def blocking_key(data: dict) -> str:
    """Cheap bucket so we only score pairs that could plausibly match, instead
    of comparing every record against every other one.

    In this dataset no single source carries every identity field: HRIS has
    names but no email; payroll and CRM have email but no names (CRM's "Full
    Name" needs a split that isn't built). HRIS<->payroll are already joined by
    exact natural_key, so the fuzzy match that actually has to happen here is
    CRM<->payroll -- which only works if both land in the same bucket. Email
    must therefore be tried first: name-first blocking would put payroll and
    CRM in disjoint namespaces and they'd never be compared at all. A single
    catch-all bucket for "nothing to block on" would also be a performance bug
    on its own -- every record lacking the preferred field would collide into
    one O(n^2) bucket."""
    email = _norm(data.get("work_email"))
    if email:
        return f"em:{email.split('@')[0][:5]}"
    last = _norm(data.get("last_name"))
    if last:
        return f"ln:{last[:6]}:{_norm(data.get('first_name'))[:1]}"
    phone = _norm(data.get("phone"))
    if phone:
        return f"ph:{phone[-4:]}"
    return "_no_identity_"
