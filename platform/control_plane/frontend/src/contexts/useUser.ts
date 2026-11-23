import { createContext, useContext } from 'react';
import type { UserInfo } from '../api/client';

interface UserContextType {
  user: UserInfo | null;
  isLoading: boolean;
}

export const UserContext = createContext<UserContextType | undefined>(undefined);

export function useUser() {
  const context = useContext(UserContext);
  if (context === undefined) {
    throw new Error('useUser must be used within a UserProvider');
  }
  return context;
}
