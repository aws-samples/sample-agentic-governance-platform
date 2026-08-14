/**
 * Shape the auth provider exposes via useAuth().
 *
 * Entra is the sole provider; it returns its AccountInfo as a generic shape,
 * opaque to consumers, which only read `isAuthenticated`, `isLoading`, and the
 * action functions. Password management happens in Microsoft's directory, not
 * in this app, so there are no password/reset methods here.
 */
export interface AuthState {
  isAuthenticated: boolean;
  isLoading: boolean;
  /** Authenticated user. Treat as opaque outside provider code. */
  user: { username: string; name?: string } | null;
  /** Most recent access token, also written to localStorage.auth_token. */
  token: string | null;

  /** Kick off the Microsoft hosted login redirect. */
  signInRedirect: () => Promise<void>;

  /** Clears local session and redirects to provider logout. */
  signOut: () => void;
}
