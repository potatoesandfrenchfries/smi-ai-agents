"""List or show previously confirmed trips for a user.

Reads directly from FileTripStore — no Temporal server required, since a
confirmed trip is already persisted to disk by persist_trip_activity.

Usage:
    PYTHONPATH=src python3 scripts/trips_cli.py list <user_id>
    PYTHONPATH=src python3 scripts/trips_cli.py show <user_id> <trip_id>
"""

import asyncio
import sys

from smi_agent.trip_store import FileTripStore


async def list_trips(user_id: str) -> None:
    trips = await FileTripStore().list_trips(user_id)
    if not trips:
        print(f"No trips found for user '{user_id}'.")
        return
    for trip in trips:
        print(
            f"{trip.trip_id}  {trip.origin} → {trip.destination}  "
            f"{trip.check_in} to {trip.check_out}  status={trip.status}  "
            f"£{trip.total_cost_gbp}  ({trip.created_at})"
        )


async def show_trip(user_id: str, trip_id: str) -> None:
    trip = await FileTripStore().get_trip(user_id, trip_id)
    if trip is None:
        print(f"No trip '{trip_id}' found for user '{user_id}'.")
        return

    print(f"Trip     : {trip.trip_id}")
    print(f"Status   : {trip.status}")
    print(f"Route    : {trip.origin} → {trip.destination}")
    print(f"Dates    : {trip.check_in} to {trip.check_out}")
    print(f"Cost     : £{trip.total_cost_gbp}")
    print("Segments:")
    for seg in trip.segments:
        print(f"  [{seg.get('type')}] {seg.get('summary')} — {seg.get('provider')}")
    print(f"Dining   : {len(trip.dining_options)} options")
    if trip.assumptions:
        print(f"Assumptions: {trip.assumptions}")


def main() -> None:
    args = sys.argv[1:]
    if len(args) < 2 or args[0] not in ("list", "show"):
        print(__doc__)
        sys.exit(1)

    command, user_id, *rest = args
    if command == "list":
        asyncio.run(list_trips(user_id))
    else:
        if not rest:
            print("show requires a trip_id: scripts/trips_cli.py show <user_id> <trip_id>")
            sys.exit(1)
        asyncio.run(show_trip(user_id, rest[0]))


if __name__ == "__main__":
    main()
