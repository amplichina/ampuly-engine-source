'use client';

import { useEffect, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import type { TracePhase } from './traceModel';
import { createOrbRenderer, getOrbAppearance } from './orbRenderer';
import type { OrbRenderer } from './orbRenderer';

// `orbCanvas.css` is imported by `main.tsx`, not here: the page's contract test
// runs under plain Node, which refuses to load a .css module (2026-10-18).

export interface OrbCanvasProps {
  phase: TracePhase;
  connected: boolean;
  actors?: number;
  staticMode?: boolean;
  className?: string;
}

/** Decorative leaf. The parent supplies accessible status/employee text. */
export function OrbCanvas({ phase, connected, actors = 0, staticMode = false, className = '' }: OrbCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const rendererRef = useRef<OrbRenderer | null>(null);
  const currentRef = useRef({ phase, connected, actors, staticMode });
  currentRef.current = { phase, connected, actors, staticMode };
  const [hasWebGL, setHasWebGL] = useState(false);

  useEffect(() => {
    const canvas = canvasRef.current;
    const container = containerRef.current;
    if (!canvas || !container) return;
    let stopped = false;
    let onScreen = true;
    const motionQuery = typeof window.matchMedia === 'function'
      ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
    const renderer = createOrbRenderer(canvas, currentRef.current);
    rendererRef.current = renderer;
    setHasWebGL(Boolean(renderer));
    if (!renderer) return;

    const refreshActivity = () => {
      if (stopped) return;
      renderer.setActive(!document.hidden && onScreen);
    };
    const refreshMotion = () => {
      if (stopped) return;
      renderer.setReducedMotion(Boolean(motionQuery?.matches) || currentRef.current.staticMode);
    };
    const resize = () => {
      if (stopped) return;
      const rect = container.getBoundingClientRect();
      renderer.resize(rect.width, rect.height, window.devicePixelRatio || 1);
    };
    const onContextLost = (event: Event) => {
      // Degrade once. No context-recovery retry loop and no second canvas/context.
      event.preventDefault();
      stopped = true;
      renderer.dispose();
      rendererRef.current = null;
      setHasWebGL(false);
    };

    const intersection = typeof IntersectionObserver === 'function'
      ? new IntersectionObserver(entries => {
        onScreen = Boolean(entries[0]?.isIntersecting);
        refreshActivity();
      }, { threshold: 0.01 }) : null;
    const resizeObserver = typeof ResizeObserver === 'function' ? new ResizeObserver(resize) : null;
    canvas.addEventListener('webglcontextlost', onContextLost);
    document.addEventListener('visibilitychange', refreshActivity);
    // Resize also handles DPR changes when moving a window between displays.
    window.addEventListener('resize', resize, { passive: true });
    motionQuery?.addEventListener('change', refreshMotion);
    intersection?.observe(container);
    resizeObserver?.observe(container);
    refreshMotion();
    resize();
    refreshActivity();

    return () => {
      stopped = true;
      intersection?.disconnect();
      resizeObserver?.disconnect();
      canvas.removeEventListener('webglcontextlost', onContextLost);
      document.removeEventListener('visibilitychange', refreshActivity);
      window.removeEventListener('resize', resize);
      motionQuery?.removeEventListener('change', refreshMotion);
      renderer.dispose();
      if (rendererRef.current === renderer) rendererRef.current = null;
    };
  }, []);

  useEffect(() => {
    const renderer = rendererRef.current;
    renderer?.update({ phase, connected, actors });
  }, [phase, connected, actors]);

  useEffect(() => {
    const query = typeof window.matchMedia === 'function'
      ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
    rendererRef.current?.setReducedMotion(staticMode || Boolean(query?.matches));
  }, [staticMode]);

  const appearance = getOrbAppearance({ phase, connected });
  const style = {
    '--ao-orb-rgb': appearance.color.map(component => Math.round(component * 255)).join(', '),
  } as CSSProperties;

  return (
    <div
      ref={containerRef}
      className={`ao-orb ${hasWebGL ? 'ao-orb--webgl' : 'ao-orb--fallback'} ${className}`.trim()}
      data-phase={phase}
      data-connected={connected ? 'true' : 'false'}
      style={style}
      aria-hidden="true"
    >
      <span className="ao-orb__fallback" />
      <canvas ref={canvasRef} className="ao-orb__canvas" width={320} height={320} aria-hidden="true" tabIndex={-1} />
      <span className="ao-orb__axis ao-orb__axis--horizontal" />
      <span className="ao-orb__axis ao-orb__axis--vertical" />
    </div>
  );
}

export default OrbCanvas;
