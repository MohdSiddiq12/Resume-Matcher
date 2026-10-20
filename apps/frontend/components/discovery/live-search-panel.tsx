'use client';

import Link from 'next/link';
import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { Alert } from '@/components/ui/alert';
import { Button, buttonClass } from '@/components/ui/button';
import { Card, CardContent, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { useTranslations } from '@/lib/i18n';
import {
  createSavedJobHuntSearch,
  deleteSavedJobHuntSearch,
  fetchSavedJobHuntSearches,
  fetchLiveJobHuntJobs,
  runSavedJobHuntSearch,
  searchLiveJobHuntJobs,
  updateSavedJobHuntSearch,
  type JobHuntLiveJob,
  type JobHuntSavedSearch,
} from '@/lib/api/jobhunt-discovery';

export function LiveSearchPanel() {
  const { t } = useTranslations();
  const [jobs, setJobs] = useState<JobHuntLiveJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [summary, setSummary] = useState<string | null>(null);
  const [warning, setWarning] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [savedSearches, setSavedSearches] = useState<JobHuntSavedSearch[]>([]);
  const [savedSearchName, setSavedSearchName] = useState('');
  const [scheduleEnabled, setScheduleEnabled] = useState(false);
  const [intervalMinutes, setIntervalMinutes] = useState(1440);
  const [savingSearch, setSavingSearch] = useState(false);
  const [runningSearchId, setRunningSearchId] = useState<string | null>(null);
  const [filterQuery, setFilterQuery] = useState('');
  const [filterLocation, setFilterLocation] = useState('');
  const [minimumFitScore, setMinimumFitScore] = useState<number | undefined>();

  const loadStoredJobs = useCallback(
    async (signal?: AbortSignal) => {
      const result = await fetchLiveJobHuntJobs({
        page: 1,
        pageSize: 10,
        query: filterQuery.trim() || undefined,
        location: filterLocation.trim() || undefined,
        minScore: minimumFitScore,
        signal,
      });
      if (!signal?.aborted) setJobs(result.items);
    },
    [filterLocation, filterQuery, minimumFitScore]
  );

  const loadSavedSearches = useCallback(async () => {
    const result = await fetchSavedJobHuntSearches();
    setSavedSearches(result.items);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([loadStoredJobs(controller.signal), loadSavedSearches()])
      .catch(() => {
        if (!controller.signal.aborted) setError(t('jobHuntLive.loadError'));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [loadSavedSearches, loadStoredJobs, t]);

  const runSearch = async () => {
    if (searching) return;
    setSearching(true);
    setError(null);
    setWarning(null);
    setSummary(null);
    try {
      const result = await searchLiveJobHuntJobs(query.trim() ? { query: query.trim() } : {});
      await loadStoredJobs();
      setSummary(t('jobHuntLive.searchSummary', { found: result.found, added: result.new }));
      if (result.errors.length > 0) setWarning(t('jobHuntLive.partialWarning'));
    } catch {
      setError(t('jobHuntLive.searchError'));
      // A failed source refresh must leave previously stored jobs visible.
      await loadStoredJobs().catch(() => undefined);
    } finally {
      setSearching(false);
    }
  };

  const saveSearch = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!savedSearchName.trim() || savingSearch) return;
    setSavingSearch(true);
    setError(null);
    try {
      await createSavedJobHuntSearch({
        name: savedSearchName.trim(),
        ...(query.trim() ? { query: query.trim() } : {}),
        enabled: scheduleEnabled,
        interval_minutes: intervalMinutes,
      });
      setSavedSearchName('');
      await loadSavedSearches();
    } catch {
      setError(t('jobHuntLive.saveSearchError'));
    } finally {
      setSavingSearch(false);
    }
  };

  const runSavedSearch = async (search: JobHuntSavedSearch) => {
    if (runningSearchId) return;
    setRunningSearchId(search.search_id);
    setError(null);
    setWarning(null);
    try {
      const result = await runSavedJobHuntSearch(search.search_id);
      await Promise.all([loadStoredJobs(), loadSavedSearches()]);
      setSummary(t('jobHuntLive.searchSummary', { found: result.found, added: result.new }));
      if (result.errors.length > 0) setWarning(t('jobHuntLive.partialWarning'));
    } catch {
      setError(t('jobHuntLive.searchError'));
    } finally {
      setRunningSearchId(null);
    }
  };

  const toggleSavedSearch = async (search: JobHuntSavedSearch) => {
    try {
      await updateSavedJobHuntSearch(search.search_id, { enabled: !search.enabled });
      await loadSavedSearches();
    } catch {
      setError(t('jobHuntLive.saveSearchError'));
    }
  };

  const changeSavedInterval = async (search: JobHuntSavedSearch, value: number) => {
    try {
      await updateSavedJobHuntSearch(search.search_id, {
        interval_minutes: value,
        enabled: search.enabled,
      });
      await loadSavedSearches();
    } catch {
      setError(t('jobHuntLive.saveSearchError'));
    }
  };

  const removeSavedSearch = async (search: JobHuntSavedSearch) => {
    try {
      await deleteSavedJobHuntSearch(search.search_id);
      setSavedSearches((current) => current.filter((item) => item.search_id !== search.search_id));
    } catch {
      setError(t('jobHuntLive.saveSearchError'));
    }
  };

  return (
    <section className="space-y-4 border-b border-ink pb-6" aria-busy={searching || loading}>
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="max-w-3xl space-y-1">
          <h2 className="font-serif text-2xl">{t('jobHuntLive.title')}</h2>
          <p className="text-sm text-ink-soft">{t('jobHuntLive.description')}</p>
        </div>
      </div>
      <form
        className="grid grid-cols-1 items-end gap-3 sm:grid-cols-[minmax(16rem,1fr)_auto]"
        onSubmit={(event) => {
          event.preventDefault();
          void runSearch();
        }}
      >
        <label className="text-xs font-mono uppercase">
          {t('jobHuntLive.queryLabel')}
          <Input
            value={query}
            maxLength={100}
            placeholder={t('jobHuntLive.queryPlaceholder')}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        <Button type="submit" disabled={searching}>
          {searching ? t('jobHuntLive.searching') : t('jobHuntLive.searchProfile')}
        </Button>
      </form>

      <div className="space-y-3 border border-panel bg-paper p-4">
        <h3 className="font-mono text-xs uppercase">{t('jobHuntLive.savedSearches')}</h3>
        <form
          className="grid grid-cols-1 gap-3 md:grid-cols-[minmax(10rem,1fr)_auto_auto_auto]"
          onSubmit={saveSearch}
        >
          <label className="text-xs font-mono uppercase">
            {t('jobHuntLive.searchName')}
            <Input
              value={savedSearchName}
              maxLength={80}
              onChange={(event) => setSavedSearchName(event.target.value)}
              placeholder={t('jobHuntLive.searchNamePlaceholder')}
            />
          </label>
          <label className="flex min-h-10 items-center gap-2 text-xs font-mono uppercase">
            <input
              type="checkbox"
              checked={scheduleEnabled}
              onChange={(event) => setScheduleEnabled(event.target.checked)}
            />
            {t('jobHuntLive.scheduleEnabled')}
          </label>
          <label className="text-xs font-mono uppercase">
            {t('jobHuntLive.refreshEvery')}
            <select
              className="h-10 w-full border border-ink bg-white px-3 text-sm"
              value={intervalMinutes}
              onChange={(event) => setIntervalMinutes(Number(event.target.value))}
              disabled={!scheduleEnabled}
            >
              <option value={60}>{t('jobHuntLive.hourly')}</option>
              <option value={360}>{t('jobHuntLive.sixHours')}</option>
              <option value={1440}>{t('jobHuntLive.daily')}</option>
              <option value={10080}>{t('jobHuntLive.weekly')}</option>
            </select>
          </label>
          <Button type="submit" disabled={savingSearch || !savedSearchName.trim()}>
            {savingSearch ? t('jobHuntLive.savingSearch') : t('jobHuntLive.saveSearch')}
          </Button>
        </form>
        <p className="text-xs text-ink-soft">{t('jobHuntLive.schedulerNote')}</p>
        {savedSearches.length > 0 ? (
          <ul className="divide-y divide-panel border-y border-panel">
            {savedSearches.map((search) => (
              <li
                key={search.search_id}
                className="flex flex-wrap items-center justify-between gap-3 py-3"
              >
                <div>
                  <p className="font-mono text-xs uppercase">{search.name}</p>
                  <p className="text-xs text-ink-soft">
                    {search.query || t('jobHuntLive.profileSearch')}
                    {' · '}
                    {search.enabled ? t('jobHuntLive.scheduled') : t('jobHuntLive.paused')}
                    {search.last_run_status ? ` · ${search.last_run_status}` : ''}
                  </p>
                  {search.enabled && search.next_run_at && (
                    <time className="text-xs text-ink-soft" dateTime={search.next_run_at}>
                      {t('jobHuntLive.nextRun')} {new Date(search.next_run_at).toLocaleString()}
                    </time>
                  )}
                </div>
                <div className="flex flex-wrap gap-3">
                  <label className="sr-only" htmlFor={`interval-${search.search_id}`}>
                    {t('jobHuntLive.refreshEvery')}
                  </label>
                  <select
                    id={`interval-${search.search_id}`}
                    className="h-8 border border-ink bg-white px-2 text-xs"
                    value={search.interval_minutes}
                    disabled={!search.enabled}
                    onChange={(event) =>
                      void changeSavedInterval(search, Number(event.target.value))
                    }
                  >
                    <option value={60}>{t('jobHuntLive.hourly')}</option>
                    <option value={360}>{t('jobHuntLive.sixHours')}</option>
                    <option value={1440}>{t('jobHuntLive.daily')}</option>
                    <option value={10080}>{t('jobHuntLive.weekly')}</option>
                  </select>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    disabled={runningSearchId !== null}
                    onClick={() => void runSavedSearch(search)}
                  >
                    {runningSearchId === search.search_id
                      ? t('jobHuntLive.searching')
                      : t('jobHuntLive.runSavedSearch')}
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => void toggleSavedSearch(search)}
                  >
                    {search.enabled ? t('jobHuntLive.pauseSearch') : t('jobHuntLive.enableSearch')}
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => void removeSavedSearch(search)}
                  >
                    {t('jobHuntLive.deleteSearch')}
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-xs text-ink-soft">{t('jobHuntLive.noSavedSearches')}</p>
        )}
      </div>

      <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
        <label className="text-xs font-mono uppercase">
          {t('jobHuntLive.filterQueryLabel')}
          <Input
            value={filterQuery}
            maxLength={100}
            onChange={(event) => setFilterQuery(event.target.value)}
          />
        </label>
        <label className="text-xs font-mono uppercase">
          {t('jobHuntLive.filterLocationLabel')}
          <Input
            value={filterLocation}
            maxLength={100}
            onChange={(event) => setFilterLocation(event.target.value)}
          />
        </label>
        <label className="text-xs font-mono uppercase">
          {t('jobHuntLive.minimumFit')}
          <select
            className="h-10 w-full border border-ink bg-white px-3 text-sm"
            value={minimumFitScore ?? ''}
            onChange={(event) =>
              setMinimumFitScore(event.target.value ? Number(event.target.value) : undefined)
            }
          >
            <option value="">{t('jobHuntLive.anyFit')}</option>
            <option value="70">70+</option>
            <option value="85">85+</option>
          </select>
        </label>
      </div>

      {summary && (
        <p role="status" className="font-mono text-xs uppercase">
          {summary}
        </p>
      )}
      {warning && <Alert tone="warning">{warning}</Alert>}
      {error && <Alert tone="error">{error}</Alert>}
      {loading && <p role="status">{t('jobHuntLive.loading')}</p>}
      {!loading && jobs.length === 0 && (
        <p className="border border-ink bg-paper p-4 text-sm">{t('jobHuntLive.empty')}</p>
      )}
      {jobs.length > 0 && (
        <>
          <p className="font-mono text-xs uppercase text-steel">
            {t('jobHuntLive.storedCount', { count: jobs.length })}
          </p>
          <ul className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            {jobs.map((job) => (
              <li key={job.job_key}>
                <Card variant="raised" noPadding className="h-full">
                  <CardContent className="flex h-full flex-col gap-3 p-5">
                    <div>
                      <CardTitle className="text-lg leading-tight">
                        <Link
                          href={`/discover/jobs/${encodeURIComponent(job.job_key)}`}
                          className="underline decoration-transparent underline-offset-4 hover:decoration-current focus-visible:outline-2 focus-visible:outline-primary"
                        >
                          {job.title}
                        </Link>
                      </CardTitle>
                      <p className="mt-2 text-sm text-ink-soft">
                        {job.company || t('jobHuntDiscovery.noCompany')}
                        {job.location && ` · ${job.location}`}
                      </p>
                    </div>
                    <div className="mt-auto flex flex-wrap items-center justify-between gap-3 border-t border-panel pt-3">
                      <span className="font-mono text-xs uppercase text-steel">
                        {t('jobHuntLive.fitScore', { score: job.fit_score ?? '—' })}
                      </span>
                      <span className="font-mono text-xs uppercase text-steel">
                        {job.description_status}
                      </span>
                      <span className="font-mono text-xs uppercase text-steel">
                        {job.posted_date || t('jobHuntDiscovery.dateUnavailable')}
                      </span>
                    </div>
                  </CardContent>
                </Card>
              </li>
            ))}
          </ul>
          <Link href="/tracker" className={buttonClass({ variant: 'link' })}>
            {t('nav.applicationTracker')}
          </Link>
        </>
      )}
    </section>
  );
}
