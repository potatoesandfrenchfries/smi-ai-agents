"""Is the personalized ranking arm actually working?

Reads directly from FileRankingStore's event log — no Temporal server
required, since feedback events are already persisted to disk by
record_ranking_feedback_activity.

Usage:
    PYTHONPATH=src python3 scripts/ranking_metrics_cli.py
    PYTHONPATH=src python3 scripts/ranking_metrics_cli.py --bucket-size 3
"""

import asyncio
import sys

from smi_agent.providers.ranking import FileRankingStore, summarize


async def show_metrics(bucket_size: int) -> None:
    events = await FileRankingStore().list_all_events()
    if not events:
        print("No ranking feedback events recorded yet.")
        return

    result = summarize(events, bucket_size=bucket_size)
    print(f"Total events: {result['total_events']}\n")

    print("Acceptance rate by arm (between-group: is bandit beating primitive overall?):")
    for arm, s in sorted(result["arm_summary"].items()):
        print(
            f"  {arm:10s} accepted={s.accepted:<4d} rejected={s.rejected:<4d} "
            f"total={s.total:<4d} rate={s.acceptance_rate:.1%}"
        )

    print(
        "\nRelevance trend (within-arm: does acceptance rate rise as a user "
        "accumulates more feedback?):"
    )
    for bucket in result["relevance_trend"]:
        lo, hi = bucket.position_range
        print(
            f"  events {lo + 1:>3d}-{hi + 1:<3d} [{bucket.arm:9s}] "
            f"accepted={bucket.accepted:<4d}/{bucket.total:<4d} rate={bucket.acceptance_rate:.1%}"
        )


def main() -> None:
    args = sys.argv[1:]
    bucket_size = 5
    if "--bucket-size" in args:
        idx = args.index("--bucket-size")
        if idx + 1 >= len(args):
            print("--bucket-size requires a value: scripts/ranking_metrics_cli.py --bucket-size 3")
            sys.exit(1)
        bucket_size = int(args[idx + 1])

    asyncio.run(show_metrics(bucket_size))


if __name__ == "__main__":
    main()
