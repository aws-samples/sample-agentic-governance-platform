import type { ReactNode } from 'react';
import type { AuthState } from './authShape';
import { EntraAuthProvider, useEntraAuth } from './EntraProvider';

// Entra is the sole auth provider.
export function AuthProvider({ children }: { children: ReactNode }) {
  return <EntraAuthProvider>{children}</EntraAuthProvider>;
}

export function useAuth(): AuthState {
  return useEntraAuth();
}
