import { Route, Routes } from "react-router-dom";
import { Toaster } from "@/components/ui/sonner";
import Layout from "./components/Layout";
import Dashboard from "./pages/Dashboard";
import Jobs from "./pages/Jobs";
import Matches from "./pages/Matches";
import Applications from "./pages/Application";
import Intervention from "./pages/Interventation";
import Pipeline from "./pages/Pipeline";
import Summary from "./pages/Summary";
import Setup from "./pages/Setup";
import Settings from "./pages/Settings";

export default function App() {
  return (
    <>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<Dashboard />} />
          <Route path="jobs" element={<Jobs />} />
          <Route path="matches" element={<Matches />} />
          <Route path="applications" element={<Applications />} />
          <Route path="intervention" element={<Intervention />} />
          <Route path="pipeline" element={<Pipeline />} />
          <Route path="summary" element={<Summary />} />
          <Route path="setup" element={<Setup />} />
          <Route path="settings" element={<Settings />} />
        </Route>
      </Routes>
      <Toaster richColors position="bottom-right" />
    </>
  );
}
