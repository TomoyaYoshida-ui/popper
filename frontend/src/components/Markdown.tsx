import type { ReactNode } from 'react'

function inline(text: string): ReactNode[] {
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g)
  return parts.filter(Boolean).map((part, index) => {
    if (part.startsWith('**') && part.endsWith('**')) {
      return <strong key={index}>{part.slice(2, -2)}</strong>
    }
    if (part.startsWith('`') && part.endsWith('`')) {
      return <code key={index}>{part.slice(1, -1)}</code>
    }
    return <span key={index}>{part}</span>
  })
}

/**
 * Safe, deliberately small Markdown renderer for model replies.
 * React escapes all text; no raw HTML is interpreted. Supported blocks are headings,
 * paragraphs, code fences, quotations and lists, with bold and inline-code spans.
 */
export function Markdown({ text }: { text: string }) {
  const lines = text.replace(/\r\n?/g, '\n').split('\n')
  const blocks: ReactNode[] = []
  let index = 0

  while (index < lines.length) {
    const line = lines[index]
    if (!line.trim()) { index += 1; continue }

    if (/^\s*```/.test(line)) {
      const start = index
      const language = line.trim().slice(3).trim()
      const code: string[] = []
      index += 1
      while (index < lines.length && !/^\s*```/.test(lines[index])) code.push(lines[index++])
      if (index < lines.length) index += 1
      blocks.push(<pre key={`code-${start}`} className="markdown-code" aria-label={language ? `${language} 代码` : '代码'}><code>{code.join('\n')}</code></pre>)
      continue
    }
    if (/^\s*>\s?/.test(line)) {
      const start = index, quote: string[] = []
      while (index < lines.length && /^\s*>\s?/.test(lines[index])) quote.push(lines[index++].replace(/^\s*>\s?/, ''))
      blocks.push(<blockquote key={`quote-${start}`}>{inline(quote.join(' '))}</blockquote>)
      continue
    }

    const heading = /^(#{1,4})\s+(.+)$/.exec(line)
    if (heading) {
      const level = heading[1].length
      const body = inline(heading[2])
      blocks.push(level === 1 ? <h2 key={index}>{body}</h2>
        : level === 2 ? <h3 key={index}>{body}</h3>
          : <h4 key={index}>{body}</h4>)
      index += 1
      continue
    }

    const unordered = /^\s*[-*]\s+(.+)$/.exec(line)
    const ordered = /^\s*\d+[.)]\s+(.+)$/.exec(line)
    if (unordered || ordered) {
      const orderedList = !!ordered
      const items: ReactNode[] = []
      while (index < lines.length) {
        const match = orderedList
          ? /^\s*\d+[.)]\s+(.+)$/.exec(lines[index])
          : /^\s*[-*]\s+(.+)$/.exec(lines[index])
        if (!match) break
        items.push(<li key={index}>{inline(match[1])}</li>)
        index += 1
      }
      blocks.push(orderedList ? <ol key={`ol-${index}`}>{items}</ol> : <ul key={`ul-${index}`}>{items}</ul>)
      continue
    }

    const paragraph: string[] = [line.trim()]
    index += 1
    while (index < lines.length && lines[index].trim()
      && !/^(#{1,4})\s+/.test(lines[index])
      && !/^\s*(?:```|>)/.test(lines[index])
      && !/^\s*(?:[-*]|\d+[.)])\s+/.test(lines[index])) {
      paragraph.push(lines[index].trim())
      index += 1
    }
    blocks.push(<p key={`p-${index}`}>{inline(paragraph.join(' '))}</p>)
  }

  return <div className="markdown">{blocks}</div>
}
