import { createContext, startTransition, useContext, useEffect, useMemo, useState } from "react";

import { apiRequest } from "./api";
import type { SessionUser, TokenResponse } from "../types/domain";


type AuthContextValue = {
  token: string | null;
  user: SessionUser | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
};


const TOKEN_KEY = "tecnocontrol-token";
const AUTH_ERROR_KEY = "tecnocontrol-auth-error";
const AuthContext = createContext<AuthContextValue | null>(null);


export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [token, setToken] = useState<string | null>(() => window.localStorage.getItem(TOKEN_KEY));
  const [user, setUser] = useState<SessionUser | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!token) {
      setLoading(false);
      return;
    }

    let cancelled = false;
    apiRequest<SessionUser>("/auth/me", { token })
      .then((nextUser) => {
        if (cancelled) {
          return;
        }
        startTransition(() => {
          setUser(nextUser);
          setLoading(false);
        });
      })
      .catch((nextError) => {
        const message = nextError instanceof Error ? nextError.message : "Tu sesion ya no es valida";
        if (cancelled) {
          return;
        }
        window.localStorage.removeItem(TOKEN_KEY);
        window.sessionStorage.setItem(AUTH_ERROR_KEY, message);
        startTransition(() => {
          setToken(null);
          setUser(null);
          setLoading(false);
        });
      });

    return () => {
      cancelled = true;
    };
  }, [token]);

  const value = useMemo<AuthContextValue>(
    () => ({
      token,
      user,
      loading,
      async login(email: string, password: string) {
        const response = await apiRequest<TokenResponse>("/auth/login", {
          method: "POST",
          body: { email, password },
        });
        window.localStorage.setItem(TOKEN_KEY, response.access_token);
        window.sessionStorage.removeItem(AUTH_ERROR_KEY);
        startTransition(() => {
          setToken(response.access_token);
          setUser(response.user);
        });
      },
      logout() {
        window.localStorage.removeItem(TOKEN_KEY);
        startTransition(() => {
          setToken(null);
          setUser(null);
        });
      },
    }),
    [loading, token, user],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}


export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used inside AuthProvider");
  }
  return context;
}


export function consumeStoredAuthError() {
  const message = window.sessionStorage.getItem(AUTH_ERROR_KEY);
  if (!message) {
    return null;
  }
  window.sessionStorage.removeItem(AUTH_ERROR_KEY);
  return message;
}
