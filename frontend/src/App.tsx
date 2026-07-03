import { Route, Routes } from "react-router-dom";
import { Toaster } from "@/components/ui/sonner";
import Layout from "./components/Layout";
import Dashboard from "./pages/Dashboard";
import Jobs from "./pages/Jobs";
import Setup from "./pages/Setup";
import Placeholder from "./pages/Placeholder";

export default function App() {
  return (
    <>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<Dashboard />} />
          <Route path="jobs" element={<Jobs />} />
          <Route path="setup" element={<Setup />} />
        <Route
          path="matches"
          element={<Placeholder title="Matches" note="Score-ranked matches with click-to-apply." />}
        />
        <Route
          path="applications"
          element={<Placeholder title="Applications" note="Every application, its status and artifacts." />}
        />
        <Route
          path="intervention"
          element={<Placeholder title="Intervention" note="Assisted hand-off for captcha-gated applications." />}
        />
        <Route
          path="pipeline"
          element={<Placeholder title="Pipeline" note="Run a full cycle and watch live logs." />}
        />
        <Route
          path="summary"
          element={<Placeholder title="Summary" note="Daily run summaries and email digests." />}
        />
        <Route
          path="settings"
          element={<Placeholder title="Settings" note="Safety switches, caps and thresholds." />}
        />
        </Route>
      </Routes>
      <Toaster richColors position="bottom-right" />
    </>
  );
}
