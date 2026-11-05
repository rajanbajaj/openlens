export interface StackedPoint {
  t: string;
  fetched: number;
  failed: number;
  skipped: number;
}

/**
 * Merge consecutive buckets until at most `cap` remain (hourly series
 * over long windows collapse toward daily). Counts sum; the merged
 * bucket keeps its first timestamp.
 */
export function capBuckets(points: StackedPoint[], cap = 48): StackedPoint[] {
  if (points.length <= cap) return points;
  const size = Math.ceil(points.length / cap);
  const merged: StackedPoint[] = [];
  for (let i = 0; i < points.length; i += size) {
    const chunk = points.slice(i, i + size);
    merged.push({
      t: chunk[0].t,
      fetched: chunk.reduce((sum, point) => sum + point.fetched, 0),
      failed: chunk.reduce((sum, point) => sum + point.failed, 0),
      skipped: chunk.reduce((sum, point) => sum + point.skipped, 0),
    });
  }
  return merged;
}

/**
 * Keyboard-accessible data-table fallback for the throughput chart:
 * one row per bucket with the fetched/failed/skipped counts.
 */
export function ThroughputTable({ points }: { points: StackedPoint[] }) {
  if (points.length === 0) return <p className="my-ol-s">No throughput yet.</p>;
  return (
    <table
      aria-label="Pages per bucket"
      data-testid="analytics-throughput-table"
      className="ol-table"
    >
      <thead>
        <tr>
          <th scope="col">Bucket</th>
          <th scope="col" className="ol-table-num">
            Fetched
          </th>
          <th scope="col" className="ol-table-num">
            Failed
          </th>
          <th scope="col" className="ol-table-num">
            Skipped
          </th>
        </tr>
      </thead>
      <tbody>
        {points.map((point) => (
          <tr key={point.t}>
            <td>{point.t}</td>
            <td className="ol-table-num">{point.fetched.toLocaleString()}</td>
            <td className="ol-table-num">{point.failed.toLocaleString()}</td>
            <td className="ol-table-num">{point.skipped.toLocaleString()}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
