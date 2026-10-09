import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  /** What to show instead of the crashed subtree. */
  fallback: (error: Error, reset: () => void) => ReactNode;
  children: ReactNode;
}

interface State {
  error: Error | null;
}

/** A render error in one widget (the chart, a page) must never take the
 *  whole shell down: the sidebar and the other pages keep working. */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("ttrronev: render error", error, info.componentStack);
  }

  render(): ReactNode {
    if (this.state.error) return this.props.fallback(this.state.error, () => this.setState({ error: null }));
    return this.props.children;
  }
}
