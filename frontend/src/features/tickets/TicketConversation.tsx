import { ArrowBendUpLeft, LockKey, PaperPlaneRight } from '@phosphor-icons/react'
import { useState, type FormEvent } from 'react'

import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { ErrorState } from '@/components/ui/States'
import { Field } from '@/components/ui/Field'
import { Skeleton } from '@/components/ui/Skeleton'
import { Textarea } from '@/components/ui/Textarea'
import type { components } from '@/types/api'
import { cn } from '@/utils/cn'

type Ticket = components['schemas']['TicketRead']
type Message = components['schemas']['MessageRead']
type Customer = components['schemas']['CustomerRead']

interface TicketConversationProps {
  ticket: Ticket
  customer?: Customer | undefined
  messages?: Message[] | undefined
  messagesLoading: boolean
  messagesError: unknown
  onRetryMessages: () => void
  currentUserId: string
  canAcceptDraft: boolean
  onAcceptDraft: (draft: Message, body: string) => Promise<void>
  canReply: boolean
  canPostInternal: boolean
  sending: boolean
  onSend: (body: string, internal: boolean) => Promise<void>
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(new Date(value))
}

function senderLabel(message: Message, customer: Customer | undefined, currentUserId: string): string {
  if (message.sender_type === 'customer') return customer?.name ?? 'Customer'
  if (message.sender_type === 'system') return 'SupportFlow'
  if (message.sender_type === 'ai_draft') return 'AI draft'
  return message.sender_user_id === currentUserId ? 'You' : 'Support team'
}

function MessageItem({
  message,
  customer,
  currentUserId,
  canAcceptDraft,
  onAcceptDraft,
}: {
  message: Message
  customer?: Customer | undefined
  currentUserId: string
  canAcceptDraft: boolean
  onAcceptDraft: (draft: Message, body: string) => Promise<void>
}) {
  const [reviewing, setReviewing] = useState(false)
  const [draftBody, setDraftBody] = useState(message.body)
  const [accepting, setAccepting] = useState(false)
  const ownMessage = message.sender_user_id === currentUserId
  const systemMessage = message.sender_type === 'system'
  const internal = message.is_internal

  return (
    <article
      className={cn(
        'flex w-full flex-col gap-1.5',
        systemMessage ? 'items-center' : ownMessage ? 'items-end' : 'items-start',
      )}
    >
      <div
        className={cn(
          'flex max-w-[88%] items-center gap-2 px-1 text-[0.6875rem] text-ink-subtle',
          ownMessage && 'flex-row-reverse',
        )}
      >
        <span className="font-medium text-ink-muted">{senderLabel(message, customer, currentUserId)}</span>
        <time dateTime={message.created_at}>{formatTime(message.created_at)}</time>
        {internal && <Badge variant="warning" size="sm" dot>Internal note</Badge>}
      </div>

      <div
        className={cn(
          'max-w-[88%] rounded-2xl px-4 py-3 text-sm leading-relaxed whitespace-pre-wrap break-words',
          systemMessage
            ? 'border border-border-subtle bg-surface-sunken text-ink-muted'
            : internal
              ? 'rounded-tl-md border border-warning-border bg-warning-subtle text-ink'
              : ownMessage
                ? 'rounded-tr-md bg-brand text-brand-fg'
                : 'rounded-tl-md border border-border-subtle bg-surface text-ink',
        )}
      >
        {message.body}
      </div>
      {message.sender_type === 'ai_draft' && canAcceptDraft && !reviewing && (
        <Button variant="secondary" size="sm" className="mt-1" onClick={() => setReviewing(true)}>
          Review and send
        </Button>
      )}
      {message.sender_type === 'ai_draft' && reviewing && (
        <div className="mt-2 flex w-full max-w-[88%] flex-col gap-2 rounded-xl border border-ai-border bg-ai-subtle p-3">
          <p className="text-xs font-medium text-ai">Review this AI draft before it reaches the customer.</p>
          <Textarea
            value={draftBody}
            onChange={(event) => setDraftBody(event.target.value)}
            aria-label="Edit AI generated reply"
            maxLength={20_000}
            rows={4}
            className="bg-surface"
          />
          <div className="flex justify-end gap-2">
            <Button variant="ghost" size="sm" disabled={accepting} onClick={() => { setReviewing(false); setDraftBody(message.body) }}>
              Cancel
            </Button>
            <Button
              variant="primary"
              size="sm"
              loading={accepting}
              disabled={!draftBody.trim()}
              onClick={() => {
                setAccepting(true)
                void onAcceptDraft(message, draftBody.trim())
                  .then(() => setReviewing(false), () => undefined)
                  .finally(() => setAccepting(false))
              }}
            >
              Send edited reply
            </Button>
          </div>
        </div>
      )}
    </article>
  )
}

function ConversationSkeleton() {
  return (
    <div className="flex flex-col gap-6 px-5 py-6" aria-label="Loading conversation">
      <div className="flex flex-col gap-2">
        <Skeleton className="h-3 w-28" />
        <Skeleton className="h-20 w-4/5 rounded-2xl" />
      </div>
      <div className="ml-auto flex w-4/5 flex-col items-end gap-2">
        <Skeleton className="h-3 w-24" />
        <Skeleton className="h-14 w-full rounded-2xl" />
      </div>
      <div className="flex flex-col gap-2">
        <Skeleton className="h-3 w-32" />
        <Skeleton className="h-16 w-3/5 rounded-2xl" />
      </div>
    </div>
  )
}

export function TicketConversation({
  ticket,
  customer,
  messages,
  messagesLoading,
  messagesError,
  onRetryMessages,
  currentUserId,
  canAcceptDraft,
  onAcceptDraft,
  canReply,
  canPostInternal,
  sending,
  onSend,
}: TicketConversationProps) {
  const [draft, setDraft] = useState('')
  const [internal, setInternal] = useState(false)
  const closed = ticket.status === 'closed'
  const replyBlocked = closed && !internal

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const body = draft.trim()
    if (!body || sending || replyBlocked || (internal ? !canPostInternal : !canReply)) return
    void onSend(body, internal).then(() => {
      setDraft('')
      setInternal(false)
    }).catch(() => undefined)
  }

  return (
    <Card variant="bezel" className="overflow-hidden">
      <div className="flex items-start justify-between gap-4 border-b border-border-subtle px-5 py-4">
        <div className="flex min-w-0 flex-col gap-1">
          <h2 className="text-sm font-semibold text-ink">Conversation</h2>
          <p className="text-xs text-ink-muted">
            {customer ? `With ${customer.name}` : 'Customer and support messages'}
          </p>
        </div>
        <Badge variant="neutral" size="sm">{messages?.length ?? '—'} messages</Badge>
      </div>

      {messagesLoading ? (
        <ConversationSkeleton />
      ) : messagesError ? (
        <ErrorState
          title="The conversation couldn’t load"
          description="The ticket is available, but its messages could not be retrieved."
          onRetry={onRetryMessages}
        />
      ) : (
        <div className="flex flex-col gap-6 px-4 py-5 sm:px-5">
          <article className="flex w-full flex-col items-start gap-1.5">
            <div className="flex items-center gap-2 px-1 text-[0.6875rem] text-ink-subtle">
              <span className="font-medium text-ink-muted">{customer?.name ?? 'Customer'}</span>
              <time dateTime={ticket.created_at}>{formatTime(ticket.created_at)}</time>
              <Badge variant="brand" size="sm">Original request</Badge>
            </div>
            <div className="max-w-[92%] rounded-2xl rounded-tl-md border border-border-subtle bg-surface px-4 py-3 text-sm leading-relaxed whitespace-pre-wrap break-words text-ink">
              {ticket.description}
            </div>
          </article>

          {messages?.map((message) => (
            <MessageItem
              key={message.id}
              message={message}
              customer={customer}
              currentUserId={currentUserId}
              canAcceptDraft={canAcceptDraft}
              onAcceptDraft={onAcceptDraft}
            />
          ))}

          {messages?.length === 0 && (
            <p className="py-2 text-center text-xs text-ink-subtle">No replies yet. The original request is above.</p>
          )}
        </div>
      )}

      <form onSubmit={handleSubmit} className="border-t border-border-subtle bg-surface-sunken/35 p-4 sm:p-5">
        {!canReply && !canPostInternal ? (
          <p className="text-sm text-ink-muted">You don’t have permission to reply to this ticket.</p>
        ) : (
          <div className="flex flex-col gap-3">
            {canPostInternal && (
              <div className="flex w-fit items-center gap-1 rounded-full border border-border-subtle bg-surface p-1">
                {canReply && (
                  <button
                    type="button"
                    aria-pressed={!internal}
                    onClick={() => setInternal(false)}
                    className={cn(
                      'inline-flex h-8 items-center gap-2 rounded-full px-3 text-xs font-medium transition-colors duration-200 ease-out-expo',
                      !internal ? 'bg-ink text-canvas' : 'text-ink-muted hover:text-ink',
                    )}
                  >
                    <ArrowBendUpLeft className="size-3.5" />
                    Reply
                  </button>
                )}
                <button
                  type="button"
                  aria-pressed={internal}
                  onClick={() => setInternal(true)}
                  className={cn(
                    'inline-flex h-8 items-center gap-2 rounded-full px-3 text-xs font-medium transition-colors duration-200 ease-out-expo',
                    internal ? 'bg-warning-subtle text-warning-text' : 'text-ink-muted hover:text-ink',
                  )}
                >
                  <LockKey className="size-3.5" />
                  Internal note
                </button>
              </div>
            )}

            {internal && (
              <div className="flex items-center gap-2 rounded-lg border border-warning-border bg-warning-subtle px-3 py-2 text-xs text-warning-text">
                <LockKey aria-hidden="true" className="size-3.5 shrink-0" />
                Only your support team can see this note. It will not be sent to the customer.
              </div>
            )}

            {replyBlocked && (
              <div className="rounded-lg border border-border-subtle bg-surface px-3 py-2 text-xs text-ink-muted">
                This ticket is closed. Reopen it before sending a customer reply. Internal notes remain available.
              </div>
            )}

            <Field label={internal ? 'Internal note' : 'Your reply'} labelHidden>
              <Textarea
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                placeholder={internal ? 'Add context for your team…' : 'Write a clear reply to the customer…'}
                aria-label={internal ? 'Internal note' : 'Your reply'}
                maxLength={20_000}
                rows={4}
                disabled={replyBlocked && !internal}
                className={cn('min-h-28 bg-surface', internal && 'border-warning-border focus-visible:ring-warning-border')}
              />
            </Field>

            <div className="flex flex-wrap items-center justify-between gap-3">
              <span className="text-[0.6875rem] tabular-nums text-ink-subtle">{draft.length.toLocaleString()} / 20,000</span>
              <Button
                type="submit"
                size="md"
                loading={sending}
                disabled={!draft.trim() || replyBlocked || (internal ? !canPostInternal : !canReply)}
                leadingIcon={internal ? <LockKey weight="bold" /> : <PaperPlaneRight weight="bold" />}
                className={cn(internal && 'bg-warning text-warning-fg hover:bg-warning-hover')}
              >
                {internal ? 'Add internal note' : 'Send reply'}
              </Button>
            </div>
          </div>
        )}
      </form>
    </Card>
  )
}
