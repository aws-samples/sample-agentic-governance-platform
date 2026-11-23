/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_URL?: string;
  readonly VITE_AUTH_PROVIDER?: 'entra';
  readonly VITE_BUILD_SHA?: string;

  // Entra (sole provider; .env.local for the dev server, .env.production written by deploy-frontend.sh for the build)
  readonly VITE_ENTRA_TENANT_ID?: string;
  readonly VITE_ENTRA_TENANT_DOMAIN?: string;
  readonly VITE_ENTRA_SPA_CLIENT_ID?: string;
  readonly VITE_ENTRA_SPA_REDIRECT_URI?: string;
  readonly VITE_ENTRA_SPA_SCOPE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
