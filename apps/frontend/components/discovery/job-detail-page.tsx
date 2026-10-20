'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';
import {
  fetchJobHuntJob,
  prepareJobHuntJobForTailoring,
  saveJobHuntJob,
  skipJobHuntJob,
  type JobHuntJobDetail,
} from '@/lib/api/jobhunt-discovery';
import { Alert } from '@/components/ui/alert';
import { Button, buttonClass } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { EmptyState } from '@/components/ui/empty-state';
import { Textarea } from '@/components/ui/textarea';
import { PageFrame } from '@/components/ui/page-frame';
import { PageHeader } from '@/components/ui/page-header';
import { useTranslations } from '@/lib/i18n';

export function JobDetailPage({
  jobKey,
  backHref = '/discover/jobs',
}: {
  jobKey: string;
  backHref?: string;
}) {
  const { t } = useTranslations();
  const router = useRouter();
  const [job, setJob] = useState<JobHuntJobDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [jobDescription, setJobDescription] = useState('');
  const [preparing, setPreparing] = useState(false);
  const [prepareError, setPrepareError] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [actionError, setActionError] = useState(false);

  const needsManualDescription =
    !job?.description_text || !['complete', 'manual'].includes(job.description_status);
  const descriptionReady = !needsManualDescription || jobDescription.trim().length >= 50;

  const handleTailor = async () => {
    if (!job || !descriptionReady || preparing) return;
    setPreparing(true);
    setPrepareError(false);
    try {
      const prepared = await prepareJobHuntJobForTailoring(
        job.job_key,
        needsManualDescription ? jobDescription : undefined
      );
      router.push(`/tailor?job_id=${encodeURIComponent(prepared.job_id)}`);
    } catch {
      setPrepareError(true);
      setPreparing(false);
    }
  };

  const handleDiscoveryAction = async (action: 'save' | 'skip') => {
    if (!job || actionBusy) return;
    setActionBusy(true);
    setActionError(false);
    try {
      if (action === 'save') await saveJobHuntJob(job.job_key);
      else await skipJobHuntJob(job.job_key);
      setJob({ ...job, pipeline_state: action === 'save' ? 'saved' : 'skipped' });
    } catch {
      setActionError(true);
    } finally {
      setActionBusy(false);
    }
  };

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(false);
    fetchJobHuntJob(jobKey, controller.signal)
      .then((result) => {
        if (!controller.signal.aborted) setJob(result);
      })
      .catch(() => {
        if (!controller.signal.aborted) setError(true);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [jobKey]);

  return (
    <PageFrame>
      <PageHeader>
        <PageHeader.Back href={backHref}>{t('jobHuntDiscovery.backToJobs')}</PageHeader.Back>
        <PageHeader.Title>{job?.title ?? t('jobHuntDiscovery.detailTitle')}</PageHeader.Title>
        <PageHeader.Subtitle>{t('jobHuntDiscovery.detailSubtitle')}</PageHeader.Subtitle>
      </PageHeader>
      <main className="space-y-6 p-6 md:p-10" aria-busy={loading}>
        {loading && (
          <p role="status" className="font-mono text-sm uppercase text-steel">
            {t('jobHuntDiscovery.loading')}
          </p>
        )}
        {error && (
          <Alert tone="error" title={t('common.error')}>
            {t('jobHuntDiscovery.detailError')}
          </Alert>
        )}
        {!loading && !error && job && (
          <Card variant="raised">
            <CardContent className="space-y-6">
              <dl className="grid grid-cols-1 gap-5 sm:grid-cols-2">
                <div>
                  <dt className="font-mono text-xs uppercase text-steel">
                    {t('jobHuntDiscovery.company')}
                  </dt>
                  <dd className="mt-1 text-ink">
                    {job.company ?? t('jobHuntDiscovery.noCompany')}
                  </dd>
                </div>
                <div>
                  <dt className="font-mono text-xs uppercase text-steel">Source</dt>
                  <dd className="mt-1 text-ink">
                    {job.source ?? t('jobHuntDiscovery.dateUnavailable')}
                  </dd>
                </div>
                <div>
                  <dt className="font-mono text-xs uppercase text-steel">
                    {t('jobHuntDiscovery.location')}
                  </dt>
                  <dd className="mt-1 text-ink">
                    {job.location ?? t('jobHuntDiscovery.noLocation')}
                  </dd>
                </div>
                <div>
                  <dt className="font-mono text-xs uppercase text-steel">
                    {t('jobHuntDiscovery.posted')}
                  </dt>
                  <dd className="mt-1 text-ink">
                    {job.posted_date ?? t('jobHuntDiscovery.dateUnavailable')}
                  </dd>
                </div>
                <div>
                  <dt className="font-mono text-xs uppercase text-steel">
                    {t('jobHuntDiscovery.description')}
                  </dt>
                  <dd className="mt-1 text-ink">
                    {job.description_status}:{' '}
                    {job.description_available
                      ? t('jobHuntDiscovery.descriptionAvailable')
                      : t('jobHuntDiscovery.descriptionUnavailable')}
                  </dd>
                </div>
                {job.description_method && (
                  <div>
                    <dt className="font-mono text-xs uppercase text-steel">
                      {t('jobHuntDiscovery.descriptionMethod')}
                    </dt>
                    <dd className="mt-1 text-ink">{job.description_method}</dd>
                  </div>
                )}
                {job.description_retrieved_at && (
                  <div>
                    <dt className="font-mono text-xs uppercase text-steel">
                      {t('jobHuntDiscovery.descriptionRetrievedAt')}
                    </dt>
                    <dd className="mt-1 text-ink">{job.description_retrieved_at}</dd>
                  </div>
                )}
              </dl>
              {job.description_text && (
                <section className="space-y-2 border-t border-panel pt-5">
                  <h2 className="font-serif text-xl">{t('jobHuntDiscovery.descriptionText')}</h2>
                  <pre className="max-h-[32rem] overflow-auto whitespace-pre-wrap break-words border border-ink bg-paper p-4 font-sans text-sm text-ink">
                    {job.description_text}
                  </pre>
                </section>
              )}
              <div className="flex flex-wrap gap-3 border-t border-panel pt-5">
                <a
                  href={job.source_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className={buttonClass({ variant: 'default' })}
                >
                  {t('jobHuntDiscovery.viewSource')}
                </a>
                <Link href="/discover/jobs" className={buttonClass({ variant: 'outline' })}>
                  {t('jobHuntDiscovery.backToJobs')}
                </Link>
              </div>
              {job.pipeline_state === 'new' ? (
                <div className="flex flex-wrap gap-3 border-t border-panel pt-5">
                  <Button
                    type="button"
                    variant="outline"
                    disabled={actionBusy}
                    onClick={() => void handleDiscoveryAction('save')}
                  >
                    {t('jobHuntDiscovery.saveJob')}
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    disabled={actionBusy}
                    onClick={() => void handleDiscoveryAction('skip')}
                  >
                    {t('jobHuntDiscovery.skipJob')}
                  </Button>
                </div>
              ) : (
                <p className="border-t border-panel pt-5 font-mono text-xs uppercase text-steel">
                  {job.pipeline_state === 'saved'
                    ? t('jobHuntDiscovery.stateSaved')
                    : t('jobHuntDiscovery.stateSkipped')}
                </p>
              )}
              {actionError && (
                <Alert tone="error" title={t('common.error')}>
                  {t('jobHuntDiscovery.actionError')}
                </Alert>
              )}
              {job.pipeline_state !== 'skipped' && (
                <div className="space-y-3 border-t border-panel pt-5">
                  <h2 className="font-serif text-xl">{t('nav.tailor')}</h2>
                  {needsManualDescription && (
                    <>
                      <p className="text-sm text-ink-soft">
                        {t('jobHuntDiscovery.manualPasteHint')}
                      </p>
                      <Textarea
                        aria-label={t('jobHuntDiscovery.description')}
                        className="min-h-48"
                        value={jobDescription}
                        onChange={(event) => setJobDescription(event.target.value)}
                        placeholder={t('tailor.jobDescriptionPlaceholder')}
                        disabled={preparing}
                      />
                    </>
                  )}
                  {prepareError && (
                    <Alert tone="error" title={t('common.error')}>
                      {t('jobHuntDiscovery.detailError')}
                    </Alert>
                  )}
                  <Button
                    type="button"
                    onClick={() => void handleTailor()}
                    disabled={!descriptionReady || preparing}
                  >
                    {preparing
                      ? t('jobHuntDiscovery.preparing')
                      : t('jobHuntDiscovery.tailorResume')}
                  </Button>
                </div>
              )}
            </CardContent>
          </Card>
        )}
        {!loading && !error && !job && (
          <EmptyState title={t('jobHuntDiscovery.notFound')} variant="framed" />
        )}
      </main>
    </PageFrame>
  );
}
