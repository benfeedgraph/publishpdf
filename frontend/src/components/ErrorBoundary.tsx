import { Component, type ErrorInfo, type ReactNode } from "react";

/** Keeps one broken view from blanking the whole dashboard: shows what failed and a way back,
 *  instead of React unmounting everything. `resetKey` clears the error when the route changes. */
export default class ErrorBoundary extends Component<{ children: ReactNode; resetKey?: string }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("View crashed:", error, info.componentStack);
  }

  componentDidUpdate(prev: { resetKey?: string }) {
    if (this.state.error && prev.resetKey !== this.props.resetKey) this.setState({ error: null });
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="card" role="alert">
        <h2 style={{ marginTop: 0 }}>This part of the page hit a problem</h2>
        <p className="muted">Your work is saved — every decision is stored as soon as you make it. Try again, or reload the page.</p>
        <p className="small muted"><code>{this.state.error.message}</code></p>
        <div className="btn-row">
          <button type="button" className="primary" onClick={() => this.setState({ error: null })}>Try again</button>
          <button type="button" className="secondary" onClick={() => window.location.reload()}>Reload the page</button>
        </div>
      </div>
    );
  }
}
