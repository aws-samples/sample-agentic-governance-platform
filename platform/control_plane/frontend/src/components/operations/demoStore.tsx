// Demo store for the Operations Build & Run flow (Epic 18). Holds the agent the
// user just built in the Studio (`builtAgent`) and the sandbox experiments they
// spun up (`experiments`). Pure reducer (spread, never mutate) + a React context
// provider + a `useDemoStore` hook that throws outside the provider — same idiom
// as contexts/UserContext.tsx.
import { createContext, useContext, useReducer, type ReactNode, type Dispatch } from 'react';
import { TENANTS, type Tenant } from './demoData';

export interface BuiltAgent {
  name: string;
  useCase: string;
  kind: 'agent' | 'workflow';
  model: string;
  account: string;
  costPerRun: number;
}

export interface DemoState {
  builtAgent: BuiltAgent | null;
  experiments: Tenant[];
}

export type DemoAction =
  | { type: 'ADD_EXPERIMENT'; tenant: Tenant }
  | { type: 'PROMOTE_AGENT'; agent: BuiltAgent };

// Seeded with the two sandbox tenants so the Experiments page has a populated
// starting point before the user adds their own.
export const INITIAL_DEMO_STATE: DemoState = {
  builtAgent: null,
  experiments: TENANTS.filter((t) => t.kind === 'sandbox'),
};

/** Pure reducer — always returns a new state object; never mutates inputs. */
export function demoReducer(state: DemoState, action: DemoAction): DemoState {
  switch (action.type) {
    case 'ADD_EXPERIMENT':
      return { ...state, experiments: [...state.experiments, action.tenant] };
    case 'PROMOTE_AGENT':
      return { ...state, builtAgent: action.agent };
    default:
      return state;
  }
}

interface DemoStoreContextType {
  state: DemoState;
  dispatch: Dispatch<DemoAction>;
}

const DemoStoreContext = createContext<DemoStoreContextType | undefined>(undefined);

export function DemoStoreProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(demoReducer, INITIAL_DEMO_STATE);
  return <DemoStoreContext.Provider value={{ state, dispatch }}>{children}</DemoStoreContext.Provider>;
}

export function useDemoStore() {
  const context = useContext(DemoStoreContext);
  if (context === undefined) {
    throw new Error('useDemoStore must be used within a DemoStoreProvider');
  }
  return context;
}
