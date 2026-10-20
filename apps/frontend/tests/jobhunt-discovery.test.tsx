import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { JobDetailPage } from '@/components/discovery/job-detail-page';
import { JobListPage } from '@/components/discovery/job-list-page';
import {
  createSavedJobHuntSearch,
  deleteSavedJobHuntSearch,
  fetchSavedJobHuntSearches,
  fetchJobHuntJob,
  fetchJobHuntJobs,
  fetchLiveJobHuntJobs,
  prepareJobHuntJobForTailoring,
  runSavedJobHuntSearch,
  searchLiveJobHuntJobs,
  saveJobHuntJob,
  skipJobHuntJob,
  updateSavedJobHuntSearch,
  type JobHuntJobPage,
  type JobHuntJobDetail,
  type JobHuntJobSummary,
} from '@/lib/api/jobhunt-discovery';

const navigation = vi.hoisted(() => ({ push: vi.fn() }));

vi.mock('next/navigation', () => ({ useRouter: () => navigation }));

vi.mock('@/lib/i18n', () => ({
  useTranslations: () => ({ t: (key: string) => key }),
}));

vi.mock('@/lib/api/jobhunt-discovery', () => ({
  createSavedJobHuntSearch: vi.fn(),
  deleteSavedJobHuntSearch: vi.fn(),
  fetchSavedJobHuntSearches: vi.fn(),
  fetchJobHuntJob: vi.fn(),
  fetchJobHuntJobs: vi.fn(),
  fetchLiveJobHuntJobs: vi.fn(),
  searchLiveJobHuntJobs: vi.fn(),
  prepareJobHuntJobForTailoring: vi.fn(),
  runSavedJobHuntSearch: vi.fn(),
  saveJobHuntJob: vi.fn(),
  skipJobHuntJob: vi.fn(),
  updateSavedJobHuntSearch: vi.fn(),
}));

const job: JobHuntJobDetail = {
  job_key: 'job-key-1',
  title: 'AI Engineer',
  company: 'Example Co',
  location: 'Dubai',
  source_url: 'https://jobs.example/1',
  source: 'portal',
  posted_date: '2026-10-01',
  first_seen: '2026-10-02',
  description_status: 'missing',
  description_available: false,
  pipeline_state: 'new',
  description_text: null,
  description_method: null,
  description_retrieved_at: null,
};

function page(items: JobHuntJobSummary[], currentPage = 1, pages = 1): JobHuntJobPage {
  return {
    items,
    page: currentPage,
    page_size: 20,
    total: items.length,
    pages,
    offset: (currentPage - 1) * 20,
  };
}

beforeEach(() => {
  vi.mocked(fetchSavedJobHuntSearches).mockResolvedValue({ items: [], total: 0 });
  vi.mocked(createSavedJobHuntSearch).mockResolvedValue({
    search_id: 'saved-search-1',
    name: 'AI jobs',
    query: null,
    filters: {},
    enabled: false,
    interval_minutes: 1440,
    next_run_at: null,
    last_run_at: null,
    last_run_status: null,
    last_run_id: null,
    created_at: '2026-10-20T00:00:00Z',
    updated_at: '2026-10-20T00:00:00Z',
  });
  vi.mocked(deleteSavedJobHuntSearch).mockResolvedValue();
  vi.mocked(updateSavedJobHuntSearch).mockResolvedValue({
    search_id: 'saved-search-1',
    name: 'AI jobs',
    query: null,
    filters: {},
    enabled: true,
    interval_minutes: 1440,
    next_run_at: '2026-10-21T00:00:00Z',
    last_run_at: null,
    last_run_status: null,
    last_run_id: null,
    created_at: '2026-10-20T00:00:00Z',
    updated_at: '2026-10-20T00:00:00Z',
  });
  vi.mocked(fetchLiveJobHuntJobs).mockResolvedValue({
    items: [],
    total: 0,
    page: 1,
    page_size: 10,
    pages: 0,
  });
  vi.mocked(searchLiveJobHuntJobs).mockResolvedValue({
    run_id: 'run-1',
    provider: 'freehire',
    status: 'complete',
    query_groups: [],
    found: 0,
    new: 0,
    items: [],
    errors: [],
  });
  vi.mocked(runSavedJobHuntSearch).mockResolvedValue({
    run_id: 'run-1',
    provider: 'freehire',
    status: 'complete',
    query_groups: [],
    found: 0,
    new: 0,
    items: [],
    errors: [],
  });
});

afterEach(() => vi.clearAllMocks());

describe('JobListPage', () => {
  it('searches the saved profile against the live source and links locally stored results', async () => {
    vi.mocked(fetchJobHuntJobs).mockResolvedValue(page([]));
    const liveResult = {
      job_key: 'freehire:ai-engineer-123',
      canonical_id: 'ai-engineer-123',
      provider: 'freehire',
      source: 'greenhouse',
      title: 'AI Engineer',
      company: 'Example Co',
      location: 'Dubai',
      source_url: 'https://jobs.example/1',
      posted_date: '2026-10-10',
      description_status: 'complete' as const,
      description_available: true,
      fit_score: 96,
      fit_evidence: null,
      salary: null,
      work_mode: 'onsite',
      employment_type: 'full_time',
      retrieved_at: '2026-10-20T00:00:00Z',
      last_checked_at: '2026-10-20T00:00:00Z',
      pipeline_state: 'new' as const,
    };
    vi.mocked(fetchLiveJobHuntJobs)
      .mockResolvedValueOnce({ items: [], total: 0, page: 1, page_size: 10, pages: 0 })
      .mockResolvedValueOnce({
        items: [liveResult],
        total: 1,
        page: 1,
        page_size: 10,
        pages: 1,
      });
    vi.mocked(searchLiveJobHuntJobs).mockResolvedValue({
      run_id: 'run-1',
      provider: 'freehire',
      status: 'complete',
      query_groups: ['title:"AI Engineer"'],
      found: 1,
      new: 1,
      items: [liveResult],
      errors: [],
    });
    render(<JobListPage />);

    fireEvent.click(await screen.findByRole('button', { name: 'jobHuntLive.searchProfile' }));
    expect(await screen.findByRole('link', { name: 'AI Engineer' })).toHaveAttribute(
      'href',
      '/discover/jobs/freehire%3Aai-engineer-123'
    );
    expect(searchLiveJobHuntJobs).toHaveBeenCalledWith({});
    expect(await screen.findByText('jobHuntLive.searchSummary')).toBeInTheDocument();
  });

  it('saves a profile search and only schedules it when explicitly enabled', async () => {
    vi.mocked(fetchJobHuntJobs).mockResolvedValue(page([]));
    const savedSearch = {
      search_id: 'saved-search-1',
      name: 'AI jobs',
      query: null,
      filters: {},
      enabled: true,
      interval_minutes: 1440,
      next_run_at: '2026-10-21T00:00:00Z',
      last_run_at: null,
      last_run_status: null,
      last_run_id: null,
      created_at: '2026-10-20T00:00:00Z',
      updated_at: '2026-10-20T00:00:00Z',
    };
    vi.mocked(fetchSavedJobHuntSearches)
      .mockResolvedValueOnce({ items: [], total: 0 })
      .mockResolvedValueOnce({ items: [savedSearch], total: 1 });
    vi.mocked(createSavedJobHuntSearch).mockResolvedValue(savedSearch);
    render(<JobListPage />);

    fireEvent.change(screen.getByLabelText('jobHuntLive.searchName'), {
      target: { value: 'AI jobs' },
    });
    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: 'jobHuntLive.saveSearch' }));

    await waitFor(() =>
      expect(createSavedJobHuntSearch).toHaveBeenCalledWith({
        name: 'AI jobs',
        enabled: true,
        interval_minutes: 1440,
      })
    );
    expect(await screen.findByText('AI jobs')).toBeInTheDocument();
    expect(await screen.findByText(/jobHuntLive.scheduled/)).toBeInTheDocument();
  });

  it('renders read-only listings and links to a job detail page', async () => {
    vi.mocked(fetchJobHuntJobs).mockResolvedValue(page([job]));
    render(<JobListPage />);

    const titleLink = await screen.findByRole('link', { name: 'AI Engineer' });
    expect(titleLink).toHaveAttribute(
      'href',
      expect.stringContaining('/discover/jobs/job-key-1?returnTo=')
    );
    expect(screen.getByText('Example Co · Dubai')).toBeInTheDocument();
    expect(
      screen.getByText(/missing: jobHuntDiscovery.descriptionUnavailable/)
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^(apply|save|dismiss)$/i })).toBeNull();
  });

  it('shows a clear empty state', async () => {
    vi.mocked(fetchJobHuntJobs).mockResolvedValue(page([]));
    render(<JobListPage />);

    expect(await screen.findByText('jobHuntDiscovery.emptyTitle')).toBeInTheDocument();
  });

  it('shows an error and retries safely', async () => {
    vi.mocked(fetchJobHuntJobs)
      .mockRejectedValueOnce(new Error('private filesystem detail'))
      .mockResolvedValueOnce(page([job]));
    render(<JobListPage />);

    expect(await screen.findByText('jobHuntDiscovery.loadError')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'jobHuntDiscovery.retry' }));
    expect(await screen.findByRole('link', { name: 'AI Engineer' })).toBeInTheDocument();
    expect(screen.queryByText('private filesystem detail')).toBeNull();
  });

  it('paginates through the bounded API result', async () => {
    vi.mocked(fetchJobHuntJobs)
      .mockResolvedValueOnce(page([job], 1, 2))
      .mockResolvedValueOnce(
        page([{ ...job, job_key: 'job-key-2', title: 'Python Developer' }], 2, 2)
      );
    render(<JobListPage />);

    fireEvent.click(await screen.findByRole('button', { name: 'jobHuntDiscovery.next' }));
    expect(await screen.findByRole('link', { name: 'Python Developer' })).toBeInTheDocument();
    expect(fetchJobHuntJobs).toHaveBeenLastCalledWith(
      expect.objectContaining({ page: 2, pageSize: 20 })
    );
  });

  it('applies schema-supported filters and sorting', async () => {
    vi.mocked(fetchJobHuntJobs).mockResolvedValue(page([job]));
    render(<JobListPage />);
    await screen.findByRole('link', { name: 'AI Engineer' });
    fireEvent.change(screen.getByLabelText('Source'), { target: { value: 'portal' } });
    fireEvent.change(screen.getByLabelText('Sort by'), { target: { value: 'title' } });
    fireEvent.click(screen.getByRole('button', { name: /Apply filters/ }));
    await waitFor(() =>
      expect(fetchJobHuntJobs).toHaveBeenLastCalledWith(
        expect.objectContaining({ source: 'portal', sort: 'title', direction: 'desc' })
      )
    );
  });
});

describe('JobDetailPage', () => {
  it('renders the full description with its extraction provenance', async () => {
    vi.mocked(fetchJobHuntJob).mockResolvedValue({
      ...job,
      description_status: 'complete',
      description_available: true,
      description_text: 'Full verified job description.',
      description_method: 'jobhunt_snapshot',
      description_retrieved_at: '2026-10-19T10:00:00Z',
    });
    render(<JobDetailPage jobKey={job.job_key} />);

    expect(await screen.findByText('Full verified job description.')).toBeInTheDocument();
    expect(screen.getByText('jobhunt_snapshot')).toBeInTheDocument();
    expect(screen.getByText('2026-10-19T10:00:00Z')).toBeInTheDocument();
  });

  it('shows safe fields and links to the original listing on explicit click', async () => {
    vi.mocked(fetchJobHuntJob).mockResolvedValue(job);
    render(<JobDetailPage jobKey="job-key-1" />);

    const source = await screen.findByRole('link', { name: 'jobHuntDiscovery.viewSource' });
    expect(source).toHaveAttribute('href', job.source_url);
    expect(source).toHaveAttribute('target', '_blank');
    expect(source).toHaveAttribute('rel', 'noopener noreferrer');
    expect(screen.queryByText(/application|resume contents/i)).toBeNull();
  });

  it('renders a generic error for unavailable job details', async () => {
    vi.mocked(fetchJobHuntJob).mockRejectedValue(new Error('private database path'));
    render(<JobDetailPage jobKey="missing" />);

    expect(await screen.findByText('jobHuntDiscovery.detailError')).toBeInTheDocument();
    expect(screen.queryByText('private database path')).toBeNull();
  });

  it('saves a discovered job to the tracker and shows the persisted state', async () => {
    vi.mocked(fetchJobHuntJob).mockResolvedValue(job);
    vi.mocked(saveJobHuntJob).mockResolvedValue();
    render(<JobDetailPage jobKey={job.job_key} />);

    fireEvent.click(await screen.findByRole('button', { name: 'jobHuntDiscovery.saveJob' }));
    await waitFor(() => expect(saveJobHuntJob).toHaveBeenCalledWith(job.job_key));
    expect(await screen.findByText('jobHuntDiscovery.stateSaved')).toBeInTheDocument();
  });

  it('persists skipped state without exposing tailoring for that job', async () => {
    vi.mocked(fetchJobHuntJob).mockResolvedValue(job);
    vi.mocked(skipJobHuntJob).mockResolvedValue();
    render(<JobDetailPage jobKey={job.job_key} />);

    fireEvent.click(await screen.findByRole('button', { name: 'jobHuntDiscovery.skipJob' }));
    await waitFor(() => expect(skipJobHuntJob).toHaveBeenCalledWith(job.job_key));
    expect(await screen.findByText('jobHuntDiscovery.stateSkipped')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'jobHuntDiscovery.tailorResume' })).toBeNull();
  });

  it('requires a full manual description and opens the existing tailor workflow', async () => {
    vi.mocked(fetchJobHuntJob).mockResolvedValue(job);
    vi.mocked(prepareJobHuntJobForTailoring).mockResolvedValue({
      job_key: job.job_key,
      job_id: 'opaque-job-id',
      created: true,
      description_status: 'manual',
    });
    render(<JobDetailPage jobKey={job.job_key} />);

    const tailor = await screen.findByRole('button', {
      name: 'jobHuntDiscovery.tailorResume',
    });
    expect(tailor).toBeDisabled();
    fireEvent.change(screen.getByRole('textbox', { name: 'jobHuntDiscovery.description' }), {
      target: { value: 'Full job description '.repeat(6) },
    });
    fireEvent.click(tailor);

    await waitFor(() =>
      expect(prepareJobHuntJobForTailoring).toHaveBeenCalledWith(
        job.job_key,
        'Full job description '.repeat(6)
      )
    );
    await waitFor(() =>
      expect(navigation.push).toHaveBeenCalledWith('/tailor?job_id=opaque-job-id')
    );
  });
});
