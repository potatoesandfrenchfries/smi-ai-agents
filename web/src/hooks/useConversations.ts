import { useCallback, useEffect, useState } from "react";
import { createConversation, listConversations } from "../api/client";
import type { ConversationListItem } from "../api/types";

/** Multi-conversation list + selection + creation. */
export function useConversations() {
  const [conversations, setConversations] = useState<ConversationListItem[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    setLoading(true);
    const res = await listConversations();
    setConversations(res.data);
    setLoading(false);
    return res.data;
  }, []);

  useEffect(() => {
    refresh().then((data) => {
      if (data.length > 0) setActiveId((current) => current ?? data[0]!.id);
    });
  }, [refresh]);

  const createNew = useCallback(async () => {
    const conv = await createConversation();
    setConversations((prev) => [conv, ...prev]);
    setActiveId(conv.id);
    return conv;
  }, []);

  return { conversations, activeId, setActiveId, loading, createNew, refresh };
}
