import { useCallback, useEffect, useRef, useState } from "react";
import { getMessages, streamChat } from "../api/client";
import type {
  ConversationCeiling,
  ConversationMessageItem,
  StepEventData,
  StructuredResponse,
} from "../api/types";

interface ChatState {
  messages: ConversationMessageItem[];
  isStreaming: boolean;
  steps: StepEventData[];
  streamingText: string;
  structuredResponse: StructuredResponse | null;
  ceiling: ConversationCeiling | null;
  error: string | null;
}

const EMPTY_STATE: ChatState = {
  messages: [],
  isStreaming: false,
  steps: [],
  streamingText: "",
  structuredResponse: null,
  ceiling: null,
  error: null,
};

/** Owns message history + live SSE turn state for one conversation. */
export function useChat(conversationId: string | null) {
  const [state, setState] = useState<ChatState>(EMPTY_STATE);
  const abortRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    abortRef.current?.();
    setState(EMPTY_STATE);
    if (!conversationId) return;

    let cancelled = false;
    getMessages(conversationId).then((res) => {
      if (!cancelled) setState((s) => ({ ...s, messages: res.data }));
    });
    return () => {
      cancelled = true;
    };
  }, [conversationId]);

  const send = useCallback(
    (message: string) => {
      if (!conversationId) return;

      const optimisticUser: ConversationMessageItem = {
        id: `local-${Date.now()}`,
        seq: state.messages.length + 1,
        role: "user",
        content: message,
        tokens: null,
        createdAt: new Date().toISOString(),
      };

      setState((s) => ({
        ...s,
        messages: [...s.messages, optimisticUser],
        isStreaming: true,
        steps: [],
        streamingText: "",
        structuredResponse: null,
        error: null,
      }));

      const abort = streamChat(conversationId, message, (event) => {
        setState((s) => {
          switch (event.type) {
            case "step": {
              const idx = s.steps.findIndex((st) => st.step_id === event.data.step_id);
              const steps =
                idx === -1
                  ? [...s.steps, event.data]
                  : s.steps.map((st, i) => (i === idx ? event.data : st));
              return { ...s, steps };
            }
            case "token":
              return { ...s, streamingText: s.streamingText + event.data };
            case "response":
              return { ...s, structuredResponse: event.data };
            case "ceiling":
              return { ...s, ceiling: event.data.ceiling };
            case "warning":
              return { ...s, ceiling: event.data.ceiling };
            case "meta":
              return { ...s, ceiling: event.data.ceiling ?? s.ceiling };
            case "error":
              return { ...s, error: event.data, isStreaming: false };
            case "done": {
              const assistantMessage: ConversationMessageItem = {
                id: `local-assistant-${Date.now()}`,
                seq: s.messages.length + 1,
                role: "assistant",
                content: s.streamingText,
                tokens: null,
                createdAt: new Date().toISOString(),
              };
              return {
                ...s,
                messages: s.streamingText ? [...s.messages, assistantMessage] : s.messages,
                isStreaming: false,
              };
            }
            default:
              return s;
          }
        });
      });

      abortRef.current = abort;
    },
    [conversationId, state.messages.length]
  );

  useEffect(() => () => abortRef.current?.(), []);

  return { ...state, send };
}
