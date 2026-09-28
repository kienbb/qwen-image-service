import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api, ApiError, onAuthError, setCsrf } from "./api";
import type { AuthResponse, Role, User } from "./types";

export const ROLE_RANK: Record<Role, number> = { viewer: 1, operator: 2, admin: 3 };

export function roleAtLeast(user: Pick<User, "role"> | null | undefined, min: Role): boolean {
  if (!user) return false;
  return (ROLE_RANK[user.role] ?? 0) >= ROLE_RANK[min];
}

type Phase = "loading" | "anon" | "must_change" | "ready" | "offline";

interface AuthCtx {
  phase: Phase;
  user: User | null;
  error: string | null;
  login: (login: string, password: string) => Promise<void>;
  setup: (username: string, password: string, email?: string) => Promise<void>;
  changePassword: (current: string, next: string) => Promise<void>;
  logout: () => Promise<void>;
  can: (min: Role) => boolean;
  refresh: () => Promise<void>;
}

const Ctx = createContext<AuthCtx | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [phase, setPhase] = useState<Phase>("loading");
  const [user, setUser] = useState<User | null>(null);
  const [error, setError] = useState<string | null>(null);
  const qc = useQueryClient();

  const accept = useCallback((r: AuthResponse) => {
    setCsrf(r.csrf);
    setUser(r.user);
    setPhase(r.user.must_change_pw ? "must_change" : "ready");
  }, []);

  const refresh = useCallback(async () => {
    try {
      accept(await api.get<AuthResponse>("auth/me", { quiet: true }));
      setError(null);
    } catch (e) {
      setCsrf(null);
      setUser(null);
      if (e instanceof ApiError && e.status === 0) {
        setError(e.message);
        setPhase("offline");
      } else setPhase("anon");
    }
  }, [accept]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    onAuthError((err) => {
      if (err.status === 401) {
        setCsrf(null);
        setUser(null);
        setPhase("anon");
        qc.clear();
      } else if (err.code === "must_change_password") setPhase("must_change");
    });
    return () => onAuthError(null);
  }, [qc]);

  const value = useMemo<AuthCtx>(
    () => ({
      phase,
      user,
      error,
      refresh,
      can: (min) => roleAtLeast(user, min),
      login: async (login, password) => {
        accept(await api.post<AuthResponse>("auth/login", { login, password }, { quiet: true }));
      },
      setup: async (username, password, email) => {
        accept(await api.post<AuthResponse>("auth/setup", { username, password, email: email || null }, { quiet: true }));
      },
      changePassword: async (current, next) => {
        const r = await api.post<{ ok: boolean; csrf: string }>("auth/password", { current, new: next });
        setCsrf(r.csrf);
        // the server clears must_change_pw; reload the user from the new session
        await refresh();
      },
      logout: async () => {
        try {
          await api.post("auth/logout", undefined, { quiet: true });
        } finally {
          setCsrf(null);
          setUser(null);
          setPhase("anon");
          qc.clear();
        }
      },
    }),
    [phase, user, error, refresh, accept, qc],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthCtx {
  const c = useContext(Ctx);
  if (!c) throw new Error("useAuth outside AuthProvider");
  return c;
}
