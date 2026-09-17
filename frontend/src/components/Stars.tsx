import { useEffect, useRef } from 'react'

/** 全屏画布星空（对齐设计稿 #stars）：~900 颗带漂移与闪烁相位的星点。 */
export function Stars() {
  const ref = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = ref.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    let width = 0
    let height = 0
    type Star = { x: number; y: number; z: number; r: number; ph: number; sp: number }
    let stars: Star[] = []
    let raf = 0

    const rebuild = () => {
      width = window.innerWidth
      height = window.innerHeight
      canvas.width = width
      canvas.height = height
      const dpr = Math.min(window.devicePixelRatio || 1, 2)
      canvas.width = width * dpr
      canvas.height = height * dpr
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      stars = Array.from({ length: 900 }, () => ({
        x: Math.random() * width,
        y: Math.random() * height,
        z: 1 + Math.random() * 0.8,
        r: 0.2 + Math.random() * 1.1,
        ph: Math.random() * Math.PI * 2,
        sp: 0.2 + Math.random() * 0.6,
      }))
    }

    let starColor = '255,255,255'
    const readStarColor = () => {
      const cs = getComputedStyle(document.documentElement)
      starColor = cs.getPropertyValue('--star').trim() || '255,255,255'
    }

    const tick = () => {
      ctx.clearRect(0, 0, width, height)
      readStarColor()
      const t = performance.now() / 1000
      for (const s of stars) {
        s.x += Math.sin(t * 0.1 + s.ph) * 0.04 * s.sp
        s.y -= 0.015 * s.sp * s.z
        if (s.y < -2) { s.y = height + 2; s.x = Math.random() * width }
        const tw = 0.55 + 0.45 * Math.sin(t * s.sp + s.ph * 5)
        const alpha = (0.25 + 0.6 * tw) * Math.min(s.z, 1.6)
        ctx.beginPath()
        ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2)
        ctx.fillStyle = `rgba(${starColor},${alpha})`
        ctx.fill()
      }
      raf = requestAnimationFrame(tick)
    }

    rebuild()
    tick()
    const onResize = () => rebuild()
    window.addEventListener('resize', onResize)
    return () => {
      cancelAnimationFrame(raf)
      window.removeEventListener('resize', onResize)
    }
  }, [])

  return <canvas ref={ref} className="stars-canvas" />
}