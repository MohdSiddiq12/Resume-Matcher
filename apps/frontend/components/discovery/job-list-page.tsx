'use client';

import Link from 'next/link';
import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react';
import { fetchJobHuntJobs, type JobHuntJobPage } from '@/lib/api/jobhunt-discovery';
import { Alert } from '@/components/ui/alert';
import { Button, buttonClass } from '@/components/ui/button';
import { Card, CardContent, CardTitle } from '@/components/ui/card';
import { EmptyState } from '@/components/ui/empty-state';
import { Input } from '@/components/ui/input';
import { PageFrame } from '@/components/ui/page-frame';
import { PageHeader } from '@/components/ui/page-header';
import { useTranslations } from '@/lib/i18n';
import { LiveSearchPanel } from '@/components/discovery/live-search-panel';

type Filters = {
  q: string;
  location: string;
  source: string;
  description: string;
  after: string;
  before: string;
  sort: string;
  direction: 'asc' | 'desc';
};

const DEFAULT_FILTERS: Filters = {
  q: '',
  location: '',
  source: '',
  description: '',
  after: '',
  before: '',
  sort: 'first_seen',
  direction: 'desc',
};

function readInitialFilters(): Filters {
  if (typeof window === 'undefined') return DEFAULT_FILTERS;
  const params = new URLSearchParams(window.location.search);
  const sort = params.get('sort') ?? DEFAULT_FILTERS.sort;
  const direction = params.get('direction');
  return {
    q: params.get('q') ?? '',
    location: params.get('location') ?? '',
    source: params.get('source') ?? '',
    description: params.get('description_available') ?? '',
    after: params.get('posted_after') ?? '',
    before: params.get('posted_before') ?? '',
    sort: ['first_seen', 'posted_date', 'title', 'company', 'location', 'source'].includes(sort)
      ? sort
      : DEFAULT_FILTERS.sort,
    direction: direction === 'asc' ? 'asc' : 'desc',
  };
}

export function JobListPage() {
  const { t } = useTranslations();
  const [data, setData] = useState<JobHuntJobPage | null>(null);
  const [filters, setFilters] = useState<Filters>(readInitialFilters);
  const [draft, setDraft] = useState<Filters>(readInitialFilters);
  const [page, setPage] = useState(() => {
    if (typeof window === 'undefined') return 1;
    const value = Number(new URLSearchParams(window.location.search).get('page') ?? 1);
    return Number.isSafeInteger(value) && value > 0 && value <= 501 ? value : 1;
  });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [retry, setRetry] = useState(0);
  const search = useMemo(() => {
    const params = new URLSearchParams();
    if (filters.q) params.set('q', filters.q);
    if (filters.location) params.set('location', filters.location);
    if (filters.source) params.set('source', filters.source);
    if (filters.description) params.set('description_available', filters.description);
    if (filters.after) params.set('posted_after', filters.after);
    if (filters.before) params.set('posted_before', filters.before);
    params.set('sort', filters.sort);
    params.set('direction', filters.direction);
    params.set('page', String(page));
    return params.toString();
  }, [filters, page]);

  useEffect(() => {
    window.history.replaceState(null, '', `/discover/jobs?${search}`);
  }, [search]);

  const load = useCallback(
    async (signal: AbortSignal) => {
      setLoading(true);
      setError(false);
      try {
        const result = await fetchJobHuntJobs({
          page,
          pageSize: 20,
          query: filters.q,
          location: filters.location,
          source: filters.source,
          descriptionAvailable: filters.description === '' ? null : filters.description === 'true',
          postedAfter: filters.after,
          postedBefore: filters.before,
          sort: filters.sort,
          direction: filters.direction,
          offset: (page - 1) * 20,
          signal,
        });
        if (!signal.aborted) setData(result);
      } catch {
        if (!signal.aborted) setError(true);
      } finally {
        if (!signal.aborted) setLoading(false);
      }
    },
    [filters, page]
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load, retry]);

  function applyFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFilters({
      ...draft,
      q: draft.q.trim(),
      location: draft.location.trim(),
      source: draft.source.trim(),
    });
    setPage(1);
  }

  const hasNext = data !== null && page < data.pages;
  const hasPrevious = page > 1;
  const returnTo = `/discover/jobs?${search}`;

  return (
    <PageFrame>
      <PageHeader>
        <PageHeader.Back href="/dashboard">{t('nav.backToDashboard')}</PageHeader.Back>
        <PageHeader.Title>{t('jobHuntDiscovery.title')}</PageHeader.Title>
        <PageHeader.Subtitle>{t('jobHuntDiscovery.subtitle')}</PageHeader.Subtitle>
      </PageHeader>
      <main className="space-y-6 p-6 md:p-10" aria-busy={loading}>
        <LiveSearchPanel />
        <form
          onSubmit={applyFilters}
          className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4"
        >
          <label className="text-xs font-mono uppercase">
            {t('jobHuntDiscovery.searchLabel')}
            <Input
              value={draft.q}
              maxLength={100}
              placeholder={t('jobHuntDiscovery.searchPlaceholder')}
              onChange={(e) => setDraft({ ...draft, q: e.target.value })}
            />
          </label>
          <label className="text-xs font-mono uppercase">
            Location
            <Input
              value={draft.location}
              maxLength={100}
              onChange={(e) => setDraft({ ...draft, location: e.target.value })}
            />
          </label>
          <label className="text-xs font-mono uppercase">
            Source
            <Input
              value={draft.source}
              maxLength={100}
              onChange={(e) => setDraft({ ...draft, source: e.target.value })}
            />
          </label>
          <label className="text-xs font-mono uppercase">
            Description
            <select
              className="h-10 w-full border border-ink bg-white px-3 text-sm"
              value={draft.description}
              onChange={(e) => setDraft({ ...draft, description: e.target.value })}
            >
              <option value="">Any</option>
              <option value="true">Available</option>
              <option value="false">Unavailable</option>
            </select>
          </label>
          <label className="text-xs font-mono uppercase">
            Posted after
            <Input
              type="date"
              value={draft.after}
              onChange={(e) => setDraft({ ...draft, after: e.target.value })}
            />
          </label>
          <label className="text-xs font-mono uppercase">
            Posted before
            <Input
              type="date"
              value={draft.before}
              onChange={(e) => setDraft({ ...draft, before: e.target.value })}
            />
          </label>
          <label className="text-xs font-mono uppercase">
            Sort by
            <select
              className="h-10 w-full border border-ink bg-white px-3 text-sm"
              value={draft.sort}
              onChange={(e) => setDraft({ ...draft, sort: e.target.value })}
            >
              <option value="first_seen">First seen</option>
              <option value="posted_date">Posting date</option>
              <option value="title">Title</option>
              <option value="company">Company</option>
              <option value="location">Location</option>
              <option value="source">Source</option>
            </select>
          </label>
          <label className="text-xs font-mono uppercase">
            Direction
            <select
              className="h-10 w-full border border-ink bg-white px-3 text-sm"
              value={draft.direction}
              onChange={(e) =>
                setDraft({ ...draft, direction: e.target.value as Filters['direction'] })
              }
            >
              <option value="desc">Descending</option>
              <option value="asc">Ascending</option>
            </select>
          </label>
          <div className="sm:col-span-2 lg:col-span-4">
            <Button type="submit" variant="outline">
              {t('jobHuntDiscovery.searchButton')} / Apply filters
            </Button>
          </div>
        </form>

        {loading && (
          <p role="status" className="font-mono text-sm uppercase text-steel">
            {t('jobHuntDiscovery.loading')}
          </p>
        )}
        {error && (
          <Alert tone="error" title={t('common.error')}>
            <div className="flex flex-wrap items-center justify-between gap-4">
              <span>{t('jobHuntDiscovery.loadError')}</span>
              <Button
                type="button"
                variant="outline"
                onClick={() => setRetry((value) => value + 1)}
              >
                {t('jobHuntDiscovery.retry')}
              </Button>
            </div>
          </Alert>
        )}
        {!loading && !error && data?.items.length === 0 && (
          <EmptyState
            title={t('jobHuntDiscovery.emptyTitle')}
            description={t('jobHuntDiscovery.emptyDescription')}
            variant="framed"
          />
        )}
        {!loading && !error && data && data.items.length > 0 && (
          <>
            <p className="font-mono text-xs uppercase tracking-wide text-steel">
              {data.total} · {t('jobHuntDiscovery.totalListings')}
            </p>
            <ul className="grid grid-cols-1 gap-4 lg:grid-cols-2">
              {data.items.map((job) => (
                <li key={job.job_key}>
                  <Card variant="raised" noPadding className="h-full">
                    <CardContent className="flex h-full flex-col gap-4 p-5">
                      <div>
                        <CardTitle className="text-xl leading-tight">
                          <Link
                            href={`/discover/jobs/${encodeURIComponent(job.job_key)}?returnTo=${encodeURIComponent(returnTo)}`}
                            className="underline decoration-transparent underline-offset-4 hover:decoration-current focus-visible:outline-2 focus-visible:outline-primary"
                          >
                            {job.title}
                          </Link>
                        </CardTitle>
                        <p className="mt-2 text-sm text-ink-soft">
                          {job.company ?? t('jobHuntDiscovery.noCompany')}
                          {job.location && ` · ${job.location}`}
                        </p>
                      </div>
                      <div className="mt-auto flex flex-wrap items-center justify-between gap-3 border-t border-panel pt-3">
                        <span className="font-mono text-xs uppercase text-steel">
                          {job.posted_date
                            ? `${t('jobHuntDiscovery.posted')}: ${job.posted_date}`
                            : t('jobHuntDiscovery.dateUnavailable')}
                        </span>
                        <span className="font-mono text-xs uppercase text-steel">
                          {job.description_status}:{' '}
                          {job.description_available
                            ? t('jobHuntDiscovery.descriptionAvailable')
                            : t('jobHuntDiscovery.descriptionUnavailable')}
                        </span>
                      </div>
                    </CardContent>
                  </Card>
                </li>
              ))}
            </ul>
            <nav
              aria-label={t('jobHuntDiscovery.pagination')}
              className="flex items-center justify-between gap-4"
            >
              <Button
                type="button"
                variant="outline"
                disabled={!hasPrevious || loading}
                onClick={() => setPage((value) => Math.max(1, value - 1))}
              >
                {t('jobHuntDiscovery.previous')}
              </Button>
              <span aria-live="polite" className="font-mono text-xs uppercase text-steel">
                {t('jobHuntDiscovery.page')} {data.page} / {Math.max(data.pages, 1)}
              </span>
              <Button
                type="button"
                variant="outline"
                disabled={!hasNext || loading}
                onClick={() => setPage((value) => value + 1)}
              >
                {t('jobHuntDiscovery.next')}
              </Button>
            </nav>
            <Link className={buttonClass({ variant: 'link' })} href="/dashboard">
              {t('nav.backToDashboard')}
            </Link>
          </>
        )}
      </main>
    </PageFrame>
  );
}
