import { Suspense, useState, type ReactNode } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router";
import { useAuth } from "../lib/auth";
import { useLive } from "../lib/live";
import { useTheme } from "../lib/theme";
import type { Role } from "../lib/types";
import { Badge, Button, Loading, cn } from "./ui";

const I = (d: string) => (
  <svg viewBox="0 0 24 24" className="h-4 w-4 shrink-0" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
    <path d={d} />
  </svg>
);

export interface NavItem {
  to: string;
  label: string;
  icon: ReactNode;
  min: Role;
}

export const NAV: NavItem[] = [
  { to: "/", label: "Tổng quan", icon: I("M3 13h8V3H3zM13 21h8V11h-8zM3 21h8v-6H3zM13 3v6h8V3z"), min: "viewer" },
  { to: "/jobs", label: "Jobs", icon: I("M4 6h16M4 12h16M4 18h10"), min: "viewer" },
  { to: "/playground", label: "Playground", icon: I("M5 3l14 9-14 9z"), min: "operator" },
  { to: "/settings", label: "Queue & Settings", icon: I("M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"), min: "viewer" },
  { to: "/models", label: "Models", icon: I("M21 16V8l-9-5-9 5v8l9 5zM3.3 7L12 12l8.7-5M12 22V12"), min: "viewer" },
  { to: "/keys", label: "API Keys", icon: I("M21 2l-2 2m-7.6 7.6a5.5 5.5 0 1 1-7.8 7.8 5.5 5.5 0 0 1 7.8-7.8zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"), min: "operator" },
  { to: "/users", label: "Người dùng", icon: I("M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM23 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8"), min: "admin" },
  { to: "/logs", label: "Logs", icon: I("M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6M8 13h8M8 17h8"), min: "admin" },
  { to: "/system", label: "Hệ thống", icon: I("M2 4h20v12H2zM8 20h8M12 16v4"), min: "viewer" },
];

function NavLinks({ onNavigate }: { onNavigate?: () => void }) {
  const { can } = useAuth();
  return (
    <nav className="flex flex-col gap-0.5">
      {NAV.filter((n) => can(n.min)).map((n) => (
        <NavLink
          key={n.to}
          to={n.to}
          end={n.to === "/"}
          onClick={onNavigate}
          className={({ isActive }) =>
            cn("flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-sm transition-colors", isActive ? "bg-primary/10 font-medium text-primary" : "text-muted hover:bg-subtle hover:text-fg")
          }
        >
          {n.icon}
          {n.label}
        </NavLink>
      ))}
    </nav>
  );
}

function ThemeButton() {
  const { isDark, cycle } = useTheme();
  return (
    <Button variant="ghost" size="icon" onClick={cycle} title={isDark ? "Chế độ sáng" : "Chế độ tối"} aria-label="Đổi giao diện">
      {isDark ? I("M12 3v1m0 16v1m9-9h-1M4 12H3m15.4-6.4l-.7.7M6.3 17.7l-.7.7m12.8 0l-.7-.7M6.3 6.3l-.7-.7M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0z") : I("M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z")}
    </Button>
  );
}

function UserBox() {
  const { user, logout } = useAuth();
  const { connected } = useLive();
  return (
    <div className="space-y-1.5 text-xs">
      <div className="flex min-w-0 items-center gap-2">
        <span className={cn("h-2 w-2 shrink-0 rounded-full", connected ? "bg-success" : "bg-warning")} title={connected ? "Live: đã kết nối" : "Live: mất kết nối"} />
        <span className="min-w-0 truncate font-medium" title={user?.username}>
          {user?.username}
        </span>
        <Badge>{user?.role}</Badge>
      </div>
      <div className="flex items-center gap-1">
        <Link to="/account/password" className="rounded px-2 py-1 text-muted hover:bg-subtle hover:text-fg">
          Đổi mật khẩu
        </Link>
        <Button variant="ghost" size="sm" onClick={() => void logout()}>
          Đăng xuất
        </Button>
      </div>
    </div>
  );
}

export function Layout() {
  const [open, setOpen] = useState(false);
  const loc = useLocation();
  const current = NAV.find((n) => (n.to === "/" ? loc.pathname === "/" : loc.pathname.startsWith(n.to)));
  return (
    <div className="min-h-screen md:flex">
      {/* desktop sidebar */}
      <aside className="sticky top-0 hidden h-screen w-56 shrink-0 flex-col border-r border-border bg-card md:flex">
        <div className="flex items-center gap-2 px-4 py-4">
          <img src={`${import.meta.env.BASE_URL}favicon.svg`} alt="" className="h-6 w-6" />
          <div className="leading-tight">
            <div className="text-sm font-semibold">Qwen Image Service</div>
            <div className="text-[11px] text-muted">Dashboard</div>
          </div>
        </div>
        <div className="flex-1 overflow-y-auto px-2">
          <NavLinks />
        </div>
        <div className="border-t border-border p-3">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-xs text-muted">Giao diện</span>
            <ThemeButton />
          </div>
          <UserBox />
        </div>
      </aside>

      {/* mobile top bar */}
      <header className="sticky top-0 z-40 border-b border-border bg-card md:hidden">
        <div className="flex h-12 items-center justify-between px-3">
          <button className="flex items-center gap-2 text-sm font-semibold" onClick={() => setOpen((o) => !o)} aria-expanded={open} aria-label="Menu">
            {I(open ? "M6 6l12 12M18 6L6 18" : "M3 6h18M3 12h18M3 18h18")}
            {current?.label ?? "QIS"}
          </button>
          <ThemeButton />
        </div>
        {open && (
          <div className="border-t border-border px-2 py-2">
            <NavLinks onNavigate={() => setOpen(false)} />
            <div className="mt-2 border-t border-border px-1 pt-2">
              <UserBox />
            </div>
          </div>
        )}
      </header>

      <main className="min-w-0 flex-1 px-3 py-4 sm:px-6 sm:py-6">
        <div className="mx-auto max-w-7xl">
          <Suspense fallback={<Loading />}>
            <Outlet />
          </Suspense>
        </div>
      </main>
    </div>
  );
}
