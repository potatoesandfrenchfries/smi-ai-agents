import { useCallback, useState } from "react";
import { startTrip } from "./api/client";
import { AppShell, type MobileTab } from "./components/layout/AppShell";
import { Sidebar } from "./components/layout/Sidebar";
import { ChatPanel } from "./components/conversation/ChatPanel";
import { ItineraryPanel } from "./components/itinerary/ItineraryPanel";
import { useConversations } from "./hooks/useConversations";
import { useItinerary } from "./hooks/useItinerary";

export default function App() {
  const { conversations, activeId, setActiveId, createNew } = useConversations();
  const [mobileTab, setMobileTab] = useState<MobileTab>("chat");
  const [activePlanId, setActivePlanId] = useState<string | null>(null);
  const [startingTrip, setStartingTrip] = useState(false);

  const itineraryState = useItinerary(activePlanId);

  const handleStartTrip = useCallback(async (rawGoal: string) => {
    setStartingTrip(true);
    try {
      const { planId } = await startTrip(rawGoal);
      setActivePlanId(planId);
    } finally {
      setStartingTrip(false);
    }
  }, []);

  const handleSelectConversation = (id: string) => {
    setActiveId(id);
    setMobileTab("chat");
  };

  return (
    <AppShell
      mobileTab={mobileTab}
      onMobileTabChange={setMobileTab}
      sidebar={
        <Sidebar
          conversations={conversations}
          activeId={activeId}
          onSelect={handleSelectConversation}
          onCreate={createNew}
        />
      }
      conversation={<ChatPanel conversationId={activeId} />}
      itinerary={
        <ItineraryPanel
          itinerary={itineraryState.itinerary}
          loading={itineraryState.loading}
          actionState={itineraryState.actionState}
          onConfirm={itineraryState.confirm}
          onReject={itineraryState.reject}
          onRequestChange={(segmentId) =>
            itineraryState.requestChanges("segment", segmentId, "Requested from console")
          }
          onRate={(segment, rating) => {
            if (!segment.candidateId) return;
            void itineraryState.rateSegment(segment.kind, segment.candidateId, rating);
          }}
          onStartTrip={handleStartTrip}
          startingTrip={startingTrip}
        />
      }
    />
  );
}
