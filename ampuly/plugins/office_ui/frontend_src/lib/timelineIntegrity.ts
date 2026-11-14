/**
 * What to say about a merged history whose parts differ in what they prove.
 *
 * A timeline shows order events, gate events, runtime events and effects as
 * one list. Two of those carry a hash chain and the rest do not, so a reader
 * who sees "verified" once and carries it across the whole page has been
 * misled by the layout. These are the three things that can honestly be said,
 * kept apart because they justify different levels of confidence.
 */

export type TimelineIntegrity = {
  streams: Array<{ stream: string; level: string; tamper_evident: boolean }>
  broken: string[]
  tamper_evident: string[]
  summary?: string
}

export type IntegrityLine = { key: string; params: Record<string, string | number> }

export function integrityLine(integrity: TimelineIntegrity | null | undefined): IntegrityLine | null {
  if (!integrity || !Array.isArray(integrity.streams) || integrity.streams.length === 0) return null
  // A chain that did not check out is the loudest thing here, and it names
  // itself: "something is wrong somewhere" sends nobody anywhere.
  if (integrity.broken.length > 0) {
    return { key: 'orders.timelineBroken', params: { streams: integrity.broken.join('、') } }
  }
  return {
    key: 'orders.timelineIntegrity',
    params: { proven: integrity.tamper_evident.length, total: integrity.streams.length },
  }
}
