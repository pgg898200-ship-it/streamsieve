// An edge code stores its box ID in the upper bits and its side in bit zero.
// Keeping coordinates separately makes the search run over contiguous numbers.
function buildAxis(rects, count, axis) {
  const codes = new Uint32Array(count * 2);
  for (let i = 0; i < codes.length; i++) codes[i] = i;
  const offset = code => (code >>> 1) * 4 + axis + (code & 1) * 2;
  codes.sort((a, b) => rects[offset(a)] - rects[offset(b)]);
  const values = new Float64Array(codes.length);
  for (let i = 0; i < codes.length; i++) values[i] = rects[offset(codes[i])];
  return searchable(codes, values);
}

function searchable(codes, values) {
  // A small directory narrows binary search to one coordinate bucket. Building
  // and querying use the same bucket expression, including floating-point ties.
  const low = values[0];
  const high = values[values.length - 1];
  const bins = Math.min(65536, Math.ceil(codes.length / 4));
  const scale = bins / (high - low);
  let directory = null;
  if (codes.length >= 512 && scale > 0 && Number.isFinite(scale)) {
    directory = new Uint32Array(bins + 1);
    let prev = -1;
    for (let i = 0; i < values.length; i++) {
      const bin = Math.min(bins - 1, Math.floor((values[i] - low) * scale));
      while (prev < bin) directory[++prev] = i;
    }
    while (prev < bins) directory[++prev] = values.length;
  }
  return { codes, values, low, high, scale, directory };
}

// Return the first coordinate strictly greater than the search point.
function upperBound(values, x, lo = 0, hi = values.length) {
  while (lo < hi) {
    const mid = lo + ((hi - lo) >>> 1);
    if (values[mid] <= x) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

function firstCrossing(axis, lo) {
  if (lo < axis.low) return 0;
  if (lo >= axis.high) return axis.values.length;
  const dir = axis.directory;
  if (!dir) return upperBound(axis.values, lo);
  const bin = Math.min(dir.length - 2, Math.floor((lo - axis.low) * axis.scale));
  return upperBound(axis.values, lo, dir[bin], dir[bin + 1]);
}

function crossingEnd(values, hi, start) {
  // Most small movements touch zero or a handful of edges.
  const stop = Math.min(values.length, start + 8);
  let pos = start;
  while (pos < stop && values[pos] <= hi) pos++;
  if (pos < stop || pos === values.length) return pos;
  return upperBound(values, hi, pos);
}

const STRIPES = 16;

function stripeId(stripes, p) {
  if (p <= stripes.low) return 0;
  if (p >= stripes.high) return STRIPES - 1;
  return Math.min(STRIPES - 1, Math.floor((p - stripes.low) * stripes.scale));
}

// Copy edges into the orthogonal strips their box intersects. In a narrow-box
// workload this avoids visiting distant fences with the same X or Y boundary.
// Abandon the optional index if it would exceed 2.5 copies per original edge.
function stripeAxis(axis, orth, r, off) {
  const scale = STRIPES / (orth.high - orth.low);
  if (!(scale > 0) || !Number.isFinite(scale)) return null;
  const stripes = { low: orth.low, high: orth.high, scale, axes: [] };
  const n = axis.codes.length / 2;
  const lo = new Uint8Array(n), hi = new Uint8Array(n);
  const lengths = new Float64Array(STRIPES + 1);
  let total = 0;
  for (let id = 0; id < n; id++) {
    const i = id * 4 + off;
    const a = lo[id] = stripeId(stripes, r[i]);
    const b = hi[id] = stripeId(stripes, r[i + 2]);
    total += b - a + 1;
    if (total > 2.5 * n) return null;
    lengths[a] += 2;
    lengths[b + 1] -= 2;
  }
  const cs = [], vs = [];
  const ptr = new Uint32Array(STRIPES);
  let length = 0;
  for (let j = 0; j < STRIPES; j++) {
    length += lengths[j];
    cs[j] = new Uint32Array(length);
    vs[j] = new Float64Array(length);
  }
  for (let k = 0; k < axis.codes.length; k++) {
    const code = axis.codes[k], id = code >>> 1;
    for (let j = lo[id]; j <= hi[id]; j++) {
      const pos = ptr[j]++;
      cs[j][pos] = code;
      vs[j][pos] = axis.values[k];
    }
  }
  for (let j = 0; j < STRIPES; j++) stripes.axes[j] = searchable(cs[j], vs[j]);
  return stripes;
}

function checkPoint(x, y) {
  if (!Number.isFinite(x) || !Number.isFinite(y)) {
    throw new RangeError("Point coordinates must be finite numbers");
  }
}

function checkEmitter(emit) {
  if (typeof emit !== "function") throw new TypeError("emit must be a function");
}

// Pack leaves into square-ish tiles and consecutive groups of sixteen. The
// outer box of a group rejects queries without reading the individual boxes.
const FANOUT = 16;

function packRects(rects, x, y) {
  const n = rects.length / 4;
  const order = new Uint32Array(n);
  // Reuse the two edge orders. Assign columns by increasing minX, then fill
  // each column in minY order. This grouping needs no additional sort.
  const slice = Math.ceil(Math.sqrt(n / FANOUT)) * FANOUT;
  const column = new Uint32Array(n);
  const cursor = new Uint32Array(Math.ceil(n / slice));
  let rank = 0;
  for (const code of x.codes) {
    if ((code & 1) === 0) column[code >>> 1] = Math.floor(rank++ / slice);
  }
  for (let i = 0; i < cursor.length; i++) cursor[i] = i * slice;
  for (const code of y.codes) {
    if ((code & 1) === 0) {
      const id = code >>> 1;
      order[cursor[column[id]]++] = id;
    }
  }

  const levels = [];
  let current = rects;
  while (current.length > 4) {
    const groups = Math.ceil(current.length / (4 * FANOUT));
    const next = new Float64Array(groups * 4);
    for (let g = 0; g < groups; g++) {
      const end = Math.min((g + 1) * FANOUT, current.length / 4);
      let left = Infinity, bottom = Infinity, right = -Infinity, top = -Infinity;
      for (let k = g * FANOUT; k < end; k++) {
        const i = (current === rects ? order[k] : k) * 4;
        left = Math.min(left, current[i]);
        bottom = Math.min(bottom, current[i + 1]);
        right = Math.max(right, current[i + 2]);
        top = Math.max(top, current[i + 3]);
      }
      next[g * 4] = left;
      next[g * 4 + 1] = bottom;
      next[g * 4 + 2] = right;
      next[g * 4 + 3] = top;
    }
    levels.push(next);
    current = next;
  }
  return { levels, order };
}

// Traverse the union of both endpoint queries once. Each box has one leaf,
// so reports need neither a scratch membership set nor callback deduplication.
function visitPacked(tree, rects, level, node, x0, y0, x1, y1, emit, pointQuery) {
  let count = 0;
  const data = level === 0 ? rects : tree.levels[level - 1];
  const limit = Math.min((node + 1) * FANOUT, data.length / 4);
  for (let k = node * FANOUT; k < limit; k++) {
    const id = level === 0 ? tree.order[k] : k;
    const i = id * 4;
    const before = !pointQuery && data[i] <= x0 && x0 < data[i + 2] &&
                    data[i + 1] <= y0 && y0 < data[i + 3];
    const after = data[i] <= x1 && x1 < data[i + 2] &&
                  data[i + 1] <= y1 && y1 < data[i + 3];
    if (level === 0) {
      if (before !== after) {
        if (pointQuery) emit(id);
        else emit(id, after);
        count++;
      }
    } else if (before || after) {
      count += visitPacked(tree, rects, level - 1, k, x0, y0, x1, y1, emit, pointQuery);
    }
  }
  return count;
}

function scanMove(r, x0, y0, x1, y1, emit) {
  let count = 0;
  for (let i = 0; i < r.length; i += 4) {
    const before = r[i] <= x0 && x0 < r[i + 2] && r[i + 1] <= y0 && y0 < r[i + 3];
    const after = r[i] <= x1 && x1 < r[i + 2] && r[i + 1] <= y1 && y1 < r[i + 3];
    if (before !== after) {
      emit(i / 4, after);
      count++;
    }
  }
  return count;
}

/** Static index over finite, nonempty, half-open 2D boxes. */
export class EdgeChime {
  #rects;
  #tree;
  #xs;
  #ys;
  #x;
  #y;

  constructor(boxes) {
    if (boxes == null || typeof boxes[Symbol.iterator] !== "function") {
      throw new TypeError("boxes must be an iterable of [minX, minY, maxX, maxY]");
    }
    const input = Array.isArray(boxes) ? boxes : Array.from(boxes);
    if (input.length > 0x7fffffff) throw new RangeError("Too many boxes");
    const r = new Float64Array(input.length * 4);
    for (let id = 0; id < input.length; id++) {
      const box = input[id];
      if (box == null || box.length !== 4) {
        throw new TypeError("Box " + id + " must have four coordinates");
      }
      const [minX, minY, maxX, maxY] = box;
      if (!Number.isFinite(minX) || !Number.isFinite(minY) ||
          !Number.isFinite(maxX) || !Number.isFinite(maxY) ||
          minX >= maxX || minY >= maxY) {
        throw new RangeError("Box " + id + " must have finite coordinates and positive dimensions");
      }
      r.set([minX, minY, maxX, maxY], id * 4);
    }
    this.#rects = r;
    this.#x = buildAxis(r, input.length, 0);
    this.#y = buildAxis(r, input.length, 1);
    if (input.length >= 256) {
      this.#xs = stripeAxis(this.#x, this.#y, r, 1);
      this.#ys = stripeAxis(this.#y, this.#x, r, 0);
    }
    this.#tree = input.length >= 256 ? packRects(r, this.#x, this.#y) : null;
  }

  /** Visit IDs containing (x,y). Returns the number visited. */
  at(x, y, emit) {
    checkPoint(x, y);
    checkEmitter(emit);
    const r = this.#rects;
    const tree = this.#tree;
    if (tree) return visitPacked(tree, r, tree.levels.length - 1, 0, x, y, x, y, emit, true);
    let count = 0;
    for (let i = 0; i < r.length; i += 4) {
      if (r[i] <= x && x < r[i + 2] && r[i + 1] <= y && y < r[i + 3]) {
        emit(i / 4);
        count++;
      }
    }
    return count;
  }

  /** Visit the symmetric difference between two endpoint memberships. */
  move(x0, y0, x1, y1, emit) {
    checkPoint(x0, y0);
    checkPoint(x1, y1);
    checkEmitter(emit);
    if (x0 === x1 && y0 === y1) return 0;
    const r = this.#rects;
    if (r.length <= 64) return scanMove(r, x0, y0, x1, y1, emit);

    const right = x1 > x0;
    const up = y1 > y0;
    const loX = right ? x0 : x1;
    const hiX = right ? x1 : x0;
    const loY = up ? y0 : y1;
    const hiY = up ? y1 : y0;
    // X crossings may depend on either endpoint's Y, so use a strip only when
    // both endpoints share it. The Y pass always requires X membership at x0.
    let x = this.#x, y = this.#y;
    if (this.#xs) {
      const old = stripeId(this.#xs, y0);
      if (old === stripeId(this.#xs, y1)) x = this.#xs.axes[old];
    }
    if (this.#ys) y = this.#ys.axes[stripeId(this.#ys, x0)];
    let xStart = 0, xEnd = 0, yStart = 0, yEnd = 0;
    if (loX !== hiX) {
      xStart = firstCrossing(x, loX);
      xEnd = crossingEnd(x.values, hiX, xStart);
    }
    if (loY !== hiY) {
      yStart = firstCrossing(y, loY);
      yEnd = crossingEnd(y.values, hiY, yStart);
    }
    // Once edge processing grows expensive, query both endpoints together in
    // the packed spatial hierarchy. Small indexes use a sequential pass.
    if (xEnd - xStart + yEnd - yStart > r.length / 16) {
      const tree = this.#tree;
      if (tree) return visitPacked(tree, r, tree.levels.length - 1, 0, x0, y0, x1, y1, emit, false);
      return scanMove(r, x0, y0, x1, y1, emit);
    }

    let count = 0;
    const yAtHiX = right ? y1 : y0;
    const yAtLoX = right ? y0 : y1;
    for (let k = xStart; k < xEnd; k++) {
      const code = x.codes[k];
      const id = code >>> 1;
      const i = id * 4;
      if (code & 1) {
        if (r[i] <= loX && r[i + 1] <= yAtLoX && yAtLoX < r[i + 3]) {
          emit(id, !right);
          count++;
        }
      } else if (hiX < r[i + 2] && r[i + 1] <= yAtHiX && yAtHiX < r[i + 3]) {
        emit(id, right);
        count++;
      }
    }

    for (let k = yStart; k < yEnd; k++) {
      const code = y.codes[k];
      const id = code >>> 1;
      const i = id * 4;
      // X already owns every transition whose endpoints differ in X membership.
      if (r[i] > loX || hiX >= r[i + 2]) continue;
      if (code & 1) {
        if (r[i + 1] <= loY) {
          emit(id, !up);
          count++;
        }
      } else if (hiY < r[i + 3]) {
        emit(id, up);
        count++;
      }
    }
    return count;
  }
}
