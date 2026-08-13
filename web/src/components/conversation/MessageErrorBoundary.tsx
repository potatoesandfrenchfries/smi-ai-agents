import { Component, type ReactNode } from "react";
import styles from "./MessageList.module.css";

/**
 * Scoped to one message's blocks, not the whole app — a render bug in a
 * single malformed message (bad LLM output, legacy persisted data predating
 * a schema fix) degrades to an inline notice instead of unmounting the
 * entire conversation view. React only offers this via a class component.
 */
export class MessageErrorBoundary extends Component<
  { children: ReactNode },
  { hasError: boolean }
> {
  state = { hasError: false };

  static getDerivedStateFromError() {
    return { hasError: true };
  }

  componentDidCatch(error: unknown) {
    console.error("[MessageErrorBoundary] failed to render message:", error);
  }

  render() {
    if (this.state.hasError) {
      return <p className={styles.text}>This message couldn't be displayed.</p>;
    }
    return this.props.children;
  }
}
