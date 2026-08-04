import type { ReactNode } from "react";
import styles from "./AppShell.module.css";

export type MobileTab = "conversations" | "chat" | "itinerary";

export function AppShell({
  sidebar,
  conversation,
  itinerary,
  mobileTab,
  onMobileTabChange,
}: {
  sidebar: ReactNode;
  conversation: ReactNode;
  itinerary: ReactNode;
  mobileTab: MobileTab;
  onMobileTabChange: (tab: MobileTab) => void;
}) {
  return (
    <div className={styles.shell}>
      <div className={styles.slot} data-slot="sidebar" data-mobile-active={mobileTab === "conversations"}>
        {sidebar}
      </div>
      <div className={styles.slot} data-slot="conversation" data-mobile-active={mobileTab === "chat"}>
        {conversation}
      </div>
      <div className={styles.slot} data-slot="itinerary" data-mobile-active={mobileTab === "itinerary"}>
        {itinerary}
      </div>

      <div className={styles.mobileTabs} role="tablist" aria-label="Panels">
        {(
          [
            ["conversations", "Trips"],
            ["chat", "Planner"],
            ["itinerary", "Itinerary"],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={mobileTab === key}
            className={styles.mobileTab}
            data-active={mobileTab === key}
            onClick={() => onMobileTabChange(key)}
          >
            {label}
          </button>
        ))}
      </div>
    </div>
  );
}
