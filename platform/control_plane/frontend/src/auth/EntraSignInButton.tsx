import { useState } from 'react';
import { useAuth } from './AuthContext';

export default function EntraSignInButton() {
  const { signInRedirect } = useAuth();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const handleSignIn = async () => {
    setLoading(true);
    setError('');
    try {
      await signInRedirect();
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Sign-in failed';
      setError(msg);
      setLoading(false);
    }
    // On success, the browser navigates away; no setLoading(false) needed.
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-slate-50 px-4">
      <div className="w-full max-w-md bg-white rounded-2xl border border-slate-200 p-10 shadow-lg">
        <div className="text-center mb-8">
          <div className="w-14 h-14 rounded-xl flex items-center justify-center bg-blue-600 mx-auto mb-4">
            <svg className="w-7 h-7 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5} aria-hidden="true">
              <path strokeLinecap="round" strokeLinejoin="round" d="M9 3v2m6-2v2M9 19v2m6-2v2M5 9H3m2 6H3m18-6h-2m2 6h-2M7 19h10a2 2 0 002-2V7a2 2 0 00-2-2H7a2 2 0 00-2 2v10a2 2 0 002 2zM9 9h6v6H9V9z" />
            </svg>
          </div>
          <h1 className="text-2xl font-bold text-slate-900 mb-2">Agentic Governance Platform</h1>
          <p className="text-sm text-slate-500">Sign in with your Microsoft Entra ID account</p>
        </div>

        {error && (
          <div className="mb-6 p-4 bg-red-50 border border-red-200 rounded-xl text-sm text-red-700">
            {error}
          </div>
        )}

        <button
          type="button"
          onClick={handleSignIn}
          disabled={loading}
          className="w-full py-3 px-4 bg-blue-600 text-white text-sm font-semibold rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-all flex items-center justify-center gap-2 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2"
        >
          {loading ? (
            <span>Redirecting…</span>
          ) : (
            <>
              <svg className="w-5 h-5" viewBox="0 0 23 23" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                <rect x="1" y="1" width="10" height="10" fill="#F25022" />
                <rect x="12" y="1" width="10" height="10" fill="#7FBA00" />
                <rect x="1" y="12" width="10" height="10" fill="#00A4EF" />
                <rect x="12" y="12" width="10" height="10" fill="#FFB900" />
              </svg>
              <span>Sign in with Microsoft</span>
            </>
          )}
        </button>

        <p className="mt-6 text-xs text-center text-slate-400">
          Tenant: {import.meta.env.VITE_ENTRA_TENANT_DOMAIN || 'unknown'}
        </p>
      </div>
    </div>
  );
}
