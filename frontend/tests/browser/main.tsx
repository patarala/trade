import { useSyncExternalStore } from "react";
import { createRoot } from "react-dom/client";

import { RegimexAnalysis } from "../../src/RegimexAnalysis";
import { currentAnalysisLink, parseAnalysisLink, subscribeAnalysisLink } from "../../src/regimexLink";
import "../../src/index.css";
import "../../src/App.css";

// Exercise the production component without a simulator, live jobs or credentials.
export function BrowserFixture() {
  const hash = useSyncExternalStore(subscribeAnalysisLink, currentAnalysisLink);
  const { link, error } = parseAnalysisLink(hash);
  if (error) return <p role="alert">{error}</p>;
  return <main style={{ maxWidth: 1100, padding: 16, margin: "0 auto" }}>
    <RegimexAnalysis simulationId={link?.simulationId ?? 3} runId={link?.runId ?? 7}
      requestedAnalysisId={link?.analysisId} requestedInstrumentId={link?.instrumentId}
      token="browser-fixture-token" />
  </main>;
}

createRoot(document.getElementById("root")!).render(<BrowserFixture />);
