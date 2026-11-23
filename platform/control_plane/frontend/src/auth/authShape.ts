export type AuthArm = 'entra' | 'devauth';

export interface AuthState {
  isAuthenticated: boolean;
  isLoading: boolean;
  user: { username: string; name?: string } | null;
  token: string | null;
  arm: AuthArm;

  signInRedirect: () => Promise<void>;

  signOut: () => void;
}
