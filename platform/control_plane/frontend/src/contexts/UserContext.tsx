// Rationale: docs/token-propagation.md §1–§2 (token homes, request token)
import { useState, useEffect, type ReactNode } from 'react';
import { userApi, type UserInfo } from '../api/client';
import { UserContext } from './useUser';

export function UserProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<UserInfo | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  const fetchUser = async () => {
    try {
      const userInfo = await userApi.getCurrentUser();
      setUser(userInfo);
    } catch (error) {
      console.error('Failed to fetch user info:', error);
      setUser(null);
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    fetchUser();
  }, []);

  return (
    <UserContext.Provider value={{ user, isLoading }}>
      {children}
    </UserContext.Provider>
  );
}
