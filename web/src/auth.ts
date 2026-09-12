// OIDC login against the hdh PROVIDER realm (Keycloak), auth-code + PKCE.
// The access token is sent as a Bearer on every API call; the backend verifies
// it against the realm's JWKS and refuses anything from another realm.

import { UserManager, WebStorageStateStore, type User } from "oidc-client-ts";

// Keycloak base URL and realm — overridable at build time for a deployment.
const KEYCLOAK_URL = (import.meta.env.VITE_KEYCLOAK_URL as string) || "http://localhost:8080";
const REALM = (import.meta.env.VITE_KEYCLOAK_REALM as string) || "hdh";

const manager = new UserManager({
  authority: `${KEYCLOAK_URL}/realms/${REALM}`,
  client_id: "hdh-web",
  redirect_uri: window.location.origin + "/",
  post_logout_redirect_uri: window.location.origin + "/",
  response_type: "code", // authorization code; PKCE is automatic for a public client
  scope: "openid profile",
  userStore: new WebStorageStateStore({ store: window.localStorage }),
  automaticSilentRenew: true,
});

/** The signed-in user, or null. Completes the redirect callback if we just
 *  came back from Keycloak (the URL carries ?code=&state=). */
export async function currentUser(): Promise<User | null> {
  const params = new URLSearchParams(window.location.search);
  if (params.has("code") && params.has("state")) {
    const user = await manager.signinRedirectCallback();
    window.history.replaceState({}, document.title, window.location.pathname); // drop ?code
    return user;
  }
  return manager.getUser();
}

export function login(): Promise<void> {
  return manager.signinRedirect();
}

export async function logout(): Promise<void> {
  await manager.signoutRedirect();
}

/** The current access token, or null — what the API layer attaches as Bearer. */
export async function accessToken(): Promise<string | null> {
  const user = await manager.getUser();
  return user && !user.expired ? user.access_token : null;
}
