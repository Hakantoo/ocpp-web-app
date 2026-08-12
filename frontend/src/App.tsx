import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { Layout } from "./components/Layout";
import { ChargerDetail } from "./pages/ChargerDetail";
import { Chargers } from "./pages/Chargers";
import { Directory } from "./pages/Directory";
import { Logs } from "./pages/Logs";
import { Overview } from "./pages/Overview";
import { SessionDetail } from "./pages/SessionDetail";
import { Sessions } from "./pages/Sessions";
import { Simulator } from "./pages/Simulator";
import { Tests } from "./pages/Tests";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // The WebSocket feed drives most refreshes; these are the fallback for
      // when it drops, and they stop a stale screen from looking authoritative.
      staleTime: 2000,
      refetchOnWindowFocus: true,
      retry: 1,
    },
  },
});

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<Overview />} />
            <Route path="chargers" element={<Chargers />} />
            <Route path="chargers/:identity" element={<ChargerDetail />} />
            <Route path="sessions" element={<Sessions />} />
            <Route path="sessions/:id" element={<SessionDetail />} />
            <Route path="directory" element={<Directory />} />
            <Route path="logs" element={<Logs />} />
            <Route path="simulator" element={<Simulator />} />
            <Route path="tests" element={<Tests />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
