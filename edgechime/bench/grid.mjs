// Independent uniform-grid comparator. Store a rectangle ID once per grid cell
// on its perimeter. A changed endpoint membership implies a boundary on any
// connecting path, including a horizontal leg followed by a vertical leg.
const maxPostings = 16 * 1024 * 1024; // 64 MiB of 32-bit rectangle IDs

function cell(value, min, inverseSize, side) {
  if (inverseSize === 0) return 0;
  return Math.max(0, Math.min(side - 1, Math.floor((value - min) * inverseSize)));
}

function inside(r, i, x, y) {
  return r[i] <= x && x < r[i + 2] && r[i + 1] <= y && y < r[i + 3];
}

function visitPerimeter(x0, y0, x1, y1, side, visit) {
  const first = y0 * side;
  const last = y1 * side;
  for (let x = x0; x <= x1; x++) {
    visit(first + x);
    if (first !== last) visit(last + x);
  }
  for (let y = y0 + 1; y < y1; y++) {
    const row = y * side;
    visit(row + x0);
    if (x0 !== x1) visit(row + x1);
  }
}

export class GridBaseline {
  constructor(boxes) {
    const n = boxes.length;
    this.rects = new Float64Array(4 * n);
    let xmin = Infinity, ymin = Infinity, xmax = -Infinity, ymax = -Infinity;
    for (let id = 0; id < n; id++) {
      const box = boxes[id];
      this.rects.set(box, id * 4);
      xmin = Math.min(xmin, box[0]);
      ymin = Math.min(ymin, box[1]);
      xmax = Math.max(xmax, box[2]);
      ymax = Math.max(ymax, box[3]);
    }
    this.xmin = xmin;
    this.ymin = ymin;
    this.scratch = [];
    this.depth = 0;

    // Start near one cell per box and reduce resolution when extensive
    // subscriptions would replicate too many edges. Raw coordinates remain
    // available for an exact scan even when a dataset exceeds the hard cap.
    this.side = Math.max(1, Math.min(512, Math.ceil(Math.sqrt(2 * n))));
    this.postingCount = 0;
    if (!n || n > maxPostings) return;
    const budget = Math.min(maxPostings, 128 * n);
    const bins = new Uint16Array(n * 4);
    while (true) {
      const side = this.side;
      const sx = side / (xmax - xmin), sy = side / (ymax - ymin);
      this.sx = Number.isFinite(sx) ? sx : 0;
      this.sy = Number.isFinite(sy) ? sy : 0;
      let total = 0;
      for (let i = 0; i < this.rects.length; i += 4) {
        const x0 = bins[i] = cell(this.rects[i], xmin, this.sx, side);
        const y0 = bins[i + 1] = cell(this.rects[i + 1], ymin, this.sy, side);
        const x1 = bins[i + 2] = cell(this.rects[i + 2], xmin, this.sx, side);
        const y1 = bins[i + 3] = cell(this.rects[i + 3], ymin, this.sy, side);
        const w = x1 - x0, h = y1 - y0;
        total += w && h ? 2 * (w + h) : w + h + 1;
      }
      if (total <= budget) {
        this.postingCount = total;
        break;
      }
      this.side = Math.max(1, Math.min(side - 1, Math.floor(side * budget / total)));
    }
    const side = this.side;
    const offsets = this.offsets = new Uint32Array(side * side + 1);
    const countCell = key => { offsets[key + 1]++; };
    for (let i = 0; i < bins.length; i += 4) {
      visitPerimeter(bins[i], bins[i + 1], bins[i + 2], bins[i + 3], side, countCell);
    }
    for (let k = 1; k < offsets.length; k++) offsets[k] += offsets[k - 1];
    const positions = offsets.slice(0, -1);
    const ids = this.ids = new Uint32Array(this.postingCount);
    let id = 0;
    const insert = key => { ids[positions[key]++] = id; };
    for (; id < n; id++) {
      const i = id * 4;
      visitPerimeter(bins[i], bins[i + 1], bins[i + 2], bins[i + 3], side, insert);
    }
  }

  at(x, y, emit) {
    let count = 0;
    const r = this.rects;
    for (let i = 0; i < r.length; i += 4) {
      if (inside(r, i, x, y)) { emit(i / 4); count++; }
    }
    return count;
  }

  scan(x0, y0, x1, y1, emit) {
    let count = 0;
    const r = this.rects;
    for (let i = 0; i < r.length; i += 4) {
      const after = inside(r, i, x1, y1);
      if (after !== inside(r, i, x0, y0)) { emit(i / 4, after); count++; }
    }
    return count;
  }

  move(x0, y0, x1, y1, emit) {
    if (x0 === x1 && y0 === y1) return 0;
    const r = this.rects;
    if (!this.offsets || r.length <= 64) return this.scan(x0, y0, x1, y1, emit);
    const side = this.side, offsets = this.offsets, ids = this.ids;
    const a = cell(x0, this.xmin, this.sx, side);
    const b = cell(y0, this.ymin, this.sy, side);
    const c = cell(x1, this.xmin, this.sx, side);
    const d = cell(y1, this.ymin, this.sy, side);
    const key = b * side + a;
    let count = 0;
    if (a === c && b === d) {
      // Perimeter construction emits each box only once in each cell.
      for (let j = offsets[key], end = offsets[key + 1]; j < end; j++) {
        const id = ids[j], i = id * 4;
        const after = inside(r, i, x1, y1);
        if (after !== inside(r, i, x0, y0)) { emit(id, after); count++; }
      }
      return count;
    }
    const xlo = Math.min(a, c), xhi = Math.max(a, c);
    const ylo = Math.min(b, d), yhi = Math.max(b, d);
    const row = b * side;
    // Horizontal cells occupy a contiguous posting range. Exclude the elbow
    // from the vertical leg because we already visited it horizontally.
    const yStart = ylo + (b === ylo ? 1 : 0);
    const yEnd = yhi - (b === yhi ? 1 : 0);
    const first = offsets[row + xlo], last = offsets[row + xhi + 1];
    let candidates = last - first;
    for (let y = yStart; y <= yEnd; y++) {
      const k = y * side + c;
      candidates += offsets[k + 1] - offsets[k];
    }
    if (candidates > r.length / 6) return this.scan(x0, y0, x1, y1, emit);

    // Per-depth scratch preserves callback reentrancy without allocating the
    // whole stamp array per move. Common one-cell moves need no scratch at all.
    const depth = this.depth++;
    try {
      const scratch = this.scratch[depth] ??= { marks: new Uint32Array(r.length / 4), generation: 0 };
      if (++scratch.generation > 0xffffffff) {
        scratch.marks.fill(0);
        scratch.generation = 1;
      }
      const marks = scratch.marks, stamp = scratch.generation;
      function visit(start, end) {
        for (let j = start; j < end; j++) {
          const id = ids[j];
          if (marks[id] === stamp) continue;
          marks[id] = stamp;
          const i = id * 4, after = inside(r, i, x1, y1);
          if (after !== inside(r, i, x0, y0)) { emit(id, after); count++; }
        }
      }
      visit(first, last);
      for (let y = yStart; y <= yEnd; y++) {
        const k = y * side + c;
        visit(offsets[k], offsets[k + 1]);
      }
      return count;
    } finally {
      this.depth--;
    }
  }
}
