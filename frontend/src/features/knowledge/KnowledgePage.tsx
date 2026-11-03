import {
  ArrowClockwise,
  ArrowRight,
  BookOpen,
  CheckCircle,
  Clock,
  FileText,
  Globe,
  MagnifyingGlass,
  Plus,
  ShieldCheck,
  Sparkle,
  Trash,
  UploadSimple,
  WarningCircle,
} from '@phosphor-icons/react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, PageHeader } from '@/components/ui/Card'
import { ConfirmDialog, Dialog } from '@/components/ui/Dialog'
import { EmptyState, ErrorState } from '@/components/ui/States'
import { Input } from '@/components/ui/Input'
import { Skeleton } from '@/components/ui/Skeleton'
import { Textarea } from '@/components/ui/Textarea'
import { can, PERMISSIONS } from '@/features/auth/permissions'
import { knowledgeKeys } from '@/features/knowledge/keys'
import { auditHistoryUrl } from '@/features/audit/keys'
import { AppShell } from '@/layouts/AppShell'
import { logout } from '@/services/api/auth'
import { apiClient } from '@/services/api/client'
import { toApiError } from '@/services/api/errors'
import { toast } from '@/store/toasts'
import { useSessionStore } from '@/store/auth'
import type { components } from '@/types/api'

type KnowledgeDocument = components['schemas']['KnowledgeDocumentRead']
type KnowledgeAnswer = components['schemas']['KnowledgeAnswerRead']
type KnowledgeSource = components['schemas']['KnowledgeSource']
type SourceKind = 'manual' | 'url' | 'upload'

const PAGE_SIZE = 50
const ACCEPTED_FILES = '.pdf,.html,.htm,.txt,.log,.csv,.md,.markdown'

function sourceLabel(source: KnowledgeDocument['source_type']): string {
  return source === 'url' ? 'Web page' : source === 'upload' ? 'File upload' : 'Written text'
}

function statusBadge(document: KnowledgeDocument): { label: string; variant: 'success' | 'warning' | 'danger' | 'neutral' } {
  if (document.status === 'failed') return { label: 'Failed', variant: 'danger' }
  if (document.status === 'pending') return { label: 'Queued', variant: 'warning' }
  if (document.status === 'processing') return { label: 'Processing', variant: 'warning' }
  return document.is_published
    ? { label: 'Ready', variant: 'success' }
    : { label: 'Not published', variant: 'neutral' }
}

function DocumentForm({
  open,
  kind,
  setKind,
  onClose,
  onSubmit,
  saving,
  error,
}: {
  open: boolean
  kind: SourceKind
  setKind: (kind: SourceKind) => void
  onClose: () => void
  onSubmit: (event: FormEvent<HTMLFormElement>) => void
  saving: boolean
  error?: string | undefined
}) {
  const [title, setTitle] = useState('')
  const [content, setContent] = useState('')
  const [url, setUrl] = useState('')
  const [file, setFile] = useState<File | null>(null)

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Add to your knowledge base"
      description="Add trusted material so your team can find answers grounded in your own support content."
      size="lg"
      footer={(
        <>
          <Button variant="ghost" onClick={onClose} disabled={saving}>Cancel</Button>
          <Button form="knowledge-document-form" type="submit" loading={saving} leadingIcon={<Plus aria-hidden="true" />}>Add document</Button>
        </>
      )}
    >
      <form id="knowledge-document-form" onSubmit={onSubmit} className="space-y-4">
        <fieldset className="grid grid-cols-3 gap-2" aria-label="Document source">
          {([
            ['manual', <FileText key="manual" aria-hidden="true" className="size-4" />, 'Write text'],
            ['url', <Globe key="url" aria-hidden="true" className="size-4" />, 'Add a URL'],
            ['upload', <UploadSimple key="upload" aria-hidden="true" className="size-4" />, 'Upload file'],
          ] as const).map(([value, icon, label]) => <button key={value} type="button" onClick={() => { setKind(value); setFile(null) }} aria-pressed={kind === value} className={`flex min-h-16 flex-col items-center justify-center gap-1.5 rounded-xl border text-xs font-medium transition-colors ${kind === value ? 'border-brand bg-brand-subtle text-brand-text' : 'border-border-subtle bg-surface text-ink-muted hover:bg-surface-sunken'}`}>{icon}{label}</button>)}
        </fieldset>

        <label className="block space-y-1.5"><span className="text-xs font-medium text-ink">Document title</span><Input required maxLength={500} value={title} onChange={(event) => setTitle(event.currentTarget.value)} placeholder="e.g. Refund and replacement policy" autoFocus /></label>

        {kind === 'manual' && <label className="block space-y-1.5"><span className="text-xs font-medium text-ink">Content</span><Textarea required maxLength={200000} rows={8} value={content} onChange={(event) => setContent(event.currentTarget.value)} placeholder="Paste a policy, troubleshooting guide, or other source material…" /><span className="block text-right text-[0.6875rem] text-ink-subtle">{content.length.toLocaleString()} / 200,000 characters</span></label>}
        {kind === 'url' && <label className="block space-y-1.5"><span className="text-xs font-medium text-ink">Web address</span><Input required type="url" maxLength={1000} value={url} onChange={(event) => setUrl(event.currentTarget.value)} placeholder="https://help.example.com/returns" /><span className="block text-[0.6875rem] text-ink-subtle">The server fetches and checks the page in the background.</span></label>}
        {kind === 'upload' && <label className="block space-y-1.5"><span className="text-xs font-medium text-ink">Source file</span><Input required type="file" accept={ACCEPTED_FILES} onChange={(event) => setFile(event.currentTarget.files?.[0] ?? null)} className="h-auto py-2 file:mr-3 file:rounded-md file:border-0 file:bg-brand-subtle file:px-2.5 file:py-1 file:text-xs file:font-medium file:text-brand-text" /><span className="block text-[0.6875rem] text-ink-subtle">PDF, HTML, text, CSV, or Markdown · up to 10 MB</span>{file && <span className="block truncate text-xs text-ink-muted">{file.name} · {(file.size / 1024).toFixed(0)} KB</span>}</label>}

        <div className="flex gap-2 rounded-lg border border-info-border bg-info-subtle/50 p-3 text-xs leading-relaxed text-info-text"><Clock aria-hidden="true" className="mt-0.5 size-4 shrink-0" /><p>Ingestion runs in the background. This document becomes searchable once processing completes.</p></div>
        {error && <p role="alert" className="rounded-lg border border-danger-border bg-danger-subtle px-3 py-2 text-xs text-danger-text">{error}</p>}
        <input type="hidden" name="knowledge-kind" value={kind} readOnly />
        <input type="hidden" name="knowledge-title" value={title} readOnly />
        <input type="hidden" name="knowledge-content" value={content} readOnly />
        <input type="hidden" name="knowledge-url" value={url} readOnly />
        <input type="hidden" name="knowledge-file" value={file?.name ?? ''} readOnly />
      </form>
    </Dialog>
  )
}

function AnswerSources({ sources }: { sources: KnowledgeSource[] }) {
  if (!sources.length) return null
  return (
    <div className="mt-5 border-t border-border-subtle pt-4">
      <h3 className="text-xs font-semibold text-ink">Sources used</h3>
      <div className="mt-2 space-y-2">
        {sources.map((source, index) => <a key={`${source.document_id}-${source.chunk_index}`} href={`#document-${source.document_id}`} className="group block rounded-lg border border-border-subtle p-3 transition-colors hover:border-brand-border hover:bg-brand-subtle/40">
          <div className="flex items-center gap-2"><span className="grid size-5 place-items-center rounded-full bg-brand-subtle text-[0.625rem] font-semibold text-brand-text">{index + 1}</span><span className="min-w-0 flex-1 truncate text-xs font-medium text-ink group-hover:text-brand-text">{source.document_title}</span><Badge variant="neutral" size="sm">{Math.round(source.similarity * 100)}% match</Badge></div>
          <p className="mt-2 line-clamp-3 pl-7 text-xs leading-relaxed text-ink-muted">“{source.excerpt}”</p>
        </a>)}
      </div>
    </div>
  )
}

export function KnowledgePage() {
  const user = useSessionStore((state) => state.user)
  const queryClient = useQueryClient()
  const canUpload = can(user?.role, PERMISSIONS.KB_UPLOAD)
  const canDelete = can(user?.role, PERMISSIONS.KB_DELETE)
  const canViewAudit = can(user?.role, PERMISSIONS.AUDIT_VIEW)
  const canAsk = can(user?.role, PERMISSIONS.AI_QUERY_KNOWLEDGE)
  const [searchInput, setSearchInput] = useState('')
  const [searchTerm, setSearchTerm] = useState('')
  const [offset, setOffset] = useState(0)
  const [createOpen, setCreateOpen] = useState(false)
  const [createInstance, setCreateInstance] = useState(0)
  const [sourceKind, setSourceKind] = useState<SourceKind>('manual')
  const [createError, setCreateError] = useState<string>()
  const [deleteTarget, setDeleteTarget] = useState<KnowledgeDocument | null>(null)
  const [question, setQuestion] = useState('')
  const [answer, setAnswer] = useState<KnowledgeAnswer | null>(null)
  const [questionError, setQuestionError] = useState<string>()

  useEffect(() => {
    const timeout = window.setTimeout(() => {
      setSearchTerm(searchInput.trim())
      setOffset(0)
    }, 250)
    return () => window.clearTimeout(timeout)
  }, [searchInput])

  const documents = useQuery({
    queryKey: knowledgeKeys.documents(searchTerm, offset),
    queryFn: async () => (await apiClient.get<KnowledgeDocument[]>('/knowledge', { params: { q: searchTerm || undefined, limit: PAGE_SIZE, offset } })).data,
    enabled: Boolean(user && can(user.role, PERMISSIONS.KB_LIST)),
    refetchInterval: (query) => query.state.data?.some((document) => document.status === 'pending' || document.status === 'processing') ? 5_000 : false,
  })

  const createDocument = useMutation({
    mutationFn: async ({ title, kind, content, url, file }: { title: string; kind: SourceKind; content: string; url: string; file: File | null }) => {
      if (kind === 'upload') {
        if (!file) throw new Error('Choose a supported file to upload.')
        const form = new FormData()
        form.append('title', title)
        form.append('file', file)
        return (await apiClient.post<KnowledgeDocument>('/knowledge/upload', form, { headers: { 'Content-Type': 'multipart/form-data' } })).data
      }
      return (await apiClient.post<KnowledgeDocument>('/knowledge', kind === 'url' ? { title, url } : { title, content })).data
    },
    onSuccess: async (document) => {
      await queryClient.invalidateQueries({ queryKey: knowledgeKeys.all })
      toast.success('Document added', `${document.title} is queued for processing.`)
      setCreateOpen(false)
      setCreateError(undefined)
    },
    onError: (error) => setCreateError(toApiError(error).message),
  })

  const deleteDocument = useMutation({
    mutationFn: async (document: KnowledgeDocument) => apiClient.delete(`/knowledge/${document.id}`),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: knowledgeKeys.all })
      toast.success('Document deleted', 'It will no longer be used in knowledge search.')
      setDeleteTarget(null)
    },
    onError: (error) => toast.error('Document couldn’t be deleted', toApiError(error).message),
  })

  const askKnowledge = useMutation({
    mutationKey: knowledgeKeys.search(),
    mutationFn: async (value: string) => (await apiClient.post<KnowledgeAnswer>('/knowledge/search', { question: value })).data,
    onSuccess: (result) => { setAnswer(result); setQuestionError(undefined) },
    onError: (error) => { setAnswer(null); setQuestionError(toApiError(error).message) },
  })

  const submitDocument = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const form = event.currentTarget
    const data = new FormData(form)
    const title = String(data.get('knowledge-title') ?? '').trim()
    const kind = String(data.get('knowledge-kind') ?? 'manual') as SourceKind
    if (!title) return setCreateError('Enter a title for this document.')
    const content = String(data.get('knowledge-content') ?? '')
    const url = String(data.get('knowledge-url') ?? '')
    const fileName = String(data.get('knowledge-file') ?? '')
    const fileInput = form.querySelector<HTMLInputElement>('input[type="file"]')
    const file = fileInput?.files?.[0] ?? null
    if (kind === 'manual' && !content.trim()) return setCreateError('Add some content before saving this document.')
    if (kind === 'url' && !url) return setCreateError('Enter the page URL to add.')
    if (kind === 'upload' && (!file || !fileName)) return setCreateError('Choose a file to upload.')
    setCreateError(undefined)
    createDocument.mutate({ title, kind, content, url, file })
  }

  const submitQuestion = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const value = question.trim()
    if (!value || askKnowledge.isPending) return
    setAnswer(null)
    setQuestionError(undefined)
    askKnowledge.mutate(value)
  }

  if (!user) return null

  return (
    <AppShell user={user} onSignOut={() => { void logout() }}>
      <div className="mx-auto flex max-w-7xl flex-col gap-6">
        <PageHeader
          title="Knowledge base"
          description="Find trusted answers in your team’s policies, guides, and support content."
          actions={canUpload ? <Button onClick={() => { setCreateError(undefined); setCreateInstance((value) => value + 1); setCreateOpen(true) }} leadingIcon={<Plus aria-hidden="true" />}>Add source</Button> : undefined}
        />

        <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1.15fr)_minmax(20rem,0.85fr)]">
          <section aria-labelledby="documents-heading" className="min-w-0">
            <Card className="overflow-hidden">
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border-subtle px-5 py-4">
                <div><h2 id="documents-heading" className="text-sm font-semibold text-ink">Sources</h2><p className="mt-1 text-xs text-ink-muted">Your organization’s ingested knowledge documents.</p></div>
                <Badge variant="neutral">{documents.data?.length ?? '—'} shown</Badge>
              </div>
              <div className="border-b border-border-subtle p-3"><Input value={searchInput} onChange={(event) => setSearchInput(event.currentTarget.value)} placeholder="Find a document by title…" aria-label="Search knowledge documents" leadingIcon={<MagnifyingGlass aria-hidden="true" />} /></div>

              {documents.isPending ? <div className="space-y-3 p-4" aria-label="Loading documents">{Array.from({ length: 5 }, (_, i) => <Skeleton key={i} className="h-20 rounded-lg" />)}</div> : documents.error ? <ErrorState title="Sources couldn’t load" error={documents.error} onRetry={() => { void documents.refetch() }} /> : documents.data?.length ? <div className="divide-y divide-border-subtle">
                {documents.data.map((document) => {
                  const status = statusBadge(document)
                  return <article id={`document-${document.id}`} key={document.id} className="scroll-mt-24 p-4 transition-colors target:bg-brand-subtle/30 sm:p-5">
                    <div className="flex items-start gap-3">
                      <span className="grid size-9 shrink-0 place-items-center rounded-xl border border-border-subtle bg-surface-sunken text-ink-muted">{document.source_type === 'url' ? <Globe aria-hidden="true" className="size-4" /> : document.source_type === 'upload' ? <UploadSimple aria-hidden="true" className="size-4" /> : <FileText aria-hidden="true" className="size-4" />}</span>
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2"><h3 className="min-w-0 flex-1 truncate text-sm font-medium text-ink">{document.title}</h3><Badge variant={status.variant} size="sm" dot>{status.label}</Badge></div>
                        <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[0.6875rem] text-ink-subtle"><span>{sourceLabel(document.source_type)}</span><span>·</span><span>{document.chunk_count} {document.chunk_count === 1 ? 'passage' : 'passages'}</span><span>·</span><span>{new Date(document.created_at).toLocaleDateString()}</span></div>
                        {document.status === 'failed' && document.error_message && <p className="mt-2 flex items-start gap-1.5 text-xs text-danger-text"><WarningCircle aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" /><span>{document.error_message}</span></p>}
                        {document.status === 'completed' && document.is_published && <p className="mt-2 flex items-center gap-1.5 text-[0.6875rem] text-success-text"><CheckCircle aria-hidden="true" className="size-3.5" />Included in grounded answers</p>}
                      </div>
                      {canViewAudit && <Link to={auditHistoryUrl('knowledge_document', document.id)} className="shrink-0 rounded-md px-2 py-1 text-xs font-medium text-ink-subtle hover:bg-surface-sunken hover:text-brand-text">History</Link>}
                      {canDelete && <Button variant="ghost" size="sm" aria-label={`Delete ${document.title}`} onClick={() => setDeleteTarget(document)} className="shrink-0 text-ink-subtle hover:text-danger-text"><Trash aria-hidden="true" className="size-4" /></Button>}
                    </div>
                  </article>
                })}
              </div> : <EmptyState icon={<BookOpen aria-hidden="true" />} title={searchTerm ? 'No matching sources' : 'Your knowledge base is empty'} description={searchTerm ? 'Try a different title or clear the search.' : canUpload ? 'Add a policy, help article, or guide to make it searchable.' : 'Ask your administrator to add trusted support material.'} action={searchTerm ? <Button size="sm" variant="secondary" onClick={() => setSearchInput('')}>Clear search</Button> : undefined} />}

              {!documents.isPending && !documents.error && documents.data && <div className="flex items-center justify-between gap-3 border-t border-border-subtle px-4 py-3"><span className="text-xs text-ink-subtle">Page {Math.floor(offset / PAGE_SIZE) + 1}</span><div className="flex gap-2"><Button size="sm" variant="secondary" disabled={offset === 0} onClick={() => setOffset((current) => Math.max(0, current - PAGE_SIZE))}>Previous</Button><Button size="sm" variant="secondary" disabled={documents.data.length < PAGE_SIZE} onClick={() => setOffset((current) => current + PAGE_SIZE)}>Next</Button></div></div>}
            </Card>
          </section>

          <section aria-labelledby="ask-heading" className="min-w-0 xl:sticky xl:top-5">
            <Card variant="bezel" className="overflow-hidden">
              <div className="flex items-start gap-3 border-b border-border-subtle px-5 py-4">
                <span className="grid size-9 place-items-center rounded-xl bg-ai-subtle text-ai"><Sparkle aria-hidden="true" className="size-4" /></span>
                <div><h2 id="ask-heading" className="text-sm font-semibold text-ink">Ask your knowledge base</h2><p className="mt-1 text-xs text-ink-muted">Answers include passages you can verify.</p></div>
              </div>
              <div className="p-4 sm:p-5">
                {canAsk ? <>
                  <form onSubmit={submitQuestion} className="space-y-3"><label htmlFor="knowledge-question" className="sr-only">Ask a question</label><Textarea id="knowledge-question" rows={4} maxLength={2000} value={question} onChange={(event) => setQuestion(event.currentTarget.value)} placeholder="How long do customers have to request a refund?" /><div className="flex items-center justify-between gap-3"><span className="text-[0.6875rem] text-ink-subtle">Answers only use published organization sources.</span><Button type="submit" disabled={!question.trim()} loading={askKnowledge.isPending} leadingIcon={<Sparkle aria-hidden="true" />}>Ask</Button></div></form>

                  {questionError && <div className="mt-4"><ErrorState title="That question couldn’t be answered" description={questionError} onRetry={() => { if (question.trim()) askKnowledge.mutate(question.trim()) }} className="py-6" /></div>}
                  {askKnowledge.isPending && <div className="mt-5 space-y-3 rounded-xl border border-border-subtle bg-surface-sunken/40 p-4" aria-label="Searching knowledge sources"><div className="flex items-center gap-2 text-xs font-medium text-ink-muted"><ArrowClockwise aria-hidden="true" className="size-4 animate-spin" />Searching your sources…</div><Skeleton className="h-3 w-full" /><Skeleton className="h-3 w-4/5" /><Skeleton className="h-3 w-2/3" /></div>}
                  {answer && <div aria-live="polite" className="mt-5 rounded-xl border border-ai-border bg-ai-subtle/30 p-4"><div className="flex items-center gap-2 text-xs font-semibold text-ai"><Sparkle aria-hidden="true" className="size-4" />Grounded answer</div><p className="mt-3 whitespace-pre-wrap text-sm leading-relaxed text-ink">{answer.answer}</p>{answer.sources.length ? <AnswerSources sources={answer.sources} /> : <p className="mt-3 border-t border-border-subtle pt-3 text-xs text-ink-muted">No source passages matched this question. Add or check your documents before relying on an answer.</p>}</div>}
                  {!answer && !askKnowledge.isPending && !questionError && <div className="mt-5 rounded-xl border border-dashed border-border bg-surface-sunken/30 px-4 py-5 text-center"><p className="text-xs text-ink-muted">Answers are based on your organization’s published content.</p><p className="mt-1 text-[0.6875rem] text-ink-subtle">If a source doesn’t support an answer, the assistant will say so.</p></div>}
                </> : <EmptyState icon={<BookOpen aria-hidden="true" />} title="Knowledge search isn’t enabled" description="Your role can browse sources, but can’t ask AI questions." />}
              </div>
            </Card>
            <div className="mt-3 flex items-center gap-2 px-1 text-[0.6875rem] text-ink-subtle"><ShieldCheck aria-hidden="true" className="size-3.5 shrink-0" /><p>Source citations link back to the exact document in this library.</p></div>
          </section>
        </div>

        <div className="flex items-center justify-between gap-4 border-t border-border-subtle pt-4 text-xs text-ink-subtle"><span>Processing happens in the background; failed documents stay visible with a reason.</span><Link to="/analytics" className="inline-flex shrink-0 items-center gap-1 font-medium text-brand-text hover:underline">View insights <ArrowRight aria-hidden="true" className="size-3.5" /></Link></div>
      </div>

      <DocumentForm
        key={createInstance}
        open={createOpen}
        kind={sourceKind}
        setKind={setSourceKind}
        onClose={() => { if (!createDocument.isPending) { setCreateOpen(false); setCreateError(undefined) } }}
        onSubmit={submitDocument}
        saving={createDocument.isPending}
        error={createError}
      />
      <ConfirmDialog
        open={Boolean(deleteTarget)}
        onClose={() => { if (!deleteDocument.isPending) setDeleteTarget(null) }}
        onConfirm={() => { if (deleteTarget) deleteDocument.mutate(deleteTarget) }}
        title="Delete this knowledge source?"
        description={deleteTarget ? `${deleteTarget.title} and its indexed passages will be removed. It will no longer appear in answers.` : undefined}
        confirmLabel="Delete source"
        variant="danger"
        loading={deleteDocument.isPending}
      />
    </AppShell>
  )
}
