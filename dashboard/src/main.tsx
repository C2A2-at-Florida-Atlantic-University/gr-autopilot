import { Component, StrictMode, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'

/** Catch render errors (e.g. a partial/legacy telemetry snapshot the UI didn't expect) and show a
 *  message instead of a blank white screen — the dashboard keeps polling and recovers on a good frame. */
class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null }
  static getDerivedStateFromError(error: Error) {
    return { error }
  }
  render() {
    if (this.state.error) {
      return (
        <div style={{ padding: 24, fontFamily: 'ui-monospace, monospace', color: 'var(--bad)' }}>
          <h2>Dashboard hit a render error</h2>
          <p style={{ color: 'var(--muted-foreground)' }}>
            Likely a malformed or partial telemetry snapshot. The page will recover on the next good
            frame — or reload.
          </p>
          <pre style={{ whiteSpace: 'pre-wrap', color: 'var(--muted-foreground)' }}>{String(this.state.error)}</pre>
          <button onClick={() => this.setState({ error: null })}>Retry</button>
        </div>
      )
    }
    return this.props.children
  }
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
)
