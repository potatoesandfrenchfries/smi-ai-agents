import { useCallback, useEffect, useState } from "react";
import { confirmTrip, getItinerary, rejectTrip, requestTripChanges } from "../api/client";
import type { ItineraryView } from "../api/types";

export function useItinerary(planId: string | null) {
  const [itinerary, setItinerary] = useState<ItineraryView | null>(null);
  const [loading, setLoading] = useState(false);
  const [actionState, setActionState] = useState<"idle" | "confirming" | "rejecting">("idle");

  const refresh = useCallback(async () => {
    if (!planId) {
      setItinerary(null);
      return;
    }
    setLoading(true);
    try {
      setItinerary(await getItinerary(planId));
    } finally {
      setLoading(false);
    }
  }, [planId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const confirm = useCallback(async () => {
    if (!planId) return;
    setActionState("confirming");
    await confirmTrip(planId);
    setItinerary((prev) => (prev ? { ...prev, status: "confirmed" } : prev));
    setActionState("idle");
  }, [planId]);

  const reject = useCallback(async () => {
    if (!planId) return;
    setActionState("rejecting");
    await rejectTrip(planId);
    setItinerary((prev) => (prev ? { ...prev, status: "rejected" } : prev));
    setActionState("idle");
  }, [planId]);

  const requestChanges = useCallback(
    async (section: string, candidateId: string, note?: string) => {
      if (!planId) return;
      await requestTripChanges(planId, { section, candidateId, note });
      await refresh();
    },
    [planId, refresh]
  );

  return { itinerary, loading, actionState, confirm, reject, requestChanges, refresh };
}
