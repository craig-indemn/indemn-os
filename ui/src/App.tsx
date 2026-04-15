import { Routes, Route, Navigate } from "react-router-dom";
import { useAuth } from "./auth/useAuth";
import { useRealtimeConnection } from "./hooks/useRealtime";
import { LoginPage } from "./auth/LoginPage";
import { MfaChallenge } from "./auth/MfaChallenge";
import { PasswordReset } from "./auth/PasswordReset";
import { Shell } from "./layout/Shell";
import { EntityListView } from "./views/EntityListView";
import { EntityDetailView } from "./views/EntityDetailView";
import { QueueView } from "./views/QueueView";
import { RoleOverview } from "./views/RoleOverview";
import { AuthEventsView } from "./views/AuthEventsView";

export default function App() {
  const { isAuthenticated } = useAuth();

  // Initialize WebSocket connection when authenticated [G-34]
  useRealtimeConnection();

  if (!isAuthenticated) {
    return (
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/mfa" element={<MfaChallenge />} />
        <Route path="/reset-password" element={<PasswordReset />} />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    );
  }

  return (
    <Shell>
      <Routes>
        <Route path="/" element={<Navigate to="/queue" replace />} />
        <Route path="/queue" element={<QueueView />} />
        <Route path="/roles" element={<RoleOverview />} />
        <Route path="/auth-events" element={<AuthEventsView />} />
        <Route path="/:entityType" element={<EntityListView />} />
        <Route path="/:entityType/:entityId" element={<EntityDetailView />} />
        <Route path="*" element={<Navigate to="/queue" replace />} />
      </Routes>
    </Shell>
  );
}
