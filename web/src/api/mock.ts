import type {
  ChatStreamEvent,
  ConversationListItem,
  ConversationMessageItem,
  EditLogEntry,
  ItineraryView,
  Paginated,
} from "./types";

/**
 * Local stand-ins used only when the gateway is unreachable (no backend
 * running yet, or a laptop demo with no infrastructure). `client.ts` always
 * attempts the real gateway first; this module exists so the interface is
 * reviewable end-to-end without Postgres/Redis/Temporal/FastAPI all running.
 * Nothing here is imported by the gateway or the Python services — it is
 * presentation-layer fallback data only.
 */

let mockConversationSeq = 0;

function newMockConversation(): ConversationListItem {
  mockConversationSeq += 1;
  return {
    id: `mock-conv-${mockConversationSeq}`,
    displayId: `CONV-${String(mockConversationSeq).padStart(4, "0")}`,
    title: null,
    status: "ACTIVE",
    agentName: "uc02_conversation",
    context: null,
    ceiling: {
      maxMessages: 100,
      maxTokens: 200000,
      messagesUsed: 0,
      tokensUsed: 0,
      messagesRemaining: 100,
      tokensRemaining: 200000,
      percentUsed: 0,
      status: "ok",
    },
    lastMessage: null,
    messageCount: 0,
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
  };
}

const mockConversations: ConversationListItem[] = [
  {
    ...newMockConversation(),
    title: "Edinburgh site visit, mid-August",
    lastMessage: {
      role: "assistant",
      preview: "I've drafted a 3-day itinerary within your £900 budget — ready to review.",
      at: new Date(Date.now() - 1000 * 60 * 40).toISOString(),
    },
    messageCount: 6,
    ceiling: {
      maxMessages: 100,
      maxTokens: 200000,
      messagesUsed: 6,
      tokensUsed: 4200,
      messagesRemaining: 94,
      tokensRemaining: 195800,
      percentUsed: 6,
      status: "ok",
    },
  },
  {
    ...newMockConversation(),
    title: "Client dinner, Lyon — next Thursday",
    lastMessage: {
      role: "user",
      preview: "Can we swap the hotel for something closer to the venue?",
      at: new Date(Date.now() - 1000 * 60 * 60 * 5).toISOString(),
    },
    messageCount: 11,
  },
];

const mockMessagesByConversation = new Map<string, ConversationMessageItem[]>();

export function mockListConversations(): Paginated<ConversationListItem> {
  return {
    data: mockConversations,
    pagination: { total: mockConversations.length, offset: 0, limit: 20 },
  };
}

export function mockCreateConversation(): ConversationListItem {
  const conv = newMockConversation();
  conv.title = "New planning conversation";
  mockConversations.unshift(conv);
  mockMessagesByConversation.set(conv.id, []);
  return conv;
}

export function mockGetMessages(conversationId: string): Paginated<ConversationMessageItem> {
  const data = mockMessagesByConversation.get(conversationId) ?? [];
  return { data, pagination: { total: data.length, offset: 0, limit: 50 } };
}

const MOCK_STEP_PLAN: Array<{ node: string; message: string }> = [
  { node: "parse_intent", message: "Reading trip goal and extracting constraints" },
  { node: "search_business_specialists", message: "Dispatching flight, hotel, and dining specialists" },
  { node: "merge_results", message: "Reconciling candidates into one itinerary" },
  { node: "policy_check", message: "Checking corporate policy compliance" },
  { node: "compile_itinerary", message: "Compiling the versioned itinerary" },
  { node: "reflect_itinerary", message: "Reviewing itinerary quality" },
];

const MOCK_REPLY =
  "I've put together a 3-day Edinburgh itinerary that stays within your £900 budget. " +
  "The direct flight keeps the schedule tight for your 9am meeting, and I've flagged one " +
  "hotel rate that's slightly above the standard corporate cap — it still clears policy " +
  "since it's within the discretionary allowance for city-centre stays.";

/** Emits a chat stream shaped exactly like the real SSE contract, on a timer. */
export function mockStreamChat(
  conversationId: string,
  userMessage: string,
  onEvent: (event: ChatStreamEvent) => void,
  signal?: AbortSignal
): Promise<void> {
  const messages = mockMessagesByConversation.get(conversationId) ?? [];
  const nextSeq = messages.length + 1;
  messages.push({
    id: `mock-msg-${nextSeq}`,
    seq: nextSeq,
    role: "user",
    content: userMessage,
    tokens: Math.ceil(userMessage.length / 4),
    createdAt: new Date().toISOString(),
  });
  mockMessagesByConversation.set(conversationId, messages);

  return new Promise((resolve) => {
    let i = 0;
    const emitStep = () => {
      if (signal?.aborted) return resolve();
      if (i >= MOCK_STEP_PLAN.length) {
        onEvent({ type: "token", data: MOCK_REPLY });
        messages.push({
          id: `mock-msg-${nextSeq + 1}`,
          seq: nextSeq + 1,
          role: "assistant",
          content: MOCK_REPLY,
          tokens: Math.ceil(MOCK_REPLY.length / 4),
          createdAt: new Date().toISOString(),
        });
        onEvent({
          type: "meta",
          data: {
            tokensThisTurn: 640,
            ceiling: {
              maxMessages: 100,
              maxTokens: 200000,
              messagesUsed: nextSeq + 1,
              tokensUsed: 4840,
              messagesRemaining: 100 - (nextSeq + 1),
              tokensRemaining: 195160,
              percentUsed: Math.round(((nextSeq + 1) / 100) * 100),
              status: "ok",
            },
          },
        });
        onEvent({ type: "done" });
        resolve();
        return;
      }
      const step = MOCK_STEP_PLAN[i]!;
      onEvent({
        type: "step",
        data: {
          step_id: `${step.node}-${String(i + 1).padStart(3, "0")}`,
          node: step.node,
          status: "in_progress",
          message: step.message,
          detail: null,
          ts: new Date().toISOString(),
          seq: i + 1,
        },
      });
      window.setTimeout(() => {
        onEvent({
          type: "step",
          data: {
            step_id: `${step.node}-${String(i + 1).padStart(3, "0")}`,
            node: step.node,
            status: "completed",
            message: step.message,
            detail: null,
            ts: new Date().toISOString(),
            seq: i + 1,
          },
        });
        i += 1;
        window.setTimeout(emitStep, 260);
      }, 420);
    };
    emitStep();
  });
}

export function mockItinerary(planId: string): ItineraryView {
  return {
    planId,
    status: "awaiting_review",
    version: 1,
    totalAmount: 842.5,
    currency: "GBP",
    assumptions: [
      "Assumed a direct flight is preferred given the 9am meeting the next day",
      "Assumed one traveler, standard cabin",
    ],
    policyStatus: "compliant",
    requiresApproval: false,
    segments: [
      {
        id: "SEG-FLT-mock0001",
        candidateId: "FLT-mock-1",
        kind: "flight",
        title: "British Airways",
        subtitle: "LHR → EDI on 2026-08-14",
        amount: 184,
        currency: "GBP",
        policyDecision: "not_applicable",
        snapshotId: "snap-8f21a3",
        reason: "Cheapest of 4 option(s) considered (£184.00)",
        handoffLink: "https://book.smartinerary.io/flight/FLT-mock-1",
        rankArm: "bandit",
      },
      {
        id: "SEG-HTL-mock0001",
        candidateId: "HTL-mock-1",
        kind: "hotel",
        title: "The Balmoral",
        subtitle: "The Balmoral — 2 nights",
        amount: 498,
        currency: "GBP",
        policyDecision: "not_applicable",
        snapshotId: "snap-2c9e10",
        reason: "Best match for your usual preferences, weighted most on rating (of 3 option(s) considered)",
        handoffLink: "https://book.smartinerary.io/hotel/HTL-mock-1",
        rankArm: "bandit",
      },
      {
        id: "dining-RST-mock-1",
        candidateId: "RST-mock-1",
        kind: "dining",
        title: "Timberyard",
        subtitle: "Scottish cuisine",
        amount: 65,
        currency: "GBP",
        policyDecision: "not_applicable",
        snapshotId: null,
        reason: "Highest-rated of 5 option(s) considered (9.1/10)",
        handoffLink: null,
        rankArm: "primitive",
      },
    ],
  };
}

export function mockEditLog(): EditLogEntry[] {
  return [{ at: new Date(Date.now() - 1000 * 60 * 30).toISOString(), summary: "Itinerary drafted" }];
}
