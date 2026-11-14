// 视界熔炉 · HORIZON FORGE — every agent's task stream spirals into a black-hole
// accretion disk; the core projects the deliverable, which is forged into parts and
// delivered outward to the task bay. three.js (MIT) + UnrealBloomPass (MIT); all shaders
// here are original Ampuly code. Theme idea credited to cyberdaddyleo/agent-display
// (PolyForm-Noncommercial) — ideas only, no code, shader or asset used.
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import * as THREE from 'three'
import { EffectComposer } from 'three/examples/jsm/postprocessing/EffectComposer.js'
import { OutputPass } from 'three/examples/jsm/postprocessing/OutputPass.js'
import { RenderPass } from 'three/examples/jsm/postprocessing/RenderPass.js'
import { UnrealBloomPass } from 'three/examples/jsm/postprocessing/UnrealBloomPass.js'

import { FX_COLORS, freshPulses, hash01, rng, stateColor, taskColor } from '../fx'
import { useFps } from '../useFps'
import { SimNote, StatStrip, ThemeTitle, TraceRecords } from './ThemeChrome'
import type { ThemeViewProps } from './types'

export const FORGE_STREAM_CAP = 2400
const DISK_POINTS = 14000

const POINT_FRAG = /* glsl */`
varying vec3 vColor;
varying float vAlpha;
varying float vSquare;
void main() {
  vec2 c = gl_PointCoord - 0.5;
  float d = vSquare > 0.5 ? max(abs(c.x), abs(c.y)) : length(c);
  float a = vSquare > 0.5 ? step(d, 0.42) : smoothstep(0.5, 0.0, d);
  if (a <= 0.001) discard;
  gl_FragColor = vec4(vColor * a * vAlpha, a * vAlpha);
}`

const DISK_VERT = /* glsl */`
attribute float aR;
attribute float aA;
attribute float aY;
attribute float aSize;
attribute vec3 aColor;
uniform float uTime;
uniform float uInner;
uniform float uFlare;
uniform float uPx;
varying vec3 vColor;
varying float vAlpha;
varying float vSquare;
void main() {
  float omega = 0.55 * pow(uInner / aR, 1.5);
  float a = aA + uTime * omega;
  vec3 p = vec3(cos(a) * aR, aY, sin(a) * aR);
  gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 1.0);
  float doppler = 0.55 + 0.75 * max(0.0, cos(a));
  gl_PointSize = aSize * uPx;
  vColor = aColor * (1.0 + uFlare * 0.8);
  vAlpha = doppler;
  vSquare = 0.0;
}`

const STREAM_VERT = /* glsl */`
attribute vec2 aStart;
attribute float aSeed;
attribute float aSpeed;
attribute float aGain;
attribute vec3 aColor;
uniform float uTime;
uniform float uInner;
uniform float uSquish;
uniform float uPx;
varying vec3 vColor;
varying float vAlpha;
varying float vSquare;
void main() {
  float s = fract(aSeed * 7.31 + uTime * aSpeed);
  float r0 = length(aStart);
  float a0 = atan(aStart.y, aStart.x);
  float e = pow(s, 0.85);
  float r = mix(r0, uInner * 1.15, e);
  float a = a0 + e * e * 3.2;
  vec2 p = vec2(cos(a), sin(a) * mix(1.0, uSquish, smoothstep(0.35, 1.0, e))) * r;
  float wob = (1.0 - e) * 10.0;
  p += vec2(sin(aSeed * 91.0 + uTime * 1.9), cos(aSeed * 53.0 + uTime * 1.4)) * wob;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 30.0, 1.0);
  gl_PointSize = mix(3.0, 1.5, e) * uPx;
  vColor = aColor;
  vAlpha = aGain * smoothstep(0.0, 0.06, s) * (1.0 - smoothstep(0.86, 1.0, s));
  vSquare = 0.0;
}`

const OUT_VERT = /* glsl */`
attribute vec2 aEnd;
attribute float aSeed;
attribute float aSpeed;
attribute vec3 aColor;
uniform float uTime;
uniform vec2 uOrigin;
uniform float uPx;
varying vec3 vColor;
varying float vAlpha;
varying float vSquare;
void main() {
  float s = fract(aSeed * 5.13 + uTime * aSpeed);
  float e = 1.0 - pow(1.0 - s, 2.0);
  vec2 d = aEnd - uOrigin;
  vec2 n = normalize(vec2(-d.y, d.x));
  vec2 p = uOrigin + d * e + n * sin(3.14159 * e) * (aSeed - 0.5) * 120.0;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 40.0, 1.0);
  gl_PointSize = (fract(aSeed * 13.7) > 0.7 ? 4.0 : 1.8) * uPx;
  vColor = aColor;
  vAlpha = smoothstep(0.0, 0.1, s) * (1.0 - smoothstep(0.9, 1.0, s));
  vSquare = fract(aSeed * 13.7) > 0.7 ? 1.0 : 0.0;
}`

interface Anchor { id: string; x: number; y: number }

function diskColor(t: number): THREE.Color {
  // t: 0 inner (white-hot) -> 1 outer (deep violet)
  const stops: [number, string][] = [[0, '#fff6e6'], [0.18, '#ffd08a'], [0.42, '#ff7a3d'], [0.7, '#c2368f'], [1, '#2a1a6e']]
  for (let i = 1; i < stops.length; i += 1) {
    if (t <= stops[i][0]) {
      const k = (t - stops[i - 1][0]) / (stops[i][0] - stops[i - 1][0])
      return new THREE.Color(stops[i - 1][1]).lerp(new THREE.Color(stops[i][1]), k)
    }
  }
  return new THREE.Color(stops[stops.length - 1][1])
}

function pointsMaterial(vertexShader: string, uniforms: Record<string, THREE.IUniform>) {
  return new THREE.ShaderMaterial({
    vertexShader, fragmentShader: POINT_FRAG, uniforms,
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
  })
}

export function ForgeTheme({ data, model, selectedId, reducedMotion, pulses, onSelect, onExpandFold, onUnsupported }: ThemeViewProps) {
  const stageRef = useRef<HTMLDivElement | null>(null)
  const canvasHostRef = useRef<HTMLDivElement | null>(null)
  const rowRefs = useRef(new Map<string, HTMLElement>())
  const bayRefs = useRef(new Map<string, HTMLElement>())
  const leftRef = useRef<HTMLElement | null>(null)
  const sceneRef = useRef<{
    rebuild: (inputs: Anchor[], outputs: Anchor[]) => void
    flare: () => void
  } | null>(null)
  const [anchors, setAnchors] = useState<{ inputs: Anchor[]; outputs: Anchor[]; w: number; h: number }>({ inputs: [], outputs: [], w: 0, h: 0 })
  const [failed, setFailed] = useState(false)
  const animate = !reducedMotion
  const fps = useFps(animate && !failed)
  const seen = useRef(new Set<string>())
  const [hot, setHot] = useState<ReadonlySet<string>>(() => new Set())
  const modelRef = useRef(model)
  modelRef.current = model

  // ---- renderer (mount once)
  useEffect(() => {
    const host = canvasHostRef.current
    if (!host) return
    let renderer: THREE.WebGLRenderer
    try {
      renderer = new THREE.WebGLRenderer({ antialias: false, alpha: false, powerPreference: 'high-performance' })
    } catch {
      setFailed(true)
      onUnsupported?.()
      return
    }
    const dpr = Math.min(1.5, window.devicePixelRatio || 1)
    renderer.setPixelRatio(dpr)
    renderer.setClearColor(new THREE.Color(FX_COLORS.space), 1)
    host.appendChild(renderer.domElement)
    const scene = new THREE.Scene()
    const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, -3000, 3000)
    camera.position.set(0, 0, 1000)
    const composer = new EffectComposer(renderer)
    composer.addPass(new RenderPass(scene, camera))
    const bloom = new UnrealBloomPass(new THREE.Vector2(256, 256), 0.7, 0.45, 0.32)
    composer.addPass(bloom)
    composer.addPass(new OutputPass())

    let inner = 60
    const uniforms = {
      uTime: { value: 0 }, uInner: { value: inner }, uFlare: { value: 0 }, uPx: { value: dpr },
      uSquish: { value: 0.28 }, uOrigin: { value: new THREE.Vector2(0, 0) },
    }
    const shared = (keys: (keyof typeof uniforms)[]) => Object.fromEntries(keys.map(key => [key, uniforms[key]])) as Record<string, THREE.IUniform>

    // stars
    const random = rng(42)
    const starGeo = new THREE.BufferGeometry()
    const starPos = new Float32Array(1600 * 3)
    for (let i = 0; i < 1600; i += 1) { starPos[i * 3] = random() * 2 - 1; starPos[i * 3 + 1] = random() * 2 - 1; starPos[i * 3 + 2] = -500 }
    starGeo.setAttribute('position', new THREE.BufferAttribute(starPos, 3))
    const stars = new THREE.Points(starGeo, new THREE.PointsMaterial({ color: 0x8fa6d8, size: 1.1 * dpr, sizeAttenuation: false, transparent: true, opacity: 0.55 }))
    scene.add(stars)

    // accretion disk (tilted) + lensed halo (facing camera)
    const disk = new THREE.Group()
    disk.rotation.x = 0.32
    disk.rotation.z = -0.12
    scene.add(disk)
    const makeDisk = (count: number, rMin: number, rMax: number, thickness: number, sizeMul: number, gain: number) => {
      const geo = new THREE.BufferGeometry()
      const aR = new Float32Array(count); const aA = new Float32Array(count); const aY = new Float32Array(count)
      const aSize = new Float32Array(count); const aColor = new Float32Array(count * 3)
      for (let i = 0; i < count; i += 1) {
        const k = Math.pow(random(), 1.8)
        aR[i] = rMin + (rMax - rMin) * k
        aA[i] = random() * Math.PI * 2
        aY[i] = (random() - 0.5) * thickness * (1 - k * 0.6)
        aSize[i] = (0.8 + random() * 1.8) * sizeMul
        const c = diskColor(k).multiplyScalar(gain * (0.6 + random() * 0.6))
        aColor[i * 3] = c.r; aColor[i * 3 + 1] = c.g; aColor[i * 3 + 2] = c.b
      }
      geo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(count * 3), 3))
      geo.setAttribute('aR', new THREE.BufferAttribute(aR, 1))
      geo.setAttribute('aA', new THREE.BufferAttribute(aA, 1))
      geo.setAttribute('aY', new THREE.BufferAttribute(aY, 1))
      geo.setAttribute('aSize', new THREE.BufferAttribute(aSize, 1))
      geo.setAttribute('aColor', new THREE.BufferAttribute(aColor, 3))
      const pts = new THREE.Points(geo, pointsMaterial(DISK_VERT, shared(['uTime', 'uInner', 'uFlare', 'uPx'])))
      pts.frustumCulled = false
      return pts
    }
    let diskPoints: THREE.Points | null = null
    let haloPoints: THREE.Points | null = null
    const halo = new THREE.Group()
    halo.rotation.x = Math.PI / 2 - 0.08
    scene.add(halo)
    const hole = new THREE.Mesh(new THREE.SphereGeometry(1, 48, 32), new THREE.MeshBasicMaterial({ color: 0x000000 }))
    scene.add(hole)
    const ring = new THREE.Mesh(new THREE.RingGeometry(1, 1.025, 160), new THREE.MeshBasicMaterial({ color: 0xffe2b0, transparent: true, opacity: 0.9, blending: THREE.AdditiveBlending, depthWrite: false }))
    ring.position.z = 5
    scene.add(ring)
    // projected deliverable hologram above the core
    const holo = new THREE.Group()
    const holoOuter = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.IcosahedronGeometry(1, 1)), new THREE.LineBasicMaterial({ color: 0x7cf7ff, transparent: true, opacity: 0.85, blending: THREE.AdditiveBlending }))
    const holoInner = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.OctahedronGeometry(0.55, 0)), new THREE.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.9, blending: THREE.AdditiveBlending }))
    holo.add(holoOuter, holoInner)
    scene.add(holo)
    const beamGeo = new THREE.BufferGeometry()
    beamGeo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(24 * 2 * 3), 3))
    const beam = new THREE.LineSegments(beamGeo, new THREE.LineBasicMaterial({ color: 0x7cf7ff, transparent: true, opacity: 0.22, blending: THREE.AdditiveBlending }))
    scene.add(beam)

    let streams: THREE.Points | null = null
    let guides: THREE.LineSegments | null = null
    let outputs: THREE.Points | null = null
    let width = 1
    let height = 1

    const layoutCore = () => {
      inner = Math.max(30, Math.min(width * 0.09, height * 0.12))
      uniforms.uInner.value = inner
      if (diskPoints) { disk.remove(diskPoints); diskPoints.geometry.dispose() }
      if (haloPoints) { halo.remove(haloPoints); haloPoints.geometry.dispose() }
      diskPoints = makeDisk(DISK_POINTS, inner * 1.3, inner * 4.6, inner * 0.12, 1.25, 0.95)
      haloPoints = makeDisk(4200, inner * 1.08, inner * 1.7, inner * 0.04, 1.0, 0.8)
      disk.add(diskPoints)
      halo.add(haloPoints)
      hole.scale.setScalar(inner)
      ring.scale.setScalar(inner * 1.04)
      holo.scale.setScalar(inner * 0.42)
      holo.position.set(0, inner * 2.9, 60)
      uniforms.uOrigin.value.set(0, inner * 2.9)
      const pos = beamGeo.getAttribute('position') as THREE.BufferAttribute
      for (let i = 0; i < 24; i += 1) {
        const a = (i / 24) * Math.PI * 2
        pos.setXYZ(i * 2, Math.cos(a) * inner * 0.3, inner * 0.6, 50)
        pos.setXYZ(i * 2 + 1, Math.cos(a) * inner * 0.42, inner * 2.9 + Math.sin(a) * inner * 0.42 * 0.3, 50)
      }
      pos.needsUpdate = true
      for (let i = 0; i < 1600; i += 1) {
        starPos[i * 3] = (hash01(`sx${i}`) - 0.5) * width
        starPos[i * 3 + 1] = (hash01(`sy${i}`) - 0.5) * height
      }
      starGeo.getAttribute('position').needsUpdate = true
    }

    const resize = () => {
      const rect = host.getBoundingClientRect()
      width = Math.max(1, rect.width)
      height = Math.max(1, rect.height)
      renderer.setSize(width, height, false)
      renderer.domElement.style.width = '100%'
      renderer.domElement.style.height = '100%'
      composer.setSize(width, height)
      bloom.resolution.set(width / 2, height / 2)
      camera.left = -width / 2; camera.right = width / 2; camera.top = height / 2; camera.bottom = -height / 2
      camera.updateProjectionMatrix()
      layoutCore()
    }
    resize()

    const rebuild = (inputs: Anchor[], outs: Anchor[]) => {
      if (streams) { scene.remove(streams); streams.geometry.dispose() }
      if (outputs) { scene.remove(outputs); outputs.geometry.dispose() }
      if (guides) { scene.remove(guides); guides.geometry.dispose(); (guides.material as THREE.Material).dispose() }
      // string-art guides: the same spiral paths the stream shader follows (no wobble),
      // plus a fan of arcs from the hologram to each real task card
      const SEG = 48
      const guideCurves: { pts: [number, number][]; color: THREE.Color; gain: number }[] = []
      for (const anchor of inputs) {
        const node = modelRef.current.members.find(member => member.id === anchor.id)
        const color = new THREE.Color(node?.taskId ? taskColor(node.taskId) : node?.kind === 'human' ? FX_COLORS.human : FX_COLORS.ai)
        const lanes = node?.active ? 3 : 1
        for (let lane = 0; lane < lanes; lane += 1) {
          const r0 = Math.hypot(anchor.x, anchor.y + (lane - 1) * 3)
          const a0 = Math.atan2(anchor.y + (lane - 1) * 3, anchor.x)
          const pts: [number, number][] = []
          for (let i = 0; i <= SEG; i += 1) {
            const e = i / SEG
            const r = r0 + (inner * 1.15 - r0) * e
            const ang = a0 + e * e * (3.2 + lane * 0.35)
            const squish = 1 + (0.28 - 1) * Math.min(1, Math.max(0, (e - 0.35) / 0.65)) ** 2 * (3 - 2 * Math.min(1, Math.max(0, (e - 0.35) / 0.65)))
            pts.push([Math.cos(ang) * r, Math.sin(ang) * squish * r])
          }
          guideCurves.push({ pts, color, gain: node?.active ? 0.22 : 0.06 })
        }
      }
      for (const anchor of outs) {
        const color = new THREE.Color(taskColor(anchor.id))
        for (let lane = 0; lane < 5; lane += 1) {
          const ox = 0; const oy = inner * 2.9
          const dx = anchor.x - ox; const dy = anchor.y - oy
          const len = Math.hypot(dx, dy) || 1
          const nx = -dy / len; const ny = dx / len
          const bend = ((lane / 4) - 0.5) * 120
          const pts: [number, number][] = []
          for (let i = 0; i <= SEG; i += 1) {
            const e = i / SEG
            const k = Math.sin(Math.PI * e) * bend
            pts.push([ox + dx * e + nx * k, oy + dy * e + ny * k])
          }
          guideCurves.push({ pts, color, gain: 0.16 })
        }
      }
      const gPos = new Float32Array(guideCurves.length * SEG * 2 * 3)
      const gCol = new Float32Array(guideCurves.length * SEG * 2 * 3)
      let gi = 0
      for (const curve of guideCurves) {
        for (let i = 0; i < SEG; i += 1) {
          for (const [j, fade] of [[i, i / SEG], [i + 1, (i + 1) / SEG]] as const) {
            const w = curve.gain * (0.35 + 0.65 * Math.sin(Math.PI * Math.min(1, fade * 1.1)))
            gPos[gi * 3] = curve.pts[j][0]; gPos[gi * 3 + 1] = curve.pts[j][1]; gPos[gi * 3 + 2] = 20
            gCol[gi * 3] = curve.color.r * w; gCol[gi * 3 + 1] = curve.color.g * w; gCol[gi * 3 + 2] = curve.color.b * w
            gi += 1
          }
        }
      }
      const gGeo = new THREE.BufferGeometry()
      gGeo.setAttribute('position', new THREE.BufferAttribute(gPos, 3))
      gGeo.setAttribute('color', new THREE.BufferAttribute(gCol, 3))
      guides = new THREE.LineSegments(gGeo, new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false }))
      guides.frustumCulled = false
      scene.add(guides)
      // inputs: particle share follows real activity; idle members trickle faintly
      const weights = inputs.map(anchor => {
        const node = modelRef.current.members.find(member => member.id === anchor.id)
        return node?.active ? 6 + Math.min(4, node.activity) * 6 : 1.5
      })
      const total = weights.reduce((sum, value) => sum + value, 0) || 1
      const count = Math.min(FORGE_STREAM_CAP, Math.round(total * 9))
      const geo = new THREE.BufferGeometry()
      const aStart = new Float32Array(count * 2); const aSeed = new Float32Array(count); const aSpeed = new Float32Array(count)
      const aGain = new Float32Array(count); const aColor = new Float32Array(count * 3)
      let cursor = 0
      inputs.forEach((anchor, index) => {
        const node = modelRef.current.members.find(member => member.id === anchor.id)
        const n = index === inputs.length - 1 ? count - cursor : Math.round((weights[index] / total) * count)
        const color = new THREE.Color(node?.taskId ? taskColor(node.taskId) : node?.kind === 'human' ? FX_COLORS.human : FX_COLORS.ai)
        for (let k = 0; k < n && cursor < count; k += 1, cursor += 1) {
          aStart[cursor * 2] = anchor.x; aStart[cursor * 2 + 1] = anchor.y + (random() - 0.5) * 8
          aSeed[cursor] = random()
          aSpeed[cursor] = (node?.active ? 0.11 : 0.05) * (0.7 + random() * 0.6)
          aGain[cursor] = node?.active ? 1 : 0.28
          aColor[cursor * 3] = color.r; aColor[cursor * 3 + 1] = color.g; aColor[cursor * 3 + 2] = color.b
        }
      })
      geo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(count * 3), 3))
      geo.setAttribute('aStart', new THREE.BufferAttribute(aStart, 2))
      geo.setAttribute('aSeed', new THREE.BufferAttribute(aSeed, 1))
      geo.setAttribute('aSpeed', new THREE.BufferAttribute(aSpeed, 1))
      geo.setAttribute('aGain', new THREE.BufferAttribute(aGain, 1))
      geo.setAttribute('aColor', new THREE.BufferAttribute(aColor, 3))
      streams = new THREE.Points(geo, pointsMaterial(STREAM_VERT, shared(['uTime', 'uInner', 'uSquish', 'uPx'])))
      streams.frustumCulled = false
      scene.add(streams)
      // outputs: forged parts fly from the hologram to each real task in the bay
      const outCount = Math.min(900, outs.length * 120)
      const og = new THREE.BufferGeometry()
      const aEnd = new Float32Array(outCount * 2); const oSeed = new Float32Array(outCount); const oSpeed = new Float32Array(outCount)
      const oColor = new Float32Array(outCount * 3)
      for (let i = 0; i < outCount; i += 1) {
        const anchor = outs[i % outs.length]
        const c = new THREE.Color(taskColor(anchor.id))
        aEnd[i * 2] = anchor.x; aEnd[i * 2 + 1] = anchor.y + (random() - 0.5) * 18
        oSeed[i] = random(); oSpeed[i] = 0.12 + random() * 0.1
        oColor[i * 3] = c.r; oColor[i * 3 + 1] = c.g; oColor[i * 3 + 2] = c.b
      }
      og.setAttribute('position', new THREE.BufferAttribute(new Float32Array(Math.max(1, outCount) * 3), 3))
      og.setAttribute('aEnd', new THREE.BufferAttribute(aEnd, 2))
      og.setAttribute('aSeed', new THREE.BufferAttribute(oSeed, 1))
      og.setAttribute('aSpeed', new THREE.BufferAttribute(oSpeed, 1))
      og.setAttribute('aColor', new THREE.BufferAttribute(oColor, 3))
      og.setDrawRange(0, outCount)
      outputs = new THREE.Points(og, pointsMaterial(OUT_VERT, shared(['uTime', 'uOrigin', 'uPx'])))
      outputs.frustumCulled = false
      scene.add(outputs)
      const tint = outs.length ? new THREE.Color(taskColor(outs[0].id)) : new THREE.Color('#5b6b8c')
      ;(holoOuter.material as THREE.LineBasicMaterial).color = tint
      holo.visible = true
    }
    sceneRef.current = { rebuild, flare: () => { uniforms.uFlare.value = 1 } }

    const observer = new ResizeObserver(() => { resize(); setAnchors(previous => ({ ...previous, w: width, h: height })) })
    observer.observe(host)
    const start = performance.now()
    let handle = 0
    let lastNow = start
    let slowFrames = 0
    let quality = 2
    const loop = (now: number) => {
      const t = animate ? (now - start) / 1000 : 14
      const dt = Math.min(0.1, (now - lastNow) / 1000)
      lastNow = now
      uniforms.uTime.value = t
      uniforms.uFlare.value = Math.max(0, uniforms.uFlare.value - dt * 0.9)
      holo.rotation.set(t * 0.4, t * 0.6, 0)
      holoInner.rotation.set(-t * 0.9, 0, t * 0.5)
      const breathe = 1 + 0.08 * Math.sin(t * 2.2) + uniforms.uFlare.value * 0.25
      holoOuter.scale.setScalar(breathe)
      ;(ring.material as THREE.MeshBasicMaterial).opacity = 0.45 + 0.15 * Math.sin(t * 3) + uniforms.uFlare.value * 0.3
      bloom.strength = 0.62 + uniforms.uFlare.value * 0.55
      // adaptive quality: a slow GPU first drops pixel ratio, then the bloom pass
      if (animate) {
        slowFrames = dt > 0.034 ? slowFrames + 1 : Math.max(0, slowFrames - 1)
        if (slowFrames > 90 && quality > 0) {
          quality -= 1
          slowFrames = 0
          if (quality === 1) { renderer.setPixelRatio(1); composer.setPixelRatio(1); uniforms.uPx.value = 1; resize() }
        }
      }
      if (quality === 0) renderer.render(scene, camera)
      else composer.render()
      if (animate) handle = requestAnimationFrame(loop)
    }
    handle = requestAnimationFrame(loop)
    const onLost = (event: Event) => { event.preventDefault(); setFailed(true); onUnsupported?.() }
    renderer.domElement.addEventListener('webglcontextlost', onLost)
    return () => {
      cancelAnimationFrame(handle)
      observer.disconnect()
      renderer.domElement.removeEventListener('webglcontextlost', onLost)
      sceneRef.current = null
      scene.traverse(object => {
        const mesh = object as THREE.Mesh
        mesh.geometry?.dispose?.()
        const material = mesh.material as THREE.Material | THREE.Material[] | undefined
        if (Array.isArray(material)) material.forEach(item => item.dispose()); else material?.dispose?.()
      })
      composer.dispose()
      renderer.dispose()
      renderer.domElement.remove()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [animate])

  // ---- measure DOM anchors (panel row edges) in world coordinates
  useLayoutEffect(() => {
    const host = canvasHostRef.current
    const left = leftRef.current
    if (!host || !left) return
    const measure = () => {
      const box = host.getBoundingClientRect()
      const leftBox = left.getBoundingClientRect()
      const toWorld = (x: number, y: number) => ({ x: x - box.left - box.width / 2, y: box.top + box.height / 2 - y })
      const inputs: Anchor[] = []
      for (const node of model.members) {
        const el = rowRefs.current.get(node.id)
        if (!el) continue
        const rect = el.getBoundingClientRect()
        const visible = rect.bottom <= leftBox.bottom && rect.top >= leftBox.top
        const y = visible ? rect.top + rect.height / 2 : leftBox.bottom - 12
        inputs.push({ id: node.id, ...toWorld(leftBox.right, y) })
      }
      const outputs: Anchor[] = []
      for (const task of model.tasks) {
        const el = bayRefs.current.get(task.id)
        if (!el) continue
        const rect = el.getBoundingClientRect()
        outputs.push({ id: task.id, ...toWorld(rect.left, rect.top + rect.height / 2) })
      }
      setAnchors({ inputs, outputs, w: box.width, h: box.height })
    }
    measure()
    left.addEventListener('scroll', measure, { passive: true })
    return () => left.removeEventListener('scroll', measure)
  }, [model, anchors.w, anchors.h])

  useEffect(() => {
    sceneRef.current?.rebuild(anchors.inputs, anchors.outputs)
  }, [anchors.inputs, anchors.outputs])

  // ---- live trace pulses: core flare + row highlight
  useEffect(() => {
    const fresh = freshPulses(pulses, seen.current)
    if (!fresh.length) return
    sceneRef.current?.flare()
    setHot(new Set(fresh.map(pulse => pulse.actorId)))
    const timer = window.setTimeout(() => setHot(new Set()), 1600)
    return () => window.clearTimeout(timer)
  }, [pulses])

  if (failed) return null
  const lead = model.tasks[0]
  return (
    <div ref={stageRef} className="collab-relations collab-theme is-forge" data-testid="collab-theme-forge" data-theme="forge">
      <div ref={canvasHostRef} className="collab-theme-gl" aria-label="视界熔炉：协作关系主题视图" role="img" />
      <div className="collab-theme-hud">
        <ThemeTitle en="HORIZON FORGE" zh="视界熔炉" tagline="子 Agent 任务流汇入视界 · 熔炉锻造交付物 · 逐件交付" />
        <StatStrip model={model} fps={fps} extra={[['协作流 FLOWS', data.activeLinks]]} />
      </div>
      <section ref={leftRef} className="collab-theme-panel collab-forge-array" aria-label="子 Agent 阵列">
        <header><span>01 / 子 Agent 阵列 · ARRAY</span><span>{model.activeMembers} 在办</span></header>
        <ol>
          {model.members.map(node => (
            <li key={node.id} ref={el => { if (el) rowRefs.current.set(node.id, el); else rowRefs.current.delete(node.id) }}
              className={`${node.active ? 'is-active' : ''}${node.id === selectedId ? ' is-selected' : ''}${hot.has(node.id) ? ' is-hot' : ''}`}>
              <button type="button" onClick={() => onSelect(node.id)}>
                <i style={{ background: node.taskId && node.active ? taskColor(node.taskId) : stateColor(node.state) }} aria-hidden="true" />
                <b>{node.label}</b>
                <span>{node.active ? (node.taskId ? (model.tasks.find(task => task.id === node.taskId)?.title ?? node.taskId) : '协作中') : '待命'}</span>
              </button>
            </li>
          ))}
          {model.folds.map(fold => (
            <li key={fold.id} className="is-fold">
              <button type="button" onClick={() => onExpandFold(fold.id.slice('fold:'.length))}>
                <i aria-hidden="true" /><b>+{fold.count ?? 0}</b><span>{fold.label} · 点击展开</span>
              </button>
            </li>
          ))}
          {model.hiddenMembers > 0 && <li className="is-fold"><span>另有 {model.hiddenMembers} 位未列出</span></li>}
        </ol>
      </section>
      <section className="collab-theme-panel collab-forge-bay" aria-label="锻造工位">
        <header><span>03 / 锻造工位 · FORGE BAY</span><span>{model.tasks.length} 件</span></header>
        {lead ? <h3>{lead.title}</h3> : <h3 className="is-idle">熔炉待料 · 暂无在办任务</h3>}
        <ul>
          {model.tasks.slice(0, 8).map((task, index) => (
            <li key={task.id} ref={el => { if (el) bayRefs.current.set(task.id, el); else bayRefs.current.delete(task.id) }}
              style={{ borderColor: taskColor(task.id) }}>
              <span style={{ color: taskColor(task.id) }}>{String(index + 1).padStart(2, '0')} / PART</span>
              <b>{task.title}</b>
              <small>{task.memberIds.length} 位协作 · {task.memberIds.slice(0, 3).map(id => model.members.find(member => member.id === id)?.label ?? id).join('、')}</small>
            </li>
          ))}
        </ul>
      </section>
      <TraceRecords model={model} title="04 / 交付记录 · DELIVERED" />
      <SimNote />
    </div>
  )
}

export default ForgeTheme
