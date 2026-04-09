"""
Skills layer: pre-packaged multi-step workflows the LLM can call as a single unit.

A skill internally calls multiple PMS operations in a fixed, correct order and
returns a rich, combined result.  Business rules (e.g. "always look up the guest
before searching rooms") are enforced HERE in Python — not in the prompt.

Contrast with tools (src/tools.py):
  - Tools  = one atomic PMS call  (get_guest_by_email, search_available_rooms …)
  - Skills = several tool calls bundled into one callable intent

The LLM should prefer skills for common, well-defined workflows and fall back to
individual tools only for atomic lookups the skills do not cover.

Skills follow the same signature convention as tool implementations:
    skill_fn(pms, current_date, **kwargs) -> dict
so they plug directly into the same dispatcher in tools.py.
"""
from __future__ import annotations

from datetime import date

from .pms import PMS


# ── Skill implementations ──────────────────────────────────────────────────────

def skill_booking_lookup(
    pms: PMS,
    email: str,
    check_in: str,
    check_out: str,
    adults: int,
    children: int = 0,
    **_,
) -> dict:
    """
    Booking workflow — step 1 of 2.

    Packages the two operations that ALWAYS happen together at the start of any
    new-reservation request:
      1. Look up whether the guest already exists (Rule 4 in the prompt)
      2. Search rooms for the requested dates and occupancy

    Business rule enforced in code:
      - Guest lookup always runs BEFORE room search — no way to skip it.
      - Returns a 'guest_is_new' flag so the LLM knows whether to include
        create_guest in the action_plan without reasoning about it.

    The LLM receives everything it needs to call submit_plan in ONE round trip
    instead of two separate tool calls.
    """
    # Step 1: guest lookup (enforced — cannot be skipped)
    guest = pms.get_guest_by_email(email)

    # Step 2: availability + pricing for all qualifying room types
    available_rooms = pms.search_available_rooms(check_in, check_out, adults, children)

    nights_count = len(pms._nights(check_in, check_out))

    return {
        # Guest context
        "guest": guest,                     # full profile dict, or None
        "guest_is_new": guest is None,      # True  → include create_guest in plan
                                            # False → use existing guest["id"]
        # Availability context
        "available_rooms": available_rooms, # list of room dicts with all rate options
        "rooms_found": len(available_rooms),
        # Stay context (echo back so LLM has everything in one place)
        "check_in": check_in,
        "check_out": check_out,
        "adults": adults,
        "children": children,
        "nights": nights_count,
    }


def skill_cancellation_lookup(
    pms: PMS,
    email: str,
    reservation_id: str,
    **_,
) -> dict:
    """
    Cancellation workflow — full context in one call.

    Packages the three operations needed for any cancellation request:
      1. Look up the guest by email
      2. Fetch the reservation (enriched with room/rate/policy names)
      3. Assess financial risk in Python (non-refundable → human review)

    Business rule enforced in code:
      - Risk assessment happens here — the LLM does NOT have to reason about
        whether "non_refundable" means requires_human_review.  The answer is
        already in the 'requires_human_review' flag in the returned dict.
      - This prevents the LLM from accidentally auto-processing a refund request
        on a non-refundable booking if the prompt rule is ever weakened.

    The LLM should copy 'requires_human_review' and 'risk_reason' directly into
    submit_plan without re-deriving them.
    """
    # Step 1: guest lookup
    guest = pms.get_guest_by_email(email)

    # Step 2: reservation lookup (enriched — includes cancellation_policy name)
    reservation = pms.get_reservation_enriched(reservation_id)

    if reservation is None:
        return {
            "found": False,
            "reservation_id": reservation_id,
            "guest": guest,
            "requires_human_review": False,
            "risk_reason": None,
        }

    # Step 3: risk assessment in Python — not left to LLM reasoning
    policy = reservation.get("cancellation_policy", "")
    is_risky = policy == "non_refundable"

    return {
        "found": True,
        "guest": guest,
        "reservation": reservation,
        "cancellation_policy": policy,
        # Risk verdict — LLM copies this into submit_plan directly
        "requires_human_review": is_risky,
        "risk_reason": (
            "Non-refundable rate — refund or cancellation requires manager approval."
            if is_risky else None
        ),
    }


def skill_guest_history(
    pms: PMS,
    email: str,
    **_,
) -> dict:
    """
    Guest history — profile + all reservations in one call.

    Replaces the two-step pattern:
      get_guest_by_email → get_guest_reservations
    with a single skill call.

    Use when the LLM needs to identify which reservation a guest is referring to
    (e.g. "my booking last month") without the guest providing a reservation ID.
    """
    # Step 1: guest lookup
    guest = pms.get_guest_by_email(email)
    if guest is None:
        return {"found": False, "email": email}

    # Step 2: all reservations, each enriched with room/rate/guest names
    reservations = pms.get_reservations_by_guest(guest["id"])
    enriched = [pms.get_reservation_enriched(r["id"]) for r in reservations]

    return {
        "found": True,
        "guest": guest,
        "reservations": enriched,
        "reservation_count": len(enriched),
    }


# ── Skill schemas (Anthropic tool-use format) ────────────────────────────
#
# These appear in ALL_PLANNING_TOOLS alongside the atomic read-tool schemas.
# They are listed FIRST so the LLM encounters and prefers them.

SKILL_SCHEMAS: list[dict] = [
    {
        "name": "skill_booking_lookup",
        "description": (
            "Use this when a guest wants to make a new reservation. "
            "Combines guest lookup + room availability search into one call. "
            "Returns whether the guest already exists and all available rooms with pricing. "
            "Always prefer this over calling get_guest_by_email and search_available_rooms separately."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "email": {
                    "type": "string",
                    "description": "Guest's email address (from email header or body).",
                },
                "check_in": {
                    "type": "string",
                    "description": "Check-in date in YYYY-MM-DD format.",
                },
                "check_out": {
                    "type": "string",
                    "description": "Check-out date in YYYY-MM-DD format.",
                },
                "adults": {
                    "type": "integer",
                    "description": "Number of adults.",
                    "minimum": 1,
                },
                "children": {
                    "type": "integer",
                    "description": "Number of children (default 0).",
                    "minimum": 0,
                },
            },
            "required": ["email", "check_in", "check_out", "adults"],
        },
    },
    {
        "name": "skill_cancellation_lookup",
        "description": (
            "Use this when a guest wants to cancel or get a refund on a reservation. "
            "Fetches guest profile + reservation details + assesses financial risk in one call. "
            "The returned 'requires_human_review' flag is pre-computed — copy it directly into submit_plan. "
            "Always prefer this over calling get_guest_by_email and get_reservation_details separately."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "email": {
                    "type": "string",
                    "description": "Guest's email address.",
                },
                "reservation_id": {
                    "type": "string",
                    "description": "The reservation ID to cancel (e.g. RES001).",
                },
            },
            "required": ["email", "reservation_id"],
        },
    },
    {
        "name": "skill_guest_history",
        "description": (
            "Use this when you need a guest's full profile and all their reservations together. "
            "Useful when the guest references 'my booking' without giving a reservation ID. "
            "Replaces the pattern of calling get_guest_by_email then get_guest_reservations separately."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "email": {
                    "type": "string",
                    "description": "Guest's email address.",
                },
            },
            "required": ["email"],
        },
    },
]


# ── Handler map (plugs into tools.py dispatcher) ──────────────────────────────

SKILL_HANDLERS: dict = {
    "skill_booking_lookup": skill_booking_lookup,
    "skill_cancellation_lookup": skill_cancellation_lookup,
    "skill_guest_history": skill_guest_history,
}