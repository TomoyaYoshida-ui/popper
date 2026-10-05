import { expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Markdown } from '../src/components/Markdown'
it('renders code and evidence warnings without interpreting model HTML', () => {
  const view = render(<Markdown text={'## 结果\n\n> **证据绑定**，仍需人工审校\n\n```python\nprint("<script>unsafe</script>")\n```\n\n- 观察一\n- 观察二'} />)
  expect(screen.getByRole('heading', { name: '结果' })).toBeTruthy()
  expect(view.container.querySelector('blockquote')?.textContent).toContain('证据绑定')
  expect(screen.getByLabelText('python 代码').textContent).toContain('<script>unsafe</script>')
  expect(view.container.querySelector('script')).toBeNull()
  expect(screen.getAllByRole('listitem')).toHaveLength(2)
})
