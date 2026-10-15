import { ArrowRight, Paperclip, Plus, Sparkle, Trash, Tray } from '@phosphor-icons/react'
import { useState, type ReactNode } from 'react'

import { AppShell } from '@/layouts/AppShell'
import { toast } from '@/store/toasts'
import { useThemeStore, type ThemePreference } from '@/store/theme'
import type { SessionUser } from '@/types/session'
import { cn } from '@/utils/cn'

import { Avatar } from '@/components/ui/Avatar'
import { Badge, CountBadge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent, CardHeader, CardTitle, PageHeader } from '@/components/ui/Card'
import { Checkbox } from '@/components/ui/Checkbox'
import { ConfirmDialog, Dialog } from '@/components/ui/Dialog'
import { DropdownMenu } from '@/components/ui/DropdownMenu'
import { Field } from '@/components/ui/Field'
import { IconButton } from '@/components/ui/IconButton'
import { Input } from '@/components/ui/Input'
import { Select } from '@/components/ui/Select'
import { Skeleton } from '@/components/ui/Skeleton'
import { EmptyState, ErrorState } from '@/components/ui/States'
import { Spinner } from '@/components/ui/Spinner'
import { Switch } from '@/components/ui/Switch'
import { Textarea } from '@/components/ui/Textarea'
import { Tooltip } from '@/components/ui/Tooltip'

/**
 * The component kit, rendered in both themes.
 *
 * A development-only route, and the reason every later phase is cheap: a
 * primitive's states are all visible in one place, so "how does a disabled
 * select look in dark mode" is a glance rather than a screen someone has to be
 * built to answer. It is also how the design language stays coherent — a
 * component added without a section here is a component nobody has seen beside
 * the others.
 *
 * The fixture user is for this route only. Nothing in the shipped application
 * reads it; the auth store supplies the real one from `GET /auth/me`.
 */
const FIXTURE_USER: SessionUser = {
  id: '00000000-0000-0000-0000-000000000000',
  name: 'Ada Lovelace',
  email: 'ada@example.com',
  role: 'admin',
  is_active: true,
  customer_id: null,
}

const SURFACE_TOKENS = [
  'bg-canvas',
  'bg-surface',
  'bg-surface-sunken',
  'bg-surface-hover',
  'bg-surface-active',
  'bg-surface-inverse',
]

const ROLE_TOKENS = [
  ['brand', 'bg-brand text-brand-fg'],
  ['danger', 'bg-danger text-danger-fg'],
  ['success', 'bg-success text-success-fg'],
  ['warning', 'bg-warning text-warning-fg'],
  ['info', 'bg-info text-info-fg'],
  ['ai', 'bg-ai text-ink-inverse'],
] as const

const STATUS_TOKENS = [
  ['open', 'bg-status-open'],
  ['assigned', 'bg-status-assigned'],
  ['in progress', 'bg-status-in-progress'],
  ['waiting', 'bg-status-waiting'],
  ['resolved', 'bg-status-resolved'],
  ['closed', 'bg-status-closed'],
] as const

const PRIORITY_TOKENS = [
  ['low', 'bg-priority-low'],
  ['medium', 'bg-priority-medium'],
  ['high', 'bg-priority-high'],
  ['urgent', 'bg-priority-urgent'],
] as const

const SERIES_TOKENS = [
  'bg-series-1',
  'bg-series-2',
  'bg-series-3',
  'bg-series-4',
  'bg-series-5',
  'bg-series-6',
]

function Section({
  title,
  description,
  children,
}: {
  title: string
  description?: string
  children: ReactNode
}) {
  return (
    <section className="flex flex-col gap-4">
      <div className="flex flex-col gap-0.5">
        <h2 className="text-sm font-semibold text-ink">{title}</h2>
        {description && <p className="text-[0.8125rem] text-ink-muted">{description}</p>}
      </div>
      {children}
    </section>
  )
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center gap-3">
      <span className="w-28 shrink-0 text-2xs tracking-wide text-ink-subtle uppercase">
        {label}
      </span>
      {children}
    </div>
  )
}

function ThemeToggle() {
  const preference = useThemeStore((state) => state.preference)
  const setPreference = useThemeStore((state) => state.setPreference)
  const options: ThemePreference[] = ['light', 'dark', 'system']

  return (
    <div
      role="radiogroup"
      aria-label="Theme"
      className="inline-flex rounded-lg border border-border-subtle bg-surface-sunken p-0.5"
    >
      {options.map((option) => (
        <button
          key={option}
          type="button"
          role="radio"
          aria-checked={preference === option}
          onClick={() => {
            setPreference(option)
          }}
          className={cn(
            'rounded-md px-3 py-1 text-xs font-medium capitalize',
            'transition-colors duration-150 ease-out-expo',
            preference === option
              ? 'bg-surface text-ink shadow-xs'
              : 'text-ink-muted hover:text-ink',
          )}
        >
          {option}
        </button>
      ))}
    </div>
  )
}

export function KitPage() {
  const [dialogOpen, setDialogOpen] = useState(false)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [email, setEmail] = useState('not-an-email')
  const [note, setNote] = useState('')

  return (
    <AppShell
      user={FIXTURE_USER}
      onSignOut={() => {
        toast.info('Development route', 'Sign-out is wired in the auth phase.')
      }}
      unreadCount={3}
    >
      <div className="flex flex-col gap-10 pb-16">
        <PageHeader
          title="Component kit"
          description="Every primitive, in both themes. Development route — not part of the product."
          actions={
            <>
              <ThemeToggle />
              <Button leadingIcon={<Plus />}>New ticket</Button>
            </>
          }
        />

        <Section title="Buttons" description="Six variants across three sizes, with loading and icon states.">
          <div className="flex flex-col gap-3">
            <Row label="Variants">
              <Button>Primary</Button>
              <Button variant="secondary">Secondary</Button>
              <Button variant="subtle">Subtle</Button>
              <Button variant="ghost">Ghost</Button>
              <Button variant="danger">Danger</Button>
              <Button variant="danger-ghost">Danger ghost</Button>
            </Row>

            <Row label="Sizes">
              <Button size="sm">Small</Button>
              <Button size="md">Medium</Button>
              <Button size="lg">Large</Button>
            </Row>

            <Row label="Icons">
              <Button leadingIcon={<Plus />}>Leading</Button>
              <Button variant="secondary" trailingIcon={<ArrowRight />}>
                Trailing
              </Button>
              <IconButton aria-label="Attach a file" icon={<Paperclip />} variant="secondary" />
              <IconButton aria-label="Delete" icon={<Trash />} variant="danger-ghost" />
            </Row>

            <Row label="States">
              <Button loading>Saving</Button>
              <Button disabled>Disabled</Button>
              <Button variant="secondary" loading>
                Loading
              </Button>
            </Row>

            <Row label="Full width">
              <Button fullWidth className="max-w-xs">
                Full width
              </Button>
            </Row>
          </div>
        </Section>

        <Section title="Badges" description="Status chips, with and without a leading dot.">
          <div className="flex flex-col gap-3">
            <Row label="Variants">
              <Badge>Neutral</Badge>
              <Badge variant="brand">Brand</Badge>
              <Badge variant="success">Success</Badge>
              <Badge variant="warning">Warning</Badge>
              <Badge variant="danger">Danger</Badge>
              <Badge variant="info">Info</Badge>
              <Badge variant="ai">AI</Badge>
              <Badge variant="outline">Outline</Badge>
            </Row>

            <Row label="With dot">
              <Badge variant="success" dot>
                Resolved
              </Badge>
              <Badge variant="warning" dot>
                Waiting
              </Badge>
              <Badge variant="danger" dot>
                Breached
              </Badge>
            </Row>

            <Row label="Sizes">
              <Badge size="sm">Small</Badge>
              <Badge size="md">Medium</Badge>
              <CountBadge count={0} />
              <span className="text-xs text-ink-muted">← count 0 renders nothing</span>
              <CountBadge count={7} />
              <CountBadge count={240} />
            </Row>
          </div>
        </Section>

        <Section title="Avatars" description="Initials, tinted deterministically from the name.">
          <Row label="Sizes">
            <Avatar name="Ada Lovelace" size="xs" />
            <Avatar name="Grace Hopper" size="sm" />
            <Avatar name="Alan Turing" size="md" />
            <Avatar name="Katherine Johnson" size="lg" />
            <Avatar name="Ada Lovelace" size="lg" />
            <Avatar name="SupportFlow" size="lg" />
          </Row>
        </Section>

        <Section title="Form controls" description="Wired through Field, so labels and errors are always associated.">
          <div className="grid max-w-3xl gap-5 sm:grid-cols-2">
            <Field label="Subject" hint="A short summary of the problem.">
              <Input placeholder="Cannot export invoices" />
            </Field>

            <Field label="Email" error="That address is not valid." required>
              <Input
                type="email"
                value={email}
                onChange={(event) => {
                  setEmail(event.target.value)
                }}
              />
            </Field>

            <Field label="Priority">
              <Select placeholder="Any priority" defaultValue="">
                <option value="low">Low</option>
                <option value="medium">Medium</option>
                <option value="high">High</option>
                <option value="urgent">Urgent</option>
              </Select>
            </Field>

            <Field label="Disabled">
              <Input disabled value="Read only" readOnly />
            </Field>

            <Field label="Internal note" className="sm:col-span-2" hint="Not visible to the customer.">
              <Textarea
                value={note}
                onChange={(event) => {
                  setNote(event.target.value)
                }}
                placeholder="Add context for the team…"
              />
            </Field>

            <div className="flex flex-col gap-3 sm:col-span-2">
              <Checkbox label="Notify the customer" description="Sends an email when the reply is posted." />
              <Checkbox label="Unchecked" />
              <Checkbox label="Disabled" disabled />
              <Switch label="Auto-assign new tickets" description="Round-robin across the team." defaultChecked />
              <Switch label="Off by default" />
            </div>
          </div>
        </Section>

        <Section title="Cards">
          <div className="grid gap-4 lg:grid-cols-3">
            <Card>
              <CardHeader>
                <CardTitle>Plain</CardTitle>
              </CardHeader>
              <CardContent>
                <p className="text-[0.8125rem] text-ink-muted">
                  A bordered surface. The default for panels that sit on the canvas.
                </p>
              </CardContent>
            </Card>

            <Card variant="raised">
              <CardHeader>
                <CardTitle>Raised</CardTitle>
              </CardHeader>
              <CardContent>
                <p className="text-[0.8125rem] text-ink-muted">
                  Ambient elevation, for anything that floats over content.
                </p>
              </CardContent>
            </Card>

            <Card variant="bezel">
              <CardContent className="pt-4">
                <p className="text-2xl font-semibold tracking-tight text-ink">Bezel</p>
                <p className="mt-1 text-[0.8125rem] text-ink-muted">
                  Double-bezel: a tray, then a pane inside it.
                </p>
              </CardContent>
            </Card>
          </div>
        </Section>

        <Section title="Loading and empty states">
          <div className="grid gap-4 lg:grid-cols-2">
            <div className="flex flex-col gap-3">
              <Row label="Spinners">
                <Spinner size="xs" />
                <Spinner size="sm" />
                <Spinner size="md" />
                <Spinner size="lg" />
              </Row>

              <div className="flex flex-col gap-2">
                <Skeleton className="h-4 w-3/4" />
                <Skeleton className="h-4 w-1/2" />
                <Skeleton className="h-4 w-2/3" />
                <Skeleton className="h-20 w-full rounded-xl" />
              </div>
            </div>

            <Card>
              <EmptyState
                icon={<Tray />}
                title="No tickets yet"
                description="When a customer writes in, it will appear here."
                action={<Button size="sm">New ticket</Button>}
              />
            </Card>
          </div>

          <Card>
            <ErrorState
              title="Could not load tickets"
              description="The request timed out after 15 seconds."
              onRetry={() => {
                toast.info('Retrying', 'Nothing to retry on this route.')
              }}
            />
          </Card>
        </Section>

        <Section title="Overlays" description="Native dialog, a menu with full keyboard support, and a hover label.">
          <Row label="Open">
            <Button variant="secondary" onClick={() => setDialogOpen(true)}>
              Dialog
            </Button>
            <Button variant="secondary" onClick={() => setConfirmOpen(true)}>
              Confirm
            </Button>

            <DropdownMenu
              label="Ticket actions"
              align="start"
              trigger={<span className="px-2 py-1 text-sm">Actions</span>}
              triggerClassName="flex items-center gap-1.5 rounded-lg border border-border bg-surface px-3 py-1.5 text-sm font-medium shadow-xs hover:bg-surface-hover"
              items={[
                { id: 'assign', label: 'Assign to me', onSelect: () => toast.success('Assigned') },
                { id: 'priority', label: 'Change priority', onSelect: () => toast.success('Priority') },
                { id: 'delete', label: 'Delete', danger: true, separatorBefore: true, onSelect: () => toast.error('Deleted') },
                { id: 'locked', label: 'Unavailable', disabled: true, onSelect: () => undefined },
              ]}
            />

            <Tooltip content="Opens the knowledge search">
              <IconButton aria-label="Knowledge search" icon={<Sparkle />} variant="secondary" />
            </Tooltip>
          </Row>

          <Row label="Toasts">
            <Button variant="secondary" size="sm" onClick={() => toast.success('Ticket assigned', 'Now owned by Ada Lovelace.')}>
              Success
            </Button>
            <Button variant="secondary" size="sm" onClick={() => toast.info('Analysis queued')}>
              Info
            </Button>
            <Button variant="secondary" size="sm" onClick={() => toast.warning('SLA due soon', '38 minutes remaining.')}>
              Warning
            </Button>
            <Button variant="secondary" size="sm" onClick={() => toast.error('Could not send reply', 'The connection dropped. Your draft is safe.')}>
              Error
            </Button>
          </Row>
        </Section>

        <Section title="Colour tokens" description="Light and dark are authored as pairs; switch the theme above.">
          <div className="flex flex-col gap-4">
            <Row label="Surfaces">
              {SURFACE_TOKENS.map((token) => (
                <div key={token} className="flex flex-col items-center gap-1.5">
                  <div className={cn('size-12 rounded-lg border border-border', token)} />
                  <span className="text-2xs text-ink-subtle">{token.replace('bg-', '')}</span>
                </div>
              ))}
            </Row>

            <Row label="Roles">
              {ROLE_TOKENS.map(([name, token]) => (
                <div key={name} className="flex flex-col items-center gap-1.5">
                  <div className={cn('grid size-12 place-items-center rounded-lg text-xs font-semibold', token)}>
                    Aa
                  </div>
                  <span className="text-2xs text-ink-subtle">{name}</span>
                </div>
              ))}
            </Row>

            <Row label="Status">
              {STATUS_TOKENS.map(([name, token]) => (
                <div key={name} className="flex flex-col items-center gap-1.5">
                  <div className={cn('size-12 rounded-lg', token)} />
                  <span className="text-2xs text-ink-subtle">{name}</span>
                </div>
              ))}
            </Row>

            <Row label="Priority">
              {PRIORITY_TOKENS.map(([name, token]) => (
                <div key={name} className="flex flex-col items-center gap-1.5">
                  <div className={cn('size-12 rounded-lg', token)} />
                  <span className="text-2xs text-ink-subtle">{name}</span>
                </div>
              ))}
            </Row>

            <Row label="Chart series">
              {SERIES_TOKENS.map((token) => (
                <div key={token} className={cn('size-12 rounded-lg', token)} />
              ))}
            </Row>
          </div>
        </Section>

        <Dialog
          open={dialogOpen}
          onClose={() => setDialogOpen(false)}
          title="Change priority"
          description="This updates the SLA clock immediately."
          footer={
            <>
              <Button variant="ghost" onClick={() => setDialogOpen(false)}>
                Cancel
              </Button>
              <Button
                onClick={() => {
                  setDialogOpen(false)
                  toast.success('Priority changed')
                }}
              >
                Save
              </Button>
            </>
          }
        >
          <Field label="New priority">
            <Select defaultValue="high">
              <option value="low">Low</option>
              <option value="medium">Medium</option>
              <option value="high">High</option>
              <option value="urgent">Urgent</option>
            </Select>
          </Field>
        </Dialog>

        <ConfirmDialog
          open={confirmOpen}
          onClose={() => setConfirmOpen(false)}
          onConfirm={() => {
            setConfirmOpen(false)
            toast.success('Ticket deleted')
          }}
          title="Delete this ticket?"
          description="This cannot be undone. The conversation and any attachments go with it."
          confirmLabel="Delete"
          variant="danger"
        />
      </div>
    </AppShell>
  )
}
