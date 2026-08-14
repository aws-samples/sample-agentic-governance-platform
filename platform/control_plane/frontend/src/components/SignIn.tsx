import EntraSignInButton from '../auth/EntraSignInButton';

// Entra is the sole auth provider. The legacy Cognito email/password/reset
// forms were archived to archive/frontend/components/SignIn.legacy.tsx.
export default function SignIn() {
  return <EntraSignInButton />;
}
