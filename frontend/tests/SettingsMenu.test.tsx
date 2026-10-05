import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { SettingsMenu } from '../src/components/SettingsMenu'
import { WorkbenchIcon } from '../src/components/WorkbenchIcon'

afterEach(() => { cleanup(); localStorage.clear(); document.documentElement.removeAttribute('data-theme') })
describe('Studio theme controls', () => {
  it('preserves the saved theme and exposes selection without decorative icon names', () => {
    localStorage.setItem('theme', 'cool')
    render(<SettingsMenu placement="rail" />)
    expect(document.documentElement.getAttribute('data-theme')).toBe('cool')
    fireEvent.click(screen.getByRole('button', { name: '设置主题' }))
    expect(screen.getByRole('button', { name: '冷黑 ✓' }).getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(screen.getByRole('button', { name: '纯白' }))
    expect(localStorage.getItem('theme')).toBe('bright')
    expect(screen.queryByRole('group', { name: '主题选择' })).toBeNull()
  })
  it('closes with Escape and returns focus to the trigger without changing theme', () => {
    render(<SettingsMenu />)
    const trigger = screen.getByRole('button', { name: '设置主题' })
    fireEvent.click(trigger)
    screen.getByRole('button', { name: '暖黑' }).focus()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    expect(document.activeElement).toBe(trigger)
    expect(localStorage.getItem('theme')).toBe('bright')
  })
  it('uses hidden SVGs, keeping actions named by their visible labels', () => {
    const { container } = render(<button><WorkbenchIcon kind="files" />打开文件</button>)
    expect(screen.getByRole('button', { name: '打开文件' })).toBeTruthy()
    expect(container.querySelector('svg')?.getAttribute('aria-hidden')).toBe('true')
  })
})
