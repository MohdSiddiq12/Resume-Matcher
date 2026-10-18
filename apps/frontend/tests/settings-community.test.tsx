import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { SettingsAbout } from '@/components/settings/community';
import { APP_VERSION } from '@/lib/config/version';

vi.mock('@/lib/i18n', () => {
  const t = (key: string) => key;
  return { useTranslations: () => ({ t }) };
});

describe('settings About section', () => {
  it('shows the app version without external community links', () => {
    render(<SettingsAbout />);

    expect(screen.getByRole('heading', { level: 2, name: 'community.aboutTitle' })).toBeVisible();
    expect(screen.getByText(`v${APP_VERSION}`)).toBeInTheDocument();
    expect(screen.queryByRole('link')).toBeNull();
  });
});
