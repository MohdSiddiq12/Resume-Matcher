import { NextRequest, NextResponse } from 'next/server';

const BACKEND_ORIGIN = process.env.BACKEND_ORIGIN || 'http://127.0.0.1:8000';

export async function proxy(request: NextRequest) {
  const { pathname, search } = request.nextUrl;
  if (pathname === '/login' || pathname.startsWith('/api/') || pathname === '/auth/callback') {
    return NextResponse.next();
  }

  try {
    const response = await fetch(`${BACKEND_ORIGIN}/api/v1/auth/session`, {
      headers: { cookie: request.headers.get('cookie') || '' },
      cache: 'no-store',
    });
    const session = (await response.json()) as { authenticated?: boolean };
    if (response.ok && session.authenticated) return NextResponse.next();
  } catch {
    // Fail closed if the authentication service cannot be reached.
    return new NextResponse('Authentication service unavailable.', { status: 503 });
  }

  const loginUrl = new URL('/login', request.url);
  loginUrl.searchParams.set('returnTo', `${pathname}${search}`);
  return NextResponse.redirect(loginUrl);
}

export const config = {
  matcher: ['/((?!_next/static|_next/image|favicon.ico|robots.txt|sitemap.xml).*)'],
};
