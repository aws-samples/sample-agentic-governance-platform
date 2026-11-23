export const ORIGIN = /(?:E\d{1,3}[A-D]?(?:-\d{1,3}[A-D]?)?\/(?:T\d{1,2}[A-Da-d]?|review|fold|merge)|hotfix\/[a-z0-9]+(?:-[a-z0-9]+)*)/
const TAIL =
  /^(?<reason>[^;]+); expires (?<date>\d{4}-\d{2}-\d{2}); added (?<origin>(?:E\d{1,3}[A-D]?(?:-\d{1,3}[A-D]?)?\/(?:T\d{1,2}[A-Da-d]?|review|fold|merge)|hotfix\/[a-z0-9]+(?:-[a-z0-9]+)*))$/
const BAND_AID =
  /\b(fallback|for now|temporar(y|ily)|workaround|hack|band-aid|quick fix|do not remove|don't remove|load-bearing|IMPORTANT|CRITICAL|TODO|FIXME|XXX)\b/i
const MAX_DAYS = 365
const DAY_MS = 86_400_000
const SEPARATOR = /\s-{2,}\s/u
const TS_LINE = /^\/*\s*@ts-expect-error(?<description>.*)/
const TS_BLOCK = /^\s*(?:\/|\*)*\s*@ts-expect-error(?<description>.*)/

function expiryDay(text) {
  const [year, month, day] = text.split('-').map(Number)
  const expiry = new Date(Date.UTC(year, month - 1, day))
  const real =
    expiry.getUTCFullYear() === year && expiry.getUTCMonth() === month - 1 && expiry.getUTCDate() === day
  return real ? expiry.getTime() / DAY_MS : null
}

export function checkTail(justification, today) {
  const tail = TAIL.exec(justification.trim())
  if (!tail) return 'sayWhy'
  if (BAND_AID.test(tail.groups.reason) || BAND_AID.test(tail.groups.origin)) return 'bandAid'
  const expiry = expiryDay(tail.groups.date)
  if (expiry === null) return 'sayWhy'
  const now = Date.UTC(today.getFullYear(), today.getMonth(), today.getDate()) / DAY_MS
  if (expiry < now) return 'expired'
  if (expiry > now + MAX_DAYS) return 'tooFar'
  return null
}

function tsExpectErrorDescription(comment) {
  const match =
    comment.type === 'Line' ? TS_LINE.exec(comment.value) : TS_BLOCK.exec(comment.value.split('\n').at(-1))
  return match?.groups.description ?? ''
}

function justificationOf(description) {
  const separator = SEPARATOR.exec(description)
  return separator ? description.slice(separator.index + separator[0].length) : ''
}

export default {
  meta: {
    type: 'problem',
    docs: { description: 'Require every suppression to carry a reason, an expiry and its origin' },
    messages: {
      sayWhy:
        'Say why, when it expires and who added it: ' +
        '`// eslint-disable-next-line <rule> -- <reason>; expires 2027-11-04; added E<n>/T<id>`, ' +
        'the origin being the epic and task, `E<n>/review`, `E<n>/fold`, `E<n>/merge` or `hotfix/<name>`. ' +
        'An expired suppression is fixed or renewed in a reviewed diff.',
      bandAid: 'The reason is the band-aid the rule exists for; fix the finding or encode the constraint.',
      expired: 'Expired {{date}}: fix the finding or renew with a new date in a reviewed diff.',
      tooFar: `An expiry is at most ${MAX_DAYS} days out.`,
    },
    schema: [],
  },
  create(context) {
    const today = new Date()
    const judge = (node, justification) => {
      const fault = checkTail(justification, today)
      if (!fault) return
      context.report({ node, messageId: fault, data: { date: TAIL.exec(justification.trim())?.groups.date } })
    }
    return {
      Program() {
        for (const directive of context.sourceCode.getDisableDirectives().directives) {
          if (directive.justification) judge(directive.node, directive.justification)
        }
        for (const comment of context.sourceCode.getAllComments()) {
          const description = tsExpectErrorDescription(comment)
          if (description.trim()) judge(comment, justificationOf(description))
        }
      },
    }
  },
}
