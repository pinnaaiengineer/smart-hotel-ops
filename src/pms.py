"""
Mock Property Management System (PMS).

This module owns all data access and mutation logic.  The agent never touches
the raw JSON directly — it goes through these methods, which enforce:
  - Night-based date ranges  (check_in inclusive, check_out exclusive)
  - Correct rate calculation (rate modifier + breakfast supplement)
  - Occupancy constraints
  - Availability decrement / restore on create / cancel
"""
from __future__ import annotations

import copy
import json
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Optional


class PMSError(Exception):
    """Raised for invalid PMS operations (not-found, no availability, etc.)."""


class PMS:
    # ── Construction ─────────────────────────────────────────────────────────

    def __init__(self, data_path: str | Path) -> None:
        self.data_path = Path(data_path)
        with open(self.data_path, encoding="utf-8") as fh:
            raw = json.load(fh)

        self.hotel: dict = raw["hotel"]
        self.policies: dict = raw["policies"]

        # Index by ID for O(1) lookups
        self.room_types: dict[str, dict] = {rt["id"]: rt for rt in raw["room_types"]}
        self.rate_plans: dict[str, dict] = {rp["id"]: rp for rp in raw["rate_plans"]}

        # Deep copy so mutations don't bleed between tests
        self.availability: dict[str, dict[str, int]] = copy.deepcopy(
            {k: v for k, v in raw["availability"].items() if not k.startswith("_")}
        )

        # Guests indexed by both email (for lookup) and ID (for joins)
        self._guests_by_email: dict[str, dict] = {
            g["email"].lower(): g for g in raw["guests"]
        }
        self._guests_by_id: dict[str, dict] = {
            g["id"]: g for g in raw["guests"]
        }

        self.reservations: dict[str, dict] = {
            r["id"]: r for r in raw["reservations"]
        }

    # ── Date helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _nights(check_in: str, check_out: str) -> list[date]:
        """
        Return the list of *night* dates occupied by the stay.
        A guest checking in Apr-20 and out Apr-23 occupies nights Apr-20, 21, 22.
        check_out date itself is NOT included.
        """
        start = date.fromisoformat(check_in)
        end = date.fromisoformat(check_out)
        nights: list[date] = []
        cur = start
        while cur < end:
            nights.append(cur)
            cur += timedelta(days=1)
        return nights

    # ── Availability ─────────────────────────────────────────────────────────

    def get_availability(self, room_type_id: str, check_in: str, check_out: str) -> int:
        """
        Return the *minimum* available rooms for room_type_id across all nights.
        Any night not listed in the availability dict counts as 0.
        """
        nights = self._nights(check_in, check_out)
        if not nights:
            return 0

        minimum = float("inf")
        for night in nights:
            day_data = self.availability.get(night.isoformat(), {})
            avail = day_data.get(room_type_id, 0)
            minimum = min(minimum, avail)

        return int(minimum) if minimum != float("inf") else 0

    # ── Pricing ──────────────────────────────────────────────────────────────

    def calculate_total(
        self,
        room_type_id: str,
        rate_plan_id: str,
        check_in: str,
        check_out: str,
        adults: int,
        children: int = 0,
    ) -> float:
        """
        Total = Σ over each night:
            (base_rate × rate_modifier) + (breakfast_supplement × (adults + children))

        Notes:
          - RP003 (Non-Refundable Saver) has modifier 0.85, no breakfast
          - RP002 (Breakfast Included)  has modifier 1.0,  supplement 250 NOK/person
          - RP004 (Flexible Rate)       has modifier 1.15, supplement 0  (breakfast free)
        """
        room = self.room_types[room_type_id]
        plan = self.rate_plans[rate_plan_id]
        nights = self._nights(check_in, check_out)

        total = 0.0
        for _ in nights:
            night_cost = room["base_rate_per_night"] * plan["rate_modifier"]
            if plan["includes_breakfast"]:
                supplement = plan.get("breakfast_supplement_per_person", 0)
                night_cost += supplement * (adults + children)
            total += night_cost

        return round(total, 2)

    # ── Search ───────────────────────────────────────────────────────────────

    def search_available_rooms(
        self, check_in: str, check_out: str, adults: int, children: int = 0
    ) -> list[dict]:
        """
        Return all room types that:
          1. Have enough max_occupancy for (adults + children)
          2. Have ≥ 1 room available for every night in the stay
        Each result includes all rate plan options with exact pricing.
        """
        requested_occupancy = adults + children
        results: list[dict] = []

        for rt_id, room in self.room_types.items():
            if room["max_occupancy"] < requested_occupancy:
                continue  # Occupancy constraint

            avail = self.get_availability(rt_id, check_in, check_out)
            if avail == 0:
                continue

            rate_options = []
            for rp_id, plan in self.rate_plans.items():
                total = self.calculate_total(
                    rt_id, rp_id, check_in, check_out, adults, children
                )
                nights_count = len(self._nights(check_in, check_out))
                rate_options.append(
                    {
                        "rate_plan_id": rp_id,
                        "rate_plan_name": plan["name"],
                        "cancellation_policy": plan["cancellation_policy"],
                        "includes_breakfast": plan["includes_breakfast"],
                        "total_amount_nok": total,
                        "nights": nights_count,
                    }
                )
            # Sort cheapest first to help the LLM present options logically
            rate_options.sort(key=lambda x: x["total_amount_nok"])

            results.append(
                {
                    "room_type_id": rt_id,
                    "room_type_name": room["name"],
                    "description": room["description"],
                    "bed_type": room["bed_type"],
                    "max_occupancy": room["max_occupancy"],
                    "amenities": room["amenities"],
                    "rooms_available": avail,
                    "rate_options": rate_options,
                }
            )

        return results

    # ── Guest CRUD ───────────────────────────────────────────────────────────

    def get_guest_by_email(self, email: str) -> Optional[dict]:
        return self._guests_by_email.get(email.strip().lower())

    def get_guest_by_id(self, guest_id: str) -> Optional[dict]:
        return self._guests_by_id.get(guest_id)

    def create_guest(
        self,
        first_name: str,
        last_name: str,
        email: str,
        phone: str = "",
        nationality: str = "",
    ) -> dict:
        email_lower = email.strip().lower()
        if email_lower in self._guests_by_email:
            # Idempotent — return existing instead of duplicating
            return self._guests_by_email[email_lower]

        guest_id = "G" + uuid.uuid4().hex[:5].upper()
        guest: dict = {
            "id": guest_id,
            "first_name": first_name.strip(),
            "last_name": last_name.strip(),
            "email": email_lower,
            "phone": phone,
            "nationality": nationality,
            "created_at": date.today().isoformat(),
        }
        self._guests_by_email[email_lower] = guest
        self._guests_by_id[guest_id] = guest
        return guest

    def get_reservations_by_guest(self, guest_id: str) -> list[dict]:
        return [r for r in self.reservations.values() if r["guest_id"] == guest_id]

    # ── Reservation CRUD ─────────────────────────────────────────────────────

    def get_reservation(self, reservation_id: str) -> Optional[dict]:
        return self.reservations.get(reservation_id)

    def get_reservation_enriched(self, reservation_id: str) -> Optional[dict]:
        """Return reservation with denormalised room/rate/guest names for easy display."""
        res = self.get_reservation(reservation_id)
        if res is None:
            return None
        enriched = dict(res)
        room = self.room_types.get(res["room_type_id"], {})
        plan = self.rate_plans.get(res["rate_plan_id"], {})
        guest = self._guests_by_id.get(res["guest_id"], {})
        enriched["room_type_name"] = room.get("name", "Unknown")
        enriched["rate_plan_name"] = plan.get("name", "Unknown")
        enriched["cancellation_policy"] = plan.get("cancellation_policy", "unknown")
        enriched["guest_name"] = f"{guest.get('first_name', '')} {guest.get('last_name', '')}".strip()
        enriched["guest_email"] = guest.get("email", "")
        return enriched

    def create_reservation(
        self,
        guest_id: str,
        room_type_id: str,
        rate_plan_id: str,
        check_in: str,
        check_out: str,
        adults: int,
        children: int = 0,
        notes: str = "",
    ) -> dict:
        # Validate IDs
        if room_type_id not in self.room_types:
            raise PMSError(f"Unknown room type: {room_type_id}")
        if rate_plan_id not in self.rate_plans:
            raise PMSError(f"Unknown rate plan: {rate_plan_id}")
        if guest_id not in self._guests_by_id:
            raise PMSError(f"Unknown guest: {guest_id}")

        # Re-check availability (guards against race conditions / human-approval delay)
        avail = self.get_availability(room_type_id, check_in, check_out)
        if avail == 0:
            raise PMSError(
                f"No availability for {room_type_id} from {check_in} to {check_out}"
            )

        # Occupancy check
        room = self.room_types[room_type_id]
        if adults + children > room["max_occupancy"]:
            raise PMSError(
                f"{room['name']} max occupancy is {room['max_occupancy']}, "
                f"requested {adults + children}"
            )

        total = self.calculate_total(room_type_id, rate_plan_id, check_in, check_out, adults, children)
        res_id = "RES" + uuid.uuid4().hex[:6].upper()

        reservation: dict = {
            "id": res_id,
            "guest_id": guest_id,
            "room_type_id": room_type_id,
            "rate_plan_id": rate_plan_id,
            "check_in": check_in,
            "check_out": check_out,
            "adults": adults,
            "children": children,
            "status": "confirmed",
            "total_amount": total,
            "notes": notes,
            "created_at": date.today().isoformat(),
        }

        # Decrement availability for each occupied night
        for night in self._nights(check_in, check_out):
            night_str = night.isoformat()
            if night_str not in self.availability:
                self.availability[night_str] = {}
            current = self.availability[night_str].get(room_type_id, 0)
            self.availability[night_str][room_type_id] = max(0, current - 1)

        self.reservations[res_id] = reservation
        return reservation

    def cancel_reservation(self, reservation_id: str) -> dict:
        res = self.reservations.get(reservation_id)
        if res is None:
            raise PMSError(f"Reservation {reservation_id} not found")
        if res["status"] == "cancelled":
            raise PMSError(f"Reservation {reservation_id} is already cancelled")

        res["status"] = "cancelled"

        # Restore availability for each previously occupied night
        for night in self._nights(res["check_in"], res["check_out"]):
            night_str = night.isoformat()
            if night_str not in self.availability:
                self.availability[night_str] = {}
            current = self.availability[night_str].get(res["room_type_id"], 0)
            self.availability[night_str][res["room_type_id"]] = current + 1

        return res

    def modify_reservation(self, reservation_id: str, **updates) -> dict:
        """
        Update allowed fields on a reservation and recalculate total.
        Allowed: check_in, check_out, adults, children, notes, rate_plan_id.
        """
        res = self.reservations.get(reservation_id)
        if res is None:
            raise PMSError(f"Reservation {reservation_id} not found")
        if res["status"] == "cancelled":
            raise PMSError("Cannot modify a cancelled reservation")

        allowed = {"check_in", "check_out", "adults", "children", "notes", "rate_plan_id"}
        for key, value in updates.items():
            if key in allowed:
                res[key] = value

        # Recalculate total after any field changes
        res["total_amount"] = self.calculate_total(
            res["room_type_id"],
            res["rate_plan_id"],
            res["check_in"],
            res["check_out"],
            res["adults"],
            res["children"],
        )
        return res

    # ── Hotel info ───────────────────────────────────────────────────────────

    def get_hotel_info(self) -> dict:
        return {"hotel": self.hotel, "policies": self.policies}

    # ── Persistence ──────────────────────────────────────────────────────────

    def save(self) -> None:
        """Write the current in-memory state back to the original JSON file."""
        data = {
            "hotel": self.hotel,
            "policies": self.policies,
            "room_types": list(self.room_types.values()),
            "rate_plans": list(self.rate_plans.values()),
            "availability": self.availability,
            "guests": list(self._guests_by_id.values()),
            "reservations": list(self.reservations.values()),
        }
        with open(self.data_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)