import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync('src/index.css', 'utf8') + '\n'
  + readFileSync('src/workbench.css', 'utf8') + '\n' + readFileSync('src/studio.css', 'utf8')
function luminance(hex: string) {
  const digits = hex.length === 4 ? [...hex.slice(1)].map(c => c+c).join('') : hex.slice(1)
  const rgb = [0, 2, 4].map(i => parseInt(digits.slice(i, i+2), 16)/255)
    .map(c => c <= .04045 ? c/12.92 : ((c+.055)/1.055)**2.4)
  return rgb[0]*.2126 + rgb[1]*.7152 + rgb[2]*.0722
}
const ratio = (a: string, b: string) => {
  const x = luminance(a), y = luminance(b)
  return (Math.max(x,y)+.05)/(Math.min(x,y)+.05)
}
describe('Workbench theme text contrast', () => {
  for (const theme of ['bright', 'light', 'warm', 'cool']) it(`${theme} body and secondary text meet 4.5:1`, () => {
    const tokens: Record<string,string> = {}
    for (const block of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
      const selector = block[1].trim()
      if (selector !== ':root' && !selector.includes(`:root[data-theme="${theme}"]`)) continue
      for (const match of block[2].matchAll(/(--[\w-]+):\s*(#[\da-fA-F]{3,6})[;\s]/g)) tokens[match[1]]=match[2]
    }
    for (const foreground of ['--fg', '--fg-2', '--muted', '--faint'])
      for (const background of ['--bg', '--bg-side', '--panel', '--card', '--card-2', '--pill-active'])
        expect(ratio(tokens[foreground], tokens[background]), `${foreground} on ${background}`).toBeGreaterThanOrEqual(4.5)
    expect(ratio(theme === 'warm' || theme === 'cool' ? '#161616' : '#ffffff', tokens['--accent'])).toBeGreaterThanOrEqual(4.5)
  })
})
