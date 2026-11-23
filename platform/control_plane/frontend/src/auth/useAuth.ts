import { createContext, useContext } from 'react';
import type { AuthState } from './authShape';

export const AuthStateContext = createContext<AuthState | null>(null);

export function useAuth(): AuthState {
  const ctx = useContext(AuthStateContext);
  if (!ctx) throw new Error('useAuth must be used within AuthProvider');
  return ctx;
}
