import type { ReactNode } from 'react';
import { EntraAuthProvider } from './EntraProvider';

export function AuthProvider({ children }: { children: ReactNode }) {
  return <EntraAuthProvider>{children}</EntraAuthProvider>;
}
