import type { ReactNode } from 'react';
import type { AuthState } from '../authShape';
import { AuthStateContext } from '../useAuth';
import { DEVAUTH_MODE, DEVAUTH_USER } from './arm';

const state: AuthState = {
  isAuthenticated: true,
  isLoading: false,
  user: { ...DEVAUTH_USER },
  token: null,
  arm: DEVAUTH_MODE,
  signInRedirect: async () => {},
  signOut: () => window.location.reload(),
};

export function AuthProvider({ children }: { children: ReactNode }) {
  return <AuthStateContext.Provider value={state}>{children}</AuthStateContext.Provider>;
}
