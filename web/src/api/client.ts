import { authHeaders, GATEWAY_URL } from "./config";
import { consumeChatStream } from "./sse";
import * as mock from "./mock";
import type {
  ChatStreamEvent,
  ConversationListItem,
  ConversationMessageItem,
  EditLogEntry,
  ItineraryView,
  Paginated,
} from "./types";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${GATEWAY_URL}${path}`, { headers: authHeaders() });
  if (!res.ok) throw new Error(`GET ${path} -> ${res.status}`);
  return (await res.json()) as T;
}

async function send<T>(path: string, method: string, body?: unknown): Promise<T> {
  const res = await fetch(`${GATEWAY_URL}${path}`, {
    method,
    headers: { ...authHeaders(), "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`${method} ${path} -> ${res.status}`);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// ── Conversations ──────────────────────────────────────────────────────────

export async function listConversations(): Promise<Paginated<ConversationListItem>> {
  try {
    return await get<Paginated<ConversationListItem>>("/api/v1/conversations");
  } catch {
    return mock.mockListConversations();
  }
}

export async function createConversation(): Promise<ConversationListItem> {
  try {
    return await send<ConversationListItem>("/api/v1/conversations", "POST", {
      agentName: "uc02_conversation",
    });
  } catch {
    return mock.mockCreateConversation();
  }
}

export async function getMessages(
  conversationId: string
): Promise<Paginated<ConversationMessageItem>> {
  if (conversationId.startsWith("mock-")) {
    return mock.mockGetMessages(conversationId);
  }
  try {
    return await get<Paginated<ConversationMessageItem>>(
      `/api/v1/conversations/${conversationId}/messages`
    );
  } catch {
    return mock.mockGetMessages(conversationId);
  }
}

/**
 * Streams one chat turn. Returns an abort function the caller invokes on
 * unmount / navigation so an in-flight generation is cancelled cleanly.
 */
export function streamChat(
  conversationId: string,
  message: string,
  onEvent: (event: ChatStreamEvent) => void
): () => void {
  const controller = new AbortController();

  const run = async () => {
    if (conversationId.startsWith("mock-")) {
      await mock.mockStreamChat(conversationId, message, onEvent, controller.signal);
      return;
    }
    try {
      const res = await fetch(
        `${GATEWAY_URL}/api/v1/conversations/${conversationId}/chat`,
        {
          method: "POST",
          headers: { ...authHeaders(), "Content-Type": "application/json" },
          body: JSON.stringify({ message }),
          signal: controller.signal,
        }
      );
      if (!res.ok) throw new Error(`chat -> ${res.status}`);
      await consumeChatStream(res, onEvent, controller.signal);
    } catch (err) {
      if (controller.signal.aborted) return;
      await mock.mockStreamChat(conversationId, message, onEvent, controller.signal);
    }
  };

  void run();
  return () => controller.abort();
}

// ── Itinerary / HITL (gateway trips surface) ───────────────────────────────

export async function startTrip(rawGoal: string): Promise<{ planId: string }> {
  try {
    return await send<{ planId: string }>("/api/v1/trips", "POST", { rawGoal });
  } catch {
    return { planId: `mock-plan-${Date.now()}` };
  }
}

export async function getItinerary(planId: string): Promise<ItineraryView> {
  if (planId.startsWith("mock-")) return mock.mockItinerary(planId);
  try {
    const res = await get<{ itinerary: ItineraryView }>(`/api/v1/trips/${planId}`);
    return res.itinerary;
  } catch {
    return mock.mockItinerary(planId);
  }
}

export async function getEditLog(planId: string): Promise<EditLogEntry[]> {
  if (planId.startsWith("mock-")) return mock.mockEditLog();
  try {
    const res = await get<{ editLog: EditLogEntry[] }>(`/api/v1/trips/${planId}/edit-log`);
    return res.editLog;
  } catch {
    return mock.mockEditLog();
  }
}

export async function confirmTrip(planId: string): Promise<void> {
  if (planId.startsWith("mock-")) return;
  await send(`/api/v1/trips/${planId}/confirm`, "POST");
}

export async function rejectTrip(planId: string): Promise<void> {
  if (planId.startsWith("mock-")) return;
  await send(`/api/v1/trips/${planId}/reject`, "POST");
}

export async function requestTripChanges(
  planId: string,
  edit: { section: string; candidateId: string; note?: string }
): Promise<void> {
  if (planId.startsWith("mock-")) return;
  await send(`/api/v1/trips/${planId}/changes`, "POST", edit);
}
