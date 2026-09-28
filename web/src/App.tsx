import { lazy, Suspense, type ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router";
import { Layout } from "./components/Layout";
import { Alert, Button, Loading } from "./components/ui";
import { useAuth } from "./lib/auth";
import { LiveProvider } from "./lib/live";
import type { Role } from "./lib/types";
import { ChangePasswordPage, LoginPage } from "./pages/Login";

const Overview = lazy(() => import("./pages/Overview"));
const Jobs = lazy(() => import("./pages/Jobs"));
const JobDetail = lazy(() => import("./pages/JobDetail"));
const Playground = lazy(() => import("./pages/Playground"));
const Settings = lazy(() => import("./pages/Settings"));
const Models = lazy(() => import("./pages/Models"));
const Keys = lazy(() => import("./pages/Keys"));
const Users = lazy(() => import("./pages/Users"));
const Logs = lazy(() => import("./pages/Logs"));
const System = lazy(() => import("./pages/System"));

function Guard({ min, children }: { min: Role; children: ReactNode }) {
  const { can } = useAuth();
  if (!can(min)) return <Alert tone="warning" title="Không đủ quyền">Trang này cần quyền {min} trở lên.</Alert>;
  return <>{children}</>;
}

export function App() {
  const { phase, error, refresh } = useAuth();
  if (phase === "loading") {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Loading />
      </div>
    );
  }
  if (phase === "offline") {
    return (
      <div className="mx-auto mt-24 max-w-md px-4">
        <Alert tone="danger" title="Không kết nối được gateway">
          {error}
          <div className="mt-3">
            <Button size="sm" onClick={() => void refresh()}>
              Thử lại
            </Button>
          </div>
        </Alert>
      </div>
    );
  }
  if (phase === "anon") return <LoginPage />;
  if (phase === "must_change") return <ChangePasswordPage forced />;

  return (
    <LiveProvider enabled>
      <Suspense fallback={<Loading />}>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<Overview />} />
            <Route path="jobs" element={<Jobs />} />
            <Route path="jobs/:id" element={<JobDetail />} />
            <Route path="playground" element={<Guard min="operator"><Playground /></Guard>} />
            <Route path="settings" element={<Settings />} />
            <Route path="models" element={<Models />} />
            <Route path="keys" element={<Guard min="operator"><Keys /></Guard>} />
            <Route path="users" element={<Guard min="admin"><Users /></Guard>} />
            <Route path="logs" element={<Guard min="admin"><Logs /></Guard>} />
            <Route path="system" element={<System />} />
            <Route path="account/password" element={<ChangePasswordPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </Suspense>
    </LiveProvider>
  );
}
