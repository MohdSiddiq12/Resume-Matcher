'use client';

import React, { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { SpinnerGap, PencilSimple } from '@phosphor-icons/react';
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Alert } from '@/components/ui/alert';
import { StatusIndicator } from '@/components/ui/status-indicator';
import { useTranslations } from '@/lib/i18n';
import { formatDate } from '@/lib/format-date';
import {
  getApplicationDetail,
  getApplicationOutreachDraft,
  listApplicationEvents,
  saveApplicationOutreachDraft,
  updateApplication,
  type ApplicationDetail,
  type ApplicationEvent,
} from '@/lib/api/tracker';

interface CardDetailModalProps {
  applicationId: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onUpdated: () => void;
}

export function CardDetailModal({
  applicationId,
  open,
  onOpenChange,
  onUpdated,
}: CardDetailModalProps) {
  const { t, locale } = useTranslations();
  const router = useRouter();
  const [detail, setDetail] = useState<ApplicationDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [notes, setNotes] = useState('');
  const [followUpDate, setFollowUpDate] = useState('');
  const [savingNotes, setSavingNotes] = useState(false);
  const [notesError, setNotesError] = useState<string | null>(null);
  const [outreachDraft, setOutreachDraft] = useState('');
  const [savingOutreach, setSavingOutreach] = useState(false);
  const [outreachError, setOutreachError] = useState<string | null>(null);
  const [events, setEvents] = useState<ApplicationEvent[]>([]);

  useEffect(() => {
    if (!open || !applicationId) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    Promise.all([
      getApplicationDetail(applicationId),
      listApplicationEvents(applicationId).catch(() => []),
      getApplicationOutreachDraft(applicationId).catch(() => null),
    ])
      .then(([data, history, savedDraft]) => {
        if (cancelled) return;
        setDetail(data);
        setNotes(data.notes ?? '');
        setFollowUpDate(data.follow_up_date ?? '');
        setEvents(history);
        setOutreachDraft(
          savedDraft === null || savedDraft.updated_at === null
            ? typeof data.resume?.outreach_message === 'string'
              ? data.resume.outreach_message
              : ''
            : savedDraft.content
        );
        setNotesError(null);
        setOutreachError(null);
      })
      .catch(() => {
        if (!cancelled) setDetail(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, applicationId]);

  // Keep textarea Enter from bubbling to dialog/global handlers.
  const handleNotesKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter') e.stopPropagation();
  };

  const handleSaveNotes = async () => {
    if (!applicationId) return;
    setSavingNotes(true);
    setNotesError(null);
    try {
      await updateApplication(applicationId, {
        notes,
        follow_up_date: followUpDate || null,
      });
      onUpdated();
    } catch {
      // Show a generic message — never echo raw backend error text inline,
      // which could contain sensitive values.
      setNotesError(t('common.error'));
    } finally {
      setSavingNotes(false);
    }
  };

  const handleSaveOutreach = async () => {
    if (!applicationId) return;
    setSavingOutreach(true);
    setOutreachError(null);
    try {
      const saved = await saveApplicationOutreachDraft(applicationId, outreachDraft);
      setOutreachDraft(saved.content);
    } catch {
      setOutreachError(t('common.error'));
    } finally {
      setSavingOutreach(false);
    }
  };

  const resumeAvailable = Boolean(detail?.resume);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>{detail?.company || t('tracker.card.companyUnknown')}</DialogTitle>
          <DialogDescription>{detail?.role || t('tracker.card.roleUnknown')}</DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          {loading ? (
            <div role="status" className="py-8">
              <SpinnerGap aria-hidden="true" className="size-5 animate-spin text-steel" />
              <span className="sr-only">{t('common.loading')}</span>
            </div>
          ) : detail ? (
            <div className="space-y-4">
              <div className="flex items-center gap-3 font-mono text-xs uppercase text-ink-soft">
                <StatusIndicator tone="neutral">
                  {t(`tracker.columns.${detail.status}`)}
                </StatusIndicator>
                {detail.applied_at && (
                  <span className="tabular-nums">
                    {formatDate(detail.applied_at, locale, { month: 'short', year: 'numeric' })}
                  </span>
                )}
              </div>

              <div className="space-y-1">
                <Label>{t('tracker.modal.jobDescription')}</Label>
                <div className="max-h-48 overflow-y-auto whitespace-pre-wrap border border-ink bg-canvas p-3 text-sm">
                  {detail.job_content || t('tracker.modal.noJobDescription')}
                </div>
              </div>

              <div className="space-y-1">
                <Label htmlFor="card-notes">{t('tracker.modal.notes')}</Label>
                <Textarea
                  id="card-notes"
                  value={notes}
                  onChange={(e) => setNotes(e.target.value)}
                  onKeyDown={handleNotesKeyDown}
                  placeholder={t('tracker.modal.notesPlaceholder')}
                  rows={3}
                />
                {notesError && <Alert tone="error">{notesError}</Alert>}
                <div className="flex justify-end">
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={handleSaveNotes}
                    disabled={savingNotes}
                  >
                    {savingNotes ? (
                      <>
                        <SpinnerGap aria-hidden="true" className="animate-spin" />
                        {t('common.saving')}
                      </>
                    ) : (
                      t('tracker.modal.saveNotes')
                    )}
                  </Button>
                </div>
              </div>

              <div className="space-y-1">
                <Label htmlFor="card-follow-up">{t('tracker.modal.followUp')}</Label>
                <Input
                  id="card-follow-up"
                  type="date"
                  value={followUpDate}
                  onChange={(event) => setFollowUpDate(event.target.value)}
                />
              </div>

              <div className="space-y-1">
                <Label htmlFor="card-outreach-draft">{t('tracker.modal.outreachDraft')}</Label>
                <p className="text-xs text-ink-soft">{t('tracker.modal.outreachDraftNote')}</p>
                <Textarea
                  id="card-outreach-draft"
                  value={outreachDraft}
                  onChange={(event) => setOutreachDraft(event.target.value)}
                  maxLength={10_000}
                  rows={5}
                />
                {outreachError && <Alert tone="error">{outreachError}</Alert>}
                <div className="flex justify-end">
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={handleSaveOutreach}
                    disabled={savingOutreach}
                  >
                    {savingOutreach ? t('common.saving') : t('tracker.modal.saveOutreachDraft')}
                  </Button>
                </div>
              </div>

              <div className="space-y-2 border-t border-panel pt-4">
                <h3 className="font-mono text-xs uppercase">{t('tracker.modal.statusHistory')}</h3>
                {events.length === 0 ? (
                  <p className="text-xs text-ink-soft">{t('tracker.modal.noStatusHistory')}</p>
                ) : (
                  <ul className="space-y-2">
                    {[...events].reverse().map((event) => (
                      <li key={event.event_id} className="border border-panel p-3 text-xs">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <span className="font-mono uppercase">
                            {event.from_status
                              ? `${t(`tracker.columns.${event.from_status}`)} → `
                              : ''}
                            {t(`tracker.columns.${event.to_status}`)}
                          </span>
                          <time dateTime={event.created_at}>
                            {new Date(event.created_at).toLocaleString(locale)}
                          </time>
                        </div>
                        <div className="mt-1 flex flex-wrap items-center justify-between gap-2 text-ink-soft">
                          <span>
                            {event.confirmed
                              ? t('tracker.modal.confirmedStatus')
                              : t('tracker.modal.unconfirmedStatus')}
                          </span>
                          <code>{event.event_id}</code>
                        </div>
                        {event.note && <p className="mt-2 whitespace-pre-wrap">{event.note}</p>}
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              {!resumeAvailable && (
                <Alert tone="warning">{t('tracker.modal.resumeUnavailable')}</Alert>
              )}
            </div>
          ) : (
            <p className="py-6 font-mono text-sm text-steel">{t('tracker.modal.loadFailed')}</p>
          )}
        </DialogBody>

        <DialogFooter>
          <Button
            type="button"
            onClick={() => {
              if (detail?.resume_id) router.push(`/builder?id=${detail.resume_id}`);
            }}
            disabled={!resumeAvailable}
          >
            <PencilSimple aria-hidden="true" />
            {t('tracker.modal.editResume')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
