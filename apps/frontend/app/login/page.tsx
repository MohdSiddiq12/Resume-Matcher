'use client';

import { useState } from 'react';
import type { FormEvent } from 'react';
import { useRouter } from 'next/navigation';

export default function LoginPage() {
  const router = useRouter();
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      const response = await fetch('/api/v1/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password }),
      });
      if (!response.ok) throw new Error('Unable to sign in. Check your password and try again.');
      const requested = new URLSearchParams(window.location.search).get('returnTo') || '/';
      const returnTo = requested.startsWith('/') && !requested.startsWith('//') ? requested : '/';
      router.replace(returnTo);
    } catch {
      setError('Unable to sign in. Check your password and try again.');
      setBusy(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-canvas px-6">
        <form
          className="w-full max-w-sm space-y-5 rounded-xl border border-line bg-white p-8 shadow-sm"
          onSubmit={submit}
        >
        <div>
          <h1 className="text-2xl font-semibold text-ink">Sign in</h1>
          <p className="mt-2 text-sm text-ink-soft">Enter your application password to continue.</p>
        </div>
        <label className="block space-y-2 text-sm font-medium text-ink">
          Password
          <input
            autoComplete="current-password"
            className="w-full rounded-md border border-line px-3 py-2"
            onChange={(event) => setPassword(event.target.value)}
            required
            type="password"
            value={password}
          />
        </label>
        {error && (
          <p className="text-sm text-red-700" role="alert">
            {error}
          </p>
        )}
        <button
          className="w-full rounded-md bg-ink px-4 py-2 font-medium text-white disabled:opacity-60"
          disabled={busy}
        >
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </main>
  );
}
