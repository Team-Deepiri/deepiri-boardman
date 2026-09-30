/**
 * Operator-entered bearer token for the privileged Boardman endpoints.
 *
 * The SPA is a public static bundle, so it must not *embed* a secret -- anything
 * shipped in the JS is readable by anyone who loads the page. Instead the operator
 * pastes the deployment's `BOARDMAN_API_TOKEN` at runtime and we keep it in
 * `sessionStorage`, which:
 *   - is scoped to the tab, so it does not linger on a shared machine,
 *   - is cleared when the tab closes,
 *   - keeps the secret out of the build artifact and out of localStorage.
 *
 * This is deliberately not a login system: it gates a handful of write actions on
 * an internal tool, it is not a substitute for one. Anything that needs real user
 * identity should go behind a session cookie at the nginx/app layer instead.
 */

const STORAGE_KEY = "boardman.apiToken";

/** Returns the stored token, or "" when absent. Never throws (private mode can block storage). */
export function getApiToken(): string {
  try {
    return window.sessionStorage.getItem(STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setApiToken(token: string): void {
  try {
    const trimmed = token.trim();
    if (trimmed) {
      window.sessionStorage.setItem(STORAGE_KEY, trimmed);
    } else {
      window.sessionStorage.removeItem(STORAGE_KEY);
    }
  } catch {
    // Storage unavailable: the token just won't persist across the request.
  }
}

export function clearApiToken(): void {
  try {
    window.sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // Ignore -- nothing to clear if storage is unavailable.
  }
}

export function hasApiToken(): boolean {
  return getApiToken() !== "";
}

/** Authorization header for privileged calls, or {} when no token is set. */
export function authHeader(): Record<string, string> {
  const token = getApiToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}
