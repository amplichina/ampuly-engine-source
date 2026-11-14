/**
 * Ampuly execution-orb renderer. Native WebGL; no remote assets or telemetry.
 * Visual inspiration: TheStack-ai/jarvis-orb (MIT), see ORB_SOURCE_NOTICE.txt.
 * Events and model state are owned by the parent; this renderer never invents
 * transitions or changes a failed/review state after an animation timeout.
 */
import type { TracePhase } from './traceModel';

export interface OrbRenderState {
  phase: TracePhase;
  connected: boolean;
  actors?: number;
}

export interface OrbAppearance {
  color: readonly [number, number, number];
  energy: number;
  pattern: number;
}

const APPEARANCES: Record<TracePhase, OrbAppearance> = {
  idle: { color: [0.25, 0.57, 0.76], energy: 0.12, pattern: 0 },
  thinking: { color: [0.27, 0.72, 1], energy: 0.48, pattern: 1 },
  searching: { color: [0.21, 0.81, 0.93], energy: 0.56, pattern: 2 },
  executing: { color: [0.2, 0.63, 1], energy: 0.85, pattern: 3 },
  collaborating: { color: [0.38, 0.82, 1], energy: 0.7, pattern: 4 },
  review: { color: [0.98, 0.68, 0.31], energy: 0.22, pattern: 5 },
  verifying: { color: [0.37, 0.85, 0.81], energy: 0.42, pattern: 6 },
  completed: { color: [0.32, 0.79, 0.66], energy: 0.12, pattern: 7 },
  failed: { color: [0.99, 0.4, 0.43], energy: 0.2, pattern: 8 },
  cancelled: { color: [0.52, 0.59, 0.67], energy: 0.06, pattern: 9 },
  unknown: { color: [0.52, 0.59, 0.67], energy: 0.04, pattern: 10 },
};

const DISCONNECTED: OrbAppearance = {
  color: [0.42, 0.49, 0.56], energy: 0, pattern: 10,
};

export function getOrbAppearance(state: OrbRenderState): OrbAppearance {
  return state.connected ? (APPEARANCES[state.phase] ?? APPEARANCES.unknown) : DISCONNECTED;
}

/** Eight is a rendering budget, never a claim about the actual employee count. */
export function clampOrbActors(actors: number | undefined): number {
  if (typeof actors !== 'number' || !Number.isFinite(actors)) return 0;
  return Math.min(8, Math.max(0, Math.floor(actors)));
}

export interface OrbFrameClock {
  request(callback: (time: number) => void): number;
  cancel(id: number): void;
}

export interface OrbRenderer {
  update(state: OrbRenderState): void;
  resize(cssWidth: number, cssHeight: number, pixelRatio?: number): void;
  setActive(active: boolean): void;
  setReducedMotion(reduced: boolean): void;
  dispose(): void;
  readonly disposed: boolean;
}

const VERTEX_SHADER = `
attribute vec2 a_position;
varying vec2 v_uv;
void main() {
  v_uv = a_position;
  gl_Position = vec4(a_position, 0.0, 1.0);
}`;

const FRAGMENT_SHADER = `
#ifdef GL_FRAGMENT_PRECISION_HIGH
precision highp float;
#else
precision mediump float;
#endif
varying vec2 v_uv;
uniform vec2 u_resolution;
uniform float u_time;
uniform vec3 u_color;
uniform float u_energy;
uniform float u_pattern;
uniform float u_actors;
uniform float u_connected;

float hash(vec3 p) {
  p = fract(p * 0.3183099 + vec3(0.11, 0.17, 0.23));
  p *= 7.0;
  return fract(p.x * p.y * p.z * (p.x + p.y + p.z));
}

float noise(vec3 p) {
  vec3 i = floor(p), f = fract(p);
  f = f * f * (3.0 - 2.0 * f);
  return mix(
    mix(mix(hash(i), hash(i + vec3(1,0,0)), f.x),
        mix(hash(i + vec3(0,1,0)), hash(i + vec3(1,1,0)), f.x), f.y),
    mix(mix(hash(i + vec3(0,0,1)), hash(i + vec3(1,0,1)), f.x),
        mix(hash(i + vec3(0,1,1)), hash(i + vec3(1,1,1)), f.x), f.y), f.z);
}

float fbm(vec3 p) {
  float value = 0.0, amplitude = 0.56;
  for (int i = 0; i < 3; i++) {
    value += amplitude * noise(p);
    p = p * 2.03 + vec3(7.2, 1.8, 3.1);
    amplitude *= 0.48;
  }
  return value;
}

mat2 rotate(float a) {
  return mat2(cos(a), -sin(a), sin(a), cos(a));
}

void main() {
  vec2 uv = v_uv;
  uv.x *= u_resolution.x / max(u_resolution.y, 1.0);
  float t = u_time * u_connected;
  float energy = u_energy;
  float radius = 0.565 + sin(t * 0.7) * 0.004 * energy;
  float r = length(uv);
  vec3 color = u_color;
  vec3 result = vec3(0.0);
  float alpha = 0.0;

  // A faint, bounded halo; the surrounding UI owns the background.
  float halo = exp(-abs(r - radius) * 13.0) * 0.11;
  halo += exp(-r * 3.2) * 0.022;
  result += color * halo;
  alpha += halo * 0.6;

  // Visible actors become satellites. Count is data-driven, capped at eight.
  if (u_actors > 1.5) {
    for (int i = 0; i < 8; i++) {
      float fi = float(i);
      if (fi >= u_actors) break;
      float tilt = -0.45 + mod(fi, 3.0) * 0.5;
      vec2 q = rotate(tilt) * uv;
      float orbitRadius = 0.78 + mod(fi, 2.0) * 0.12;
      vec2 ellipse = vec2(q.x / orbitRadius, q.y / (orbitRadius * 0.38));
      float ring = exp(-abs(length(ellipse) - 1.0) * 125.0);
      float a = fi * 6.2831853 / max(u_actors, 1.0) + t * (0.13 + mod(fi, 2.0) * 0.045);
      vec2 position = rotate(-tilt) * vec2(cos(a) * orbitRadius, sin(a) * orbitRadius * 0.38);
      float d = length(uv - position);
      float satellite = exp(-d * 125.0) + exp(-d * 32.0) * 0.24;
      // The back half disappears behind the sphere rather than crossing its face.
      float inFront = step(0.0, sin(a));
      float visibility = max(inFront, step(radius + 0.015, r));
      float orbitVisible = step(radius + 0.018, r);
      result += color * (ring * 0.038 * orbitVisible + satellite * visibility * 1.2);
      alpha += ring * 0.022 * orbitVisible + satellite * visibility * 0.7;
    }
  }

  if (r < radius) {
    float z = sqrt(max(radius * radius - dot(uv, uv), 0.0));
    vec3 p = vec3(uv, z) / radius;
    vec3 flow = p;
    flow.xz = rotate(t * 0.075) * flow.xz;
    float field = fbm(flow * 5.0 + vec3(0.0, t * 0.035, t * 0.02));
    float fine = fbm(flow * 13.0 + vec3(t * 0.015, 2.1, 0.0));
    vec3 n = normalize(p + 0.075 * vec3(field - 0.4, fine - 0.4, 0.0));
    vec3 light = normalize(vec3(-0.45, 0.68, 1.1));
    float diffuse = max(dot(n, light), 0.0);
    float fresnel = pow(1.0 - max(p.z, 0.0), 2.35);
    float specular = pow(max(dot(reflect(-light, n), vec3(0,0,1)), 0.0), 38.0);

    float longitude = atan(flow.x, flow.z);
    float latitude = asin(clamp(flow.y, -1.0, 1.0));
    float filament = pow(0.5 + 0.5 * sin(latitude * 35.0 + field * 9.0 - t * (0.4 + energy)), 10.0);
    float meridian = pow(0.5 + 0.5 * sin(longitude * 22.0 + field * 5.0 + t * 0.2), 15.0);
    float veins = filament * 0.09 + meridian * 0.06;
    float scan = exp(-pow((p.y - sin(t * 0.4) * 0.75) * 13.0, 2.0));
    float scanStrength = step(1.5, u_pattern) * (1.0 - step(3.5, u_pattern));
    float particle = pow(noise(floor(flow * 80.0)), 30.0) * (0.14 + energy * 0.2);

    vec3 surface = mix(vec3(0.022, 0.045, 0.075), color * 0.32, diffuse * 0.35 + field * 0.42);
    surface += color * (fresnel * 1.05 + veins * (0.6 + energy) + particle);
    surface += color * scan * 0.15 * scanStrength;
    surface += mix(color, vec3(0.85, 0.95, 1.0), 0.7) * specular * 0.65;
    surface += color * pow(max(0.0, 1.0 - r / radius), 3.0) * field * 0.055;
    result = surface;
    alpha = 0.995;
  }

  // Linear -> display gamma, then premultiply for transparent compositing.
  result = pow(max(result, vec3(0.0)), vec3(0.85));
  alpha = clamp(alpha, 0.0, 1.0);
  gl_FragColor = vec4(result * alpha, alpha);
}`;

function compileShader(gl: WebGLRenderingContext, type: number, source: string): WebGLShader | null {
  const shader = gl.createShader(type);
  if (!shader) return null;
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    gl.deleteShader(shader);
    return null;
  }
  return shader;
}

/**
 * Resource ownership is local to one canvas. Returns null on unavailable WebGL.
 * The caller owns visibility/context-loss listeners and must call dispose.
 * The injected frame clock is for focused lifecycle tests, not a second loop.
 */
export function createOrbRenderer(
  canvas: HTMLCanvasElement,
  initialState: OrbRenderState,
  clock: OrbFrameClock = {
    request: callback => window.requestAnimationFrame(callback),
    cancel: id => window.cancelAnimationFrame(id),
  },
): OrbRenderer | null {
  let gl: WebGLRenderingContext | null = null;
  try {
    gl = canvas.getContext('webgl', {
      alpha: true, antialias: false, depth: false, stencil: false,
      premultipliedAlpha: true, preserveDrawingBuffer: false,
      powerPreference: 'low-power',
    });
  } catch {
    return null;
  }
  if (!gl) return null;

  const vertex = compileShader(gl, gl.VERTEX_SHADER, VERTEX_SHADER);
  const fragment = compileShader(gl, gl.FRAGMENT_SHADER, FRAGMENT_SHADER);
  if (!vertex || !fragment) {
    if (vertex) gl.deleteShader(vertex);
    if (fragment) gl.deleteShader(fragment);
    return null;
  }
  const program = gl.createProgram();
  const buffer = gl.createBuffer();
  if (!program || !buffer) {
    if (program) gl.deleteProgram(program);
    if (buffer) gl.deleteBuffer(buffer);
    gl.deleteShader(vertex);
    gl.deleteShader(fragment);
    return null;
  }
  gl.attachShader(program, vertex);
  gl.attachShader(program, fragment);
  gl.linkProgram(program);
  gl.deleteShader(vertex);
  gl.deleteShader(fragment);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    gl.deleteProgram(program);
    gl.deleteBuffer(buffer);
    return null;
  }

  const attribute = gl.getAttribLocation(program, 'a_position');
  if (attribute < 0) {
    gl.deleteProgram(program);
    gl.deleteBuffer(buffer);
    return null;
  }
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]), gl.STATIC_DRAW);
  const uniforms = {
    resolution: gl.getUniformLocation(program, 'u_resolution'),
    time: gl.getUniformLocation(program, 'u_time'),
    color: gl.getUniformLocation(program, 'u_color'),
    energy: gl.getUniformLocation(program, 'u_energy'),
    pattern: gl.getUniformLocation(program, 'u_pattern'),
    actors: gl.getUniformLocation(program, 'u_actors'),
    connected: gl.getUniformLocation(program, 'u_connected'),
  };

  let state = { ...initialState };
  let active = false;
  let reducedMotion = false;
  let disposed = false;
  let frame: number | null = null;
  let previousTime: number | null = null;
  let lastDrawTime = -Infinity;
  let elapsed = 0;

  const draw = () => {
    if (disposed || !gl || gl.isContextLost()) return;
    const appearance = getOrbAppearance(state);
    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.useProgram(program);
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.enableVertexAttribArray(attribute);
    gl.vertexAttribPointer(attribute, 2, gl.FLOAT, false, 0, 0);
    gl.uniform2f(uniforms.resolution, canvas.width, canvas.height);
    gl.uniform1f(uniforms.time, reducedMotion ? 0 : elapsed);
    gl.uniform3f(uniforms.color, ...appearance.color);
    gl.uniform1f(uniforms.energy, appearance.energy);
    gl.uniform1f(uniforms.pattern, appearance.pattern);
    gl.uniform1f(uniforms.actors, clampOrbActors(state.actors));
    gl.uniform1f(uniforms.connected, state.connected ? 1 : 0);
    gl.drawArrays(gl.TRIANGLES, 0, 6);
  };

  const cancelFrame = () => {
    if (frame !== null) clock.cancel(frame);
    frame = null;
    previousTime = null;
  };
  const shouldAnimate = () => active && state.connected && !reducedMotion && !disposed;
  const tick = (time: number) => {
    frame = null;
    if (!shouldAnimate()) return;
    if (previousTime !== null) elapsed += Math.min(Math.max(time - previousTime, 0), 100) / 1000;
    previousTime = time;
    const frameInterval = state.phase === 'idle' || state.phase === 'completed' ? 1000 / 18 : 1000 / 28;
    if (time - lastDrawTime >= frameInterval) {
      draw();
      lastDrawTime = time;
    }
    if (shouldAnimate()) frame = clock.request(tick);
  };
  const synchronize = () => {
    if (disposed) return;
    if (shouldAnimate()) {
      if (frame === null) frame = clock.request(tick);
    } else {
      cancelFrame();
    }
    if (active) draw();
  };

  return {
    update(nextState) {
      if (disposed) return;
      state = { ...nextState };
      synchronize();
    },
    resize(cssWidth, cssHeight, pixelRatio = 1) {
      if (disposed || !Number.isFinite(cssWidth) || !Number.isFinite(cssHeight)) return;
      // Keep both pixel count and texture dimensions bounded, including on DPR 3 phones.
      const ratio = Math.min(1.75, Math.max(0.5, Number.isFinite(pixelRatio) ? pixelRatio : 1));
      const width = Math.min(1024, Math.max(1, Math.round(cssWidth * ratio)));
      const height = Math.min(1024, Math.max(1, Math.round(cssHeight * ratio)));
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
      if (active) draw();
    },
    setActive(value) {
      if (disposed || active === value) return;
      active = value;
      synchronize();
    },
    setReducedMotion(value) {
      if (disposed || reducedMotion === value) return;
      reducedMotion = value;
      synchronize();
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      active = false;
      cancelFrame();
      if (gl) {
        gl.deleteBuffer(buffer);
        gl.deleteProgram(program);
        gl = null;
      }
    },
    get disposed() { return disposed; },
  };
}
