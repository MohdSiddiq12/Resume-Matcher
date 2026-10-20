'use client';

import React, { useEffect, useState } from 'react';
import Link from 'next/link';
import { PageFrame } from '@/components/ui/page-frame';
import { PageHeader } from '@/components/ui/page-header';
import { Alert } from '@/components/ui/alert';
import { buttonClass } from '@/components/ui/button';
import { listFollowUps, type FollowUpListResponse } from '@/lib/api/tracker';
import { useTranslations } from '@/lib/i18n';

export default function FollowUpsPage() {
  const { t } = useTranslations();
  const [data, setData] = useState<FollowUpListResponse | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let active = true;
    listFollowUps()
      .then((result) => active && setData(result))
      .catch(() => active && setError(true));
    return () => {
      active = false;
    };
  }, []);

  return (
    <PageFrame width="wide">
      <PageHeader>
        <PageHeader.Title>{t('jobHuntFollowUps.title')}</PageHeader.Title>
        <PageHeader.Subtitle>{t('jobHuntFollowUps.subtitle')}</PageHeader.Subtitle>
      </PageHeader>
      <main className="flex-1 space-y-4 overflow-y-auto p-6">
        <div>
          <Link href="/tracker" className={buttonClass({ variant: 'outline' })}>
            {t('nav.applicationTracker')}
          </Link>
        </div>
        {error && <Alert tone="error">{t('jobHuntFollowUps.loadError')}</Alert>}
        {!data && !error && <p role="status">{t('jobHuntFollowUps.loading')}</p>}
        {data?.items.length === 0 && <p>{t('jobHuntFollowUps.empty')}</p>}
        {data?.items.map((item) => (
          <article key={item.application_id} className="border border-ink bg-canvas p-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <h2 className="font-semibold">{item.role || t('jobHuntFollowUps.roleUnknown')}</h2>
                <p className="text-sm text-ink-soft">
                  {item.company || t('jobHuntFollowUps.companyUnknown')}
                </p>
              </div>
              <div className="flex items-center gap-3 font-mono text-xs uppercase">
                {item.follow_up_overdue && (
                  <span className="text-destructive">{t('jobHuntFollowUps.overdue')}</span>
                )}
                <time dateTime={item.follow_up_date || undefined}>{item.follow_up_date}</time>
              </div>
              <Link href="/tracker" className={buttonClass({ variant: 'outline' })}>
                {t('jobHuntFollowUps.openTracker')}
              </Link>
            </div>
          </article>
        ))}
      </main>
    </PageFrame>
  );
}
