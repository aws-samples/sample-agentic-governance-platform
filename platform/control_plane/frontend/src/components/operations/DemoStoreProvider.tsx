import { useReducer, type ReactNode } from 'react';
import { DemoStoreContext, INITIAL_DEMO_STATE, demoReducer } from './demoStore';

export function DemoStoreProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(demoReducer, INITIAL_DEMO_STATE);
  return <DemoStoreContext.Provider value={{ state, dispatch }}>{children}</DemoStoreContext.Provider>;
}
