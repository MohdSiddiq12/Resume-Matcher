import { JobDetailPage } from '@/components/discovery/job-detail-page';

export default async function DiscoverJobDetailRoute({
  params,
  searchParams,
}: {
  params: Promise<{ jobKey: string }>;
  searchParams: Promise<{ returnTo?: string }>;
}) {
  const { jobKey } = await params;
  const { returnTo } = await searchParams;
  const backHref = returnTo?.startsWith('/discover/jobs?') ? returnTo : '/discover/jobs';
  return <JobDetailPage jobKey={jobKey} backHref={backHref} />;
}
