import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import LoginPage from '@/app/login/page';

const mockReplace = vi.hoisted(() => vi.fn());
vi.mock('next/navigation', () => ({ useRouter: () => ({ replace: mockReplace }) }));

describe('login page', () => {
  beforeEach(() => {
    mockReplace.mockReset();
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response('{}', { status: 200 }))
    );
  });

  it('submits the password and navigates after a successful login', async () => {
    render(<LoginPage />);
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'sample-password' } });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));

    await waitFor(() => expect(mockReplace).toHaveBeenCalledWith('/'));
    expect(fetch).toHaveBeenCalledWith(
      '/api/v1/auth/login',
      expect.objectContaining({ method: 'POST' })
    );
  });
});
