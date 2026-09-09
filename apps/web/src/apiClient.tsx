import { useEffect, useState } from "react";

// SPEC-M5: shared-secret gate. A plain fetch (not the api() wrapper below)
// cannot attach this header for <img src=...> -- those two endpoints
// (PDF page images, evidence crops) are fetched as blobs instead
// (useAuthedImage below), never loaded as a bare URL, so the key never
// appears in a URL, browser history, or an access log the way a
// query-string key would. Shared between main.tsx and IfcViewer.tsx (not
// defined in main.tsx itself) since IfcViewer.tsx also calls the API
// directly and must not import from the app's entry module.
// sessionStorage, not localStorage: cleared when the tab/browser closes
// instead of sitting on disk indefinitely, which matters on a shared or
// public machine. This does not eliminate the underlying trade-off CodeQL
// flags (js/clear-text-storage-of-sensitive-data) -- any script-readable
// browser storage is exposed to an XSS vulnerability on this page the
// same way -- see D-015 for why that residual risk is accepted rather
// than built around (a real per-user session/cookie flow) for a single
// shared demo secret whose entire security model is already
// "possession = access," not per-identity credentials.
const API_KEY_STORAGE_KEY = "armie_api_key";

export function getStoredApiKey(): string | null {
  try { return sessionStorage.getItem(API_KEY_STORAGE_KEY); } catch { return null; }
}

export function setStoredApiKey(key: string): void {
  // codeql[js/clear-text-storage-of-sensitive-data] -- reviewed and
  // accepted, not overlooked: see the comment above this function and
  // SPEC-M5's §I addendum for why this specific shared-secret model
  // (possession = access for every legitimate holder already) does not
  // warrant a real server-side session just to avoid script-readable
  // storage.
  try { sessionStorage.setItem(API_KEY_STORAGE_KEY, key); } catch { /* per-viewer convenience only; a private/blocked storage context just re-prompts next time */ }
}

export function withAuthHeader(init?: RequestInit): RequestInit {
  const key = getStoredApiKey();
  if (!key) return init || {};
  const headers = new Headers(init?.headers);
  headers.set("Authorization", `Bearer ${key}`);
  return { ...init, headers };
}

export class ApiAuthError extends Error {}

export const api = async <T,>(path: string, init?: RequestInit): Promise<T> => {
  const response = await fetch(path, withAuthHeader(init));
  if (response.status === 401) throw new ApiAuthError("Missing or invalid access key.");
  if (!response.ok) throw new Error(await response.text());
  return response.json() as Promise<T>;
};

// Fetches an image endpoint with the same Authorization header as api()
// and exposes it as an object URL, since a plain <img src="/api/..."> has
// no way to carry a custom header. Revokes the previous object URL on
// every change/unmount so this doesn't leak blob URLs as the user pages
// through drawing pages or citations.
export function useAuthedImage(url: string | null): string | null {
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!url) { setObjectUrl(null); return; }
    let cancelled = false;
    let currentUrl: string | null = null;
    (async () => {
      try {
        const response = await fetch(url, withAuthHeader());
        if (!response.ok) throw new Error(`${response.status}`);
        const blob = await response.blob();
        if (cancelled) return;
        currentUrl = URL.createObjectURL(blob);
        setObjectUrl(currentUrl);
      } catch {
        if (!cancelled) setObjectUrl(null);
      }
    })();
    return () => {
      cancelled = true;
      if (currentUrl) URL.revokeObjectURL(currentUrl);
    };
  }, [url]);
  return objectUrl;
}

// A component, not calling useAuthedImage directly at each <img> call site:
// some usages render inside a .map() over a list, and a hook can only be
// called from a proper component (one instance per list item here), never
// from inside a loop callback in the parent itself.
export function AuthedImage({ src, alt, className }: { src: string; alt: string; className?: string }) {
  const url = useAuthedImage(src);
  return url ? <img src={url} alt={alt} className={className} /> : null;
}
