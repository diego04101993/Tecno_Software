import { Navigate, Outlet, Route, BrowserRouter as Router, Routes } from "react-router-dom";

import { BranchShell } from "./layouts/BranchShell";
import { ClientShell } from "./layouts/ClientShell";
import { GlobalShell } from "./layouts/GlobalShell";
import { useAuth } from "./lib/auth";
import { getDefaultAppPath, getLegacyRoutePath, type LegacySection } from "./lib/workspace";
import { BranchLayoutsPage } from "./pages/branch/BranchLayoutsPage";
import { BranchAudioPage } from "./pages/branch/BranchAudioPage";
import { LayoutEditorPage } from "./pages/branch/LayoutEditorPage";
import { LayoutPreviewPage } from "./pages/branch/LayoutPreviewPage";
import { BranchOverviewPage } from "./pages/branch/BranchOverviewPage";
import { BranchPreviewPage } from "./pages/branch/BranchPreviewPage";
import { BranchTimelinePage } from "./pages/branch/BranchTimelinePage";
import { ChannelDetailPage } from "./pages/channel/ChannelDetailPage";
import { ClientAudioPage } from "./pages/client/ClientAudioPage";
import { ClientDataSourcesPage } from "./pages/client/ClientDataSourcesPage";
import { DatasetDetailPage } from "./pages/client/DatasetDetailPage";
import { ClientOperationPage } from "./pages/client/ClientOperationPage";
import { ClientOverviewPage } from "./pages/client/ClientOverviewPage";
import { ClientUsersPage } from "./pages/client/ClientUsersPage";
import { BranchesPage } from "./pages/BranchesPage";
import { CampaignsPage } from "./pages/CampaignsPage";
import { ChannelsPage } from "./pages/ChannelsPage";
import { ClientsPage } from "./pages/ClientsPage";
import { ContentsPage } from "./pages/ContentsPage";
import { LoginPage } from "./pages/LoginPage";
import { KioskPage } from "./pages/KioskPage";
import { TeamUsersPage } from "./pages/TeamUsersPage";
import { TouchExperiencesPage } from "./pages/TouchExperiencesPage";
import { TouchExperienceBuilderPage } from "./pages/TouchExperienceBuilderPage";
import { VideowallPage } from "./pages/VideowallPage";

function LoadingScreen() {
  return <div className="grid min-h-screen place-items-center bg-mist font-display text-2xl text-ink">Cargando consola...</div>;
}

function ProtectedLayout() {
  const { loading, token } = useAuth();

  if (loading) {
    return <LoadingScreen />;
  }

  if (!token) {
    return <Navigate to="/login" replace />;
  }

  return <Outlet />;
}

function HomeRedirect() {
  const { loading, token, user } = useAuth();

  if (loading) {
    return <LoadingScreen />;
  }

  return <Navigate to={token ? getDefaultAppPath(user) : "/login"} replace />;
}

function RoleEntryRedirect() {
  const { loading, token, user } = useAuth();

  if (loading) {
    return <LoadingScreen />;
  }

  if (!token) {
    return <Navigate to="/login" replace />;
  }

  return <Navigate to={getDefaultAppPath(user)} replace />;
}

function LegacyRedirect({ section }: { section: LegacySection }) {
  const { loading, token, user } = useAuth();

  if (loading) {
    return <LoadingScreen />;
  }

  if (!token) {
    return <Navigate to="/login" replace />;
  }

  return <Navigate to={getLegacyRoutePath(section, user)} replace />;
}

export function App() {
  return (
    <Router>
      <Routes>
        <Route path="/" element={<HomeRedirect />} />
        <Route path="/login" element={<LoginPage />} />
        <Route element={<ProtectedLayout />}>
          <Route path="/app" element={<RoleEntryRedirect />} />

          <Route path="/app/clients" element={<GlobalShell />}>
            <Route index element={<ClientsPage />} />
          </Route>
          <Route path="/app/team" element={<GlobalShell />}>
            <Route path="users" element={<TeamUsersPage />} />
          </Route>

          <Route path="/app/clients/:clientId" element={<ClientShell />}>
            <Route index element={<Navigate to="overview" replace />} />
            <Route path="overview" element={<ClientOverviewPage />} />
            <Route path="operations" element={<ClientOperationPage />} />
            <Route path="branches" element={<BranchesPage />} />
            <Route path="campaigns" element={<CampaignsPage />} />
            <Route path="contents" element={<ContentsPage />} />
            <Route path="data-sources" element={<ClientDataSourcesPage />} />
            <Route path="data-sources/:datasetId" element={<DatasetDetailPage />} />
            <Route path="videowalls" element={<VideowallPage />} />
            <Route path="kiosk" element={<KioskPage />} />
            <Route path="kiosk/touch" element={<TouchExperiencesPage />} />
            <Route path="kiosk/touch/:experienceId" element={<TouchExperienceBuilderPage />} />
            <Route path="users" element={<ClientUsersPage />} />
            <Route path="audio" element={<ClientAudioPage />} />
          </Route>

          <Route path="/app/clients/:clientId/branches/:branchId" element={<BranchShell />}>
            <Route index element={<Navigate to="overview" replace />} />
            <Route path="overview" element={<BranchOverviewPage />} />
            <Route path="channels" element={<ChannelsPage />} />
            <Route path="preview" element={<BranchPreviewPage />} />
            <Route path="timeline" element={<BranchTimelinePage />} />
            <Route path="layouts" element={<BranchLayoutsPage />} />
            <Route path="layouts/:layoutId/editor" element={<LayoutEditorPage />} />
            <Route path="layouts/:layoutId/preview" element={<LayoutPreviewPage />} />
            <Route path="audio" element={<BranchAudioPage />} />
            <Route path="channels/:channelId" element={<ChannelDetailPage />} />
          </Route>

          <Route path="/app/dashboard" element={<LegacyRedirect section="dashboard" />} />
          <Route path="/app/branches" element={<LegacyRedirect section="branches" />} />
          <Route path="/app/channels" element={<LegacyRedirect section="channels" />} />
          <Route path="/app/campaigns" element={<LegacyRedirect section="campaigns" />} />
          <Route path="/app/contents" element={<LegacyRedirect section="contents" />} />
          <Route path="/app/videowalls" element={<LegacyRedirect section="videowalls" />} />
          <Route path="/app/kiosk" element={<LegacyRedirect section="kiosk" />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Router>
  );
}
