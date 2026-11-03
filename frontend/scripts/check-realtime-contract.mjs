import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const backendEvents = readFileSync(
  resolve(frontendRoot, '../backend/app/websocket/events.py'),
  'utf8',
)
const protocol = readFileSync(resolve(frontendRoot, 'src/services/realtime/protocol.ts'), 'utf8')
const invalidations = readFileSync(
  resolve(frontendRoot, 'src/services/realtime/invalidate.ts'),
  'utf8',
)

function requireBlock(source, start, end, label) {
  const startAt = source.indexOf(start)
  const endAt = source.indexOf(end, startAt + start.length)
  if (startAt < 0 || endAt < 0) throw new Error(`Could not locate ${label}`)
  return source.slice(startAt + start.length, endAt)
}

const backendEnum = requireBlock(
  backendEvents,
  'class RealtimeEventType(StrEnum):',
  '\n\n# ---------------------------------------------------------------------------',
  'backend realtime event enum',
)
const backendEventByName = new Map(
  [...backendEnum.matchAll(/^\s+([A-Z_]+)\s*=\s*['"]([^'"]+)['"]/gm)].map((match) => [
    match[1],
    match[2],
  ]),
)

const backendMapping = requireBlock(
  backendEvents,
  'REALTIME_FOR_EVENT: Mapping[TicketEventType, RealtimeEventType] = {',
  '\n\n# Timeline entries',
  'backend ticket event mapping',
)
const mappedEventNames = new Set(
  backendMapping
    .split('\n')
    .map((line) => line.split('#')[0])
    .flatMap((line) => [...line.matchAll(/RealtimeEventType\.([A-Z_]+)/g)].map((match) => match[1]))
    .map((name) => backendEventByName.get(name))
    .filter(Boolean),
)

const frontendEventsBlock = requireBlock(
  protocol,
  'export const REALTIME_EVENT_TYPES = [',
  '] as const',
  'frontend event list',
)
const frontendEvents = new Set([...frontendEventsBlock.matchAll(/['"]([^'"]+)['"]/g)].map((match) => match[1]))
const invalidationBlock = requireBlock(
  invalidations,
  'export const REALTIME_INVALIDATIONS:',
  '\n}\n\nexport function',
  'frontend invalidation table',
)
const invalidationEvents = new Set(
  [...invalidationBlock.matchAll(/^\s*['"]([^'"]+)['"]:/gm)].map((match) => match[1]),
)

const frontendTicketEvents = new Set([...frontendEvents].filter((event) => event.startsWith('ticket.')))
const mismatch = (label, expected, actual) => {
  const missing = [...expected].filter((value) => !actual.has(value))
  const unexpected = [...actual].filter((value) => !expected.has(value))
  if (missing.length || unexpected.length) {
    throw new Error(
      `${label} mismatch; missing: ${missing.join(', ') || 'none'}; unexpected: ${unexpected.join(', ') || 'none'}`,
    )
  }
}

mismatch('Frontend ticket events vs backend REALTIME_FOR_EVENT', mappedEventNames, frontendTicketEvents)
mismatch('Frontend invalidation table vs frontend protocol', frontendEvents, invalidationEvents)
if (!backendEventByName.has('NOTIFICATION_CREATED') || !frontendEvents.has('notification.created')) {
  throw new Error('notification.created is missing from one side of the realtime protocol')
}

console.log(`Realtime contract matches: ${frontendEvents.size} events, ${mappedEventNames.size} ticket events.`)
