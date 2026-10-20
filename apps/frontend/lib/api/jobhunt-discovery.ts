import { apiFetch } from './client';

export interface JobHuntJobSummary {
  job_key: string;
  title: string;
  company: string | null;
  location: string | null;
  source_url: string;
  source: string | null;
  posted_date: string | null;
  first_seen: string | null;
  description_status: 'missing' | 'complete' | 'partial' | 'manual' | 'failed' | 'unknown';
  description_available: boolean;
  pipeline_state: 'new' | 'saved' | 'skipped';
}

export interface JobHuntJobDetail extends JobHuntJobSummary {
  description_text: string | null;
  description_method: string | null;
  description_retrieved_at: string | null;
}

export interface JobHuntJobPage {
  items: JobHuntJobSummary[];
  page: number;
  page_size: number;
  total: number;
  pages: number;
  offset: number;
}

export interface JobHuntLiveJob {
  job_key: string;
  canonical_id: string;
  provider: string;
  source: string | null;
  title: string;
  company: string | null;
  location: string | null;
  source_url: string;
  posted_date: string | null;
  description_status: 'missing' | 'complete' | 'partial' | 'manual';
  description_available: boolean;
  fit_score: number | null;
  fit_evidence: Record<string, unknown> | null;
  salary: Record<string, unknown> | null;
  work_mode: string | null;
  employment_type: string | null;
  retrieved_at: string;
  last_checked_at: string;
  pipeline_state: 'new' | 'saved' | 'skipped';
}

export interface JobHuntLiveJobDetail extends JobHuntLiveJob {
  description_text: string | null;
  description_method: string;
  description_sha256: string | null;
}

export interface JobHuntLiveJobPage {
  items: JobHuntLiveJob[];
  total: number;
  page: number;
  page_size: number;
  pages: number;
}

export interface JobHuntLiveSearchResponse {
  run_id: string;
  provider: string;
  status: 'complete' | 'partial';
  query_groups: string[];
  found: number;
  new: number;
  items: JobHuntLiveJob[];
  errors: string[];
}

export interface JobHuntLiveSearchRequest {
  query?: string;
  country?: string;
  city?: string;
  jobage?: number;
  include_remote?: boolean;
  limit_per_query?: number;
}

export interface JobHuntSavedSearch {
  search_id: string;
  name: string;
  query: string | null;
  filters: Omit<JobHuntLiveSearchRequest, 'query'>;
  enabled: boolean;
  interval_minutes: number;
  next_run_at: string | null;
  last_run_at: string | null;
  last_run_status: string | null;
  last_run_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface JobHuntSavedSearchList {
  items: JobHuntSavedSearch[];
  total: number;
}

export interface JobHuntSavedSearchCreate extends JobHuntLiveSearchRequest {
  name: string;
  enabled?: boolean;
  interval_minutes?: number;
}

export type JobHuntSavedSearchUpdate = Partial<JobHuntSavedSearchCreate>;

export interface PreparedJobHuntJob {
  job_key: string;
  job_id: string;
  created: boolean;
  description_status: 'complete' | 'manual';
}

export interface JobHuntJobQuery {
  page?: number;
  pageSize?: number;
  query?: string;
  location?: string;
  minScore?: number;
  source?: string;
  descriptionAvailable?: boolean | null;
  postedAfter?: string;
  postedBefore?: string;
  sort?: string;
  direction?: 'asc' | 'desc';
  offset?: number;
  signal?: AbortSignal;
}

async function readResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    // Do not surface internal database paths/errors to the browser.
    throw new Error('Job discovery is temporarily unavailable.');
  }
  return (await response.json()) as T;
}

export async function fetchJobHuntJobs({
  page = 1,
  pageSize = 20,
  query,
  location,
  source,
  descriptionAvailable,
  postedAfter,
  postedBefore,
  sort = 'first_seen',
  direction = 'desc',
  offset,
  signal,
}: JobHuntJobQuery = {}): Promise<JobHuntJobPage> {
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  if (query) params.set('q', query);
  if (location) params.set('location', location);
  if (source) params.set('source', source);
  if (descriptionAvailable !== undefined && descriptionAvailable !== null) {
    params.set('description_available', String(descriptionAvailable));
  }
  if (postedAfter) params.set('posted_after', postedAfter);
  if (postedBefore) params.set('posted_before', postedBefore);
  params.set('sort', sort);
  params.set('direction', direction);
  if (offset !== undefined) params.set('offset', String(offset));
  const response = await apiFetch(`/api/v1/discovery/jobs?${params.toString()}`, { signal });
  return readResponse<JobHuntJobPage>(response);
}

export async function fetchJobHuntJob(
  jobKey: string,
  signal?: AbortSignal
): Promise<JobHuntJobDetail> {
  const route = jobKey.startsWith('freehire:')
    ? `/api/v1/discovery/jobs/live-jobs/${encodeURIComponent(jobKey)}`
    : `/api/v1/discovery/jobs/${encodeURIComponent(jobKey)}`;
  const response = await apiFetch(route, {
    signal,
  });
  return readResponse<JobHuntJobDetail>(response);
}

export async function searchLiveJobHuntJobs(
  payload: JobHuntLiveSearchRequest = {}
): Promise<JobHuntLiveSearchResponse> {
  const response = await apiFetch('/api/v1/discovery/jobs/live-search', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  return readResponse<JobHuntLiveSearchResponse>(response);
}

export async function fetchSavedJobHuntSearches(): Promise<JobHuntSavedSearchList> {
  const response = await apiFetch('/api/v1/discovery/jobs/saved-searches');
  return readResponse<JobHuntSavedSearchList>(response);
}

export async function createSavedJobHuntSearch(
  payload: JobHuntSavedSearchCreate
): Promise<JobHuntSavedSearch> {
  const response = await apiFetch('/api/v1/discovery/jobs/saved-searches', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  return readResponse<JobHuntSavedSearch>(response);
}

export async function updateSavedJobHuntSearch(
  searchId: string,
  payload: JobHuntSavedSearchUpdate
): Promise<JobHuntSavedSearch> {
  const response = await apiFetch(
    `/api/v1/discovery/jobs/saved-searches/${encodeURIComponent(searchId)}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }
  );
  return readResponse<JobHuntSavedSearch>(response);
}

export async function deleteSavedJobHuntSearch(searchId: string): Promise<void> {
  const response = await apiFetch(
    `/api/v1/discovery/jobs/saved-searches/${encodeURIComponent(searchId)}`,
    { method: 'DELETE' }
  );
  await readResponse(response);
}

export async function runSavedJobHuntSearch(searchId: string): Promise<JobHuntLiveSearchResponse> {
  const response = await apiFetch(
    `/api/v1/discovery/jobs/saved-searches/${encodeURIComponent(searchId)}/run`,
    { method: 'POST' }
  );
  return readResponse<JobHuntLiveSearchResponse>(response);
}

export async function fetchLiveJobHuntJobs({
  query,
  location,
  minScore,
  page = 1,
  pageSize = 10,
  signal,
}: JobHuntJobQuery = {}): Promise<JobHuntLiveJobPage> {
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  if (query) params.set('q', query);
  if (location) params.set('location', location);
  if (minScore !== undefined) params.set('min_score', String(minScore));
  const response = await apiFetch(`/api/v1/discovery/jobs/live-jobs?${params.toString()}`, {
    signal,
  });
  return readResponse<JobHuntLiveJobPage>(response);
}

export async function prepareJobHuntJobForTailoring(
  jobKey: string,
  jobDescription?: string
): Promise<PreparedJobHuntJob> {
  const response = await apiFetch(
    `/api/v1/discovery/jobs/${jobKey.startsWith('freehire:') ? 'live-jobs/' : ''}${encodeURIComponent(jobKey)}/tailor-job`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job_description: jobDescription?.trim() || null }),
    }
  );
  return readResponse<PreparedJobHuntJob>(response);
}

export async function saveJobHuntJob(jobKey: string): Promise<void> {
  const route = jobKey.startsWith('freehire:') ? 'live-jobs/' : '';
  const response = await apiFetch(
    `/api/v1/discovery/jobs/${route}${encodeURIComponent(jobKey)}/save`,
    {
      method: 'POST',
    }
  );
  await readResponse(response);
}

export async function skipJobHuntJob(jobKey: string): Promise<void> {
  const route = jobKey.startsWith('freehire:') ? 'live-jobs/' : '';
  const response = await apiFetch(
    `/api/v1/discovery/jobs/${route}${encodeURIComponent(jobKey)}/skip`,
    {
      method: 'POST',
    }
  );
  await readResponse(response);
}
