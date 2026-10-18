'use client';

import { usePathname, useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';

function readCsrfCookie(): string {
  const entry = document.cookie.split('; ').find((part) => part.startsWith('rm_csrf='));
  return entry ? decodeURIComponent(entry.slice('rm_csrf='.length)) : '';
}

export default function AuthGate({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [ready, setReady] = useState(false);
  const [authenticated, setAuthenticated] = useState(false);

  useEffect(() => {
    if (pathname === '/login') {
      setReady(true);
      return;
    }
    let active = true;
    fetch('/api/v1/auth/session', { cache: 'no-store' })
      .then(async (response) => {
        if (!response.ok) throw new Error('Session check failed');
        return (await response.json()) as { authenticated?: boolean };
      })
      .then((session) => {
        if (!active) return;
        if (session.authenticated) {
          setAuthenticated(true);
          setReady(true);
        } else {
          router.replace(`/login?returnTo=${encodeURIComponent(pathname || '/')}`);
        }
      })
      .catch(() => {
        if (active) router.replace('/login');
      });
    return () => {
      active = false;
    };
  }, [pathname, router]);

  if (pathname === '/login') return <>{children}</>;
  if (!ready || !authenticated) {
    return <div className="min-h-screen" role="status" aria-label="Checking session" />;
  }

  return (
    <>
      {children}
      <button
        className={[
          'fixed right-4 top-4 z-[100] rounded-md border border-line',
          'bg-canvas px-3 py-2 text-sm text-ink-soft shadow',
        ].join(' ')}
        onClick={async () => {
          await fetch('/api/v1/auth/logout', {
            method: 'POST',
            headers: { 'X-CSRF-Token': readCsrfCookie() },
          });
          router.replace('/login');
        }}
        type="button"
      >
        Log out
      </button>
    </>
  );
}
