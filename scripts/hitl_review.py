"""Shared interactive HITL (human-in-the-loop) review loop for the CLIs.

Both cli.py and nlcli.py submit the same ItineraryWorkflow and need the same
review/edit/confirm interaction once it's running — this module is that
shared loop so it isn't duplicated (and drifted) between the two scripts.
"""

from __future__ import annotations

import asyncio

from smi_agent.activities.itinerary_workflow import ItineraryEditRequest, ItineraryWorkflow

_SECTION_KEYS = {"f": "flight", "h": "hotel", "r": "restaurant", "a": "attraction"}


def describe_candidate(section: str, c: dict) -> str:
    if section == "flight":
        return f"{c.get('airline')} {c.get('origin')}→{c.get('destination')} dep {c.get('departure')} — £{c.get('price_gbp')}"
    if section == "hotel":
        return f"{c.get('name')} ({c.get('stars')}★) — £{c.get('total_price_gbp')} total, {c.get('nights')} nights"
    if section == "restaurant":
        return f"{c.get('name')} — {c.get('cuisine')}, {c.get('price_band')} (~£{c.get('avg_spend_per_person_gbp')}/person)"
    if section == "attraction":
        fee = c.get("entry_fee_gbp")
        return f"{c.get('name')} — {c.get('category')} ({'£' + str(fee) if fee is not None else 'price TBC'})"
    return str(c)


def print_itinerary(it) -> None:
    """Works for both ItineraryResult (mid-review queries) and
    ItineraryWorkflowResult (the final result) — same field names throughout.
    """
    print()
    print(f"Status      : {it.status}")
    print(f"Policy      : {it.policy_status}")
    if it.total_cost_gbp:
        print(f"Total cost  : £{it.total_cost_gbp:.2f}")

    print()
    print("Segments:")
    for seg in it.segments:
        price = f"  £{seg['price_gbp']:.2f}" if seg.get("price_gbp") else ""
        print(f"  [{seg['type'].upper()}] {seg['summary']}{price}")
        print(f"           Provider : {seg['provider']}")

    if it.dining_options:
        print()
        print("Dining options:")
        for r in it.dining_options[:3]:
            print(f"  {r.get('name')} — {r.get('cuisine')} — {r.get('price_band')}")

    if it.policy_status == "breach" and it.budget_alternatives:
        print()
        print("Over budget — cheaper alternatives:")
        for i, alt in enumerate(it.budget_alternatives, start=1):
            fit = "within budget" if alt.get("within_budget") else "still over budget"
            print(f"  {i}. {alt['label']} — £{alt['total_cost_gbp']:.2f} (saves £{alt['savings_gbp']:.2f}, {fit})")


async def wait_for_first_itinerary(handle, timeout_seconds: int = 30):
    """Poll current_itinerary() until the workflow has finished its first
    generation and reached the review wait, or give up after timeout_seconds.
    """
    for _ in range(timeout_seconds * 2):
        itinerary = await handle.query(ItineraryWorkflow.current_itinerary)
        if itinerary is not None:
            return itinerary
        await asyncio.sleep(0.5)
    return None


async def run_review_loop(handle, itinerary):
    """Drive the interactive review/edit/confirm loop against a running
    ItineraryWorkflow, then return its final ItineraryWorkflowResult.

    Edits reorder just the requested section's already-fetched candidates
    (or override the budget) and re-run only merge/policy/compile inside the
    workflow — flights/hotels/restaurants/attractions are never re-searched.
    """
    print("=== ITINERARY (draft — review before confirming) ===")
    while True:
        print_itinerary(itinerary)

        print()
        print("What would you like to do?")
        print("  [c] Confirm this itinerary")
        print("  [f] Change flight")
        print("  [h] Change hotel")
        print("  [r] Change restaurant")
        print("  [a] Change attraction")
        print("  [b] Change budget")
        print("  [x] Reject / cancel")
        action = input("> ").strip().lower()

        if action == "c":
            await handle.signal(ItineraryWorkflow.confirm)
            break
        if action == "x":
            await handle.signal(ItineraryWorkflow.reject)
            break

        if action == "b":
            raw_budget = input("  New budget (£): ").strip()
            try:
                new_budget = float(raw_budget)
            except ValueError:
                print("  Not a number — try again.")
                continue
            await handle.signal(
                ItineraryWorkflow.request_changes,
                ItineraryEditRequest(section="budget", budget_gbp=new_budget),
            )
        elif action in _SECTION_KEYS:
            section = _SECTION_KEYS[action]
            options = await handle.query(ItineraryWorkflow.available_options)
            candidates = options.get(section, [])
            if not candidates:
                print(f"  No {section} options available for this trip.")
                continue
            print(f"\n  {section.title()} options:")
            for i, c in enumerate(candidates, start=1):
                print(f"    {i}. {describe_candidate(section, c)}")
            choice = input(f"  Pick a number [1-{len(candidates)}], or Enter to cancel: ").strip()
            if not choice:
                continue
            try:
                chosen = candidates[int(choice) - 1]
            except (ValueError, IndexError):
                print("  Invalid choice — try again.")
                continue
            await handle.signal(
                ItineraryWorkflow.request_changes,
                ItineraryEditRequest(section=section, candidate_id=chosen["id"]),
            )
        else:
            print("  Unrecognised option.")
            continue

        print("  Applying edit ...")
        await asyncio.sleep(1.5)
        updated = await handle.query(ItineraryWorkflow.current_itinerary)
        if updated is not None:
            itinerary = updated
        print("=== ITINERARY (updated — review before confirming) ===")

    final = await handle.result()
    print()
    print("=== FINAL RESULT ===")
    print(f"Status: {final.status}")

    if final.status == "confirmed":
        print_itinerary(final)
        print()
        print("Confirmed — awaiting handoff to booking (out of scope for this prototype).")
    elif final.status == "rejected":
        print("You rejected this itinerary. No booking will be made.")
    elif final.status == "review_timed_out":
        print("No response was received within the review window.")

    if final.errors:
        print()
        print("Errors:", final.errors)

    return final
