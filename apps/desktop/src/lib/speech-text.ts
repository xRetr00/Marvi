import type {
  Blockquote,
  Code,
  Content,
  Delete,
  Emphasis,
  Heading,
  Image,
  InlineCode,
  Link,
  List,
  ListItem,
  Paragraph,
  Root,
  Strong,
  Table,
  TableCell,
  TableRow,
  Text,
  ThematicBreak
} from 'mdast'
import { toString } from 'mdast-util-to-string'
import remarkGfm from 'remark-gfm'
import remarkParse from 'remark-parse'
import { unified } from 'unified'

const EMOJI_RE = /(?:[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}]|[\u{FE0F}\u{200D}]|[\u{E0020}-\u{E007F}])+/gu

const PARAGRAPH_BREAK_RE = /[ \t]*\n{2,}[ \t]*/g
const SOFT_BREAK_RE = /[ \t]*\n[ \t]*/g

const THINKING_PREFIX_RE =
  /^\s*(?:\([^)\n]{1,48}\)\s*)?(?:processing|thinking|reasoning|analyzing|pondering|contemplating|musing|cogitating|ruminating|deliberating|mulling|reflecting|computing|synthesizing|formulating|brainstorming)\.\.\.\s*/i

const URL_RE = /\bhttps?:\/\/\S+/gi

const markdownParser = unified().use(remarkParse).use(remarkGfm)

function normalizeLineBreaks(text: string): string {
  return text
    .replace(/\r\n?/g, '\n')
    .replace(/(\p{L})-\n(\p{L})/gu, '$1$2')
    .replace(PARAGRAPH_BREAK_RE, '. ')
    .replace(SOFT_BREAK_RE, ' ')
}

function stripSpeechNoise(text: string): string {
  return normalizeLineBreaks(text)
    .replace(THINKING_PREFIX_RE, ' ')
    .replace(URL_RE, ' link ')
    .replace(EMOJI_RE, ' ')
    .replace(/\s+([,.!?;:])/g, '$1')
    .replace(/\s+/g, ' ')
    .trim()
}

function sentence(text: string): string {
  const cleaned = stripSpeechNoise(text)

  if (!cleaned) {
    return ''
  }

  return /[.!?]$/.test(cleaned) ? cleaned : `${cleaned}.`
}

function joinParts(parts: string[]): string {
  return parts.map(part => part.trim()).filter(Boolean).join(' ')
}

function readableList(items: string[]): string {
  const clean = items.map(item => stripSpeechNoise(item)).filter(Boolean)

  if (clean.length <= 1) {
    return clean[0] ?? ''
  }

  if (clean.length === 2) {
    return `${clean[0]} and ${clean[1]}`
  }

  return `${clean.slice(0, -1).join(', ')}, and ${clean.at(-1)}`
}

function renderChildren(children: Content[] | undefined): string {
  return joinParts((children ?? []).map(renderNode))
}

function renderParagraph(node: Paragraph): string {
  return sentence(renderChildren(node.children))
}

function renderHeading(node: Heading): string {
  return sentence(toString(node))
}

function renderList(node: List): string {
  return joinParts(
    node.children.map((item, index) => {
      const text = renderListItem(item)
      const prefix = node.ordered ? `${(node.start ?? 1) + index}. ` : ''

      return sentence(`${prefix}${text}`)
    })
  )
}

function renderListItem(node: ListItem): string {
  return joinParts(node.children.map(renderNode)).replace(/\s+/g, ' ').trim()
}

function renderBlockquote(node: Blockquote): string {
  const text = renderChildren(node.children)

  return text ? sentence(`Quote: ${text}`) : ''
}

function renderCode(_node: Code): string {
  return 'Code block skipped.'
}

function renderInlineCode(node: InlineCode): string {
  return node.value
}

function renderLink(node: Link): string {
  const text = renderChildren(node.children) || node.url

  return stripSpeechNoise(text)
}

function renderImage(node: Image): string {
  const label = stripSpeechNoise(node.alt ?? '')

  return label ? sentence(`Image skipped: ${label}`) : 'Image skipped.'
}

function renderTableCell(node: TableCell): string {
  return stripSpeechNoise(renderChildren(node.children))
}

function renderTableRow(node: TableRow): string[] {
  return node.children.map(renderTableCell).filter(Boolean)
}

function renderTable(node: Table): string {
  const [headerRow, ...bodyRows] = node.children.map(renderTableRow)

  if (!headerRow?.length) {
    return `Table with ${bodyRows.length} rows.`
  }

  return sentence(`Table with columns ${headerRow.join(', ')}, and ${bodyRows.length} rows`)
}

function renderFormattingNode(node: Strong | Emphasis | Delete): string {
  return renderChildren(node.children)
}

function renderText(node: Text): string {
  return node.value
}

function renderThematicBreak(_node: ThematicBreak): string {
  return ''
}

function renderNode(node: Content): string {
  switch (node.type) {
    case 'blockquote':
      return renderBlockquote(node)

    case 'break':
      return ' '

    case 'code':
      return renderCode(node)

    case 'delete':

    case 'emphasis':

    case 'strong':
      return renderFormattingNode(node)

    case 'heading':
      return renderHeading(node)

    case 'html':

    case 'definition':

    case 'footnoteDefinition':

    case 'yaml':
      return ''

    case 'image':
      return renderImage(node)

    case 'imageReference':
      return sentence(`Image skipped: ${stripSpeechNoise(node.alt ?? '')}`)

    case 'inlineCode':
      return renderInlineCode(node)

    case 'link':
      return renderLink(node)

    case 'linkReference':
      return renderChildren(node.children)

    case 'list':
      return renderList(node)

    case 'listItem':
      return renderListItem(node)

    case 'paragraph':
      return renderParagraph(node)

    case 'table':
      return renderTable(node)

    case 'tableCell':
      return renderTableCell(node)

    case 'tableRow':
      return renderTableRow(node).join(' ')

    case 'text':
      return renderText(node)

    case 'thematicBreak':
      return renderThematicBreak(node)

    default:
      return ''
  }
}

export function renderMarkdownForSpeech(text: string): string {
  if (!text.trim()) {
    return ''
  }

  const tree = markdownParser.parse(text) as Root

  return stripSpeechNoise(joinParts(tree.children.map(renderNode)))
}

export function sanitizeTextForSpeech(text: string): string {
  return renderMarkdownForSpeech(text)
}
