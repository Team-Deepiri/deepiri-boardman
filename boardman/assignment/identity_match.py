"""
Link GitHub roster rows → Plaky workspace users when emails or names differ.

Design:
- **Heuristic-first:** strong structural signals (email / login / name similarity) — no hardcoded name lists.
- **Optional LLM** gray-zone pass (`ASSIGNMENT_IDENTITY_LLM_ENABLED`) — **off by default**; enable only if
  you explicitly want model inference beyond `best_plaky_match_for_github` scores.
- Conservative tie-breaking in best_plaky_match_for_github (ambiguity margin + runner-up gap).
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from boardman.assignment.identity_common import (
    github_public_email as _github_email,
    normalize_identity_text as _normalize_text,
    plaky_display_name as _plaky_display_name,
    plaky_email_addresses as _all_plaky_emails,
)
from boardman.settings import settings


def _norm_ws_casefold(s: str) -> str:
    t = _normalize_text(s)
    t = unicodedata.normalize("NFD", t)
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return " ".join(t.split()).casefold()


def _local_part(email: str) -> str:
    e = (email or "").strip().lower()
    if "@" not in e:
        return ""
    return e.split("@", 1)[0].strip()


def _domain_part(email: str) -> str:
    e = (email or "").strip().lower()
    if "@" not in e:
        return ""
    return e.split("@", 1)[1].strip()


def _strip_plus_tag(local: str) -> str:
    return (local or "").split("+", 1)[0].strip().lower()


def _gmailish_domain(email: str) -> bool:
    d = _domain_part(email)
    return d in ("gmail.com", "googlemail.com") or d.endswith(".gmail.com")


def _gmail_normalize_local(local: str) -> str:
    base = _strip_plus_tag(local)
    return base.replace(".", "")


def _similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _first_names_plausibly_same(a: str, b: str) -> bool:
    """
    Structural check for same given name (no nickname table).
    Covers John/Jonathan (not substring-prefix) via same initial + similarity floor.
    """
    a, b = _norm_token(a), _norm_token(b)
    if not a or not b:
        return False
    if a == b:
        return True
    if _similar(a, b) >= 0.72:
        return True
    if len(a) >= 3 and len(b) >= 3:
        if a.startswith(b[: min(4, len(b))]) or b.startswith(a[: min(4, len(a))]):
            return True
    if len(a) >= 3 and len(b) >= 3 and a[0] == b[0] and _similar(a, b) >= 0.52:
        return True
    return False


def _norm_token(token: str) -> str:
    return (token or "").strip().lower()


def _name_tokens(name: str) -> List[str]:
    n = _norm_ws_casefold(name)
    if not n:
        return []
    return re.findall(r"[\w']+", n)


def _canonical_full_name(name: str) -> str:
    """Normalize 'Doe, John' → 'john doe' for comparison."""
    n = _norm_ws_casefold(name)
    if not n:
        return ""
    if "," in n:
        left, right = n.split(",", 1)
        left, right = left.strip(), right.strip()
        if left and right:
            return f"{right} {left}".strip()
    return n


def _initials_from_tokens(tokens: Sequence[str]) -> str:
    """e.g. ['john', 'michael', 'doe'] → 'jmd' (first char of each word, max 4)."""
    parts: List[str] = []
    for t in tokens[:5]:
        if t:
            parts.append(t[0])
    return "".join(parts)[:4]


def _login_token_variants(login: str) -> Set[str]:
    """jsmith, john.smith → {jsmith, john, smith, ...}"""
    s = login.strip().lower()
    out: Set[str] = {s}
    if not s:
        return out
    for sep in ".-_":
        if sep in s:
            for p in s.split(sep):
                p = p.strip()
                if len(p) >= 2:
                    out.add(p)
    return out


def _login_initial_plus_lastname(gh_login: str, pl_name: str, gh_display_name: str = "") -> int:
    """`jsmith` ↔ John Smith; `john.smith` ↔ John Smith (no separator in login)."""
    login = gh_login.strip().lower()
    toks = _name_tokens(pl_name)
    if len(login) < 3 or len(toks) < 2:
        return 0
    first, last = _norm_token(toks[0]), toks[-1]
    if not first or not last:
        return 0

    ght = _name_tokens(gh_display_name)
    if not ght:
        return 0
    gf = _norm_token(ght[0])
    if not _first_names_plausibly_same(gf, first):
        return 0

    compact = f"{first[0]}{last}"
    if login == compact:
        return 6600
    if len(compact) >= 4 and _similar(login, compact) >= 0.9:
        return int(6000 + _similar(login, compact) * 400)
    glued = f"{first}{last}"
    norm_login = re.sub(r"[.\-_]", "", login)
    norm_glued = re.sub(r"[.\-_]", "", glued)
    if norm_login == norm_glued:
        return 6400
    return 0


def _login_vs_local_score(gh_login: str, plaky_local: str) -> int:
    if not gh_login or not plaky_local:
        return 0
    loc = _strip_plus_tag(plaky_local)
    if not loc:
        return 0
    if gh_login == loc:
        return 9000
    variants = _login_token_variants(gh_login)
    if loc in variants or gh_login in loc or loc in gh_login:
        if min(len(gh_login), len(loc)) >= 3:
            return 7600
    lr = _similar(gh_login, loc)
    if lr >= 0.92:
        return int(6800 + lr * 200)
    if lr >= 0.88:
        return int(6000 + lr * 200)
    return 0


def _email_pair_score(gh_email: str, pe: str) -> Tuple[int, bool]:
    """
    Returns (score, is_strong_email_signal).
    Strong = exact, same-local, or very high similarity (used for corroboration).
    """
    if not gh_email or not pe:
        return 0, False

    ge, pve = gh_email.strip().lower(), pe.strip().lower()
    if ge == pve:
        return 10_000, True

    gl, pl = _local_part(ge), _local_part(pve)
    gd, pd = _domain_part(ge), _domain_part(pve)

    strong = False
    best = 0

    full_sim = _similar(ge, pve)
    if full_sim >= 0.97:
        best = max(best, int(8800 + full_sim * 100))
        strong = True
    elif full_sim >= 0.94:
        best = max(best, int(8200 + full_sim * 80))
        strong = True
    elif full_sim >= 0.90:
        best = max(best, int(7400 + full_sim * 60))
        strong = True
    elif full_sim >= 0.86:
        best = max(best, int(6600 + full_sim * 50))

    if gl and pl:
        glc, plc = _strip_plus_tag(gl), _strip_plus_tag(pl)
        if glc == plc:
            if gd == pd:
                best = max(best, 9300)
            else:
                best = max(best, 8800)
            strong = True
        else:
            loc_sim = _similar(glc, plc)
            if loc_sim >= 0.94:
                best = max(best, int(7800 + loc_sim * 100))
                strong = True
            elif loc_sim >= 0.88:
                best = max(best, int(7000 + loc_sim * 80))
            elif loc_sim >= 0.82:
                best = max(best, int(6200 + loc_sim * 60))

        if _gmailish_domain(ge) or _gmailish_domain(pve):
            gn = _gmail_normalize_local(gl)
            pn = _gmail_normalize_local(pl)
            if gn and pn and gn == pn:
                best = max(best, 8900)
                strong = True

    return best, strong


def _name_match_score(gh_name: str, pl_name: str) -> Tuple[int, bool, bool]:
    """
    Returns (score, used_last_name_only, high_name_similarity).
    used_last_name_only: True when score leans on surname token overlap (down-rank elsewhere).
    """
    if not gh_name or not pl_name:
        return 0, False, False

    ca = _canonical_full_name(gh_name)
    cb = _canonical_full_name(pl_name)
    if not ca or not cb:
        return 0, False, False

    if ca == cb:
        return 8500, False, True

    sim = _similar(ca, cb)
    high_sim = sim >= 0.90
    if sim >= 0.96:
        return int(8200 + sim * 100), False, True
    if sim >= 0.92:
        return int(7600 + sim * 100), False, True
    if sim >= 0.88:
        return int(6800 + sim * 80), False, True
    if sim >= 0.82:
        return int(5800 + sim * 80), False, False

    ga, gb = _name_tokens(gh_name), _name_tokens(pl_name)
    if not ga or not gb:
        return 0, False, False

    la, lb = ga[-1], gb[-1]
    if la == lb and len(la) >= 2:
        a0, b0 = ga[0], gb[0]
        fa = _norm_token(a0)
        fb = _norm_token(b0)
        if fa == fb:
            return 7400, False, False
        fst_sim = _similar(fa, fb)
        if fst_sim >= 0.86:
            return int(7200 + fst_sim * 100), False, False
        # "john" vs "jonathan" — prefix / substring (not same-first-letter-only)
        if _first_names_plausibly_same(a0, b0) and fst_sim >= 0.48:
            return int(6800 + fst_sim * 200), False, False
        if fst_sim < 0.72:
            return 420, True, False
        return int(4800 + fst_sim * 400), False, False

    # Jaccard on normalized tokens
    sa = {_norm_token(t) for t in ga}
    sb = {_norm_token(t) for t in gb}
    inter = len(sa & sb)
    union = len(sa | sb) or 1
    j = inter / union
    if j >= 0.66 and inter >= 2:
        return int(6500 + j * 400), False, False
    if j >= 0.5 and inter >= 2:
        return int(5200 + j * 400), False, False

    # Initials line up (J. Doe vs John Doe)
    ia = _initials_from_tokens(ga)
    ib = _initials_from_tokens(gb)
    if len(ia) >= 2 and len(ib) >= 2 and ia[0] == ib[0] and la == lb:
        return 7100, False, False

    return 0, False, False


def score_github_vs_plaky(gh: Dict[str, Any], plaky: Dict[str, Any]) -> int:
    gh_login = str(gh.get("login") or "").strip().lower()
    gh_email = _github_email(gh)
    gh_name = str(gh.get("name") or "").strip()

    pl_emails = _all_plaky_emails(plaky)
    pl_name = _plaky_display_name(plaky)

    email_scores: List[int] = []
    any_strong_email = False
    for pe in pl_emails:
        sc, strong = _email_pair_score(gh_email, pe)
        if sc:
            email_scores.append(sc)
        if strong:
            any_strong_email = True

    login_scores: List[int] = []
    for pe in pl_emails:
        loc = _local_part(pe)
        if loc:
            login_scores.append(_login_vs_local_score(gh_login, loc))

    name_score = 0
    name_last_only = False
    name_high_sim = False
    if gh_name and pl_name:
        name_score, name_last_only, name_high_sim = _name_match_score(gh_name, pl_name)

    initial_last = (
        _login_initial_plus_lastname(gh_login, pl_name, gh_name) if gh_login and pl_name else 0
    )

    # Also: GitHub login tokens vs Plaky display tokens (e.g. login 'jsmith' vs 'John Smith')
    login_name_boost = 0
    if gh_login and pl_name and len(gh_login) >= 3:
        p_tokens = set(_name_tokens(pl_name))
        variants = _login_token_variants(gh_login)
        if p_tokens & variants:
            login_name_boost = 5200
        elif len(gh_login) >= 4:
            for t in p_tokens:
                if len(t) >= 3 and (gh_login in t or t in gh_login):
                    login_name_boost = 4800
                    break

    email_best = max(email_scores) if email_scores else 0
    login_best = max(login_scores) if login_scores else 0
    peaks = [email_best, login_best, name_score, login_name_boost, initial_last]
    base = max(peaks)

    # Corroboration: two channels agree → small bonus (capped)
    bonus = 0
    channels = 0
    if email_best >= 6500:
        channels += 1
    if login_best >= 6500:
        channels += 1
    if name_score >= 6500 or name_high_sim:
        channels += 1
    if channels >= 2:
        bonus = 350
    elif channels >= 1 and name_score >= 5600 and (email_best >= 5000 or login_best >= 6000):
        bonus = 250

    raw = base + bonus

    # Last-name-heavy hit without email/login anchor stays weak
    if name_last_only and base <= 5000 and not any_strong_email and login_best < 6000:
        raw = min(raw, 580)

    # Name-only weak fuzzy: cap unless login↔email-local or initial+lastname matched strongly
    anchor_non_name = max(login_best, initial_last, login_name_boost)
    if (
        not any_strong_email
        and anchor_non_name < 6000
        and name_score > 0
        and name_score < 6000
        and email_best < 5000
    ):
        raw = min(raw, 620)

    had_exact_email = email_best >= 10_000
    if (
        settings.assignment_identity_llm_enabled
        and not had_exact_email
        and settings.assignment_identity_llm_gray_low <= raw <= settings.assignment_identity_llm_gray_high
    ):
        from boardman.assignment import llm_identity_match as _lim

        conf = _lim.llm_same_person_confidence(gh, plaky)
        if conf is not None:
            hi = settings.assignment_identity_llm_min_confidence
            lo = settings.assignment_identity_llm_reject_below
            if conf >= hi:
                raw = max(raw, int(7600 + conf * 2100))
            elif conf <= lo:
                raw = min(raw, 460)

    return min(int(raw), 10_000)


def best_plaky_match_for_github(
    gh: Dict[str, Any],
    plaky_users: List[Dict[str, Any]],
    *,
    min_score: int = 640,
    ambiguity_margin: int = 45,
) -> Tuple[Optional[str], str, int]:
    """
    Returns (plaky_user_id_or_none, reason, best_score).
    reason: matched | below_threshold | ambiguous
    """
    scored: List[Tuple[str, int]] = []
    for p in plaky_users:
        if not isinstance(p, dict):
            continue
        uid = str(p.get("id") or p.get("userId") or "").strip()
        if not uid:
            continue
        scored.append((uid, score_github_vs_plaky(gh, p)))

    if not scored:
        return None, "below_threshold", 0

    scored.sort(key=lambda x: -x[1])
    best_id, best_score = scored[0]
    second = scored[1][1] if len(scored) > 1 else 0
    third = scored[2][1] if len(scored) > 2 else 0

    if best_score < min_score:
        return None, "below_threshold", best_score

    # Near-tie with second
    if second >= best_score - ambiguity_margin:
        return None, "ambiguous", best_score

    # Three-way cluster: second and third both close to best
    if len(scored) >= 3 and third >= best_score - ambiguity_margin:
        return None, "ambiguous", best_score

    return best_id, "matched", best_score
