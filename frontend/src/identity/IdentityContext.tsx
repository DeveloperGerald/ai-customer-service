import React, { createContext, useCallback, useContext, useMemo, useState } from "react";
import { DEFAULT_IDENTITY, DEMO_IDENTITIES, DemoIdentity } from "../demo-tokens";

interface IdentityState {
  current: DemoIdentity;
  all: DemoIdentity[];
  setCurrent: (next: DemoIdentity) => void;
  bearer: string;
  tenantId: string;
}

const IdentityContext = createContext<IdentityState | null>(null);

const STORAGE_KEY = "ai-cs-demo-identity-index";

export function IdentityProvider({ children }: { children: React.ReactNode }) {
  const [index, setIndex] = useState<number>(() => {
    const saved = Number(localStorage.getItem(STORAGE_KEY));
    return Number.isFinite(saved) && saved >= 0 && saved < DEMO_IDENTITIES.length ? saved : 0;
  });

  const current = DEMO_IDENTITIES[index] ?? DEFAULT_IDENTITY;

  const setCurrent = useCallback((next: DemoIdentity) => {
    const i = DEMO_IDENTITIES.findIndex(d => d.actor_id === next.actor_id && d.tenant_id === next.tenant_id);
    if (i >= 0) {
      setIndex(i);
      localStorage.setItem(STORAGE_KEY, String(i));
    }
  }, []);

  const value = useMemo<IdentityState>(() => {
    return {
      current,
      all: DEMO_IDENTITIES,
      setCurrent,
      bearer: `Bearer ${current.access_token}`,
      tenantId: current.tenant_id,
    };
  }, [current, setCurrent]);

  return <IdentityContext.Provider value={value}>{children}</IdentityContext.Provider>;
}

export function useIdentity(): IdentityState {
  const ctx = useContext(IdentityContext);
  if (!ctx) {
    throw new Error("useIdentity must be used inside <IdentityProvider />");
  }
  return ctx;
}
