import { createContext, useContext, type Dispatch } from 'react';
import { TENANTS, type Tenant } from './demoData';

export interface DemoState {
  experiments: Tenant[];
}

export type DemoAction = { type: 'ADD_EXPERIMENT'; tenant: Tenant };

export const INITIAL_DEMO_STATE: DemoState = {
  experiments: TENANTS.filter((t) => t.kind === 'sandbox'),
};

export function demoReducer(state: DemoState, action: DemoAction): DemoState {
  switch (action.type) {
    case 'ADD_EXPERIMENT':
      return { ...state, experiments: [...state.experiments, action.tenant] };
  }
}

interface DemoStoreContextType {
  state: DemoState;
  dispatch: Dispatch<DemoAction>;
}

export const DemoStoreContext = createContext<DemoStoreContextType | undefined>(undefined);

export function useDemoStore() {
  const context = useContext(DemoStoreContext);
  if (context === undefined) {
    throw new Error('useDemoStore must be used within a DemoStoreProvider');
  }
  return context;
}
