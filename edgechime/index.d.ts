export type Box = readonly [minX: number, minY: number, maxX: number, maxY: number];

/** Immutable index over half-open boxes. IDs follow the input order. */
export class EdgeChime {
  constructor(boxes: Iterable<Box>);
  /** Calls emit for each box containing the point and returns the call count. */
  at(x: number, y: number, emit: (id: number) => void): number;
  /** Calls emit once per changed membership. The callback order is unspecified. */
  move(
    x0: number, y0: number, x1: number, y1: number,
    emit: (id: number, entered: boolean) => void,
  ): number;
}
