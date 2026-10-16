import {
  ArrowRight,
  BookOpen,
  ChartLine,
  ChatsCircle,
  Lock,
  Prohibit,
  Sparkle,
  Timer,
  UserSwitch,
} from '@phosphor-icons/react'
import { Link } from 'react-router-dom'

import { Reveal } from '@/components/marketing/Reveal'
import { Badge } from '@/components/ui/Badge'
import { Card } from '@/components/ui/Card'
import { cn } from '@/utils/cn'

/**
 * The public landing page.
 *
 * Two rules governed what is on it.
 *
 * **Nothing describes a feature the product does not have.** Every claim below
 * maps to something in the API today — the ticket lifecycle in §5, the
 * permission matrix in §3, the SLA policy model, the AI attribution rules in
 * §41, and the knowledge base's refusal behaviour in §24. Where a screen is
 * still to be built (the ticket queue, the dashboards), the copy describes the
 * committed behaviour rather than a screenshot of a page that does not exist
 * yet. Revisit this file if any phase is dropped: a landing page is the one
 * place a false claim is indistinguishable from a true one.
 *
 * **No numbers.** There is no "10,000 teams" and no metric strip, because there
 * is no data behind them and §60 forbids inventing it. Anything that looked
 * like a dashboard here would be a fabricated screenshot with extra steps.
 */

/** §5's lifecycle, minus the branches — the straight line through it. */
const LIFECYCLE = [
  { label: 'Open', variant: 'info' },
  { label: 'Assigned', variant: 'brand' },
  { label: 'In progress', variant: 'brand' },
  { label: 'Resolved', variant: 'success' },
  { label: 'Closed', variant: 'neutral' },
] as const

const PRINCIPLES = [
  {
    icon: <Sparkle weight="light" />,
    title: 'AI drafts. People send.',
    body: 'Every suggestion arrives labelled, with the provider and model that produced it. Accepting one puts it in the composer — a person still presses send. There is no path where generated text reaches a customer unread.',
  },
  {
    icon: <BookOpen weight="light" />,
    title: 'Answers cite, or they refuse.',
    body: 'A knowledge-base answer arrives with the documents behind it, quoted and scored by relevance. Ask something the corpus cannot support and it says so, rather than answering anyway.',
  },
  {
    icon: <Lock weight="light" />,
    title: 'Permissions are enforced, not hidden.',
    body: 'Four roles, one matrix, evaluated on the server against every request. The interface respects the same table, so nobody is shown a control that would be refused.',
  },
] as const

const FEATURES = [
  {
    icon: <ChatsCircle weight="light" />,
    title: 'A thread that keeps its history',
    body: 'Customer replies, agent replies, and internal notes in one timeline, with notes marked as notes. Attachments are checked against their actual bytes, not the extension they arrived with.',
  },
  {
    icon: <Timer weight="light" />,
    title: 'SLAs that say what is left',
    body: 'A policy per priority, a live countdown on every ticket, and a warning before the breach rather than a report after it. The timer re-syncs from the server, so it cannot drift into a comfortable lie.',
  },
  {
    icon: <UserSwitch weight="light" />,
    title: 'Four desks, one product',
    body: 'Admins configure. Managers see the whole board. Agents see what is assigned to them. Customers see their own requests and nothing else — not a note, not a name they should not have.',
  },
  {
    icon: <ChartLine weight="light" />,
    title: 'Metrics that admit their gaps',
    body: 'Volume, resolution time, and SLA performance over a window the screen names out loud. A day with no tickets is a gap in the line, not a zero.',
  },
  {
    icon: <Prohibit weight="light" />,
    title: 'Status changes that follow the rules',
    body: 'The client reads the same transition table the server enforces, so an illegal move is never offered — rather than offered and then refused with an error nobody can act on.',
  },
] as const

export function LandingPage() {
  return (
    <main className="pt-28 sm:pt-32">
      {/* ---------------------------------------------------------------- Hero */}
      <section className="px-4 sm:px-6">
        <div className="mx-auto flex max-w-3xl flex-col items-center gap-6 text-center">
          <Reveal>
            <span className="eyebrow inline-flex items-center gap-2 rounded-full border border-brand-border bg-brand-subtle px-3 py-1 text-brand-text">
              <span className="size-1.5 rounded-full bg-brand" aria-hidden="true" />
              Support desk
            </span>
          </Reveal>

          <Reveal delay={0.06}>
            <h1 className="text-4xl font-semibold tracking-[-0.035em] text-balance text-ink sm:text-6xl">
              The support desk that shows its working.
            </h1>
          </Reveal>

          <Reveal delay={0.12}>
            <p className="max-w-xl text-base text-pretty text-ink-muted sm:text-lg">
              Tickets, SLAs, and an assistant that drafts but never sends. Every suggestion
              carries its label, every answer carries its sources, and every status change
              follows a rule you can read.
            </p>
          </Reveal>

          <Reveal delay={0.18}>
            <div className="flex flex-col items-center gap-3 sm:flex-row">
              <Link
                to="/register"
                className="group inline-flex items-center gap-2.5 rounded-full bg-brand py-2 pr-2 pl-6 text-sm font-medium text-brand-fg shadow-sm transition-colors duration-150 ease-out-expo hover:bg-brand-hover"
              >
                Create a workspace
                <span
                  aria-hidden="true"
                  className="grid size-8 place-items-center rounded-full bg-brand-fg/15 transition-transform duration-300 ease-out-expo group-hover:translate-x-1"
                >
                  <ArrowRight weight="bold" className="size-4" />
                </span>
              </Link>

              <Link
                to="/login"
                className="rounded-full border border-border bg-surface px-6 py-2.5 text-sm font-medium text-ink shadow-xs transition-colors duration-150 ease-out-expo hover:bg-surface-hover"
              >
                Sign in
              </Link>
            </div>
          </Reveal>
        </div>

        {/* The lifecycle, framed in a bezel. Real domain content from §5 rather
            than a mock dashboard: it communicates something true about the
            product and needs no invented data to do it. */}
        <Reveal delay={0.24} className="mx-auto mt-14 max-w-3xl">
          <Card variant="bezel">
            <div className="flex flex-col gap-5 px-5 py-6 sm:px-7 sm:py-8">
              <p className="eyebrow text-ink-subtle">Ticket lifecycle</p>

              <div className="flex flex-wrap items-center gap-2">
                {LIFECYCLE.map((state, index) => (
                  <span key={state.label} className="flex items-center gap-2">
                    {index > 0 && (
                      <ArrowRight
                        aria-hidden="true"
                        weight="light"
                        className="size-3.5 text-ink-subtle"
                      />
                    )}
                    <Badge variant={state.variant} size="md">
                      {state.label}
                    </Badge>
                  </span>
                ))}
              </div>

              <p className="text-[0.8125rem] leading-relaxed text-ink-muted">
                Six states and seven permitted moves. Reopening is its own action rather than a
                status anyone can write, and the interface offers only the edges that exist.
              </p>
            </div>
          </Card>
        </Reveal>
      </section>

      {/* ---------------------------------------------------------- Principles */}
      <section className="px-4 pt-28 sm:px-6 sm:pt-36">
        <div className="mx-auto max-w-5xl">
          <Reveal>
            <div className="flex max-w-2xl flex-col gap-3">
              <span className="eyebrow text-ink-subtle">How it behaves</span>
              <h2 className="text-2xl font-semibold tracking-[-0.025em] text-balance text-ink sm:text-3xl">
                Three promises the product keeps even when it would be easier not to.
              </h2>
            </div>
          </Reveal>

          <div className="mt-10 grid gap-4 sm:grid-cols-3">
            {PRINCIPLES.map((principle, index) => (
              <Reveal key={principle.title} delay={index * 0.08}>
                <Card variant="bezel" className="h-full">
                  <div className="flex h-full flex-col gap-3 px-5 py-5">
                    <span
                      aria-hidden="true"
                      className="grid size-9 place-items-center rounded-xl border border-border-subtle bg-surface-sunken text-brand-text [&_svg]:size-4.5"
                    >
                      {principle.icon}
                    </span>

                    <h3 className="text-sm font-semibold text-ink">{principle.title}</h3>
                    <p className="text-[0.8125rem] leading-relaxed text-ink-muted">
                      {principle.body}
                    </p>
                  </div>
                </Card>
              </Reveal>
            ))}
          </div>
        </div>
      </section>

      {/* ------------------------------------------------------------ Features */}
      <section className="px-4 pt-28 sm:px-6 sm:pt-36">
        <div className="mx-auto max-w-5xl">
          <Reveal>
            <div className="flex max-w-2xl flex-col gap-3">
              <span className="eyebrow text-ink-subtle">The desk</span>
              <h2 className="text-2xl font-semibold tracking-[-0.025em] text-balance text-ink sm:text-3xl">
                Everything an agent needs, and nothing they have to work around.
              </h2>
            </div>
          </Reveal>

          {/* An asymmetric bento rather than a uniform grid: the first card spans
              two columns so the eye has an entry point instead of five equal
              options. Everything collapses to one column below `sm`. */}
          <div className="mt-10 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {FEATURES.map((feature, index) => (
              <Reveal
                key={feature.title}
                delay={(index % 3) * 0.08}
                className={cn(index === 0 && 'sm:col-span-2 lg:col-span-1')}
              >
                <Card variant="plain" className="h-full">
                  <div className="flex h-full flex-col gap-3 p-5">
                    <span
                      aria-hidden="true"
                      className="grid size-9 place-items-center rounded-xl border border-border-subtle bg-surface-sunken text-ink-muted [&_svg]:size-4.5"
                    >
                      {feature.icon}
                    </span>

                    <h3 className="text-sm font-semibold text-ink">{feature.title}</h3>
                    <p className="text-[0.8125rem] leading-relaxed text-ink-muted">
                      {feature.body}
                    </p>
                  </div>
                </Card>
              </Reveal>
            ))}
          </div>
        </div>
      </section>

      {/* ------------------------------------------------------------ Closing */}
      <section className="px-4 pt-28 sm:px-6 sm:pt-36">
        <Reveal className="mx-auto max-w-3xl">
          <Card variant="bezel">
            <div className="flex flex-col items-center gap-5 px-6 py-12 text-center sm:px-12 sm:py-16">
              <h2 className="text-2xl font-semibold tracking-[-0.025em] text-balance text-ink sm:text-3xl">
                Start with an empty desk.
              </h2>

              <p className="max-w-md text-sm text-pretty text-ink-muted">
                Creating a workspace takes a minute and makes you its administrator. Bring your
                team in afterwards, one role at a time.
              </p>

              <Link
                to="/register"
                className="group mt-1 inline-flex items-center gap-2.5 rounded-full bg-brand py-2 pr-2 pl-6 text-sm font-medium text-brand-fg shadow-sm transition-colors duration-150 ease-out-expo hover:bg-brand-hover"
              >
                Create a workspace
                <span
                  aria-hidden="true"
                  className="grid size-8 place-items-center rounded-full bg-brand-fg/15 transition-transform duration-300 ease-out-expo group-hover:translate-x-1"
                >
                  <ArrowRight weight="bold" className="size-4" />
                </span>
              </Link>
            </div>
          </Card>
        </Reveal>
      </section>
    </main>
  )
}
